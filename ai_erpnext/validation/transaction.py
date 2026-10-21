"""Shared validator for every registered AI document type. Every rule runs in Python against ERPNext data; AI extraction
is never treated as validation. Per-type behaviour comes from the registry definition."""

import frappe
from frappe.utils import flt, getdate

from ai_erpnext.constants import FieldStatus
from ai_erpnext.validation.base import DATA, DUPLICATE, BaseValidator, ValidationReport
from ai_erpnext.validation.duplicate import find_duplicates
from ai_erpnext.validation.totals import validate_line, validate_totals

LABELS = {
	"customer": "Customer",
	"supplier": "Supplier",
	"company": "Company",
	"currency": "Currency",
	"posting_date": "Posting Date",
	"transaction_date": "Date",
	"due_date": "Due Date",
	"delivery_date": "Delivery Date",
	"schedule_date": "Required By",
	"valid_till": "Valid Till",
	"warehouse": "Warehouse",
	"apply_discount_on": "Discount",
	"return_against": "Original Invoice",
	"material_request_type": "Purpose",
	"bom_item": "Finished Item",
	"bom_quantity": "BOM Quantity",
	"taxes_and_charges": "Tax Template",
}
# Keys whose unresolved result blocks creation even when the field itself is optional on the target.
BLOCK_IF_UNRESOLVED = (
	"due_date",
	"valid_till",
	"warehouse",
	"apply_discount_on",
	"return_against",
	"material_request_type",
	"bom_item",
	"taxes_and_charges",
)
ITEM_FLAG_LABELS = {"is_sales_item": "sales", "is_purchase_item": "purchase", "is_stock_item": "stock"}


