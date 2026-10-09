import frappe
from frappe.utils import flt, getdate

from ai_erpnext.constants import FieldStatus
from ai_erpnext.validation.base import DATA, DUPLICATE, MAPPING, BaseValidator, ValidationReport
from ai_erpnext.validation.duplicate import find_duplicate_sales_invoices
from ai_erpnext.validation.totals import validate_line, validate_totals

FIELD_LABELS = {
	"customer": "Customer",
	"company": "Company",
	"currency": "Currency",
	"posting_date": "Posting Date",
	"due_date": "Due Date",
	"warehouse": "Warehouse",
}


class SalesInvoiceValidator(BaseValidator):
	def validate(self, outcome) -> ValidationReport:
		report = ValidationReport()
		mapped, results, canonical = outcome.mapped, outcome.results, outcome.canonical
		target = self.definition.target_doctype

		if not frappe.db.exists("DocType", target):
			report.error("doctype_missing", f"Target DocType '{target}' does not exist")
		elif not frappe.has_permission(target, "create"):
			report.error("no_create_permission", f"You do not have permission to create {target}")

		self._validate_header(report, results)
		self._validate_items(report, mapped, results)
		self._validate_dates(report, mapped)
		self._validate_currency(report, mapped)
		self._validate_party_currency(report, mapped)
		validate_totals(report, canonical)
		self._validate_taxes(report, canonical, mapped)
		self._validate_duplicates(report, mapped, canonical)
		return report

	# ------------------------------------------------------------------------------
	def _validate_header(self, report, results):
		for key, label in FIELD_LABELS.items():
			res = results.get(key)
			if res is None:
				continue
			required = key in self.definition.required_fields
			if res.status in (FieldStatus.NOT_FOUND, FieldStatus.AMBIGUOUS, FieldStatus.INVALID):
				if required or key in ("due_date", "warehouse"):
					report.error(f"{key}_{res.status.lower()}", f"{label}: {res.message}", key)
			elif res.status == FieldStatus.NOT_PROVIDED and required:
				report.error(f"{key}_missing", f"{label} is required but was not found on the document", key)
			elif res.status == FieldStatus.DEFAULTED:
				report.warning(f"{key}_defaulted", f"{label}: {res.message} ({res.name})", key)
			elif res.matched and res.match_type not in ("exact", "exact_code", "manual", "date:iso"):
				report.warning(f"{key}_{res.match_type}", f"{label}: '{res.input}' matched to '{res.display_name}' by {res.match_type.replace('_', ' ')}", key)
			if key == "company" and res.extra.get("unmatched_issuer"):
				report.warning(
					"issuer_not_company",
					f"Invoice issuer '{res.extra['unmatched_issuer']}' is not one of your companies. Confirm this is really your sales invoice.",
					key,
				)

	def _validate_items(self, report, mapped, results):
		rows = mapped.get("items") or []
		if not rows:
			report.error("no_items", "No line items found on the document", "items")
			return

		for idx, row in enumerate(rows, start=1):
			key = f"item_{idx}"
			item_res, uom_res = results.get(key), results.get(f"{key}_uom")
			if not row.get("item_code"):
				report.error(f"item_{(item_res.status if item_res else 'missing').lower()}", f"Row {idx} Item: {item_res.message if item_res else 'missing'}", key)
			else:
				if item_res.match_type not in ("exact", "exact_code", "manual"):
					report.warning(f"item_{item_res.match_type}", f"Row {idx} Item: '{item_res.input}' matched to '{row['item_code']}' by {item_res.match_type.replace('_', ' ')}", key)
				if not frappe.get_cached_value("Item", row["item_code"], "is_sales_item"):
					report.error("item_not_sales_item", f"Row {idx}: Item {row['item_code']} is not a sales item", key)

			if not row.get("uom"):
				msg = uom_res.message if uom_res and uom_res.message else "UOM missing"
				report.error("uom_unresolved", f"Row {idx} UOM: {msg}", f"{key}_uom")
			else:
				if uom_res.status == FieldStatus.DEFAULTED:
					report.warning("uom_defaulted", f"Row {idx} UOM: {uom_res.message} ({row['uom']})", f"{key}_uom")
				elif uom_res.match_type not in ("exact", "manual"):
					report.warning(f"uom_{uom_res.match_type}", f"Row {idx} UOM: '{uom_res.input}' matched to '{row['uom']}'", f"{key}_uom")
				if row.get("item_code") and not has_uom_conversion(row["item_code"], row["uom"], row.get("stock_uom")):
					report.error(
						"uom_no_conversion",
						f"Row {idx}: UOM '{row['uom']}' has no conversion factor for item {row['item_code']} (stock UOM {row.get('stock_uom')}). Choose another UOM or configure the item.",
						f"{key}_uom",
					)

			validate_line(report, idx, row.get("qty"), row.get("rate"), row.get("amount"), key)

	def _validate_dates(self, report, mapped):
		posting, due = mapped.get("posting_date"), mapped.get("due_date")
		if posting and due and getdate(due) < getdate(posting):
			report.error("due_before_posting", f"Due Date {due} is before Posting Date {posting}", "due_date")
		if posting and mapped.get("company"):
			from erpnext.accounts.utils import get_fiscal_year

			if not get_fiscal_year(posting, company=mapped["company"], boolean=True, raise_on_missing=False):
				report.error(
					"no_fiscal_year",
					f"Posting Date {posting} is not in any active Fiscal Year for {mapped['company']}. Correct the date or create the Fiscal Year.",
					"posting_date",
				)

	def _validate_currency(self, report, mapped):
		currency, company = mapped.get("currency"), mapped.get("company")
		if not (currency and company):
			return
		company_currency = frappe.get_cached_value("Company", company, "default_currency")
		if currency == company_currency:
			return
		from erpnext.setup.utils import get_exchange_rate

		rate = get_exchange_rate(currency, company_currency, mapped.get("posting_date"))
		if not rate:
			report.error("no_exchange_rate", f"No exchange rate found for {currency} to {company_currency}", "currency")
		else:
			report.warning("foreign_currency", f"Invoice in {currency}; exchange rate {rate} to {company_currency} will be applied", "currency")

	def _validate_party_currency(self, report, mapped):
		"""Mirror ERPNext's rule (AccountsController.validate_party_account_currency) so the
		user is asked before confirming instead of ERPNext rejecting the draft afterwards."""
		customer, company, currency = mapped.get("customer"), mapped.get("company"), mapped.get("currency")
		if not (customer and company and currency):
			return
		from erpnext.accounts.party import get_party_account, get_party_gle_currency
		from erpnext.accounts.utils import get_account_currency

		gle_currency = get_party_gle_currency("Customer", customer, company)
		if gle_currency and gle_currency != currency:
			report.error("party_currency_mismatch",
				f"Customer {customer} already has accounting entries in {gle_currency} for {company}, so ERPNext only allows invoices in {gle_currency} (the invoice is in {currency}).",
				"currency")
			return
		account = get_party_account("Customer", customer, company)
		account_currency = get_account_currency(account) if account else None
		multi = frappe.db.get_single_value("Accounts Settings", "allow_multi_currency_invoices_against_single_party_account")
		if not gle_currency and account_currency and account_currency != currency and not multi:
			report.error("party_currency_mismatch",
				f"Customer {customer}'s receivable account {account} is in {account_currency}, but the invoice is in {currency}. ERPNext requires them to match.",
				"currency")

	def _validate_taxes(self, report, canonical, mapped=None):
		rate, amount = canonical.get("tax_rate"), canonical.get("tax_amount")
		if rate is not None and rate > 1:
			rate = rate / 100.0
		if amount is None and rate:
			base = canonical.get("subtotal")
			if base is None:
				base = sum(flt(r.get("amount")) for r in canonical.get("items", []))
			amount = flt(base) * rate
		if mapped and mapped.get("taxes_and_charges"):
			report.warning("taxes_template", f"Taxes from template {mapped['taxes_and_charges']} will be calculated by ERPNext; compare with the document's tax.", "taxes")
			return
		if flt(amount) > 0:
			rate_txt = f" ({flt(rate * 100, 2)}%)" if rate is not None else ""
			report.warning(
				"taxes_not_mapped",
				f"Document shows tax {flt(amount, 2)}{rate_txt}. Tax rows are not auto-mapped; apply the correct Sales Taxes template in the draft before submitting.",
				"taxes",
			)

	def _validate_duplicates(self, report, mapped, canonical):
		if not mapped.get("customer"):
			return
		duplicates = find_duplicate_sales_invoices(
			mapped["customer"],
			canonical.get("invoice_number"),
			mapped.get("company"),
			mapped.get("posting_date"),
			canonical.get("total_amount"),
		)
		report.duplicates = duplicates
		for dup in duplicates:
			report.error("duplicate", f"Possible duplicate: {dup['name']} ({dup['reason']})", "duplicate", category=DUPLICATE)


def has_uom_conversion(item_code, uom, stock_uom=None):
	"""True if ERPNext has a real conversion for uom (ERPNext itself silently falls back to 1)."""
	stock_uom = stock_uom or frappe.get_cached_value("Item", item_code, "stock_uom")
	if uom == stock_uom:
		return True
	item = frappe.get_cached_value("Item", item_code, ["variant_of"], as_dict=True) or {}
	parents = [item_code] + ([item.get("variant_of")] if item.get("variant_of") else [])
	if frappe.get_all("UOM Conversion Detail", filters={"parent": ["in", parents], "parenttype": "Item", "uom": uom}, limit=1):
		return True
	return bool(
		frappe.get_all("UOM Conversion Factor", filters={"from_uom": uom, "to_uom": stock_uom}, limit=1)
		or frappe.get_all("UOM Conversion Factor", filters={"from_uom": stock_uom, "to_uom": uom}, limit=1)
	)
