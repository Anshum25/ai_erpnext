"""Guided creation of ERPNext records through the chat.

The user names what to create ("create purchase invoice"); the assistant asks for the
fields a person has to provide, one question at a time, validates every answer against
real ERPNext data, shows a summary and creates the record only after explicit confirmation.
Transactions are always created as Draft through the standard insert() (normal permissions
and ERPNext validation); nothing is submitted. Missing customers, suppliers and items can be
created on the way with the same flow, after their own confirmation.
"""

import re
import uuid

import frappe
from frappe.utils import flt, nowdate, strip_html

from ai_erpnext.mapping.customer import CustomerMatcher
from ai_erpnext.mapping.item import ItemMatcher
from ai_erpnext.mapping.normalize import clean_text, normalize_name, parse_date, parse_number


def F(fieldname, label, type, required=True, doctype=None, filters=None, default=None, ask=None, master=None, prompt=None, skip_label=None):
	return {"fieldname": fieldname, "label": label, "type": type, "required": required, "doctype": doctype, "filters": filters or {},
		"default": default, "ask": required if ask is None else ask, "master": master, "prompt": prompt, "skip_label": skip_label}


COMPANY = F("company", "Company", "link", doctype="Company", default="company")
CUSTOMER = F("customer", "Customer", "link", doctype="Customer", filters={"disabled": 0}, master="Customer", prompt="Which customer is this for?")
SUPPLIER = F("supplier", "Supplier", "link", doctype="Supplier", filters={"disabled": 0}, master="Supplier", prompt="Which supplier is this from?")
WAREHOUSE = {"doctype": "Warehouse", "filters": {"is_group": 0, "disabled": 0, "company": "$company"}}

SPECS = {
	"Sales Invoice": {"party": "customer", "items": "selling", "header": [CUSTOMER, COMPANY,
		F("posting_date", "Posting date", "date", default="today"), F("due_date", "Due date", "date", required=False)]},
	"Purchase Invoice": {"party": "supplier", "items": "buying", "header": [SUPPLIER, COMPANY,
		F("posting_date", "Posting date", "date", default="today"),
		F("bill_no", "Supplier invoice number", "data", required=False, ask=True, prompt="What is the supplier's invoice number?", skip_label="Skip"),
		F("due_date", "Due date", "date", required=False)]},
	"Sales Order": {"party": "customer", "items": "selling", "header": [CUSTOMER, COMPANY,
		F("transaction_date", "Order date", "date", default="today"),
		F("delivery_date", "Delivery date", "date", prompt="When should it be delivered?")]},
	"Purchase Order": {"party": "supplier", "items": "buying", "header": [SUPPLIER, COMPANY,
		F("transaction_date", "Order date", "date", default="today"),
		F("schedule_date", "Required by", "date", prompt="By when do you need the items?")]},
	"Quotation": {"party": "party_name", "items": "selling", "fixed": {"quotation_to": "Customer"}, "header": [
		F("party_name", "Customer", "link", doctype="Customer", filters={"disabled": 0}, master="Customer", prompt="Which customer is the quotation for?"),
		COMPANY, F("transaction_date", "Quotation date", "date", default="today"), F("valid_till", "Valid till", "date", required=False)]},
	"Delivery Note": {"party": "customer", "items": "selling", "header": [CUSTOMER, COMPANY,
		F("posting_date", "Posting date", "date", default="today"),
		F("set_warehouse", "Warehouse", "link", prompt="Which warehouse are the items delivered from?", **WAREHOUSE)]},
	"Purchase Receipt": {"party": "supplier", "items": "buying", "header": [SUPPLIER, COMPANY,
		F("posting_date", "Posting date", "date", default="today"),
		F("set_warehouse", "Warehouse", "link", prompt="Which warehouse are the items received into?", **WAREHOUSE)]},
	"Customer": {"party": None, "items": None, "title": "customer_name", "header": [
		F("customer_name", "Customer name", "data", prompt="What is the customer's name?"),
		F("customer_type", "Customer type", "select", default="Company", ask=False),
		F("customer_group", "Customer group", "link", required=False, doctype="Customer Group", filters={"is_group": 0}),
		F("territory", "Territory", "link", required=False, doctype="Territory", filters={"is_group": 0})]},
	"Supplier": {"party": None, "items": None, "title": "supplier_name", "header": [
		F("supplier_name", "Supplier name", "data", prompt="What is the supplier's name?"),
		F("supplier_type", "Supplier type", "select", default="Company", ask=False),
		F("supplier_group", "Supplier group", "link", required=False, doctype="Supplier Group", filters={"is_group": 0})]},
	"Item": {"party": None, "items": None, "title": "item_name", "fixed": {"is_sales_item": 1, "is_purchase_item": 1}, "header": [
		F("item_code", "Item code", "data", prompt="What item code should the new item have?"),
		F("item_name", "Item name", "data", required=False, default="item_code"),
		F("item_group", "Item group", "link", doctype="Item Group", filters={"is_group": 0}, prompt="Which item group does it belong to?"),
		F("stock_uom", "Stock unit (UOM)", "link", doctype="UOM", filters={"enabled": 1}, default="stock_uom"),
		F("is_stock_item", "Stock or service", "check", prompt="Is it a stock item (tracked in inventory) or a non-stock item / service?")]},
}
MASTERS = ("Customer", "Supplier", "Item")
CREATABLE = tuple(SPECS)

