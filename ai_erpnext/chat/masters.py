"""Customer / Item creation through the conversation, only after explicit user approval.

Only fields ERPNext marks as required (plus the explicit stock/non-stock choice for Items)
are asked; values come from the document (names, descriptions) or from the user, never
invented. Every value is validated here and again by ERPNext's own insert().
"""

import frappe
from frappe import _

from ai_erpnext.capability import service as capability
from ai_erpnext.capability.access import readable_exists
from ai_erpnext.mapping.customer import CustomerMatcher
from ai_erpnext.mapping.item import ItemMatcher
from ai_erpnext.mapping.normalize import clean_text

TREE_DOCTYPES = {"Customer Group", "Territory", "Item Group"}

FIELDS = {
	"Customer": {
		"customer_name": {"label": "customer name", "type": "Data"},
		"customer_type": {"label": "customer type", "type": "Select"},
		"customer_group": {"label": "customer group", "type": "Link", "options": "Customer Group"},
		"territory": {"label": "territory", "type": "Link", "options": "Territory"},
	},
	"Item": {
		"item_code": {"label": "item code", "type": "Data"},
		"item_name": {"label": "item name", "type": "Data"},
		"item_group": {"label": "item group", "type": "Link", "options": "Item Group"},
		"stock_uom": {"label": "stock unit (UOM)", "type": "Link", "options": "UOM"},
		"is_stock_item": {"label": "stock or service", "type": "Check"},
	},
}
# Asked even though ERPNext has a default, because the choice matters for accounting/stock.
ALWAYS_ASK = {"Item": ["is_stock_item"]}
SUMMARY_FIELDS = {
	"Customer": ["customer_name", "customer_type", "customer_group", "territory"],
	"Item": ["item_code", "item_name", "item_group", "stock_uom", "is_stock_item"],
}


def customer_draft(for_key, name):
	values = {"customer_type": _meta_default("Customer", "customer_type") or "Company"}
	if clean_text(name, 140):
		values["customer_name"] = clean_text(name, 140)
	for field, setting in (("customer_group", "customer_group"), ("territory", "territory")):
		default = frappe.db.get_single_value("Selling Settings", setting)
		if default:
			values[field] = default
	return _draft("Customer", for_key, values)


def item_draft(for_key, name, description=None, uom=None):
	values = {"is_sales_item": 1}
	if clean_text(name, 140):
		values["item_name"] = clean_text(name, 140)
	if clean_text(description, 1000):
		values["description"] = clean_text(description, 1000)
	values["stock_uom"] = uom or frappe.db.get_single_value("Stock Settings", "stock_uom") or None
	default_group = frappe.db.get_single_value("Stock Settings", "item_group")
	if default_group and not frappe.db.get_value("Item Group", default_group, "is_group"):
		values["item_group"] = default_group
	if not values["stock_uom"]:
		values.pop("stock_uom")
	return _draft("Item", for_key, values)


def _draft(doctype, for_key, values):
	meta = frappe.get_meta(doctype)
	required = [f.fieldname for f in meta.fields if f.reqd and not f.read_only and f.fieldtype not in ("Table", "Table MultiSelect")]
	if doctype == "Item" and frappe.db.get_single_value("Stock Settings", "item_naming_by") != "Item Code":
		required = [f for f in required if f != "item_code"]  # naming series will name the item
	unsupported = [f for f in required if f not in FIELDS[doctype]]
	ask = [f for f in required if f in FIELDS[doctype] and not values.get(f)]
	ask += [f for f in ALWAYS_ASK.get(doctype, []) if f not in ask and f not in values]
	return {"doctype": doctype, "for_key": for_key, "values": values, "ask": ask, "unsupported_required": unsupported}


def next_field(draft):
	return draft["ask"][0] if draft["ask"] else None


