"""What a chat-built document still needs before ERPNext will accept it.

The guided forms (chat/forms.py) collect the fields a person has to choose. Some
requirements are not visible in DocType metadata, though: ERPNext's controllers require a
warehouse on some rows of some DocTypes, and sites can make extra fields mandatory. This
module checks those before the preview, so the chat can ask instead of failing on Create:

- ERPNext's own defaulting runs on an unsaved copy of the document (the controller's
  set_missing_values, the same call insert() makes), so a valid configured default
  (item / item group / brand default, or the company's default warehouse)
  is found exactly as ERPNext would find it. Nothing is written.
- Per-row warehouse rules come from the ERPNext v16 controllers (see WAREHOUSE_RULES).
- Frappe's own mandatory check is used for fields the chat can collect. Other `reqd` fields
  (naming series, price lists, totals...) are filled by ERPNext during insert, so they are
  not treated as missing here.

insert() stays the final authority: explain() turns its errors into a field and row so the
chat can ask for the right value and keep everything else the user entered.
"""

import copy
import re

import frappe
from frappe.utils import strip_html

# Which rows ERPNext requires a warehouse on (ERPNext v16):
#   Purchase Order    erpnext.buying.utils.validate_stock_item_warehouse (stock items)
#   Purchase Receipt  erpnext.buying.utils.validate_for_items (stock items)
#   Sales Order       SalesOrder.validate_warehouse (stock items and bundles with stock items)
#   Delivery Note     DeliveryNote.validate_warehouse (stock items, incl. bundle contents)
# Sales/Purchase Invoices need one only with update_stock, which the chat never sets, and
# Quotations never do.
WAREHOUSE_RULES = {
	"Purchase Order": "Which warehouse should {items} be received into?",
	"Purchase Receipt": "Which warehouse should {items} be received into?",
	"Sales Order": "Which warehouse will {items} be delivered from?",
	"Delivery Note": "Which warehouse will {items} be delivered from?",
	"Material Request": "Which warehouse are {items} needed in?",
}
BUNDLE_DOCTYPES = ("Sales Order", "Delivery Note")

# ERPNext messages that mean "this row needs a warehouse" (validation happens in controller code).
WAREHOUSE_ERRORS = re.compile(
	r"(Warehouse is mandatory for stock Item|Source warehouse required for stock item|Warehouse required for stock Item"
	r"|Warehouse is required)", re.I)
# Field types the chat can ask for generically when a site makes such a field mandatory.
COLLECTIBLE_TYPES = {"Data": "data", "Small Text": "data", "Text": "data", "Date": "date", "Select": "select", "Link": "link",
	"Int": "number", "Float": "number", "Currency": "number", "Percent": "number"}


def needs_warehouse(doctype, item_code):
	"""True if ERPNext will refuse this row of `doctype` without a warehouse."""
	if doctype not in WAREHOUSE_RULES or not item_code:
		return False
	if frappe.get_cached_value("Item", item_code, "is_stock_item"):
		return True
	if doctype in BUNDLE_DOCTYPES and frappe.db.exists("Product Bundle", {"new_item_code": item_code, "disabled": 0}):
		bundle_items = frappe.get_all("Product Bundle Item", filters={"parent": item_code, "parenttype": "Product Bundle"}, pluck="item_code")
		return any(frappe.get_cached_value("Item", code, "is_stock_item") for code in bundle_items)
	return False