ITEM_FILTERS = {"selling": {"is_sales_item": 1}, "buying": {"is_purchase_item": 1}}
LABEL_ALIASES = {"date": "posting_date", "invoice date": "posting_date", "order date": "transaction_date", "party": None,
	"supplier invoice number": "bill_no", "bill number": "bill_no", "bill no": "bill_no", "required by": "schedule_date",
	"required by date": "schedule_date", "delivery date": "delivery_date", "warehouse": "set_warehouse", "uom": "stock_uom", "unit": "stock_uom"}


# ------------------------------------------------------------------------------------------------
# Public entry points used by the engine
# ------------------------------------------------------------------------------------------------
def active(ctx):
	return bool(ctx.st.get("forms"))


def match_doctype(text):
	value = normalize_name(text).rstrip("s")
	for dt in CREATABLE:
		if value in (dt.lower(), dt.lower().rstrip("s")):
			return dt
	return None


def start(ctx, doctype, values=None, items=None, return_to=None):
	if doctype not in SPECS:
		ctx.say(f"I can't create {doctype} records from the chat. I can create: {', '.join(CREATABLE)}.")
		return
	if not frappe.has_permission(doctype, "create"):
		ctx.say(f"You don't have permission to create {doctype} records.")
		return
	spec = SPECS[doctype]
	form = {"doctype": doctype, "values": {}, "labels": {}, "items": [], "draft_item": None, "queued_items": [],
		"items_done": False, "return_to": return_to}
	ctx.st.setdefault("forms", []).append(form)
	for f in spec["header"]:
		default = _default(f, form)
		if default not in (None, ""):
			form["values"][f["fieldname"]] = default
			form["labels"][f["fieldname"]] = default
	notes = []
	for key, raw in (values or {}).items():
		field = _field(doctype, spec["party"] if key == "party" else key)
		if field and clean_text(raw, 140):
			error = _set(ctx, form, field, raw, from_text=True)
			if error:
				notes.append(error)
	for it in (items or [])[:20]:
		if isinstance(it, dict) and clean_text(it.get("item"), 140):
			form["queued_items"].append({"text": clean_text(it.get("item"), 140), "qty": parse_number(it.get("qty")), "rate": parse_number(it.get("rate"))})
	intro = f"Let's create a {'new ' if doctype in MASTERS else ''}{doctype}" + ("" if doctype in MASTERS else " (it will be saved as a Draft)") + ". I'll ask only for what's needed."
	if doctype == "Sales Invoice" and not return_to:
		intro += " You can also attach an invoice image instead."
	ctx.say(intro)
	for n in notes:
		ctx.say(n)
	return next_step(ctx)


def cancel(ctx):
	forms = ctx.st.get("forms") or []
	ctx.st["forms"] = []
	ctx.st["pending"] = None
	ctx.say(f"Cancelled{' the ' + forms[0]['doctype'] if forms else ''}. Nothing was created.")


def next_step(ctx):
	form = ctx.st["forms"][-1]
	spec = SPECS[form["doctype"]]
	for f in spec["header"]:
		wanted = f["ask"] or f["fieldname"] in form.get("force_ask", [])
		if wanted and f["fieldname"] not in form["values"] and f["fieldname"] not in form.get("skipped", []):
			return _ask_field(ctx, form, f)
	if spec["items"] and not form["items_done"]:
		return _items_step(ctx, form)
	return _ask_confirm(ctx, form)


