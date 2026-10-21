"""Guided creation of ERPNext records through the chat.

The user names what to create ("create purchase invoice"); the assistant asks for the
fields a person has to provide, one question at a time, validates every answer against
real ERPNext data, shows a summary and creates the record only after explicit confirmation.
Transactions are always created as Draft through the standard insert() (normal permissions
and ERPNext validation); nothing is submitted. Missing customers, suppliers and items can be
created on the way with the same flow, after their own confirmation.

Before the preview, chat/requirements.py checks what ERPNext will additionally require (row
warehouses, fields a site made mandatory) using ERPNext's own defaulting, so the chat asks for
missing values instead of failing on Create. insert() remains the final authority.

Linked masters (customer, supplier, item, warehouse) are always chosen from existing records
first: matching records are offered (with "Show more" when there are more), and "Create new
..." is a separate, explicit choice, offered only for masters the chat can create and the
user may create. A name that matches nothing is never created automatically. The form keeps
everything already answered: corrections ("change quantity to 5", "use Finished Goods
warehouse"), a change of document type ("I meant Purchase Receipt") and "start over" act on
this state instead of starting a new conversation.
"""

import re
import uuid

import frappe
from frappe.utils import flt, nowdate, strip_html

from ai_erpnext.capability import service as capability
from ai_erpnext.capability.access import readable_exists, readable_names
from ai_erpnext.chat import requirements
from ai_erpnext.mapping.customer import CustomerMatcher
from ai_erpnext.mapping.item import ItemMatcher
from ai_erpnext.mapping.normalize import clean_text, normalize_name, parse_date, parse_number


def F(fieldname, label, type, required=True, doctype=None, filters=None, default=None, ask=None, master=None, prompt=None, skip_label=None, when=None):
	"""when: {fieldname: (values...)}: the field exists on this form only while those fields have one of those values."""
	return {"fieldname": fieldname, "label": label, "type": type, "required": required, "doctype": doctype, "filters": filters or {},
		"default": default, "ask": required if ask is None else ask, "master": master, "prompt": prompt, "skip_label": skip_label, "when": when}


COMPANY = F("company", "Company", "link", doctype="Company", default="company")
CUSTOMER = F("customer", "Customer", "link", doctype="Customer", filters={"disabled": 0}, master="Customer", prompt="Which customer is this for?")
SUPPLIER = F("supplier", "Supplier", "link", doctype="Supplier", filters={"disabled": 0}, master="Supplier", prompt="Which supplier is this from?")
WAREHOUSE = {"doctype": "Warehouse", "filters": {"is_group": 0, "disabled": 0, "company": "$company"}}
# One warehouse for all stock rows. Not asked up front: only when a stock row has no valid
# default (see _requirements), and it can be changed with "change warehouse to ...".
SET_WAREHOUSE = F("set_warehouse", "Warehouse", "link", required=False, ask=False, **WAREHOUSE)
# Stock Entry types the chat may draft: plain receipts, issues and transfers (manufacturing, repacking and subcontracting
# entries need BOMs and work orders the chat doesn't collect).
STOCK_ENTRY_TYPES = ("Material Receipt", "Material Issue", "Material Transfer")

SPECS = {
	"Sales Invoice": {"party": "customer", "items": "selling", "header": [CUSTOMER, COMPANY,
		F("posting_date", "Posting date", "date", default="today"), F("due_date", "Due date", "date", required=False)]},
	"Purchase Invoice": {"party": "supplier", "items": "buying", "header": [SUPPLIER, COMPANY,
		F("posting_date", "Posting date", "date", default="today"),
		F("bill_no", "Supplier invoice number", "data", required=False, ask=True, prompt="What is the supplier's invoice number?", skip_label="Skip"),
		F("due_date", "Due date", "date", required=False)]},
	"Sales Order": {"party": "customer", "items": "selling", "header": [CUSTOMER, COMPANY,
		F("transaction_date", "Order date", "date", default="today"),
		F("delivery_date", "Delivery date", "date", prompt="When should it be delivered?"), SET_WAREHOUSE]},
	"Purchase Order": {"party": "supplier", "items": "buying", "header": [SUPPLIER, COMPANY,
		F("transaction_date", "Order date", "date", default="today"),
		F("schedule_date", "Required by", "date", prompt="By when do you need the items?"), SET_WAREHOUSE]},
	"Quotation": {"party": "party_name", "items": "selling", "fixed": {"quotation_to": "Customer"}, "header": [
		F("party_name", "Customer", "link", doctype="Customer", filters={"disabled": 0}, master="Customer", prompt="Which customer is the quotation for?"),
		COMPANY, F("transaction_date", "Quotation date", "date", default="today"), F("valid_till", "Valid till", "date", required=False)]},
	"Delivery Note": {"party": "customer", "items": "selling", "header": [CUSTOMER, COMPANY,
		F("posting_date", "Posting date", "date", default="today"), SET_WAREHOUSE]},
	"Purchase Receipt": {"party": "supplier", "items": "buying", "header": [SUPPLIER, COMPANY,
		F("posting_date", "Posting date", "date", default="today"), SET_WAREHOUSE]},
	# Draft-only internal stock documents: no prices are asked (rates: False); ERPNext values the stock itself.
	"Material Request": {"party": None, "items": "any", "rates": False, "header": [
		F("material_request_type", "Purpose", "select", prompt="What is the request for?"), COMPANY,
		F("transaction_date", "Request date", "date", default="today"),
		F("schedule_date", "Required by", "date", prompt="By when are the items needed?"), SET_WAREHOUSE]},
	"Stock Entry": {"party": None, "items": "stock", "rates": False, "header": [
		F("stock_entry_type", "Type", "link", doctype="Stock Entry Type", filters={"name": ["in", list(STOCK_ENTRY_TYPES)]},
			prompt="Is this a material receipt, a material issue or a transfer between warehouses?"), COMPANY,
		F("posting_date", "Posting date", "date", default="today"),
		F("from_warehouse", "Source warehouse", "link", prompt="Which warehouse do the items leave?", when={"stock_entry_type": ("Material Issue", "Material Transfer")}, **WAREHOUSE),
		F("to_warehouse", "Target warehouse", "link", prompt="Which warehouse do the items go into?", when={"stock_entry_type": ("Material Receipt", "Material Transfer")}, **WAREHOUSE)]},
	"Customer": {"party": None, "items": None, "title": "customer_name", "header": [
		F("customer_name", "Customer name", "data", prompt="What is the customer's name?"),
		F("customer_type", "Customer type", "select", default="Company", ask=False),
		F("customer_group", "Customer group", "link", required=False, doctype="Customer Group", filters={"is_group": 0}),
		F("territory", "Territory", "link", required=False, doctype="Territory", filters={"is_group": 0})]},
	"Supplier": {"party": None, "items": None, "title": "supplier_name", "header": [
		F("supplier_name", "Supplier name", "data", prompt="What is the supplier's name?"),
		F("supplier_type", "Supplier type", "select", default="Company", ask=False),
		F("supplier_group", "Supplier group", "link", required=False, doctype="Supplier Group", filters={"is_group": 0})]},
	"Lead": {"party": None, "items": None, "title": "first_name", "header": [
		F("first_name", "Contact name", "data", prompt="What is the lead's name?"),
		F("company_name", "Organization", "data", required=False, ask=True, prompt="Which organization is the lead from?", skip_label="Skip"),
		F("email_id", "Email", "data", required=False, ask=True, prompt="What is the lead's email address?", skip_label="Skip"),
		F("mobile_no", "Mobile number", "data", required=False, ask=True, prompt="What is the lead's mobile number?", skip_label="Skip")]},
	"Issue": {"party": None, "items": None, "title": "subject", "header": [
		F("subject", "Subject", "data", prompt="What is the issue about? Give it a short subject."),
		F("customer", "Customer", "link", required=False, ask=True, doctype="Customer", filters={"disabled": 0}, prompt="Which customer raised it?", skip_label="Skip"),
		F("description", "Description", "data", required=False, ask=True, prompt="Describe the problem.", skip_label="Skip")]},
	"Item": {"party": None, "items": None, "title": "item_name", "fixed": {"is_sales_item": 1, "is_purchase_item": 1}, "header": [
		F("item_code", "Item code", "data", prompt="What item code should the new item have?"),
		F("item_name", "Item name", "data", required=False, default="item_code"),
		F("item_group", "Item group", "link", doctype="Item Group", filters={"is_group": 0}, prompt="Which item group does it belong to?"),
		F("stock_uom", "Stock unit (UOM)", "link", doctype="UOM", filters={"enabled": 1}, default="stock_uom"),
		F("is_stock_item", "Stock or service", "check", prompt="Is it a stock item (tracked in inventory) or a non-stock item / service?")]},
}
# Records ERPNext saves immediately (no Draft state): the confirmation says so. Only Customer, Supplier and Item can also be created
# on the way from inside another form ("Create new ..."): that offer is driven by the `master` of a link field, not by this list.
MASTERS = ("Customer", "Supplier", "Item", "Lead", "Issue")
CREATABLE = tuple(SPECS)