class TransactionValidator(BaseValidator):
	def validate(self, outcome) -> ValidationReport:
		report = ValidationReport()
		d = self.definition
		mapped, results, canonical = outcome.mapped, outcome.results, outcome.canonical
		target = d.target_doctype

		if not frappe.db.exists("DocType", target):
			report.error("doctype_missing", f"Target DocType '{target}' does not exist")
		elif not frappe.has_permission(target, "create"):
			report.error("no_create_permission", f"You do not have permission to create {target}")

		self._validate_header(report, results)
		self._validate_items(report, mapped, results)
		self._validate_dates(report, mapped)
		if d.currency:
			self._validate_currency(report, mapped)
		if d.party_currency_check:
			self._validate_party_currency(report, mapped)
		if d.rate_required:
			validate_totals(report, _absolute(canonical) if d.is_return else canonical)
		if d.supports_discount:
			self._validate_discount(report, canonical)
		if d.tax_template_doctype:
			self._validate_taxes(report, canonical, mapped)
		if d.is_return:
			self._validate_return(report, results)
		if d.target_doctype == "BOM":
			self._validate_bom(report, mapped)
		if d.warehouse:
			self._validate_warehouses(report, mapped)
		self._validate_duplicates(report, mapped, canonical)
		return report

	# ------------------------------------------------------------------------------
	def _validate_header(self, report, results):
		d = self.definition
		keys = [
			"company",
			*(["currency"] if d.currency else []),
			*([d.party_doctype.lower()] if d.party_doctype else []),
			*([d.date_field] if d.date_field else []),
			*([d.second_date_field] if d.second_date_field else []),
			*(k for k in BLOCK_IF_UNRESOLVED if k in results and k not in ("due_date", "valid_till")),
			"bom_quantity",
		]
		for key in dict.fromkeys(keys):
			res = results.get(key)
			if res is None:
				continue
			label = LABELS.get(key, key.replace("_", " ").title())
			required = key in d.required_fields or (key == d.second_date_field and d.second_date_required)
			if res.status in (FieldStatus.NOT_FOUND, FieldStatus.AMBIGUOUS, FieldStatus.INVALID):
				if required or key in BLOCK_IF_UNRESOLVED:
					report.error(f"{key}_{res.status.lower()}", f"{label}: {res.message}", key)
			elif res.status == FieldStatus.NOT_PROVIDED and (
				required or (res.requires_review and key in BLOCK_IF_UNRESOLVED)
			):
				report.error(
					f"{key}_missing",
					f"{label}: {res.message or 'required but not found on the document'}",
					key,
				)
			elif res.status == FieldStatus.DEFAULTED:
				report.warning(
					f"{key}_defaulted",
					f"{label}: {res.message}" + (f" ({res.name})" if res.name else ""),
					key,
				)
			elif res.matched and res.match_type not in (
				"exact",
				"exact_code",
				"manual",
				"date:iso",
				"computed",
				"default",
			):
				report.warning(
					f"{key}_{res.match_type}",
					f"{label}: '{res.input}' matched to '{res.display_name}' by {res.match_type.replace('_', ' ')}",
					key,
				)
			elif res.matched and res.match_type == "computed" and res.message:
				report.warning(f"{key}_computed", res.message, key)

	def _validate_items(self, report, mapped, results):
		d = self.definition
		rows = mapped.get("items") or []
		if not rows:
			report.error("no_items", "No line items found on the document", "items")
			return

		for idx, row in enumerate(rows, start=1):
			key = f"item_{idx}"
			item_res, uom_res = results.get(key), results.get(f"{key}_uom")
			if not row.get("item_code"):
				report.error(
					f"item_{(item_res.status if item_res else 'missing').lower()}",
					f"Row {idx} Item: {item_res.message if item_res else 'missing'}",
					key,
				)
			else:
				if item_res.match_type not in ("exact", "exact_code", "manual"):
					report.warning(
						f"item_{item_res.match_type}",
						f"Row {idx} Item: '{item_res.input}' matched to '{row['item_code']}' by {item_res.match_type.replace('_', ' ')}",
						key,
					)
				for flag in d.item_filters:
					if not frappe.get_cached_value("Item", row["item_code"], flag):
						code = "item_not_sales_item" if flag == "is_sales_item" else f"item_not_{flag[3:]}"
						report.error(
							code,
							f"Row {idx}: Item {row['item_code']} is not a {ITEM_FLAG_LABELS.get(flag, flag)} item",
							key,
						)
				if d.target_doctype == "BOM" and row["item_code"] == mapped.get("item"):
					report.error(
						"bom_recursion",
						f"Row {idx}: the finished item can't be one of its own components",
						key,
					)

			if not row.get("uom"):
				msg = uom_res.message if uom_res and uom_res.message else "UOM missing"
				report.error("uom_unresolved", f"Row {idx} UOM: {msg}", f"{key}_uom")
			else:
				if uom_res.status == FieldStatus.DEFAULTED:
					report.warning(
						"uom_defaulted", f"Row {idx} UOM: {uom_res.message} ({row['uom']})", f"{key}_uom"
					)
				elif uom_res.match_type not in ("exact", "manual"):
					report.warning(
						f"uom_{uom_res.match_type}",
						f"Row {idx} UOM: '{uom_res.input}' matched to '{row['uom']}'",
						f"{key}_uom",
					)
				if row.get("item_code") and not has_uom_conversion(
					row["item_code"], row["uom"], row.get("stock_uom")
				):
					report.error(
						"uom_no_conversion",
						f"Row {idx}: UOM '{row['uom']}' has no conversion factor for item {row['item_code']} (stock UOM {row.get('stock_uom')}). Choose another UOM or configure the item.",
						f"{key}_uom",
					)

			qty = abs(row["qty"]) if d.is_return and row.get("qty") is not None else row.get("qty")
			if d.rate_required:
				rate = abs(row["rate"]) if d.is_return and row.get("rate") is not None else row.get("rate")
				amount = (
					abs(row["amount"]) if d.is_return and row.get("amount") is not None else row.get("amount")
				)
				validate_line(report, idx, qty, rate, amount, key)
			elif qty is None:
				report.error("qty_missing", f"Row {idx}: quantity missing", key, category=DATA)
			elif qty <= 0:
				report.error(
					"qty_not_positive",
					f"Row {idx}: quantity must be greater than 0 (got {qty})",
					key,
					category=DATA,
				)

	def _validate_dates(self, report, mapped):
		d = self.definition
		first, second = (
			(mapped.get(d.date_field) if d.date_field else None),
			(mapped.get(d.second_date_field) if d.second_date_field else None),
		)
		if first and second and getdate(second) < getdate(first):
			code = (
				"due_before_posting"
				if d.second_date_field == "due_date"
				else f"{d.second_date_field}_before_date"
			)
			report.error(
				code,
				f"{LABELS.get(d.second_date_field)} {second} is before {LABELS.get(d.date_field)} {first}",
				d.second_date_field,
			)
		if first and mapped.get("company"):
			from erpnext.accounts.utils import get_fiscal_year

			if not get_fiscal_year(first, company=mapped["company"], boolean=True, raise_on_missing=False):
				report.error(
					"no_fiscal_year",
					f"{LABELS.get(d.date_field)} {first} is not in any active Fiscal Year for {mapped['company']}. Correct the date or create the Fiscal Year.",
					d.date_field,
				)

	def _validate_currency(self, report, mapped):
		currency, company = mapped.get("currency"), mapped.get("company")
		if not (currency and company):
			return
		company_currency = frappe.get_cached_value("Company", company, "default_currency")
		if currency == company_currency:
			return
		from erpnext.setup.utils import get_exchange_rate

		rate = get_exchange_rate(
			currency, company_currency, mapped.get(self.definition.date_field or "posting_date")
		)
		if not rate:
			report.error(
				"no_exchange_rate", f"No exchange rate found for {currency} to {company_currency}", "currency"
			)
		else:
			report.warning(
				"foreign_currency",
				f"Document in {currency}; exchange rate {rate} to {company_currency} will be applied",
				"currency",
			)

	def _validate_party_currency(self, report, mapped):
		"""Mirror ERPNext's rule (AccountsController.validate_party_account_currency) so the user is asked before
		confirming instead of ERPNext rejecting the draft afterwards."""
		d = self.definition
		party, company, currency = mapped.get(d.party_field), mapped.get("company"), mapped.get("currency")
		if not (party and company and currency):
			return
		from erpnext.accounts.party import get_party_account, get_party_gle_currency
		from erpnext.accounts.utils import get_account_currency

		party_type = d.party_doctype
		gle_currency = get_party_gle_currency(party_type, party, company)
		if gle_currency and gle_currency != currency:
			report.error(
				"party_currency_mismatch",
				f"{party_type} {party} already has accounting entries in {gle_currency} for {company}, so ERPNext only allows documents in {gle_currency} (this one is in {currency}).",
				"currency",
			)
			return
		account = get_party_account(party_type, party, company)
		account_currency = get_account_currency(account) if account else None
		multi = frappe.db.get_single_value(
			"Accounts Settings", "allow_multi_currency_invoices_against_single_party_account"
		)
		if not gle_currency and account_currency and account_currency != currency and not multi:
			report.error(
				"party_currency_mismatch",
				f"{party_type} {party}'s {'receivable' if party_type == 'Customer' else 'payable'} account {account} is in {account_currency}, but the document is in {currency}. ERPNext requires them to match.",
				"currency",
			)

	def _validate_discount(self, report, canonical):
		discount = flt(canonical.get("discount_amount"))
		if discount < 0:
			report.error(
				"discount_negative",
				f"The discount can't be negative (got {discount})",
				"apply_discount_on",
				category=DATA,
			)
			return
		if discount <= 0:
			return
		subtotal = canonical.get("subtotal")
		if subtotal is None:
			subtotal = sum(
				abs(flt(r.get("amount"))) for r in canonical.get("items") or [] if r.get("amount") is not None
			)
		if subtotal and discount > abs(flt(subtotal)):
			report.error(
				"discount_too_large",
				f"The discount {flt(discount, 2)} is larger than the subtotal {flt(subtotal, 2)}",
				"apply_discount_on",
				category=DATA,
			)

	def _validate_taxes(self, report, canonical, mapped):
		rate, amount = canonical.get("tax_rate"), canonical.get("tax_amount")
		if rate is not None and rate > 1:
			rate = rate / 100.0
		if amount is None and rate:
			base = canonical.get("subtotal")
			if base is None:
				base = sum(flt(r.get("amount")) for r in canonical.get("items", []))
			amount = flt(base) * rate
		if mapped and mapped.get("taxes_and_charges"):
			report.warning(
				"taxes_template",
				f"Taxes from template {mapped['taxes_and_charges']} will be calculated by ERPNext; compare with the document's tax.",
				"taxes",
			)
			return
		if abs(flt(amount)) > 0:
			rate_txt = f" ({flt(rate * 100, 2)}%)" if rate is not None else ""
			report.warning(
				"taxes_not_mapped",
				f"Document shows tax {flt(abs(flt(amount)), 2)}{rate_txt}. Tax rows are not auto-mapped; apply the correct {self.definition.tax_template_doctype.replace(' Template', '')} template in the draft before submitting.",
				"taxes",
			)

	def _validate_return(self, report, results):
		res = results.get("return_against")
		if res and res.status == FieldStatus.NOT_PROVIDED:
			report.warning("return_not_linked", f"Original Invoice: {res.message}", "return_against")

	def _validate_bom(self, report, mapped):
		qty = flt(mapped.get("quantity"))
		if qty <= 0:
			report.error(
				"bom_quantity_invalid",
				f"The BOM quantity must be greater than 0 (got {mapped.get('quantity')})",
				"bom_quantity",
				category=DATA,
			)
		item = mapped.get("item")
		if item and frappe.get_cached_value("Item", item, "has_variants"):
			report.error(
				"bom_item_template", f"{item} is a template item; a BOM needs a concrete item", "bom_item"
			)

	def _validate_warehouses(self, report, mapped):
		"""Rows ERPNext will refuse without a warehouse, after its own defaulting (item / company defaults)."""
		from ai_erpnext.chat import requirements

		d = self.definition
		rows = mapped.get("items") or []
		if d.warehouse == "rows":
			missing = [
				i for i, r in enumerate(rows, start=1) if r.get("item_code") and not r.get("warehouse")
			]
		else:
			stock = [
				i
				for i, r in enumerate(rows, start=1)
				if r.get("item_code")
				and not r.get("warehouse")
				and requirements.needs_warehouse(d.target_doctype, r["item_code"])
			]
			if not stock:
				return
			doc = {
				"doctype": d.target_doctype,
				"company": mapped.get("company"),
				"items": [{"item_code": r["item_code"], "qty": r.get("qty") or 1} for r in rows],
			}
			if d.party_field and mapped.get(d.party_field):
				doc[d.party_field] = mapped[d.party_field]
			defaults = requirements.inspect(doc)["defaults"]
			missing = [i for i in stock if not defaults.get(i)]
		if missing:
			report.error(
				"warehouse_missing",
				f"Row{'s' if len(missing) > 1 else ''} {', '.join(str(i) for i in missing)} need{'' if len(missing) > 1 else 's'} a warehouse of {mapped.get('company') or 'the company'}",
				"warehouse",
			)

	def _validate_duplicates(self, report, mapped, canonical):
		duplicates = find_duplicates(self.definition, mapped, canonical)
		report.duplicates = duplicates
		for dup in duplicates:
			report.error(
				"duplicate",
				f"Possible duplicate: {dup['name']} ({dup['reason']})",
				"duplicate",
				category=DUPLICATE,
			)


