"""Shared mapper: canonical extraction + direction classification -> ERPNext-ready JSON for one registered definition.

Every value comes from the document, a permission-aware master lookup, a validated default, or an explicit reviewer
choice (overrides); nothing is invented. The counterparty is the party detection/direction.py identified as "the other
side" (never our own company), and it is matched against Customers or Suppliers depending on the target. Results are
keyed so the engine and the review dialog can ask about each one; mapped keys are target-DocType fieldnames.
"""

from typing import ClassVar

import frappe
from frappe.utils import flt, nowdate

from ai_erpnext.constants import FieldStatus
from ai_erpnext.mapping.base import BaseMatcher, MatchResult
from ai_erpnext.mapping.currency import CurrencyMatcher
from ai_erpnext.mapping.customer import CustomerMatcher
from ai_erpnext.mapping.item import ItemMatcher
from ai_erpnext.mapping.normalize import clean_text, parse_date
from ai_erpnext.mapping.supplier import SupplierMatcher
from ai_erpnext.mapping.uom import UOMMatcher
from ai_erpnext.mapping.warehouse import WarehouseMatcher

PARTY_MATCHERS = {"Customer": CustomerMatcher, "Supplier": SupplierMatcher}
DISCOUNT_BASES = ("Net Total", "Grand Total")
NO_ORIGINAL = "__none__"  # reviewer choice: a return without a link to the original invoice
EXACT_TOLERANCE = 0.05


class TaxTemplateMatcher(BaseMatcher):
	"""Existing Sales/Purchase Taxes and Charges Template of the document's company (chosen by the user)."""

	title_field = "title"
	base_filters: ClassVar[dict] = {"disabled": 0}

	def __init__(self, company=None, doctype="Sales Taxes and Charges Template"):
		self.doctype = doctype
		super().__init__({"company": company} if company else None)


class BaseDocumentMapper:
	"""Shared plumbing for per-document mappers (override handling, result bookkeeping)."""

	def __init__(self, definition):
		self.definition = definition
		self.results: dict[str, MatchResult] = {}
		self.overrides: dict = {}

	def map(self, canonical: dict, overrides: dict | None = None, classification=None):
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
			result = MatchResult(
				input=raw,
				message="Not on document" if not required else "Required date missing",
				requires_review=required,
			)
		self.results[key] = result
		return result


def second_date_input(definition, canonical):
	"""The printed date that becomes the definition's second date (due, delivery, required-by, valid-till)."""
	return {
		"due_date": canonical.get("due_date"),
		"delivery_date": canonical.get("delivery_date"),
		"schedule_date": canonical.get("delivery_date"),
		"valid_till": canonical.get("valid_till"),
	}.get(definition.second_date_field)


def discount_base(canonical):
	"""("Net Total" | "Grand Total" | None, reason) for the document's discount, decided from its own numbers.

	Without document taxes both bases give the same totals. With taxes, the discount is before tax when the tax matches
	rate x (subtotal - discount), after tax when it matches rate x subtotal; anything else can't be decided."""
	discount = flt(canonical.get("discount_amount"))
	if discount <= 0:
		return None, "no discount"
	tax = canonical.get("tax_amount")
	if not flt(tax):
		return "Net Total", "no tax on the document"
	rate = canonical.get("tax_rate")
	if rate is None and canonical.get("tax_component_rate") is not None:
		rate = canonical["tax_component_rate"]
	if rate is None:
		return None, "the document shows a tax amount but no tax rate"
	rate = flt(rate) / 100.0 if flt(rate) > 1 else flt(rate)
	subtotal = canonical.get("subtotal")
	if subtotal is None:
		subtotal = sum(
			flt(r.get("amount")) for r in canonical.get("items") or [] if r.get("amount") is not None
		)
	before = abs(rate * (flt(subtotal) - discount) - flt(tax)) <= max(EXACT_TOLERANCE, flt(tax) * 0.002)
	after = abs(rate * flt(subtotal) - flt(tax)) <= max(EXACT_TOLERANCE, flt(tax) * 0.002)
	if before and not after:
		return "Net Total", "tax was calculated after the discount"
	if after and not before:
		return "Grand Total", "the discount was taken off after tax"
	return None, "the document's numbers don't show whether the discount is before or after tax"