def answer(ctx, pending, value, from_text):
	form = ctx.st["forms"][-1]
	kind, key = pending["kind"], pending.get("key")
	if value == "__cancel__" or (from_text and re.match(r"^\s*(cancel|stop|abort|no)\s*$", value or "", re.I) and kind == "form_confirm"):
		if len(ctx.st["forms"]) > 1:
			ctx.st["forms"].pop()
			ctx.say(f"OK, I won't create the {form['doctype'].lower()}.")
			return next_step(ctx)
		return cancel(ctx)
	if kind == "form_field":
		field = _field(form["doctype"], key)
		if value == "__skip__" and not field["required"]:
			form.setdefault("skipped", []).append(key)
			return next_step(ctx)
		if value == "__create__" and field.get("master"):
			return _start_master(ctx, form, field["master"], pending.get("search") or "", {"field": key})
		error = _set(ctx, form, field, value, from_text=from_text)
		if error == "__asked__":
			return
		if error:
			ctx.say(error)
		return next_step(ctx)
	if kind == "form_item":
		return _answer_item(ctx, form, key, value, from_text, pending)
	if kind == "form_more_items":
		if value == "__more__" or re.search(r"\b(yes|add|another|more)\b", value or "", re.I):
			form["draft_item"] = {}
		elif value == "__done__" or re.search(r"\b(no|done|that'?s all|finish|nothing)\b", value or "", re.I):
			form["items_done"] = True
		else:
			ctx.say("Please choose “Add another item” or “No, that's all”.")
			return _ask_more(ctx, form)
		return next_step(ctx)
	if kind == "form_confirm":
		if value == "__create__" or re.match(r"^\s*(yes|y|ok|okay|confirm|create|go ahead|do it|sure)\b", value or "", re.I):
			return _create(ctx, form)
		if value == "__retry__":
			return _create(ctx, form)
		ctx.say("Please choose Create or Cancel, or tell me what to change (for example “change customer to …”).")
		return _ask_confirm(ctx, form)


def correction(ctx, text):
	"""Handle “change X to Y”, “row 2 qty 5”, “remove row 1”, “add item” while a form is open."""
	form = ctx.st["forms"][-1]
	m = re.match(r"^\s*(?:please\s+)?(?:remove|delete|drop)\s+(?:row|line|item)\s*#?(\d+)\s*$", text, re.I)
	if m:
		idx = int(m.group(1)) - 1
		if 0 <= idx < len(form["items"]):
			removed = form["items"].pop(idx)
			ctx.say(f"Removed row {idx + 1} ({removed.get('item_name') or removed['item_code']}) from this draft.")
		else:
			ctx.say("There's no such row.")
		ctx.st["pending"] = None
		return next_step(ctx) or True
	if re.match(r"^\s*(?:please\s+)?add\s+(?:another\s+|an?\s+|one\s+more\s+)?item\b", text, re.I) and SPECS[form["doctype"]]["items"]:
		form["items_done"] = False
		form["draft_item"] = {}
		ctx.st["pending"] = None
		return next_step(ctx) or True
	m = re.match(r"^\s*(?:please\s+)?(?:change|set|update|make)?\s*(?:row|line|item)\s*#?(\d+)\s+(qty|quantity|rate|price|item)\s*(?:to|as|=|:|is)?\s+(.+?)\s*$", text, re.I)
	if m:
		idx, what, value = int(m.group(1)) - 1, m.group(2).lower(), m.group(3)
		if not 0 <= idx < len(form["items"]):
			ctx.say("There's no such row.")
			return True
		row = form["items"][idx]
		if what in ("qty", "quantity"):
			qty = parse_number(value)
			if qty is None or qty <= 0:
				ctx.say("The quantity must be a number greater than 0.")
				return True
			row["qty"] = qty
		elif what in ("rate", "price"):
			rate = parse_number(value)
			if rate is None or rate < 0:
				ctx.say("The rate must be a number of 0 or more.")
				return True
			row["rate"] = rate
		else:
			res = _resolve_item(form, value)
			if res[0] != "ok":
				ctx.say(f"I couldn't find a single item matching “{value}”.")
				return True
			row.update(item_code=res[1], item_name=res[2])
		ctx.say(f"Updated row {idx + 1}.")
		ctx.st["pending"] = None
		return next_step(ctx) or True
	m = re.match(r"^\s*(?:please\s+)?(?:change|set|update|use|make)\s+(?:the\s+)?(.+?)\s+(?:to|as|=|:|is)\s+(.+?)\s*$", text, re.I)
	if m:
		field = _field_by_label(form, m.group(1))
		if field:
			error = _set(ctx, form, field, m.group(2), from_text=True)
			if error == "__asked__":
				return True
			ctx.say(error or f"{field['label']} set to {form['labels'].get(field['fieldname'])}.")
			ctx.st["pending"] = None
			return next_step(ctx) or True
	return False