ITEM_FILTERS = {"selling": {"is_sales_item": 1}, "buying": {"is_purchase_item": 1}, "stock": {"is_stock_item": 1}, "any": {}}
LABEL_ALIASES = {"date": "posting_date", "invoice date": "posting_date", "order date": "transaction_date", "party": None,
	"supplier invoice number": "bill_no", "bill number": "bill_no", "bill no": "bill_no", "required by": "schedule_date",
	"required by date": "schedule_date", "delivery date": "delivery_date", "warehouse": "set_warehouse", "uom": "stock_uom", "unit": "stock_uom"}
PAGE = 10  # existing records offered per question (then "Show more")
# Short names people type for a document type ("create po").
DOCTYPE_ALIASES = {"ticket": "Issue", "po": "Purchase Order", "so": "Sales Order", "pi": "Purchase Invoice", "si": "Sales Invoice",
	"dn": "Delivery Note", "pr": "Purchase Receipt", "grn": "Purchase Receipt", "quote": "Quotation", "purchase bill": "Purchase Invoice",
	"vendor": "Supplier", "client": "Customer", "product": "Item", "mr": "Material Request", "indent": "Material Request",
	"material requisition": "Material Request", "stock transfer": "Stock Entry", "material receipt": "Stock Entry", "material issue": "Stock Entry"}

_DT_ALT = "|".join(sorted((re.escape(n) for n in [*(dt.lower() for dt in SPECS), *DOCTYPE_ALIASES]), key=len, reverse=True))
SWITCH_RE = re.compile(r"^\s*(?:no,?\s*|sorry,?\s*|oops,?\s*)?(?:i\s+meant|i\s+mean|switch\s+to|change\s+(?:it|this)\s+(?:to|into)|make\s+(?:it|this)|"
	r"it\s+should\s+be|(?:it'?s|it\s+is)\s+actually)\s+(?:a\s+|an\s+)?(?P<dt>" + _DT_ALT + r")s?(?:\s*,?\s*not\s+(?:a\s+|an\s+)?(?:" + _DT_ALT
	+ r"))?\s*[.!]*\s*$", re.I)
SWITCH_NOT_RE = re.compile(r"^\s*(?:no,?\s*)?(?:a\s+|an\s+)?(?P<dt>" + _DT_ALT + r")\s*,?\s*not\s+(?:a\s+|an\s+)?(?:" + _DT_ALT + r")\s*[.!]*\s*$", re.I)
WAREHOUSE_RE = re.compile(r"^\s*(?:please\s+)?(?:use|set|change|make|update)\s+(?:the\s+)?(?:(?:target|source|accepted|default|set|receiving|delivery|"
	r"rejected)\s+)?warehouse\s*(?:to|as|=|:|is)?\s+(?P<v>.+?)\s*[.!]*\s*$", re.I)
USE_WAREHOUSE_RE = re.compile(r"^\s*(?:please\s+)?(?:use|set)\s+(?:the\s+)?(?P<v>.+?)\s+warehouse(?:\s+(?:for|on)\s+(?:all|every|the)\s+(?:rows|items))?\s*[.!]*\s*$", re.I)
QTY_RATE_FIELD_RE = re.compile(r"^\s*(?:please\s+)?(?:change|set|update|make|use)\s+(?:the\s+)?(?P<what>qty|quantity|rate|price)\s*(?:to|as|=|:|is|of)?\s+(?P<v>.+?)\s*$", re.I)
QTY_AND_RATE_RE = re.compile(r"^\s*(?:qty\s*|quantity\s*)?(?P<q>\d[\d,]*(?:\.\d+)?)\s*(?:units?|pcs|nos|pieces)?\s*(?:@|at|x|×|,|and|for|rate)\s*"
	r"(?:rate\s*|price\s*)?(?:of\s*)?(?P<r>\d[\d,]*(?:\.\d+)?)\s*(?:each|per\s+unit|/\s*unit)?\s*$", re.I)