def _absolute(canonical):
	"""Totals of a credit/debit note are compared as magnitudes (the note may print them negative or positive)."""
	out = dict(canonical)
	for key in ("subtotal", "discount_amount", "tax_amount", "total_amount"):
		if out.get(key) is not None:
			out[key] = abs(flt(out[key]))
	out["items"] = [
		{**r, **{k: abs(flt(r[k])) for k in ("qty", "rate", "amount") if r.get(k) is not None}}
		for r in canonical.get("items") or []
	]
	return out


def has_uom_conversion(item_code, uom, stock_uom=None):
	"""True if ERPNext has a real conversion for uom (ERPNext itself silently falls back to 1)."""
	stock_uom = stock_uom or frappe.get_cached_value("Item", item_code, "stock_uom")
	if uom == stock_uom:
		return True
	item = frappe.get_cached_value("Item", item_code, ["variant_of"], as_dict=True) or {}
	parents = [item_code] + ([item.get("variant_of")] if item.get("variant_of") else [])
	if frappe.get_all(
		"UOM Conversion Detail",
		filters={"parent": ["in", parents], "parenttype": "Item", "uom": uom},
		limit=1,
	):
		return True
	return bool(
		frappe.get_all("UOM Conversion Factor", filters={"from_uom": uom, "to_uom": stock_uom}, limit=1)
		or frappe.get_all("UOM Conversion Factor", filters={"from_uom": stock_uom, "to_uom": uom}, limit=1)
	)