# ------------------------------------------------------------------------------------------------
# Header fields
# ------------------------------------------------------------------------------------------------
def _field(doctype, fieldname):
	return next((f for f in SPECS[doctype]["header"] if f["fieldname"] == fieldname), None)


def _field_by_label(form, label):
	label = normalize_name(label)
	fieldname = LABEL_ALIASES.get(label)
	spec = SPECS[form["doctype"]]
	if label in ("party", "customer", "supplier") and spec["party"]:
		fieldname = spec["party"]
	for f in spec["header"]:
		if f["fieldname"] == fieldname or normalize_name(f["label"]) == label or f["fieldname"].replace("_", " ") == label:
			return f
	return None


def _default(f, form):
	d = f.get("default")
	if d == "today":
		return nowdate()
	if d == "company":
		permitted = frappe.get_list("Company", pluck="name", limit_page_length=50)
		for c in (frappe.defaults.get_user_default("Company"), frappe.db.get_single_value("Global Defaults", "default_company")):
			if c and c in permitted:
				return c
		return permitted[0] if len(permitted) == 1 else None
	if d == "stock_uom":
		return frappe.db.get_single_value("Stock Settings", "stock_uom") or None
	if d == "item_code":
		return None  # filled from item_code at creation
	if f["type"] == "select" and d:
		return d
	return None


def _filters(f, form):
	return {k: (form["values"].get(v[1:]) if isinstance(v, str) and v.startswith("$") else v) for k, v in (f.get("filters") or {}).items()
		if not (isinstance(v, str) and v.startswith("$") and not form["values"].get(v[1:]))}


def _suggestions(doctype, filters, limit=6):
	try:
		return frappe.get_list(doctype, filters=filters, pluck="name", limit_page_length=limit, order_by="modified desc")
	except frappe.PermissionError:
		return []


def _ask_field(ctx, form, f, search=None, candidates=None, text=None):
	options, input_type = [], "text"
	if f["type"] == "link":
		names = candidates if candidates is not None else _suggestions(f["doctype"], _filters(f, form))
		options = [{"label": n, "value": n} for n in names]
		if f.get("master"):
			options.append({"label": f"Create new {f['master'].lower()}" + (f" “{search}”" if search else ""), "value": "__create__", "style": "secondary"})
	elif f["type"] == "date":
		input_type = "date"
		if f["fieldname"] == "due_date" and form["values"].get("posting_date"):
			options = [{"label": f"Same as posting date ({form['values']['posting_date']})", "value": form["values"]["posting_date"]}]
	elif f["type"] == "select":
		input_type = None
		opts = [o for o in (frappe.get_meta(form["doctype"]).get_field(f["fieldname"]).options or "").split("\n") if o]
		options = [{"label": o, "value": o} for o in opts]
	elif f["type"] == "check":
		input_type = None
		options = [{"label": "Stock item (tracked in inventory)", "value": "1"}, {"label": "Non-stock item / service", "value": "0"}]
	if f["fieldname"] == "item_code" and form["values"].get("item_name") and not frappe.db.exists("Item", form["values"]["item_name"]):
		options.insert(0, {"label": f"Use the item name: {form['values']['item_name']}", "value": form["values"]["item_name"]})
	if not f["required"] and f.get("skip_label"):
		options.append({"label": f["skip_label"], "value": "__skip__", "style": "secondary"})
	prompt = text or f.get("prompt") or f"Which {f['label'].lower()} should I use?"
	if f["type"] == "link" and input_type == "text" and not candidates:
		prompt += " Pick one or type a name to search."
	q = ctx.ask("form_field", prompt, key=f["fieldname"], options=options, input=input_type)
	if search:
		ctx.st["pending"]["search"] = search