def answer_term(text):
	"""The name in a reply such as “Use Amit Patel.”, “it's palmer productions” or “no, the one called X”."""
	text = re.sub(r"^\s*(?:no,?\s*)?(?:(?:please\s+)?use|it'?s|it\s+is|i\s+mean|i\s+meant|the\s+one\s+called|called|named)\s+", "", text or "", flags=re.I)
	return text.strip(" .!\"'“”") or None


# ------------------------------------------------------------------------------------------------
# Public entry points used by the engine
# ------------------------------------------------------------------------------------------------
def active(ctx):
	return bool(ctx.st.get("forms"))


def match_doctype(text):
	name = normalize_name(text)
	if DOCTYPE_ALIASES.get(name) or DOCTYPE_ALIASES.get(name.rstrip("s")):
		return DOCTYPE_ALIASES.get(name) or DOCTYPE_ALIASES[name.rstrip("s")]
	value = name.rstrip("s")
	for dt in CREATABLE:
		if value in (dt.lower(), dt.lower().rstrip("s")):
			return dt
	return None


def start(ctx, doctype, values=None, items=None, return_to=None, rows=None, intro=None):
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
		if not field or not clean_text(raw, 140):
			continue
		if field["type"] == "link":
			# Pre-filled names are only accepted when they match one record; otherwise the field's
			# question offers the matches (or a separate "Create new") instead of guessing.
			status, found, display = _resolve_link(field, form, clean_text(raw, 140), from_text=True)
			if status == "ok":
				form["values"][field["fieldname"]], form["labels"][field["fieldname"]] = found, display
			else:
				form.setdefault("prefill", {})[field["fieldname"]] = {"search": clean_text(raw, 140), "candidates": found if status == "ambiguous" else None}
			continue
		error = _set(ctx, form, field, raw, from_text=True)
		if error and error != "__asked__":
			notes.append(error)
	for it in (items or [])[:20]:
		if isinstance(it, dict) and clean_text(it.get("item"), 140):
			form["queued_items"].append({"text": clean_text(it.get("item"), 140), "qty": parse_number(it.get("qty")), "rate": parse_number(it.get("rate"))})
	for row in rows or []:  # rows carried over from another document type, re-checked for this one
		if _item_ok(form, row["item_code"]):
			form["items"].append({k: row.get(k) for k in ("item_code", "item_name", "qty", "rate", "uom")})
		else:
			notes.append(f"Item {row['item_code']} can't be used on a {doctype}, so I left it out.")
	form["items_done"] = bool(form["items"]) and not form["queued_items"]
	if intro is None:
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


def switch(ctx, text):
	"""“I meant Purchase Receipt” / “Purchase Receipt, not Purchase Order”: change the document type
	of the open form and keep every value that still applies. Returns True if handled."""
	m = SWITCH_RE.match(text) or SWITCH_NOT_RE.match(text)
	if not m:
		return False
	new = match_doctype(m.group("dt"))
	forms = ctx.st["forms"]
	form = forms[-1]
	ctx.st["pending"] = None
	if len(forms) > 1:
		ctx.say(f"Let's finish or cancel the new {form['doctype'].lower()} first; then you can switch the {forms[0]['doctype']}.")
		return next_step(ctx) or True
	if new == form["doctype"]:
		ctx.say(f"This already is a {new}.")
		return next_step(ctx) or True
	if not frappe.has_permission(new, "create"):
		ctx.say(f"You don't have permission to create {new} records, so I kept the {form['doctype']}.")
		return next_step(ctx) or True
	old_spec, new_spec = SPECS[form["doctype"]], SPECS[new]
	values, kept = {}, []
	old_party_dt = _field(form["doctype"], old_spec["party"])["doctype"] if old_spec["party"] else None
	new_party_dt = _field(new, new_spec["party"])["doctype"] if new_spec["party"] else None
	if old_party_dt and old_party_dt == new_party_dt and form["values"].get(old_spec["party"]):
		values[new_spec["party"]] = form["values"][old_spec["party"]]
		kept.append(old_party_dt.lower())
	for key in ("company", "set_warehouse", "due_date", "schedule_date", "delivery_date"):
		if form["values"].get(key) and _field(new, key):
			values[key] = form["values"][key]
			kept.append(_field(new, key)["label"].lower())
	rows = form["items"] if new_spec["items"] and old_spec["items"] else []
	ctx.st["forms"] = []
	start(ctx, new, values=values, rows=rows, intro=f"Switched to a {'new ' if new in MASTERS else ''}{new}" + ("" if new in MASTERS else " (it will be saved as a Draft)")
		+ (f", keeping the {', '.join(kept)}" + (f" and {len(rows)} item row{'s' if len(rows) != 1 else ''}" if rows else "") if kept or rows else "")
		+ ". Nothing was created.")
	return True


def restart(ctx):
	"""“Start over”: the outermost form begins again with no values; nothing was created."""
	doctype = ctx.st["forms"][0]["doctype"]
	ctx.st["forms"] = []
	ctx.st["pending"] = None
	return start(ctx, doctype, intro=f"Starting the {doctype} again from the beginning. Nothing was created.")