def inspect(doc_dict):
	"""Run ERPNext's defaulting on an unsaved copy and report what is still needed.

	Returns {"defaults": {row_idx: warehouse ERPNext would use}, "missing": [fieldname, ...]}
	where "missing" lists parent fields that Frappe's mandatory check still reports after
	defaulting. The copy is discarded; messages ERPNext shows while defaulting are dropped.
	"""
	doc = frappe.get_doc(copy.deepcopy(doc_dict))
	try:
		doc._set_defaults()
		if hasattr(doc, "set_missing_values"):
			doc.set_missing_values(for_validate=True)
	except Exception:
		# A defaulting problem (e.g. no exchange rate) is reported by insert() itself, with context.
		frappe.local.message_log = []
		return {"defaults": {}, "missing": []}
	finally:
		frappe.local.message_log = []
	defaults = {row.idx or i + 1: row.get("warehouse") for i, row in enumerate(doc.get("items") or []) if row.get("warehouse")}
	missing = [fieldname for fieldname, _msg in doc._get_missing_mandatory_fields()]
	return {"defaults": defaults, "missing": missing}


def collectible_field(doctype, fieldname):
	"""A form-field definition for a mandatory field the chat can ask for, or None.

	Only simple field types at permission level 0 that are editable in the form; links are
	resolved and permission-checked like every other link answer."""
	df = frappe.get_meta(doctype).get_field(fieldname)
	if not df or df.read_only or df.hidden or df.permlevel or df.fieldtype not in COLLECTIBLE_TYPES:
		return None
	if not frappe.has_permission(doctype, "write") and not frappe.has_permission(doctype, "create"):
		return None
	kind = COLLECTIBLE_TYPES[df.fieldtype]
	field = {"fieldname": df.fieldname, "label": df.label or df.fieldname, "type": kind, "required": True, "doctype": None,
		"filters": {}, "default": None, "ask": True, "master": None, "prompt": f"ERPNext requires “{df.label or df.fieldname}”. What should it be?",
		"skip_label": None}
	if kind == "link":
		if not df.options or not frappe.db.exists("DocType", df.options):
			return None
		field["doctype"] = df.options
		if frappe.get_meta(df.options).has_field("company"):
			field["filters"] = {"company": "$company"}  # never a record of another company
	return field


def explain(exc, doctype):
	"""Turn an insert() failure into {"kind", "fields", "rows", "items", "message"}.

	kind: "mandatory" (fields lists the missing fieldnames), "warehouse" (items lists the
	affected item codes), "link" (an invalid link) or "other". The message is plain text,
	without HTML, tracebacks or internal identifiers beyond what ERPNext itself shows.
	"""
	message = strip_html(str(exc) or "").strip()
	shown = [strip_html(_message_text(m)).strip() for m in (frappe.local.message_log or [])]
	frappe.local.message_log = []
	if isinstance(exc, frappe.MandatoryError):
		fields = [f.strip() for f in message.split(":", 1)[-1].split(",") if f.strip()]
		rows = sorted({int(r) for m in shown for r in re.findall(r"Row #(\d+)", m)})
		readable = "; ".join(m.replace("Error: ", "") for m in shown if m) or f"Missing values: {', '.join(fields)}"
		return {"kind": "mandatory", "fields": fields, "rows": rows, "items": [], "message": readable[:300]}
	text = " ".join([message, *shown])
	if WAREHOUSE_ERRORS.search(text) or exc.__class__.__name__ in ("WarehouseRequired", "WarehouseMissingError"):
		items = re.findall(r"(?:Item|item)\s+([^\s,.:;]+)", text)
		rows = sorted({int(r) for r in re.findall(r"Row (?:#|No )?(\d+)", text)})
		return {"kind": "warehouse", "fields": ["warehouse"], "rows": rows, "items": items, "message": (message or text)[:300]}
	if isinstance(exc, frappe.LinkValidationError):
		return {"kind": "link", "fields": [], "rows": [], "items": [], "message": (message or text)[:300]}
	return {"kind": "other", "fields": [], "rows": [], "items": [], "message": (message or " ".join(shown) or "unknown error")[:300]}


def _message_text(raw):
	import json

	try:
		msg = json.loads(raw) if isinstance(raw, str) else raw
	except ValueError:
		return str(raw)
	return str((msg or {}).get("message") or "") if isinstance(msg, dict) else str(msg)