def _set(ctx, form, f, raw, from_text):
	"""Validate one header value. Returns an error sentence, "__asked__" if a follow-up question was asked, or None."""
	value = clean_text(raw, 140)
	if value is None:
		return f"Please give the {f['label'].lower()}."
	name = f["fieldname"]
	if f["type"] == "data":
		if name == "item_code" and frappe.db.exists("Item", value):
			return f"An item with code “{value}” already exists. Please choose another code."
		if name in ("customer_name", "supplier_name"):
			existing = _exists_named(form["doctype"], name, value)
			if not existing and name == "customer_name":
				res = CustomerMatcher().match(value)  # also catches “Acme Ltd” vs “ACME Ltd.”
				existing = res.name if res.matched else None
			if existing:
				return f"A {form['doctype'].lower()} called “{existing}” already exists, so I won't create a duplicate. Choose another name or say “cancel”."
		form["values"][name] = form["labels"][name] = value
		return None
	if f["type"] == "date":
		if value.lower() in ("today", "now"):
			value = nowdate()
		parsed = parse_date(value)
		if not parsed.ok:
			return "That date could be read two ways. Please use YYYY-MM-DD." if parsed.ambiguous else "Please give the date as YYYY-MM-DD."
		form["values"][name] = form["labels"][name] = parsed.value
		return None
	if f["type"] == "select":
		opts = [o for o in (frappe.get_meta(form["doctype"]).get_field(name).options or "").split("\n") if o]
		match = next((o for o in opts if o.lower() == value.lower()), None)
		if not match:
			return f"Please choose one of: {', '.join(opts)}."
		form["values"][name] = form["labels"][name] = match
		return None
	if f["type"] == "check":
		v = value.lower()
		if v in ("1", "yes", "stock", "stock item", "stock item (tracked in inventory)"):
			form["values"][name], form["labels"][name] = 1, "Stock item"
		elif v in ("0", "no", "service", "non-stock", "non stock", "non-stock item / service", "non-stock item"):
			form["values"][name], form["labels"][name] = 0, "Non-stock / service"
		else:
			return "Is it a stock item or a non-stock item / service?"
		return None
	# link
	status, found, display = _resolve_link(f, form, value, from_text)
	if status == "ok":
		form["values"][name], form["labels"][name] = found, display
		if name == "company" and "set_warehouse" in form["values"]:
			form["values"].pop("set_warehouse")  # warehouse belongs to a company
		return None
	if status == "ambiguous":
		_ask_field(ctx, form, f, search=value, candidates=found, text=f"Several {f['label'].lower()} records match “{value}”. Which one?")
		return "__asked__"
	if f.get("master"):
		ctx.ask("form_field", f"I couldn't find a {f['master'].lower()} called “{value}”. Should I create it, or search again?",
			key=name, options=[{"label": f"Create new {f['master'].lower()} “{value}”", "value": "__create__"}], input="text")
		ctx.st["pending"]["search"] = value
		return "__asked__"
	return f"I couldn't find a {f['label'].lower()} called “{value}”."


class _GenericMatcher:
	"""Name/title matcher for Link fields without a dedicated matcher.
	Only an exact name or exact (case-insensitive) title is a match; partial matches are suggestions."""

	def __init__(self, doctype, title=None, filters=None):
		self.doctype, self.filters = doctype, filters or {}
		meta = frappe.get_meta(doctype)
		self.title = title or (meta.title_field if meta.title_field and meta.get_field(meta.title_field) else None)

	def match(self, value):
		fields = ["name"] + ([self.title] if self.title else [])
		exact = frappe.get_list(self.doctype, filters={**self.filters, "name": value}, fields=fields, limit_page_length=2)
		if not exact and self.title:
			exact = frappe.get_list(self.doctype, filters={**self.filters, self.title: value}, fields=fields, limit_page_length=3)
		if len(exact) == 1:
			return _Result(exact, self.title, matched=True)
		rows = exact or frappe.get_list(self.doctype, filters=self.filters, or_filters=[[f, "like", f"%{value}%"] for f in fields],
			fields=fields, limit_page_length=6)
		return _Result(rows, self.title, matched=False)


class _Result:
	def __init__(self, rows, title, matched):
		self.matched = matched
		self.name = rows[0]["name"] if matched else None
		self.display = (rows[0].get(title) or rows[0]["name"]) if matched and title else self.name
		self.candidates = [] if matched else [{"name": r["name"], "display_name": r.get(title) or r["name"]} for r in rows]


def _exists_named(doctype, title_field, value):
	return frappe.db.get_value(doctype, {title_field: value}, "name")


def _resolve_link(f, form, value, from_text):
	doctype, filters = f["doctype"], _filters(f, form)
	if not from_text:  # button values are exact record names; re-check existence and permission
		rows = frappe.get_list(doctype, filters={**filters, "name": value}, pluck="name", limit_page_length=1)
		return ("ok", rows[0], value) if rows else ("none", None, None)
	if doctype == "Customer":
		res = CustomerMatcher().match(value)
		if res.matched:
			return "ok", res.name, res.display_name
		if res.candidates:
			return "ambiguous", [c["name"] for c in res.candidates], None
		row = CustomerMatcher().resolve_name(value)
		return ("ok", row["name"], row["name"]) if row else ("none", None, None)
	res = _GenericMatcher(doctype, filters=filters).match(value)
	if res.matched:
		return "ok", res.name, res.display
	if res.candidates:
		return "ambiguous", [c["name"] for c in res.candidates], None
	return "none", None, None