def next_step(ctx):
	form = ctx.st["forms"][-1]
	spec = SPECS[form["doctype"]]
	for f in _fields(form):
		wanted = f["ask"] or f["fieldname"] in form.get("force_ask", [])
		if wanted and f["fieldname"] not in form["values"] and f["fieldname"] not in form.get("skipped", []):
			prefill = (form.get("prefill") or {}).pop(f["fieldname"], None)
			if prefill and prefill.get("candidates"):
				return _ask_field(ctx, form, f, search=prefill["search"], candidates=prefill["candidates"],
					text=f"Several {f['label'].lower()} records match “{prefill['search']}”. Which one?")
			if prefill:
				return _ask_not_found(ctx, form, f, prefill["search"])
			return _ask_field(ctx, form, f)
	if spec["items"] and not form["items_done"]:
		return _items_step(ctx, form)
	if _requirements(ctx, form):
		return
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
		field = _field(form["doctype"], key, form)
		if value == "__skip__" and not field["required"]:
			form.setdefault("skipped", []).append(key)
			return next_step(ctx)
		if value == "__create__" and field.get("master") and _can_create(field["master"]):
			return _start_master(ctx, form, field["master"], pending.get("search") or "", {"field": key})
		if value == "__more__" and field["type"] == "link":
			return _ask_field(ctx, form, field, offset=int(pending.get("offset") or 0) + PAGE)
		if from_text and field["type"] == "link":
			value = answer_term(value) or value
		error = _set(ctx, form, field, value, from_text=from_text)
		if error == "__asked__":
			return
		if error:
			ctx.say(error)
		return next_step(ctx)
	if kind == "form_item":
		return _answer_item(ctx, form, key, value, from_text, pending)
	if kind == "form_warehouse":
		return _answer_warehouse(ctx, form, pending, value, from_text)
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
		if value in ("__create__", "__retry__") or re.match(r"^\s*(yes|y|ok|okay|confirm|create|go ahead|do it|sure|retry)\b", value or "", re.I):
			# Re-check on the server with the values as they are now (the user may have corrected some,
			# or a record may have changed since the preview), then create.
			if _requirements(ctx, form):
				return
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
	m = re.match(r"^\s*(?:please\s+)?(?:change|set|update|make|use)?\s*(?:row|line|item)\s*#?(\d+)\s+(qty|quantity|rate|price|item|warehouse)\s*(?:to|as|=|:|is)?\s+(.+?)\s*$", text, re.I)
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
		elif what == "warehouse":
			if not requirements.needs_warehouse(form["doctype"], row["item_code"]):
				ctx.say(f"Row {idx + 1} ({row.get('item_name') or row['item_code']}) doesn't need a warehouse on a {form['doctype']}.")
				return True
			status, found, _display = _resolve_link(SET_WAREHOUSE, form, value, from_text=True)
			if status != "ok":
				ctx.say(f"I couldn't find a single warehouse of {form['values'].get('company')} matching “{value}”.")
				return True
			row["warehouse"] = found
		else:
			res = _resolve_item(form, value)
			if res[0] != "ok":
				ctx.say(f"I couldn't find a single item matching “{value}”.")
				return True
			row.update(item_code=res[1], item_name=res[2], warehouse=None, default_warehouse=None)
		ctx.say(f"Updated row {idx + 1}.")
		ctx.st["pending"] = None
		return next_step(ctx) or True
	m = WAREHOUSE_RE.match(text) or USE_WAREHOUSE_RE.match(text)
	if m and not (ctx.st.get("pending") or {}).get("kind") == "form_warehouse":
		return _correct_warehouse(ctx, form, m.group("v"))
	m = QTY_RATE_FIELD_RE.match(text)
	if m and SPECS[form["doctype"]]["items"]:
		return _correct_qty_rate(ctx, form, m.group("what").lower(), m.group("v"))
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
		if len(m.group(1).split()) <= 4 and re.match(r"^\s*(?:please\s+)?(?:change|set|update)\b", text, re.I):
			# A field this form doesn't have: say what can be changed instead of treating the message as an answer.
			ctx.say(f"I can't change “{clean_text(m.group(1), 40)}” on a {form['doctype']} here. " + _changeable(form))
			ctx.st["pending"] = None
			return next_step(ctx) or True
	return False


def _changeable(form):
	labels = [f["label"] for f in _fields(form) if f["type"] != "select" or f["ask"]]
	text = "You can change: " + ", ".join(labels)
	if SPECS[form["doctype"]]["items"]:
		text += "; and item rows, e.g. “row 1 qty 5”, “row 1 rate 20”, “row 1 item …”" + (", “row 1 warehouse …”" if
			form["doctype"] in requirements.WAREHOUSE_RULES else "") + ", “remove row 1” or “add item”"
	return text + "."


def _correct_warehouse(ctx, form, value):
	"""“use Finished Goods warehouse”, “set target warehouse to Stores”: one warehouse for all stock rows."""
	ctx.st["pending"] = None
	field = _field(form["doctype"], "set_warehouse", form)
	if not field:
		reason = ("stock isn't updated by it (the Delivery Note or Purchase Receipt moves the stock)" if form["doctype"] in
			("Sales Invoice", "Purchase Invoice") else "it doesn't move stock")
		ctx.say(f"A {form['doctype']} created here has no warehouse: {reason}. Nothing was changed.")
		return next_step(ctx) or True
	value = re.sub(r"\s+warehouse$", "", answer_term(value) or "", flags=re.I) or value
	status, found, _display = _resolve_link(field, form, value, from_text=True)
	if status == "ambiguous":
		_ask_field(ctx, form, field, search=value, candidates=found, text=f"Several warehouses match “{value}”. Which one should all stock rows use?")
		return True
	if status != "ok":
		company = form["values"].get("company")
		ctx.say(f"I couldn't find a warehouse of {company or 'this company'} matching “{value}” that you can use. Nothing was changed.")
		return next_step(ctx) or True
	_set(ctx, form, field, found, from_text=False)
	ctx.say(f"Warehouse set to {found} for all stock rows of this {form['doctype']}.")
	return next_step(ctx) or True


def _correct_qty_rate(ctx, form, what, value):
	"""“change quantity to 5” without a row number: the item being added, or the only row."""
	number = parse_number(value)
	is_qty = what in ("qty", "quantity")
	if number is None or (is_qty and number <= 0) or number < 0:
		ctx.say("The quantity must be a number greater than 0." if is_qty else "The rate must be a number of 0 or more.")
		return True
	key = "qty" if is_qty else "rate"
	pending = ctx.st.get("pending") or {}
	if form.get("draft_item") and pending.get("kind") == "form_item":
		form["draft_item"][key] = number
		ctx.st["pending"] = None
		return next_step(ctx) or True
	if len(form["items"]) == 1:
		form["items"][0][key] = number
		ctx.say(f"Updated the {'quantity' if is_qty else 'rate'} of row 1 to {flt(number)}.")
		ctx.st["pending"] = None
		return next_step(ctx) or True
	if not form["items"]:
		ctx.say("There are no item rows yet.")
		return True
	ctx.say(f"This {form['doctype']} has {len(form['items'])} rows. Which one? For example “row 2 {'qty' if is_qty else 'rate'} {flt(number)}”.")
	return True


# ------------------------------------------------------------------------------------------------
# Header fields
# ------------------------------------------------------------------------------------------------
def _fields(form):
	"""The spec's header fields that apply now (see F(when=...)) plus fields this site made mandatory (added when found missing)."""
	values = form.get("values") or {}
	header = [f for f in SPECS[form["doctype"]]["header"]
		if not f.get("when") or all(values.get(k) in allowed for k, allowed in f["when"].items())]
	return header + form.get("extra", [])


def _field(doctype, fieldname, form=None):
	fields = _fields(form) if form else SPECS[doctype]["header"]
	return next((f for f in fields if f["fieldname"] == fieldname), None)


def _field_by_label(form, label):
	label = normalize_name(label)
	fieldname = LABEL_ALIASES.get(label)
	spec = SPECS[form["doctype"]]
	if label in ("party", "customer", "supplier") and spec["party"]:
		fieldname = spec["party"]
	for f in _fields(form):
		if f["fieldname"] == fieldname or normalize_name(f["label"]) == label or f["fieldname"].replace("_", " ") == label:
			return f
	return None