class TransactionMapper(BaseDocumentMapper):
	def map(self, canonical: dict, overrides: dict | None = None, classification=None):
		d = self.definition
		self.results, self.overrides = {}, dict(overrides or {})
		cls = classification
		mapped = {"doctype": d.target_doctype, "items": []}

		# Company: decided by the classification (the user's choice or validated default), never by a name alone.
		if cls and cls.company:
			self.results["company"] = (
				MatchResult.manual("Company", cls.company, cls.company)
				if cls.company_source == "selected"
				else MatchResult.found(
					"Company",
					cls.company,
					cls.company,
					1.0,
					"exact" if cls.company_source == "document" else "default",
				)
			)
		else:
			self.results["company"] = MatchResult(
				doctype="Company",
				status=FieldStatus.NOT_FOUND,
				match_type="not_found",
				requires_review=True,
				message=(cls.message if cls else None) or "Which of your companies is this document for?",
				candidates=[
					{"name": n, "display_name": n, "score": 1.0}
					for n in (cls.company_candidates if cls else [])
				],
			)
		company = cls.company if cls else None
		mapped["company"] = company

		party = None
		if d.party_doctype:
			key = d.party_doctype.lower()
			matcher = PARTY_MATCHERS[d.party_doctype]()
			counterparty = cls.counterparty if cls else None
			res = self.resolve(
				key,
				matcher,
				lambda: matcher.match(counterparty)
				if counterparty
				else MatchResult(
					doctype=d.party_doctype,
					input=None,
					message=f"The {d.party_doctype.lower()} isn't named on the document",
				),
			)
			party = res.name if res.status in FieldStatus.RESOLVED else None
			mapped[d.party_field] = party

		if d.currency:
			currency = self.resolve(
				"currency",
				CurrencyMatcher(),
				lambda: CurrencyMatcher().resolve(
					canonical.get("currency"),
					customer=party,
					company=company,
					party_doctype=d.party_doctype or "Customer",
				),
			)
			mapped["currency"] = currency.name if currency.status in FieldStatus.RESOLVED else None

		if d.date_field:
			posting = self.resolve_date(d.date_field, canonical.get("invoice_date"))
			if posting.status == FieldStatus.NOT_PROVIDED:
				posting = MatchResult.defaulted(
					None, nowdate(), nowdate(), "Document date not on document; using today"
				)
				self.results[d.date_field] = posting
			mapped[d.date_field] = posting.name if posting.matched else None
		if d.second_date_field:
			second = self.resolve_date(
				d.second_date_field, second_date_input(d, canonical), required=d.second_date_required
			)
			mapped[d.second_date_field] = second.name if second.matched else None

		if d.warehouse:
			wh_matcher = WarehouseMatcher(company)
			wh = self.resolve(
				"warehouse",
				wh_matcher,
				lambda: wh_matcher.resolve(canonical.get("warehouse"))
				if company
				else MatchResult(doctype="Warehouse"),
			)
			if wh.status in FieldStatus.RESOLVED:
				mapped["set_warehouse"] = wh.name

		if d.tax_template_doctype and self.overrides.get("taxes_and_charges"):
			template_matcher = TaxTemplateMatcher(company, d.tax_template_doctype)
			template = self.resolve(
				"taxes_and_charges", template_matcher, lambda: MatchResult(doctype=template_matcher.doctype)
			)
			if template.matched:
				mapped["taxes_and_charges"] = template.name

		if d.supports_discount:
			self._map_discount(canonical, mapped)
		if d.is_return:
			self._map_return(canonical, mapped, party, company)
		if d.reference_field:
			ref = self._reference(canonical, cls)
			if ref:
				mapped[d.reference_field] = ref
		if d.target_doctype == "Material Request":
			self._map_request_type(canonical, mapped)
		if d.target_doctype == "BOM":
			self._map_bom_item(canonical, mapped, company)

		rows = canonical.get("items") or []
		for row in rows:
			mapped["items"].append(self._map_item(row, company, mapped))

		self.results["taxes"] = MatchResult(
			input={k: canonical.get(k) for k in ("tax_rate", "tax_amount")},
			status=FieldStatus.NOT_PROVIDED,
			match_type="info",
			message=None,
		)
		return mapped, self.results

	# ---------------------------------------------------------------------------------------------
	def _reference(self, canonical, cls):
		"""The other party's own number for this document: the printed number when they issued it, else the reference
		they quoted (a customer's PO number printed on our order confirmation)."""
		issued_by_them = bool(cls and cls.issuer and cls.counterparty and cls.issuer == cls.counterparty)
		value = canonical.get("invoice_number") if issued_by_them else canonical.get("reference_number")
		if (
			self.definition.reference_field == "po_no"
			and cls
			and cls.kind != "purchase_order"
			and issued_by_them
		):
			value = canonical.get(
				"reference_number"
			)  # a customer's order number, never the number of our own document
		return clean_text(value, 140)

	def _map_discount(self, canonical, mapped):
		amount = flt(canonical.get("discount_amount"))
		if amount <= 0:
			return
		mapped["discount_amount"] = abs(amount)
		override = self.overrides.get("apply_discount_on")
		base, reason = discount_base(canonical)
		if override in DISCOUNT_BASES:
			result = MatchResult.manual(None, override, override)
		elif base:
			result = MatchResult.found(None, base, base, 1.0, "computed")
			result.message = f"Discount {flt(amount, 2)} applied on the {base.lower()} ({reason})"
		else:
			result = MatchResult(
				input=amount,
				status=FieldStatus.AMBIGUOUS,
				match_type="discount_base",
				requires_review=True,
				message=f"The document has a discount of {flt(amount, 2)}, but {reason}.",
				candidates=[{"name": b, "display_name": b, "score": 1.0} for b in DISCOUNT_BASES],
			)
		self.results["apply_discount_on"] = result
		if result.matched:
			mapped["apply_discount_on"] = result.name

	def _map_return(self, canonical, mapped, party, company):
		d = self.definition
		mapped["is_return"] = 1
		original = canonical.get("original_invoice_number")
		override = self.overrides.get("return_against")
		if override == NO_ORIGINAL:
			self.results["return_against"] = MatchResult.defaulted(
				d.target_doctype,
				None,
				"not linked",
				"Created without a link to the original invoice (your choice)",
				input=original,
			)
			return
		wanted = override or original
		if not wanted:
			self.results["return_against"] = MatchResult(
				doctype=d.target_doctype,
				input=None,
				message="The document doesn't name the original invoice; the return won't be linked to one",
			)
			return
		if not (party and company):
			self.results["return_against"] = MatchResult(
				doctype=d.target_doctype,
				input=wanted,
				requires_review=True,
				message="Waiting for the party and company to look up the original invoice",
			)
			return
		base = {"docstatus": 1, "is_return": 0, "company": company, d.party_field: party}
		found = frappe.get_list(d.target_doctype, filters={**base, "name": wanted}, pluck="name", limit=2)
		if not found and d.target_doctype == "Purchase Invoice":
			found = frappe.get_list(
				d.target_doctype, filters={**base, "bill_no": wanted}, pluck="name", limit=3
			)
		if not found and d.target_doctype == "Sales Invoice":
			from ai_erpnext.validation.duplicate import SOURCE_TAG

			found = [
				r.name
				for r in frappe.get_list(
					d.target_doctype,
					filters={**base, "remarks": ["like", f"%{SOURCE_TAG} {wanted}%"]},
					fields=["name"],
					limit=3,
				)
			]
		if len(found) == 1:
			result = (
				MatchResult.manual(d.target_doctype, found[0], found[0])
				if override
				else MatchResult.found(d.target_doctype, found[0], found[0], 1.0, "exact", input=original)
			)
			mapped["return_against"] = found[0]
		elif found:
			result = MatchResult.ambiguous(
				d.target_doctype, wanted, [{"name": n, "display_name": n, "score": 1.0} for n in found]
			)
		else:
			result = MatchResult.not_found(
				d.target_doctype,
				wanted,
				f"No submitted {d.target_doctype} '{wanted}' of this {d.party_doctype.lower()} and company that you can access",
			)
		self.results["return_against"] = result

	def _map_request_type(self, canonical, mapped):
		options = [
			o
			for o in (
				frappe.get_meta("Material Request").get_field("material_request_type").options or ""
			).split("\n")
			if o
		]
		raw = self.overrides.get("material_request_type") or canonical.get("material_request_type")
		match = next((o for o in options if raw and o.lower() == str(raw).strip().lower()), None)
		if match:
			result = (
				MatchResult.manual(None, match, match)
				if self.overrides.get("material_request_type")
				else MatchResult.found(None, match, match, 1.0, "exact", input=raw)
			)
			mapped["material_request_type"] = match
		else:
			result = MatchResult(
				input=raw,
				status=FieldStatus.NOT_FOUND if raw else FieldStatus.NOT_PROVIDED,
				match_type="select",
				requires_review=True,
				message=(
					f"'{raw}' is not a Material Request purpose"
					if raw
					else "The request's purpose isn't printed"
				),
				candidates=[{"name": o, "display_name": o, "score": 1.0} for o in options],
			)
		self.results["material_request_type"] = result

	def _map_bom_item(self, canonical, mapped, company):
		bom = canonical.get("bom_item") or {}
		res = self.resolve(
			"bom_item",
			ItemMatcher(),
			lambda: ItemMatcher().match(bom)
			if (bom.get("item_code") or bom.get("item_name"))
			else MatchResult(doctype="Item", message="The finished item isn't named on the document"),
		)
		mapped["item"] = res.name if res.matched else None
		qty = bom.get("qty")
		if qty is None:
			self.results["bom_quantity"] = MatchResult.defaulted(
				None, 1, "1", "Quantity not printed; the BOM is for 1 unit of the finished item"
			)
			mapped["quantity"] = 1
		else:
			self.results["bom_quantity"] = MatchResult.found(None, qty, str(qty), 1.0, "exact", input=qty)
			mapped["quantity"] = flt(qty)
		if company:
			mapped["currency"] = frappe.get_cached_value("Company", company, "default_currency")

	def _map_item(self, row, company, mapped):
		key = f"item_{row['idx']}"
		item = self.resolve(key, ItemMatcher(), lambda: ItemMatcher().match(row))
		item_code = item.name if item.matched else None
		stock_uom = frappe.get_cached_value("Item", item_code, "stock_uom") if item_code else None

		def compute_uom():
			if row["uom"]:
				return UOMMatcher().match(row["uom"])
			if stock_uom:
				return MatchResult.defaulted(
					"UOM", stock_uom, stock_uom, "UOM not on document; used item's stock UOM"
				)
			return MatchResult(doctype="UOM", message="UOM not on document")

		uom = self.resolve(f"{key}_uom", UOMMatcher(), compute_uom)
		qty = flt(row["qty"]) if row["qty"] is not None else None
		mapped_row = {
			"item_code": item_code,
			"item_name": item.display_name if item_code else None,
			"description": row["description"] or row["item_name"],
			"qty": qty,
			"uom": uom.name if uom.status in FieldStatus.RESOLVED else None,
			"stock_uom": stock_uom,
			"rate": flt(row["rate"]) if row["rate"] is not None else None,
			"amount": flt(row["amount"]) if row["amount"] is not None else None,
		}
		default_warehouse = mapped.get("set_warehouse")
		if row.get("warehouse"):
			wh_matcher = WarehouseMatcher(company)
			wh = self.resolve(f"{key}_warehouse", wh_matcher, lambda: wh_matcher.match(row["warehouse"]))
			if wh.matched:
				mapped_row["warehouse"] = wh.name
		elif default_warehouse:
			mapped_row["warehouse"] = default_warehouse
		if self.definition.row_date_field and mapped.get(self.definition.second_date_field):
			mapped_row[self.definition.row_date_field] = mapped[self.definition.second_date_field]
		return mapped_row