# ------------------------------------------------------------------------------------------------
# Items
# ------------------------------------------------------------------------------------------------
def _resolve_item(form, value):
	"""("ok", code, name) | ("ambiguous", [codes], None) | ("not_allowed", code, reason) | ("none", None, None)."""
	flags = ITEM_FILTERS[SPECS[form["doctype"]]["items"]]

	def allowed(code):
		return bool(frappe.get_list("Item", filters={"name": code, "disabled": 0, **flags}, pluck="name", limit_page_length=1))

	res = ItemMatcher().match({"item_name": value, "item_code": value})
	if res.matched:
		if not allowed(res.name):
			kind = "sales" if "is_sales_item" in flags else "purchase"
			return "not_allowed", res.name, f"Item {res.name} is not a {kind} item."
		return "ok", res.name, frappe.db.get_value("Item", res.name, "item_name")
	candidates = [c["name"] for c in res.candidates if allowed(c["name"])]
	return ("ambiguous", candidates, None) if candidates else ("none", None, None)


def _items_step(ctx, form):
	if form["draft_item"] is None and form["queued_items"]:
		q = form["queued_items"].pop(0)
		status, code, name = _resolve_item(form, q["text"])
		form["draft_item"] = {"text": q["text"], "qty": q["qty"], "rate": q["rate"]}
		if status == "ok":
			form["draft_item"].update(item_code=code, item_name=name)
	if form["draft_item"] is None:
		if form["items"]:
			return _ask_more(ctx, form)
		form["draft_item"] = {}
	d = form["draft_item"]
	if not d.get("item_code"):
		return _ask_item(ctx, form, search=d.get("text"))
	if d.get("qty") is None or d["qty"] <= 0:
		return ctx.ask("form_item", f"How many of {d['item_name'] or d['item_code']}?", key="qty", input="text")
	if d.get("rate") is None or d["rate"] < 0:
		options = []
		price = _price(form, d["item_code"])
		if price:
			options.append({"label": f"Use price list rate {flt(price[0], 2)} ({price[1]})", "value": str(flt(price[0], 2))})
		return ctx.ask("form_item", f"What is the rate (price per unit) for {d['item_name'] or d['item_code']}?", key="rate", options=options, input="text")
	uom = frappe.db.get_value("Item", d["item_code"], "stock_uom")
	form["items"].append({"item_code": d["item_code"], "item_name": d.get("item_name"), "qty": d["qty"], "rate": d["rate"], "uom": uom})
	form["draft_item"] = None
	ctx.say(f"Added {flt(d['qty'])} × {d.get('item_name') or d['item_code']} at {flt(d['rate'], 2)}.")
	if form["queued_items"]:
		return _items_step(ctx, form)
	return _ask_more(ctx, form)


def _ask_more(ctx, form):
	return ctx.ask("form_more_items", "Do you want to add another item?",
		options=[{"label": "Add another item", "value": "__more__"}, {"label": "No, that's all", "value": "__done__", "style": "secondary"}])


def _ask_item(ctx, form, search=None, candidates=None, text=None):
	flags = ITEM_FILTERS[SPECS[form["doctype"]]["items"]]
	names = candidates if candidates is not None else _suggestions("Item", {"disabled": 0, "has_variants": 0, **flags})
	options = [{"label": f"{frappe.db.get_value('Item', n, 'item_name')} ({n})" if frappe.db.get_value("Item", n, "item_name") != n else n, "value": n} for n in names]
	options.append({"label": "Create new item" + (f" “{search}”" if search else ""), "value": "__create__", "style": "secondary"})
	prompt = text or ("Which item? Type the item name or code." if not search else f"I couldn't find the item “{search}”. Pick one, search again, or create it.")
	ctx.ask("form_item", prompt, key="item_code", options=options, input="text")
	ctx.st["pending"]["search"] = search


