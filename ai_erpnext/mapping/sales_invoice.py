"""Sales Invoice mapper: canonical extraction -> ERPNext-ready Sales Invoice JSON."""

from frappe.utils import flt

from ai_erpnext.constants import FieldStatus
from ai_erpnext.mapping.base import BaseMatcher, MatchResult
from ai_erpnext.mapping.company import CompanyMatcher
from ai_erpnext.mapping.currency import CurrencyMatcher
from ai_erpnext.mapping.customer import CustomerMatcher
from ai_erpnext.mapping.item import ItemMatcher
from ai_erpnext.mapping.normalize import parse_date
from ai_erpnext.mapping.uom import UOMMatcher
from ai_erpnext.mapping.warehouse import WarehouseMatcher


class BaseDocumentMapper:
	"""Shared plumbing for per-document mappers (override handling, result bookkeeping)."""

	def __init__(self, definition):
		self.definition = definition
		self.results: dict[str, MatchResult] = {}
		self.overrides: dict = {}

	def map(self, canonical: dict, overrides: dict | None = None):
		raise NotImplementedError

	def resolve(self, key, matcher: BaseMatcher, compute):
		"""Use the reviewer's manual choice for `key` if present, else run `compute()`."""
		override = self.overrides.get(key)
		if override:
			row = matcher.resolve_name(override)
			if row:
				result = MatchResult.manual(matcher.doctype, row["name"], matcher._display(row))
			else:
				result = MatchResult(
					doctype=matcher.doctype,
					input=override,
					status=FieldStatus.INVALID,
					match_type="manual",
					requires_review=True,
					message=f"{matcher.doctype} '{override}' does not exist, is disabled, or is not permitted",
				)
			original = compute()
			result.input = original.input if original.input is not None else result.input
		else:
			result = compute()
		self.results[key] = result
		return result

	def resolve_date(self, key, raw, required=False):
		override = self.overrides.get(key)
		parsed = parse_date(override or raw)
		if parsed.ok:
			result = MatchResult(
				input=raw,
				status=FieldStatus.MATCHED,
				matched=True,
				name=parsed.value,
				display_name=parsed.value,
				confidence=1.0 if parsed.fmt == "iso" or override else 0.95,
				match_type="manual" if override else f"date:{parsed.fmt}",
			)
		elif parsed.ambiguous:
			result = MatchResult(
				input=raw,
				status=FieldStatus.INVALID,
				match_type="ambiguous_date",
				requires_review=True,
				message=f"Date '{raw}' is ambiguous (day/month order); please confirm",
			)
		elif raw:
			result = MatchResult(
				input=raw,
				status=FieldStatus.INVALID,
				match_type="invalid_date",
				requires_review=True,
				message=f"'{raw}' is not a valid date",
			)
		else:
			result = MatchResult(input=raw, message="Not on document" if not required else "Required date missing")
		self.results[key] = result
		return result


class SalesInvoiceMapper(BaseDocumentMapper):
	def map(self, canonical: dict, overrides: dict | None = None):
		self.results, self.overrides = {}, dict(overrides or {})

		company = self.resolve("company", CompanyMatcher(), lambda: CompanyMatcher().resolve(canonical["company_names"]))
		company_name = company.name if company.status in FieldStatus.RESOLVED else None

		customer = self.resolve(
			"customer", CustomerMatcher(), lambda: CustomerMatcher().match(canonical["customer_name"])
		)
		customer_name = customer.name if customer.status in FieldStatus.RESOLVED else None

		currency = self.resolve(
			"currency",
			CurrencyMatcher(),
			lambda: CurrencyMatcher().resolve(canonical["currency"], customer=customer_name, company=company_name),
		)

		warehouse_matcher = WarehouseMatcher(company_name)
		warehouse = self.resolve("warehouse", warehouse_matcher, lambda: warehouse_matcher.resolve(canonical["warehouse"]))

		posting = self.resolve_date("posting_date", canonical["invoice_date"])
		if posting.status == FieldStatus.NOT_PROVIDED:
			from frappe.utils import nowdate

			posting = MatchResult.defaulted(None, nowdate(), nowdate(), "Invoice date not on document; using today")
			self.results["posting_date"] = posting
		due = self.resolve_date("due_date", canonical["due_date"])

		mapped = {
			"doctype": self.definition.target_doctype,
			"company": company_name,
			"customer": customer_name,
			"posting_date": posting.name if posting.matched else None,
			"due_date": due.name if due.matched else None,
			"currency": currency.name if currency.status in FieldStatus.RESOLVED else None,
			"items": [],
		}
		if warehouse.status in FieldStatus.RESOLVED:
			mapped["set_warehouse"] = warehouse.name

		for row in canonical["items"]:
			mapped["items"].append(self._map_item(row, company_name, mapped.get("set_warehouse")))

		self.results["taxes"] = MatchResult(
			input={k: canonical.get(k) for k in ("tax_rate", "tax_amount")},
			status=FieldStatus.NOT_PROVIDED,
			match_type="info",
			message=None,
		)
		return mapped, self.results

	def _map_item(self, row, company, default_warehouse):
		key = f"item_{row['idx']}"
		item = self.resolve(key, ItemMatcher(), lambda: ItemMatcher().match(row))
		item_code = item.name if item.matched else None
		stock_uom = None
		if item_code:
			import frappe

			stock_uom = frappe.get_cached_value("Item", item_code, "stock_uom")

		def compute_uom():
			if row["uom"]:
				return UOMMatcher().match(row["uom"])
			if stock_uom:
				return MatchResult.defaulted("UOM", stock_uom, stock_uom, "UOM not on document; used item's stock UOM")
			return MatchResult(doctype="UOM", message="UOM not on document")

		uom = self.resolve(f"{key}_uom", UOMMatcher(), compute_uom)
		mapped_row = {
			"item_code": item_code,
			"item_name": item.display_name if item_code else None,
			"description": row["description"] or row["item_name"],
			"qty": flt(row["qty"]) if row["qty"] is not None else None,
			"uom": uom.name if uom.status in FieldStatus.RESOLVED else None,
			"stock_uom": stock_uom,
			"rate": flt(row["rate"]) if row["rate"] is not None else None,
			"amount": flt(row["amount"]) if row["amount"] is not None else None,
		}
		if row["warehouse"]:
			wh_matcher = WarehouseMatcher(company)
			wh = self.resolve(f"{key}_warehouse", wh_matcher, lambda: wh_matcher.match(row["warehouse"]))
			if wh.matched:
				mapped_row["warehouse"] = wh.name
		elif default_warehouse:
			mapped_row["warehouse"] = default_warehouse
		return mapped_row