def _default(f, form):
	d = f.get("default")
	if d == "today":
		return nowdate()
	if d == "company":
		from ai_erpnext.chat.queries import usable_companies

		permitted = usable_companies(form["doctype"])[:50]
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


def _suggestions(doctype, filters, limit=PAGE, offset=0):
	if doctype == "Company":
		from ai_erpnext.chat.queries import usable_companies

		return usable_companies()[offset: offset + limit]
	try:
		return frappe.get_list(doctype, filters=filters, pluck="name", offset=offset, limit=limit, order_by="modified desc")
	except frappe.PermissionError:
		frappe.local.message_log = []
		return []


def _can_create(master):
	"""A "Create new ..." choice is offered only for masters the chat can create and the user may create."""
	return master in MASTERS and frappe.has_permission(master, "create")


def _ask_field(ctx, form, f, search=None, candidates=None, text=None, offset=0):
	options, input_type, more = [], "text", False
	if f["type"] == "link":
		names = candidates if candidates is not None else _suggestions(f["doctype"], _filters(f, form), limit=PAGE + 1, offset=offset)
		more = candidates is None and len(names) > PAGE
		names = names[:PAGE] if candidates is None else names
		options = [{"label": n, "value": n} for n in names]
		if more:
			options.append({"label": f"Show more {f['label'].lower()}s", "value": "__more__", "style": "secondary"})
		if f.get("master") and _can_create(f["master"]):
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
	if f["fieldname"] == "item_code" and form["values"].get("item_name") and not readable_exists("Item", form["values"]["item_name"]):
		options.insert(0, {"label": f"Use the item name: {form['values']['item_name']}", "value": form["values"]["item_name"]})
	if not f["required"] and f.get("skip_label"):
		options.append({"label": f["skip_label"], "value": "__skip__", "style": "secondary"})
	prompt = text or f.get("prompt") or f"Which {f['label'].lower()} should I use?"
	if f["type"] == "link" and input_type == "text" and not candidates:
		creatable = f.get("master") and _can_create(f["master"])
		if options:
			prompt += " Choose one below" + (f" or create a new {f['master'].lower()}" if creatable else "") + ". You can also type a name to search."
		else:
			prompt += (f" There are none yet: create a new {f['master'].lower()}" if creatable else " Type a name to search") + "."
	ctx.ask("form_field", prompt, key=f["fieldname"], options=options, input=input_type)
	ctx.st["pending"]["offset"] = offset
	if search:
		ctx.st["pending"]["search"] = search


def _ask_not_found(ctx, form, f, value):
	"""No record matches what the user typed: offer existing records and, separately, "Create new"."""
	if f.get("master") and _can_create(f["master"]):
		text = f"I couldn't find a {f['master'].lower()} called “{value}”. Pick an existing one, type another name, or create “{value}” as a new {f['master'].lower()}."
	else:
		text = f"I couldn't find a {f['label'].lower()} called “{value}”. Pick one or type another name."
	_ask_field(ctx, form, f, search=value, text=text)


def _set(ctx, form, f, raw, from_text):
	"""Validate one header value. Returns an error sentence, "__asked__" if a follow-up question was asked, or None."""
	value = clean_text(raw, 140)
	if value is None:
		return f"Please give the {f['label'].lower()}."
	name = f["fieldname"]
	if f["type"] == "data":
		if name == "item_code" and readable_exists("Item", value):
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
	if f["type"] == "number":
		number = parse_number(value)
		if number is None:
			return f"Please give the {f['label'].lower()} as a number."
		form["values"][name] = form["labels"][name] = number
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
		if name in ("company", "set_warehouse"):
			if name == "company":
				form["values"].pop("set_warehouse", None)  # warehouses belong to a company
				form["labels"].pop("set_warehouse", None)
			for row in form.get("items") or []:  # a header warehouse applies to every stock row
				row.update(warehouse=None, default_warehouse=None)
		return None
	if status == "ambiguous":
		_ask_field(ctx, form, f, search=value, candidates=found, text=f"Several {f['label'].lower()} records match “{value}”. Which one?")
		return "__asked__"
	if f.get("master"):
		_ask_not_found(ctx, form, f, value)
		return "__asked__"
	return f"I couldn't find a {f['label'].lower()} called “{value}”."


def _with(filters, fieldname, value):
	"""The field's own restrictions AND fieldname = value, as a filter list.

	Merging into the dict ({**filters, "name": value}) would replace a restriction on the same field (an allow-list such as
	{"name": ["in", [...]]}) with the user's value; a list keeps both conditions."""
	conditions = [[k, v[0], v[1]] if isinstance(v, (list, tuple)) and len(v) == 2 else [k, "=", v] for k, v in (filters or {}).items()]
	return [*conditions, [fieldname, "=", value]]


class _GenericMatcher:
	"""Name/title matcher for Link fields without a dedicated matcher.
	Only an exact name or exact (case-insensitive) title is a match; partial matches are suggestions."""

	def __init__(self, doctype, title=None, filters=None):
		self.doctype, self.filters = doctype, filters or {}
		meta = frappe.get_meta(doctype)
		# DocTypes without a title field usually keep the human name in "<doctype>_name"
		# (Warehouse: warehouse_name "Stores" for the record "Stores - ABC").
		own_name = frappe.scrub(doctype) + "_name"
		self.title = title or (meta.title_field if meta.title_field and meta.get_field(meta.title_field) else
			(own_name if meta.has_field(own_name) else None))

	def match(self, value):
		fields = ["name"] + ([self.title] if self.title else [])
		exact = frappe.get_list(self.doctype, filters=_with(self.filters, "name", value), fields=fields, limit=2)
		if not exact and self.title:
			exact = frappe.get_list(self.doctype, filters=_with(self.filters, self.title, value), fields=fields, limit=3)
		if len(exact) == 1:
			return _Result(exact, self.title, matched=True)
		rows = exact or frappe.get_list(self.doctype, filters=self.filters, or_filters=[[f, "like", f"%{value}%"] for f in fields],
			fields=fields, limit=6)
		return _Result(rows, self.title, matched=False)


class _Result:
	def __init__(self, rows, title, matched):
		self.matched = matched
		self.name = rows[0]["name"] if matched else None
		self.display = (rows[0].get(title) or rows[0]["name"]) if matched and title else self.name
		self.candidates = [] if matched else [{"name": r["name"], "display_name": r.get(title) or r["name"]} for r in rows]


def _exists_named(doctype, title_field, value):
	return (readable_names(doctype, {title_field: value}, limit=1) or [None])[0]