def _answer_item(ctx, form, key, value, from_text, pending):
	d = form["draft_item"] if form["draft_item"] is not None else {}
	form["draft_item"] = d
	if key == "item_code":
		if value == "__create__":
			return _start_master(ctx, form, "Item", pending.get("search") or d.get("text") or "", {"field": "item_code", "row": "draft"})
		if not from_text:
			rows = frappe.get_list("Item", filters={"name": value, "disabled": 0, **ITEM_FILTERS[SPECS[form["doctype"]]["items"]]}, pluck="name", limit_page_length=1)
			status, code, name = ("ok", rows[0], frappe.db.get_value("Item", rows[0], "item_name")) if rows else ("none", None, None)
		else:
			status, code, name = _resolve_item(form, value)
		if status == "ok":
			d.update(item_code=code, item_name=name, text=value)
		elif status == "ambiguous":
			return _ask_item(ctx, form, search=value, candidates=code, text=f"Several items match “{value}”. Which one?")
		elif status == "not_allowed":
			ctx.say(name)
			return _ask_item(ctx, form)
		else:
			d["text"] = value
			return _ask_item(ctx, form, search=value)
	elif key == "qty":
		qty = parse_number(value)
		if qty is None or qty <= 0:
			ctx.say("The quantity must be a number greater than 0.")
		else:
			d["qty"] = qty
	elif key == "rate":
		rate = parse_number(value)
		if rate is None or rate < 0:
			ctx.say("The rate must be a number of 0 or more.")
		else:
			d["rate"] = rate
	return next_step(ctx)


def _price(form, item_code):
	kind = SPECS[form["doctype"]]["items"]
	row = frappe.get_all("Item Price", filters={"item_code": item_code, kind: 1}, fields=["price_list_rate", "price_list"], order_by="modified desc", limit=1)
	return (row[0]["price_list_rate"], row[0]["price_list"]) if row else None


# ------------------------------------------------------------------------------------------------
# Nested master creation, confirmation and creation
# ------------------------------------------------------------------------------------------------
def _start_master(ctx, form, master, name, return_to):
	title = SPECS[master]["title"] if master != "Item" else "item_name"
	values = {title: name} if name else {}
	if master == "Item" and name:
		values = {"item_name": name}
	ctx.st["pending"] = None
	return start(ctx, master, values=values, return_to=return_to)


def _ask_confirm(ctx, form):
	spec, doctype = SPECS[form["doctype"]], form["doctype"]
	rows = [{"label": f["label"], "value": form["labels"].get(f["fieldname"], form["values"].get(f["fieldname"]))}
		for f in spec["header"] if form["values"].get(f["fieldname"]) not in (None, "")]
	card = {"type": "form_preview", "title": f"New {doctype}" + ("" if doctype in MASTERS else " (Draft)"), "rows": rows}
	if spec["items"]:
		if not form["items"]:
			form["items_done"] = False
			ctx.say("A document needs at least one item.")
			return next_step(ctx)
		card["items"] = [{"name": i.get("item_name") or i["item_code"], "code": i["item_code"], "qty": i["qty"], "uom": i.get("uom"),
			"rate": flt(i["rate"], 2), "amount": flt(i["qty"] * i["rate"], 2)} for i in form["items"]]
		card["note"] = (f"Items total {flt(sum(i['qty'] * i['rate'] for i in form['items']), 2)} before taxes. ERPNext fills accounts, currency, "
			"price list and taxes from your settings and calculates the final totals.")
	label = f"Create {doctype}" if doctype in MASTERS else f"Create draft {doctype}"
	text = f"Here's the {doctype.lower()}. Shall I create it" + ("?" if doctype in MASTERS else " as a Draft? It will not be submitted.") \
		+ " You can still change anything, e.g. “change " + (spec["party"] or spec["header"][0]["fieldname"]).replace("_", " ") + " to …”" \
		+ (" or “row 1 qty 5”." if spec["items"] else ".")
	ctx.ask("form_confirm", text, options=[{"label": label, "value": "__create__"}, {"label": "Cancel", "value": "__cancel__", "style": "secondary"}], card=card)


def _doc_dict(form):
	spec, values = SPECS[form["doctype"]], dict(form["values"])
	if form["doctype"] == "Item" and not values.get("item_name"):
		values["item_name"] = values.get("item_code")
	doc = {"doctype": form["doctype"], **values, **spec.get("fixed", {})}
	if form["doctype"] in ("Sales Invoice", "Purchase Invoice") and values.get("posting_date") and values["posting_date"] != nowdate():
		doc["set_posting_time"] = 1
	if spec["items"]:
		rows = []
		for i in form["items"]:
			row = {"item_code": i["item_code"], "qty": i["qty"], "rate": i["rate"]}
			if form["doctype"] == "Purchase Order":
				row["schedule_date"] = values.get("schedule_date")
			if form["doctype"] == "Sales Order":
				row["delivery_date"] = values.get("delivery_date")
			if form["doctype"] in ("Delivery Note", "Purchase Receipt") and values.get("set_warehouse"):
				row["warehouse"] = values["set_warehouse"]
			rows.append(row)
		doc["items"] = rows
	return doc