def field_options(doctype, fieldname):
	spec = FIELDS[doctype][fieldname]
	if spec["type"] == "Select":
		opts = [o for o in (frappe.get_meta(doctype).get_field(fieldname).options or "").split("\n") if o]
		return [{"label": o, "value": o} for o in opts]
	if spec["type"] == "Check":
		return [{"label": "Stock item (tracked in inventory)", "value": "1"}, {"label": "Non-stock item / service", "value": "0"}]
	if spec["type"] == "Link":
		filters = {"is_group": 0} if spec["options"] in TREE_DOCTYPES else {}
		if spec["options"] == "UOM":
			filters = {"enabled": 1}
			names = frappe.get_list("UOM", filters=filters, pluck="name", limit=0)
			preferred = [u for u in ("Nos", "Unit", "Hour", "Kg") if u in names]
			return [{"label": n, "value": n} for n in preferred]
		names = frappe.get_list(spec["options"], filters=filters, pluck="name", limit=12, order_by="name asc")
		return [{"label": n, "value": n} for n in names]
	return []


def set_field(draft, fieldname, raw):
	"""Validate and store one value. Returns an error sentence or None."""
	doctype = draft["doctype"]
	spec = FIELDS[doctype].get(fieldname)
	if not spec:
		return _("I can't set {0} here.").format(fieldname)
	value = clean_text(raw, 140)
	if value is None:
		return _("Please give a value for the {0}.").format(spec["label"])

	if spec["type"] == "Data":
		if fieldname == "item_code" and readable_exists("Item", value):
			return _("An item with code “{0}” already exists. Choose another code, or say “use existing”.").format(value)
	elif spec["type"] == "Select":
		opts = [o["value"] for o in field_options(doctype, fieldname)]
		match = next((o for o in opts if o.lower() == value.lower()), None)
		if not match:
			return _("Please choose one of: {0}.").format(", ".join(opts))
		value = match
	elif spec["type"] == "Check":
		v = value.lower()
		if v in ("1", "yes", "stock", "stock item", "stock item (tracked in inventory)", "inventory"):
			value = 1
		elif v in ("0", "no", "service", "non-stock", "non stock", "non-stock item / service", "non-stock item"):
			value = 0
		else:
			return _("Is it a stock item or a non-stock item / service?")
	elif spec["type"] == "Link":
		value = _resolve_link(spec["options"], value)
		if not value:
			return _("I couldn't find a {0} called “{1}”.").format(spec["label"], clean_text(raw, 140))

	draft["values"][fieldname] = value
	if fieldname in draft["ask"]:
		draft["ask"].remove(fieldname)
	return None


def _resolve_link(doctype, value):
	filters = {"is_group": 0} if doctype in TREE_DOCTYPES else ({"enabled": 1} if doctype == "UOM" else {})
	exact = frappe.get_list(doctype, filters={**filters, "name": value}, pluck="name", limit=2)
	if exact:
		return exact[0]
	like = frappe.get_list(doctype, filters={**filters, "name": ["like", f"%{value}%"]}, pluck="name", limit=2)
	return like[0] if len(like) == 1 else None


def summary(draft):
	rows = []
	for f in SUMMARY_FIELDS[draft["doctype"]]:
		v = draft["values"].get(f)
		if f == "is_stock_item" and v is not None:
			v = "Stock item" if int(v) else "Non-stock / service"
		rows.append({"label": FIELDS[draft["doctype"]][f]["label"].capitalize(), "value": v if v not in (None, "") else "Not set"})
	return rows


def find_existing(draft):
	v = draft["values"]
	if draft["doctype"] == "Customer":
		res = CustomerMatcher().match(v.get("customer_name"))
	else:
		res = ItemMatcher().match({"item_name": v.get("item_name"), "item_code": v.get("item_code")})
	return res.name if res.matched else None


def create(draft):
	values = {"doctype": draft["doctype"], **draft["values"]}
	capability.reject_protected(values)
	doc = frappe.get_doc(values)
	doc.docstatus = 0
	doc.insert()  # standard permissions and validation
	return doc.name


def _meta_default(doctype, fieldname):
	field = frappe.get_meta(doctype).get_field(fieldname)
	return field.default if field else None