def _resolve_link(f, form, value, from_text):
	doctype, filters = f["doctype"], _filters(f, form)
	if doctype == "Company":
		return _resolve_company(form, value)
	if not from_text:  # button values are exact record names; re-check existence and permission
		rows = frappe.get_list(doctype, filters=_with(filters, "name", value), pluck="name", limit=1)
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
		return bool(frappe.get_list("Item", filters={"name": code, "disabled": 0, **flags}, pluck="name", limit=1))

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
	rates = spec_rates(form)
	if d.get("qty") is None or d["qty"] <= 0:
		hint = " You can give the rate too, e.g. “5 at 20”." if rates else ""
		return ctx.ask("form_item", f"How many of {d['item_name'] or d['item_code']}?{hint}", key="qty", input="text")
	if rates and (d.get("rate") is None or d["rate"] < 0):
		options = []
		price = _price(form, d["item_code"])
		if price:
			options.append({"label": f"Use price list rate {flt(price[0], 2)} ({price[1]})", "value": str(flt(price[0], 2))})
		return ctx.ask("form_item", f"What is the rate (price per unit) for {d['item_name'] or d['item_code']}?", key="rate", options=options, input="text")
	uom = frappe.db.get_value("Item", d["item_code"], "stock_uom")
	form["items"].append({"item_code": d["item_code"], "item_name": d.get("item_name"), "qty": d["qty"], "rate": d.get("rate") if rates else None, "uom": uom})
	form["draft_item"] = None
	ctx.say(f"Added {flt(d['qty'])} × {d.get('item_name') or d['item_code']}" + (f" at {flt(d['rate'], 2)}." if rates else "."))
	if form["queued_items"]:
		return _items_step(ctx, form)
	return _ask_more(ctx, form)


def spec_rates(form):
	"""Whether item rows of this document carry a price the user gives (not for material requests and stock entries)."""
	return SPECS[form["doctype"]].get("rates", True)


def _ask_more(ctx, form):
	return ctx.ask("form_more_items", "Do you want to add another item?",
		options=[{"label": "Add another item", "value": "__more__"}, {"label": "No, that's all", "value": "__done__", "style": "secondary"}])


def _ask_item(ctx, form, search=None, candidates=None, text=None, offset=0):
	flags = ITEM_FILTERS[SPECS[form["doctype"]]["items"]]
	names = candidates if candidates is not None else _suggestions("Item", {"disabled": 0, "has_variants": 0, **flags}, limit=PAGE + 1, offset=offset)
	more = candidates is None and len(names) > PAGE
	names = names[:PAGE] if candidates is None else names
	titles = dict(frappe.get_all("Item", filters={"name": ["in", names or [""]]}, fields=["name", "item_name"], as_list=True))
	options = [{"label": f"{titles.get(n)} ({n})" if titles.get(n) and titles.get(n) != n else n, "value": n} for n in names]
	if more:
		options.append({"label": "Show more items", "value": "__more__", "style": "secondary"})
	creatable = _can_create("Item")
	if creatable:
		options.append({"label": "Create new item" + (f" “{search}”" if search else ""), "value": "__create__", "style": "secondary"})
	prompt = text or ("Which item? Pick one or type the item name or code." if not search else
		f"I couldn't find the item “{search}”. Pick one, search again" + (", or create it." if creatable else "."))
	ctx.ask("form_item", prompt, key="item_code", options=options, input="text")
	ctx.st["pending"].update(search=search, offset=offset)


def _answer_item(ctx, form, key, value, from_text, pending):
	d = form["draft_item"] if form["draft_item"] is not None else {}
	form["draft_item"] = d
	if key == "item_code":
		if value == "__more__":
			return _ask_item(ctx, form, offset=int(pending.get("offset") or 0) + PAGE)
		if value == "__create__" and _can_create("Item"):
			return _start_master(ctx, form, "Item", pending.get("search") or d.get("text") or "", {"field": "item_code", "row": "draft"})
		if from_text:
			value = answer_term(value) or value
		if not from_text:
			rows = frappe.get_list("Item", filters={"name": value, "disabled": 0, **ITEM_FILTERS[SPECS[form["doctype"]]["items"]]}, pluck="name", limit=1)
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
		both = QTY_AND_RATE_RE.match(value or "") if from_text else None
		qty = parse_number(both.group("q")) if both else parse_number(value)
		if qty is None or qty <= 0:
			ctx.say("The quantity must be a number greater than 0.")
		else:
			d["qty"] = qty
			if both:
				d["rate"] = parse_number(both.group("r"))
	elif key == "rate":
		rate = parse_number(value)
		if rate is None or rate < 0:
			ctx.say("The rate must be a number of 0 or more.")
		else:
			d["rate"] = rate
	return next_step(ctx)


def _price(form, item_code):
	kind = SPECS[form["doctype"]]["items"]
	try:  # permission-aware: a user who can't read Item Price gets no suggested rate
		row = frappe.get_list("Item Price", filters={"item_code": item_code, kind: 1}, fields=["price_list_rate", "price_list"], order_by="modified desc", limit=1)
	except frappe.PermissionError:
		frappe.local.message_log = []
		return None
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


# ------------------------------------------------------------------------------------------------
# What ERPNext needs beyond the collected fields (checked before every preview and retry)
# ------------------------------------------------------------------------------------------------
def _requirements(ctx, form):
	"""Re-check the collected values and ask for anything ERPNext will still need.

	Returns True if a question was asked (the caller must not show the preview yet)."""
	doctype, spec = form["doctype"], SPECS[form["doctype"]]
	company = form["values"].get("company")
	# 1. Everything collected is checked again: a record may have been disabled, the company changed...
	for f in _fields(form):
		value = form["values"].get(f["fieldname"])
		if f["type"] == "link" and value not in (None, "") and not _link_ok(f, form, value):
			form["values"].pop(f["fieldname"], None)
			form["labels"].pop(f["fieldname"], None)
			form.setdefault("force_ask", []).append(f["fieldname"])
			where = f" for {company}" if company and "company" in (f.get("filters") or {}) else ""
			ctx.say(f"{f['label']} “{value}” can't be used{where}. Please choose another.")
			next_step(ctx)
			return True
	for idx, row in enumerate(form.get("items") or [], start=1):
		if not _item_ok(form, row["item_code"]):
			form["items"].pop(idx - 1)
			form["items_done"] = False
			ctx.say(f"Row {idx}: item {row['item_code']} can't be used here any more, so I removed it.")
			next_step(ctx)
			return True
		if row.get("warehouse") and not _link_ok(SET_WAREHOUSE, form, row["warehouse"]):
			ctx.say(f"Row {idx}: “{row['warehouse']}” isn't a warehouse of {company} that you can use.")
			row["warehouse"] = None

	# 2. ERPNext's own defaulting on an unsaved copy: which rows get a valid default warehouse
	report = requirements.inspect(_doc_dict(form, check=True))
	need = []
	for idx, row in enumerate(form.get("items") or [], start=1):
		if not _needs_warehouse(form, row):
			row["default_warehouse"] = None
			continue
		default = report["defaults"].get(idx)
		usable = default and not row.get("no_default") and _link_ok(SET_WAREHOUSE, form, default)
		row["default_warehouse"] = default if usable else None
		if not _row_warehouse(form, row):
			need.append(idx)
	if need:
		_ask_warehouse(ctx, form, need)
		return True

	# 3. Mandatory fields still empty after ERPNext's defaulting that the chat can collect:
	#    fields of this form, and fields the site made mandatory with a Custom Field.
	meta = frappe.get_meta(doctype)
	for fieldname in report["missing"]:
		if form["values"].get(fieldname) not in (None, ""):
			continue
		f = _field(doctype, fieldname, form)
		if not f and (meta.get_field(fieldname) or {}).get("is_custom_field"):
			f = requirements.collectible_field(doctype, fieldname)
			if f:
				form.setdefault("extra", []).append(f)
		if f:
			if fieldname in form.get("skipped", []):
				form["skipped"].remove(fieldname)
			form.setdefault("force_ask", []).append(fieldname)
			next_step(ctx)
			return True
	return False