def _create(ctx, form):
	doctype = form["doctype"]
	frappe.db.savepoint("ai_chat_form")
	try:
		doc = frappe.get_doc(_doc_dict(form))
		doc.flags.ignore_mandatory = False
		doc.insert()  # standard permissions + ERPNext validation; never submit()
		if doc.meta.is_submittable and doc.docstatus != 0:
			raise frappe.ValidationError("Created documents must stay Draft")
	except frappe.MandatoryError as e:
		frappe.db.rollback(save_point="ai_chat_form")
		frappe.local.message_log = []
		missing = [m.strip() for m in str(e).split(":")[-1].split(",")]
		known = [m for m in missing if _field(doctype, m)]
		for m in known:
			form["values"].pop(m, None)
			form.setdefault("force_ask", []).append(m)
			if m in form.get("skipped", []):
				form["skipped"].remove(m)
		if known:
			ctx.say("ERPNext needs a few more details.")
			return next_step(ctx)
		return _failed(ctx, form, f"ERPNext requires fields I can't collect here: {', '.join(missing)}. Please create it in ERPNext.")
	except Exception as e:
		frappe.db.rollback(save_point="ai_chat_form")
		frappe.local.message_log = []
		return _failed(ctx, form, strip_html(str(e) or "unknown error")[:300])

	ctx.st["forms"].pop()
	ctx.st["pending"] = None
	notes = erpnext_notes()
	if doctype in MASTERS:
		ctx.say(f"{doctype} {doc.name} created.", card={"type": "created", "title": f"{doctype} created", "doctype": doctype, "name": doc.name,
			"rows": [{"label": doctype, "value": doc.name}], "action_label": f"Open {doctype}"})
		ret = form.get("return_to")
		if ret and ctx.st["forms"]:
			parent = ctx.st["forms"][-1]
			if ret.get("row") == "draft":
				parent["draft_item"] = dict(parent.get("draft_item") or {}, item_code=doc.name, item_name=doc.get("item_name"))
			else:
				parent["values"][ret["field"]] = doc.name
				parent["labels"][ret["field"]] = doc.get(SPECS[doctype]["title"]) or doc.name
			ctx.say(f"Back to the {parent['doctype']}.")
			return next_step(ctx)
		return
	party = doc.get("customer_name") or doc.get("supplier_name") or doc.get("party_name") or doc.get("customer") or doc.get("supplier")
	date = doc.get("posting_date") or doc.get("transaction_date")
	status = "Draft" if doc.docstatus == 0 else "Submitted"
	ctx.say(f"Your {doctype} draft has been created successfully. Open it to review and submit it yourself in ERPNext."
		+ (("\nERPNext notes: " + " ".join(notes)) if notes else ""),
		card={"type": "created", "title": f"{doctype} {doc.name}", "doctype": doctype, "name": doc.name, "action_label": f"Open {doctype}",
			"rows": [{"label": "ID", "value": doc.name}, {"label": "Party", "value": party}, {"label": "Date", "value": str(date)},
				{"label": "Grand total", "value": f"{flt(doc.get('grand_total'), 2)} {doc.get('currency') or ''}".strip()},
				{"label": "Status", "value": status}]})


def erpnext_notes():
	"""Move ERPNext's informational messages from a successful save into the chat (no pop-ups)."""
	import json as _json

	notes = []
	for raw in frappe.local.message_log or []:
		try:
			msg = _json.loads(raw) if isinstance(raw, str) else raw
		except ValueError:
			msg = {"message": raw}
		text = strip_html(str((msg or {}).get("message") or "")).strip()
		if text:
			notes.append(text[:300])
	frappe.local.message_log = []
	return notes[:3]


def _failed(ctx, form, reason):
	ctx.say(f"ERPNext didn't accept the {form['doctype']}: {reason}")
	ctx.ask("form_confirm", "Nothing was created. You can change a value (for example “change customer to …”), retry, or cancel.",
		options=[{"label": "Retry", "value": "__retry__"}, {"label": "Cancel", "value": "__cancel__", "style": "secondary"}])