def _needs_warehouse(form, row):
	return bool(row.get("needs_warehouse")) or requirements.needs_warehouse(form["doctype"], row["item_code"])


def _row_warehouse(form, row, include_default=True):
	"""The warehouse a row will be created with: chosen for the row, chosen for all rows, or ERPNext's default."""
	if not _needs_warehouse(form, row):
		return None
	return row.get("warehouse") or form["values"].get("set_warehouse") or (row.get("default_warehouse") if include_default else None)


def _resolve_company(form, value):
	"""A company the user may use, by name, company name or abbreviation (see queries.usable_companies for why this is not get_list)."""
	from ai_erpnext.chat.queries import usable_companies

	allowed = usable_companies(form["doctype"])
	rows = frappe.get_all("Company", filters={"name": ["in", allowed or [""]]}, fields=["name", "company_name", "abbr"], limit=0)
	text = (value or "").strip().lower()
	exact = [r.name for r in rows if text in (r.name.lower(), (r.company_name or "").lower(), (r.abbr or "").lower())]
	if len(exact) == 1:
		return "ok", exact[0], exact[0]
	partial = [r.name for r in rows if text and text in r.name.lower()]
	if len(partial) == 1:
		return "ok", partial[0], partial[0]
	return ("ambiguous", partial[:PAGE], None) if partial else ("none", None, None)


def _link_ok(f, form, value):
	if f["doctype"] == "Company":
		from ai_erpnext.chat.queries import usable_companies

		return value in usable_companies(form["doctype"])
	try:
		return bool(frappe.get_list(f["doctype"], filters=_with(_filters(f, form), "name", value), pluck="name", limit=1))
	except frappe.PermissionError:
		frappe.local.message_log = []
		return False


def _item_ok(form, code):
	flags = ITEM_FILTERS[SPECS[form["doctype"]]["items"]]
	return bool(frappe.get_list("Item", filters={"name": code, "disabled": 0, **flags}, pluck="name", limit=1))


def _ask_warehouse(ctx, form, rows, candidates=None, text=None):
	items = [form["items"][i - 1] for i in rows]
	names = ", ".join(dict.fromkeys(it.get("item_name") or it["item_code"] for it in items))
	which = f"row {rows[0]} ({names})" if len(rows) == 1 else f"rows {', '.join(map(str, rows))} ({names})"
	company = form["values"].get("company")
	names = candidates if candidates is not None else _suggestions("Warehouse", _filters(SET_WAREHOUSE, form), limit=8)
	if not names:
		ctx.say(f"ERPNext requires a warehouse for {which}, but there's no warehouse of {company} that you can use. "
			"Create one in ERPNext (Stock > Warehouse) or change the company, then say “retry”.")
		return ctx.ask("form_confirm", "Nothing was created yet.",
			options=[{"label": "Retry", "value": "__retry__"}, {"label": "Cancel", "value": "__cancel__", "style": "secondary"}])
	prompt = text or (requirements.WAREHOUSE_RULES[form["doctype"]].format(items=which)
		+ f" ERPNext requires a warehouse for stock items, and no default warehouse of {company} applies.")
	ctx.ask("form_warehouse", prompt + " Pick one or type a name to search.", key="warehouse",
		options=[{"label": n, "value": n} for n in names], input="text")
	ctx.st["pending"]["rows"] = rows


def _answer_warehouse(ctx, form, pending, value, from_text):
	rows = [r for r in pending.get("rows") or [] if 1 <= r <= len(form["items"])]
	if from_text:  # “use Stores warehouse” -> “Stores”
		value = re.sub(r"\s+warehouse$", "", answer_term(value) or "", flags=re.I) or value
	status, found, _display = _resolve_link(SET_WAREHOUSE, form, value, from_text)
	if status == "ambiguous":
		return _ask_warehouse(ctx, form, rows, candidates=found, text=f"Several warehouses match “{value}”. Which one?")
	if status != "ok":
		ctx.say(f"I couldn't find a warehouse of {form['values'].get('company')} called “{value}”.")
		return _ask_warehouse(ctx, form, rows)
	for r in rows:
		form["items"][r - 1]["warehouse"] = found
	return next_step(ctx)


# ------------------------------------------------------------------------------------------------
# Confirmation and creation
# ------------------------------------------------------------------------------------------------
def _ask_confirm(ctx, form):
	spec, doctype = SPECS[form["doctype"]], form["doctype"]
	rows = [{"label": f["label"], "value": form["labels"].get(f["fieldname"], form["values"].get(f["fieldname"]))}
		for f in _fields(form) if form["values"].get(f["fieldname"]) not in (None, "")]
	card = {"type": "form_preview", "title": f"New {doctype}" + ("" if doctype in MASTERS else " (Draft)"), "rows": rows}
	if spec["items"]:
		if not form["items"]:
			form["items_done"] = False
			ctx.say("A document needs at least one item.")
			return next_step(ctx)
		card["items"] = []
		for i in form["items"]:
			item = {"name": i.get("item_name") or i["item_code"], "code": i["item_code"], "qty": i["qty"], "uom": i.get("uom")}
			if spec_rates(form):
				item.update(rate=flt(i["rate"], 2), amount=flt(i["qty"] * i["rate"], 2))
			warehouse = _row_warehouse(form, i)
			if warehouse:
				item["warehouse"] = warehouse
				item["warehouse_note"] = "ERPNext default" if warehouse == i.get("default_warehouse") and not (i.get("warehouse") or form["values"].get("set_warehouse")) else None
			card["items"].append(item)
		card["note"] = (f"Items total {flt(sum(i['qty'] * i['rate'] for i in form['items']), 2)} before taxes. ERPNext fills accounts, currency, "
			"price list and taxes from your settings and calculates the final totals.") if spec_rates(form) else (
			"No prices are entered here: ERPNext values the stock from your valuation settings.")
		if any(it.get("warehouse_note") for it in card["items"]):
			card["note"] += " Warehouses marked “ERPNext default” come from your item or stock settings; say “row 1 warehouse …” to change one."
	label = f"Create {doctype}" if doctype in MASTERS else f"Create draft {doctype}"
	text = f"Here's the {doctype.lower()}. Shall I create it" + ("? It is saved immediately: this record has no Draft state." if doctype in MASTERS else " as a Draft? It will not be submitted.") \
		+ " You can still change anything, e.g. “change " + (spec["party"] or spec["header"][0]["fieldname"]).replace("_", " ") + " to …”" \
		+ (" or “row 1 qty 5”." if spec["items"] else ".")
	ctx.ask("form_confirm", text, options=[{"label": label, "value": "__create__"}, {"label": "Cancel", "value": "__cancel__", "style": "secondary"}], card=card)


def _doc_dict(form, check=False):
	"""The document to insert. check=True: the copy for requirements.inspect, where ERPNext's
	own defaulting must fill row warehouses, so defaults found earlier are not passed in."""
	spec = SPECS[form["doctype"]]
	# A value of a field that no longer applies (a source warehouse after a transfer became a receipt) is never sent.
	applicable, conditional = {f["fieldname"] for f in _fields(form)}, {f["fieldname"] for f in spec["header"] if f.get("when")}
	values = {k: v for k, v in form["values"].items() if k in applicable or k not in conditional}
	# Applied to stock rows below. Passing it as the header value would make ERPNext copy it
	# onto rows that don't need a warehouse (services, non-stock items).
	values.pop("set_warehouse", None)
	if form["doctype"] == "Item" and not values.get("item_name"):
		values["item_name"] = values.get("item_code")
	doc = {"doctype": form["doctype"], **values, **spec.get("fixed", {})}
	if form["doctype"] in ("Sales Invoice", "Purchase Invoice", "Stock Entry") and values.get("posting_date") and values["posting_date"] != nowdate():
		doc["set_posting_time"] = 1
	if spec["items"]:
		rows = []
		for i in form["items"]:
			row = {"item_code": i["item_code"], "qty": i["qty"]}
			if spec_rates(form):
				row["rate"] = i["rate"]
			if form["doctype"] in ("Purchase Order", "Material Request"):
				row["schedule_date"] = values.get("schedule_date")
			if form["doctype"] == "Stock Entry":  # the header warehouses, per row (ERPNext checks them against the type)
				if values.get("from_warehouse"):
					row["s_warehouse"] = values["from_warehouse"]
				if values.get("to_warehouse"):
					row["t_warehouse"] = values["to_warehouse"]
			if form["doctype"] == "Sales Order":
				row["delivery_date"] = values.get("delivery_date")
			warehouse = _row_warehouse(form, i, include_default=not check)
			if warehouse:
				row["warehouse"] = warehouse
			rows.append(row)
		doc["items"] = rows
	return doc


def _create(ctx, form):
	doctype = form["doctype"]
	frappe.db.savepoint("ai_chat_form")
	try:
		doc_dict = _doc_dict(form)
		# Only allowlisted fields, never docstatus/owner/workflow fields, whatever the form holds (fail closed).
		capability.assert_creation_safe(doctype, doc_dict, extra=[f["fieldname"] for f in form.get("extra", [])])
		doc = frappe.get_doc(doc_dict)
		doc.flags.ignore_mandatory = False
		doc.docstatus = 0
		doc.insert()  # standard permissions + ERPNext validation; never submit()
		if doc.meta.is_submittable and doc.docstatus != 0:
			raise frappe.ValidationError("Created documents must stay Draft")
	except Exception as e:
		# nothing of the failed attempt remains; the form (everything the user entered) is kept
		frappe.db.rollback(save_point="ai_chat_form")
		return _rejected(ctx, form, requirements.explain(e, doctype))

	ctx.st["forms"].pop()
	ctx.st["pending"] = None
	ctx.st["last_created"] = {"doctype": doctype, "name": doc.name}  # “this Purchase Order” in a follow-up question
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
				*([{"label": "Grand total", "value": f"{flt(doc.get('grand_total'), 2)} {doc.get('currency') or ''}".strip()}] if doc.meta.has_field("grand_total") else []),
				{"label": "Status", "value": status}]})


def _rejected(ctx, form, info):
	"""ERPNext refused the document: ask for what it needs when the chat can collect it."""
	doctype = form["doctype"]
	if info["kind"] == "mandatory":
		asked = []
		for fieldname in info["fields"]:
			f = _field(doctype, fieldname, form)
			if not f:
				f = requirements.collectible_field(doctype, fieldname)
				if f:
					form.setdefault("extra", []).append(f)
			if f:
				form["values"].pop(fieldname, None)
				form.setdefault("force_ask", []).append(fieldname)
				if fieldname in form.get("skipped", []):
					form["skipped"].remove(fieldname)
				asked.append(f["label"])
		if asked:
			ctx.say(f"ERPNext needs a few more details: {', '.join(asked)}.")
			return next_step(ctx)
		return _failed(ctx, form, f"{info['message']} These values can't be set from the chat; please complete the document in ERPNext.")
	if info["kind"] == "warehouse" and SPECS[doctype]["items"]:
		rows = [r for r in info["rows"] if 1 <= r <= len(form["items"])]
		rows = rows or [i for i, row in enumerate(form["items"], start=1) if row["item_code"] in info["message"]]
		if rows:
			for r in rows:
				row = form["items"][r - 1]
				row["needs_warehouse"] = True  # ERPNext said so, whatever our own rules thought
				if row.get("default_warehouse") and not row.get("warehouse"):
					row["no_default"] = True  # don't offer the default ERPNext just refused
				row["warehouse"] = None
			ctx.say(f"ERPNext didn't accept the {doctype}: {info['message']}")
			return _ask_warehouse(ctx, form, rows)
	return _failed(ctx, form, info["message"])


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
