"""Read-only ERPNext data queries for the assistant.

A query plan (produced by the router model from the user's words) is only a request: it is
validated here against an explicit allowlist of DocTypes, fields, operations and filters,
then executed with Frappe's permission-aware get_list/get_doc. Nothing here writes to the
database, and no model-generated SQL is ever executed. Result text and numbers are built
here from real query results, never by the model.

Company isolation: get_list already applies the user's User Permissions. On top of that,
every query on a DocType with a company field is explicitly limited to the companies the
user may read (or to the one company the user named, if it is one of those), so a plan can
never widen the scope and a restricted user gets "nothing you can access" instead of data.
"""

import datetime
from dataclasses import dataclass, field

import frappe
from frappe.model import get_permitted_fields
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, nowdate

from ai_erpnext.mapping.normalize import clean_text, parse_date

PAGE_SIZE = 10
MAX_PAGE = 20
MAX_OFFSET = 500
MAX_RANGE_DAYS = 3 * 366


class QueryError(Exception):
	"""User-facing reason a plan can't run (unsupported, not permitted, invalid)."""


class QueryClarify(Exception):
	"""An essential parameter is missing or ambiguous; ask the user."""

	def __init__(self, question, options=None):
		super().__init__(question)
		self.question = question
		self.options = options or []


@dataclass
class Spec:
	title: str  # field shown as the record's human title
	list_fields: tuple
	detail_fields: tuple
	date_field: str | None = None
	party_field: str | None = None
	party_title_field: str | None = None
	amount_field: str | None = None
	status_field: str | None = "status"
	search_fields: tuple = ()
	child: tuple = ()  # (table fieldname, fields)
	operations: tuple = ("list", "count", "details")
	labels: dict = field(default_factory=dict)


TXN_ITEMS = ("items", ("item_code", "item_name", "qty", "uom", "rate", "amount"))

REGISTRY = {
	"Sales Invoice": Spec("customer_name", ("name", "customer_name", "posting_date", "grand_total", "currency", "status"),
		("name", "customer", "customer_name", "company", "posting_date", "due_date", "currency", "net_total", "total_taxes_and_charges",
			"grand_total", "outstanding_amount", "status"), "posting_date", "customer", "customer_name", "grand_total", search_fields=("name", "customer", "customer_name"),
		child=TXN_ITEMS, operations=("list", "count", "details", "sales_summary", "overdue", "unpaid", "outstanding_by_customer")),
	"Purchase Invoice": Spec("supplier_name", ("name", "supplier_name", "posting_date", "grand_total", "currency", "status"),
		("name", "supplier", "supplier_name", "company", "posting_date", "due_date", "bill_no", "currency", "net_total", "total_taxes_and_charges",
			"grand_total", "outstanding_amount", "status"), "posting_date", "supplier", "supplier_name", "grand_total", search_fields=("name", "supplier", "supplier_name", "bill_no"),
		child=TXN_ITEMS, operations=("list", "count", "details", "overdue", "unpaid")),
	"Sales Order": Spec("customer_name", ("name", "customer_name", "transaction_date", "grand_total", "currency", "status"),
		("name", "customer", "customer_name", "company", "transaction_date", "delivery_date", "currency", "grand_total", "per_delivered", "per_billed", "status"),
		"transaction_date", "customer", "customer_name", "grand_total", search_fields=("name", "customer", "customer_name"), child=TXN_ITEMS),
	"Purchase Order": Spec("supplier_name", ("name", "supplier_name", "transaction_date", "grand_total", "currency", "status"),
		("name", "supplier", "supplier_name", "company", "transaction_date", "schedule_date", "currency", "grand_total", "per_received", "per_billed", "status"),
		"transaction_date", "supplier", "supplier_name", "grand_total", search_fields=("name", "supplier", "supplier_name"), child=TXN_ITEMS),
	"Delivery Note": Spec("customer_name", ("name", "customer_name", "posting_date", "grand_total", "currency", "status"),
		("name", "customer", "customer_name", "company", "posting_date", "currency", "grand_total", "per_billed", "status"),
		"posting_date", "customer", "customer_name", "grand_total", search_fields=("name", "customer", "customer_name"), child=TXN_ITEMS),
	"Purchase Receipt": Spec("supplier_name", ("name", "supplier_name", "posting_date", "grand_total", "currency", "status"),
		("name", "supplier", "supplier_name", "company", "posting_date", "currency", "grand_total", "per_billed", "status"),
		"posting_date", "supplier", "supplier_name", "grand_total", search_fields=("name", "supplier", "supplier_name"), child=TXN_ITEMS),
	"Stock Entry": Spec("stock_entry_type", ("name", "stock_entry_type", "posting_date", "total_outgoing_value", "total_incoming_value", "docstatus"),
		("name", "stock_entry_type", "purpose", "company", "posting_date", "from_warehouse", "to_warehouse", "total_outgoing_value", "total_incoming_value", "docstatus"),
		"posting_date", amount_field="total_outgoing_value", status_field=None, search_fields=("name", "stock_entry_type"),
		child=("items", ("item_code", "item_name", "qty", "uom", "s_warehouse", "t_warehouse"))),
	"Payment Entry": Spec("party_name", ("name", "payment_type", "party_name", "posting_date", "paid_amount", "paid_from_account_currency", "status"),
		("name", "payment_type", "party_type", "party", "party_name", "company", "posting_date", "mode_of_payment", "paid_amount",
			"paid_from_account_currency", "received_amount", "paid_to_account_currency", "reference_no", "status"),
		"posting_date", "party", "party_name", "paid_amount", search_fields=("name", "party", "party_name", "reference_no")),
	"Customer": Spec("customer_name", ("name", "customer_name", "customer_group", "territory", "customer_type"),
		("name", "customer_name", "customer_type", "customer_group", "territory", "default_currency", "mobile_no", "email_id", "disabled"),
		status_field=None, search_fields=("name", "customer_name", "mobile_no", "email_id")),
	"Supplier": Spec("supplier_name", ("name", "supplier_name", "supplier_group", "country", "supplier_type"),
		("name", "supplier_name", "supplier_type", "supplier_group", "country", "default_currency", "mobile_no", "email_id", "disabled"),
		status_field=None, search_fields=("name", "supplier_name", "mobile_no", "email_id")),
	"Item": Spec("item_name", ("name", "item_name", "item_group", "stock_uom", "is_stock_item"),
		("name", "item_name", "item_group", "stock_uom", "is_stock_item", "is_sales_item", "is_purchase_item", "description", "disabled"),
		status_field=None, search_fields=("name", "item_name")),
	# HR: only non-sensitive organisational fields; salary, bank and personal data are never exposed.
	"Employee": Spec("employee_name", ("name", "employee_name", "department", "designation", "status"),
		("name", "employee_name", "department", "designation", "company", "branch", "status"), status_field="status",
		search_fields=("name", "employee_name")),
}

DATE_PRESETS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_quarter", "last_quarter", "this_year", "last_year", "last_7_days", "last_30_days")
DOCSTATUS = {"draft": 0, "submitted": 1, "cancelled": 2}
SORTS = ("newest", "oldest", "highest", "lowest")


# ------------------------------------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------------------------------------
def validate(plan):
	"""Normalise a router plan into a safe query dict, or raise QueryError/QueryClarify."""
	if not isinstance(plan, dict):
		raise QueryError("I couldn't understand that request.")
	doctype = _match_doctype(plan.get("doctype"))
	if not doctype:
		raise QueryError(f"I can't look up “{clean_text(plan.get('doctype'), 60) or 'that'}” yet. I can search: {', '.join(REGISTRY)}.")
	if not frappe.has_permission(doctype, "read"):
		raise QueryError(f"You don't have permission to view {doctype} records.")
	spec = REGISTRY[doctype]
	op = clean_text(plan.get("operation"), 40) or "list"
	if op not in spec.operations:
		raise QueryError(f"I can't do “{op.replace('_', ' ')}” for {doctype}.")

	filters = plan.get("filters") if isinstance(plan.get("filters"), dict) else {}
	q = {"doctype": doctype, "operation": op, "company": _match_company(doctype, filters.get("company")),
		"party": clean_text(filters.get("party"), 140) if spec.party_field else None,
		"search": clean_text(filters.get("search"), 140), "name": clean_text(filters.get("name"), 140),
		"status": _match_status(doctype, filters.get("status")), "docstatus": None, "date": None,
		"sort": plan.get("sort") if plan.get("sort") in SORTS else "newest",
		"limit": max(1, min(MAX_PAGE, int(flt(plan.get("limit")) or PAGE_SIZE))),
		"offset": max(0, min(MAX_OFFSET, int(flt(plan.get("offset"))))), "fields": None}
	ds = clean_text(filters.get("docstatus"), 20)
	if ds and ds.lower() in DOCSTATUS:
		q["docstatus"] = ds.lower()

	permitted = set(get_permitted_fields(doctype)) | {"name"}
	allowed = set(spec.detail_fields) | set(spec.list_fields)
	requested = plan.get("fields") if isinstance(plan.get("fields"), list) else []
	bad = [f for f in requested if isinstance(f, str) and f not in allowed]
	if bad:
		raise QueryError(f"I can't show {', '.join(clean_text(b, 40) for b in bad[:3])} for {doctype}.")
	if requested:
		q["fields"] = [f for f in requested if f in permitted]

	date = plan.get("date") if isinstance(plan.get("date"), dict) else None
	if date:
		q["date"] = _resolve_dates(date, spec)
	if op == "sales_summary" and not q["date"]:
		raise QueryClarify("For which period should I total the sales? For example this month, last month or a date range.",
			options=["This month", "Last month", "This year"])
	if op == "details" and not (q["name"] or q["search"] or q["party"]):
		raise QueryClarify(f"Which {doctype} should I show?")
	return q


def _match_doctype(value):
	value = (clean_text(value, 60) or "").lower().rstrip("s")
	for dt in REGISTRY:
		if value in (dt.lower(), dt.lower().rstrip("s")):
			return dt
	return None


def has_company(doctype):
	return bool(frappe.get_meta(doctype).has_field("company"))


def permitted_companies():
	"""Companies the user may read (User Permissions applied), or None if that can't be listed
	(the user has no read access on Company); get_list's own User Permission check still applies then."""
	try:
		return frappe.get_list("Company", pluck="name", limit=0, order_by="name asc")
	except frappe.PermissionError:
		frappe.local.message_log = []
		return None


def _match_company(doctype, value):
	value = clean_text(value, 140)
	if not value:
		return None
	if not has_company(doctype):
		return None  # masters such as Customer or Item are shared by all companies
	allowed = permitted_companies()
	if allowed is not None:
		rows = frappe.get_list("Company", filters={"name": ["in", allowed or [""]]}, fields=["name", "company_name", "abbr"], limit=0)
		match = [r.name for r in rows if value.lower() in (r.name.lower(), (r.company_name or "").lower(), (r.abbr or "").lower())]
		if len(match) == 1:
			return match[0]
	# Same answer whether the company doesn't exist or isn't permitted: nothing leaks.
	raise QueryError(f"I couldn't find a company “{value}” that you have access to.")


def _company_filter(q):
	"""[filters, note] limiting a query to the requested company or to all permitted companies."""
	if not has_company(q["doctype"]):
		return [], None
	if q.get("company"):
		return [["company", "=", q["company"]]], f"company {q['company']}"
	allowed = permitted_companies()
	if allowed is None:
		return [], None
	return [["company", "in", allowed or [""]]], None


def _match_status(doctype, value):
	value = clean_text(value, 40)
	spec = REGISTRY[doctype]
	if not value or not spec.status_field:
		return None
	options = [o for o in (frappe.get_meta(doctype).get_field(spec.status_field).options or "").split("\n") if o]
	return next((o for o in options if o.lower() == value.lower()), None)


def _resolve_dates(date, spec):
	field = "creation" if date.get("field") == "created" else spec.date_field
	if not field:
		raise QueryError("These records have no date to filter on.")
	preset = date.get("preset")
	if preset in DATE_PRESETS:
		start, end = _preset_range(preset)
	else:
		start, end = parse_date(date.get("from")), parse_date(date.get("to"))
		if not (start.ok and end.ok):
			raise QueryClarify("Which dates exactly? Please give them as YYYY-MM-DD, for example 2026-09-01 to 2026-09-30.")
		start, end = getdate(start.value), getdate(end.value)
	if start > end:
		start, end = end, start
	if (end - start).days > MAX_RANGE_DAYS:
		raise QueryError("That date range is too long. Please choose up to 3 years.")
	return {"field": field, "from": str(start), "to": str(end), "label": preset.replace("_", " ") if preset in DATE_PRESETS else None}


def _preset_range(preset):
	today = getdate(nowdate())
	if preset == "today":
		return today, today
	if preset == "yesterday":
		d = add_days(today, -1)
		return d, d
	if preset in ("this_week", "last_week"):
		start = today - datetime.timedelta(days=today.weekday())
		if preset == "last_week":
			start = start - datetime.timedelta(days=7)
		return start, start + datetime.timedelta(days=6)
	if preset == "this_month":
		return get_first_day(today), get_last_day(today)
	if preset == "last_month":
		d = add_months(today, -1)
		return get_first_day(d), get_last_day(d)
	if preset in ("this_quarter", "last_quarter"):
		q_start = datetime.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
		if preset == "last_quarter":
			q_start = getdate(add_months(q_start, -3))
		return q_start, getdate(add_days(add_months(q_start, 3), -1))
	if preset == "this_year":
		return datetime.date(today.year, 1, 1), datetime.date(today.year, 12, 31)
	if preset == "last_year":
		return datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31)
	if preset == "last_7_days":
		return getdate(add_days(today, -6)), today
	return getdate(add_days(today, -29)), today  # last_30_days


# ------------------------------------------------------------------------------------------------
# Execution (read-only, permission-aware)
# ------------------------------------------------------------------------------------------------
def execute(q):
	op = q["operation"]
	if op == "count":
		return _count(q)
	if op == "details":
		return _details(q)
	if op == "sales_summary":
		return _sales_summary(q)
	if op == "outstanding_by_customer":
		return _outstanding_by_customer(q)
	return _list(q)


def _base_filters(q):
	spec = REGISTRY[q["doctype"]]
	filters, or_filters, notes = [], [], []
	if q["operation"] in ("overdue", "unpaid"):
		filters += [["docstatus", "=", 1], ["outstanding_amount", ">", 0], ["is_return", "=", 0]]
		notes.append("submitted, with an outstanding amount")
		if q["operation"] == "overdue":
			filters.append(["due_date", "<", nowdate()])
			notes.append(f"due before {nowdate()}")
	elif q["docstatus"]:
		filters.append(["docstatus", "=", DOCSTATUS[q["docstatus"]]])
		notes.append(q["docstatus"])
	elif frappe.get_meta(q["doctype"]).is_submittable:
		filters.append(["docstatus", "<", 2])
		notes.append("draft and submitted (cancelled excluded)")
	company_filters, company_note = _company_filter(q)
	filters += company_filters
	if company_note:
		notes.append(company_note)
	if q["status"]:
		filters.append([spec.status_field, "=", q["status"]])
		notes.append(f"status {q['status']}")
	if q["party"]:
		or_filters += [[spec.party_field, "like", f"%{q['party']}%"]] + ([[spec.party_title_field, "like", f"%{q['party']}%"]] if spec.party_title_field else [])
		notes.append(f"{spec.party_field} matching “{q['party']}”")
	if q["search"]:
		or_filters += [[f, "like", f"%{q['search']}%"] for f in spec.search_fields]
		notes.append(f"matching “{q['search']}”")
	if q["date"]:
		d = q["date"]
		filters.append([d["field"], "between", [d["from"], d["to"]]])
		label = "created" if d["field"] == "creation" else frappe.get_meta(q["doctype"]).get_label(d["field"]).lower()
		notes.append(f"{label} {d['from']} to {d['to']}" + (f" ({d['label']})" if d["label"] else ""))
	return filters, or_filters, notes


def _order_by(q):
	spec = REGISTRY[q["doctype"]]
	if q["sort"] in ("highest", "lowest") and spec.amount_field:
		return f"{spec.amount_field} {'desc' if q['sort'] == 'highest' else 'asc'}"
	date = spec.date_field or "creation"
	direction = "asc" if q["sort"] == "oldest" else "desc"
	return f"{date} {direction}, creation {direction}"


def _columns(doctype, fields):
	meta = frappe.get_meta(doctype)
	cols = []
	for f in fields:
		df = meta.get_field(f)
		label = "ID" if f == "name" else (df.label if df else f.replace("_", " ").title())
		ftype = "link" if f == "name" else ("currency" if df and df.fieldtype in ("Currency", "Float") else ("date" if df and df.fieldtype in ("Date", "Datetime") else "text"))
		cols.append({"key": f, "label": label, "type": ftype})
	return cols


def _permitted(doctype, fields):
	permitted = set(get_permitted_fields(doctype)) | {"name"}
	return [f for f in fields if f in permitted]


def _list(q):
	doctype, spec = q["doctype"], REGISTRY[q["doctype"]]
	fields = _permitted(doctype, q["fields"] or list(spec.list_fields))
	if q["operation"] in ("overdue", "unpaid"):
		fields = _permitted(doctype, ["name", spec.title, "posting_date", "due_date", "grand_total", "outstanding_amount", "currency", "status"])
	if "name" not in fields:
		fields = ["name"] + fields
	filters, or_filters, notes = _base_filters(q)
	rows = frappe.get_list(doctype, filters=filters, or_filters=or_filters or None, fields=fields, order_by=_order_by(q),
		limit_start=q["offset"], limit_page_length=q["limit"] + 1)
	has_more = len(rows) > q["limit"]
	rows = rows[: q["limit"]]
	what = {"overdue": f"overdue {doctype}s", "unpaid": f"unpaid {doctype}s"}.get(q["operation"], f"{doctype} records")
	if not rows:
		return {"text": f"I found no {what}" + (" on this page." if q["offset"] else " matching that.") + (f" Filters: {'; '.join(notes)}." if notes else ""),
			"names": [], "titles": []}
	start = q["offset"] + 1
	summary = f"I found {len(rows)}{'+' if has_more else ''} {what}" if not q["offset"] else f"Showing {what} {start}–{q['offset'] + len(rows)}"
	card = {"type": "table", "title": doctype, "doctype": doctype, "columns": _columns(doctype, fields),
		"rows": [{**{k: _jsonable(r.get(k)) for k in fields}, "_name": r["name"], "_n": start + i} for i, r in enumerate(rows)],
		"note": "From your ERPNext data. " + ("Filters: " + "; ".join(notes) + ". " if notes else "") + f"Sorted {q['sort']} first.",
		"has_more": has_more}
	return {"text": summary + ".", "card": card, "names": [r["name"] for r in rows], "titles": [str(r.get(spec.title) or "") for r in rows]}


def _count(q):
	filters, or_filters, notes = _base_filters(q)
	rows = frappe.get_list(q["doctype"], filters=filters, or_filters=or_filters or None, fields=[{"COUNT": "*", "as": "n"}])
	n = int(rows[0]["n"]) if rows else 0
	return {"text": f"There {'is' if n == 1 else 'are'} {n} {q['doctype']} record{'' if n == 1 else 's'} you can access"
		+ (f" ({'; '.join(notes)})" if notes else "") + ". From your ERPNext data.", "names": [], "titles": []}


def _details(q):
	doctype, spec = q["doctype"], REGISTRY[q["doctype"]]
	name = q["name"]
	scope = _company_filter(q)[0]
	rows = frappe.get_list(doctype, filters=[["name", "=", name], *scope], pluck="name", limit_page_length=1) if name else []
	if not rows:
		term = name or q["search"] or q["party"]
		rows = frappe.get_list(doctype, filters=scope or None, or_filters=[[f, "like", f"%{term}%"] for f in spec.search_fields],
			fields=["name", spec.title], limit_page_length=6, order_by="modified desc")
		if not rows:
			return {"text": f"I couldn't find a {doctype} matching “{term}” that you can access.", "names": [], "titles": []}
		if len(rows) > 1:
			raise QueryClarify(f"Several {doctype} records match “{term}”. Which one?",
				options=[f"{doctype} {r['name']}" for r in rows[:5]])
		rows = [rows[0]["name"]]
	doc = frappe.get_doc(doctype, rows[0])
	doc.check_permission("read")
	fields = _permitted(doctype, q["fields"] or list(spec.detail_fields))
	if "name" in fields:
		fields.remove("name")
	meta = frappe.get_meta(doctype)
	card_rows = [{"label": meta.get_label(f), "value": _display(doc.get(f), meta.get_field(f))} for f in fields if doc.get(f) not in (None, "")]
	card = {"type": "record", "title": f"{doctype} {doc.name}", "doctype": doctype, "name": doc.name, "rows": card_rows,
		"action_label": f"Open {doctype}", "note": "From your ERPNext data."}
	if spec.child and doc.get(spec.child[0]):
		child_doctype = meta.get_field(spec.child[0]).options
		child_meta = frappe.get_meta(child_doctype)
		child_permitted = set(get_permitted_fields(child_doctype, parenttype=doctype))
		cfields = [f for f in spec.child[1] if f in child_permitted]
		card["items"] = {"columns": [{"key": f, "label": child_meta.get_label(f)} for f in cfields],
			"rows": [{f: _jsonable(r.get(f)) for f in cfields} for r in doc.get(spec.child[0])[:25]]}
	if doctype == "Customer" and frappe.has_permission("Sales Invoice", "read"):
		outstanding = frappe.get_list("Sales Invoice", filters=[["customer", "=", doc.name], ["docstatus", "=", 1], ["outstanding_amount", ">", 0],
			*_company_filter({"doctype": "Sales Invoice", "company": None})[0]],
			fields=["currency", {"SUM": "outstanding_amount", "as": "total"}, {"COUNT": "*", "as": "n"}], group_by="currency")
		card["rows"].append({"label": "Outstanding (submitted Sales Invoices)",
			"value": "; ".join(f"{flt(o['total'], 2)} {o['currency']} in {o['n']} invoice(s)" for o in outstanding) or "None"})
	return {"text": f"Here are the details of {doctype} {doc.name}.", "card": card, "names": [doc.name], "titles": [str(doc.get(spec.title) or "")]}


def _sales_summary(q):
	filters, or_filters, notes = _base_filters(dict(q, docstatus=q["docstatus"] or "submitted", status=None))
	rows = frappe.get_list("Sales Invoice", filters=filters, or_filters=or_filters or None,
		fields=["company", "currency", {"SUM": "grand_total", "as": "total"}, {"SUM": "base_grand_total", "as": "base_total"}, {"COUNT": "*", "as": "n"}],
		group_by="company, currency", order_by="company asc")
	period = f"{q['date']['from']} to {q['date']['to']}"
	if not rows:
		return {"text": f"I found no {'; '.join(notes)} Sales Invoices for {period}.", "names": [], "titles": []}
	table_rows = []
	for r in rows:
		company_currency = frappe.get_cached_value("Company", r["company"], "default_currency")
		table_rows.append({"company": r["company"], "currency": r["currency"], "invoices": r["n"], "total": flt(r["total"], 2),
			"base_total": f"{flt(r['base_total'], 2)} {company_currency}"})
	card = {"type": "table", "title": f"Sales {period}", "doctype": None,
		"columns": [{"key": "company", "label": "Company"}, {"key": "currency", "label": "Invoice currency"}, {"key": "invoices", "label": "Invoices"},
			{"key": "total", "label": "Total (invoice currency)", "type": "currency"}, {"key": "base_total", "label": "Total in company currency"}],
		"rows": table_rows,
		"note": "From your ERPNext data. Grand totals of " + "; ".join(notes) + " Sales Invoices; credit notes count as negative amounts. "
			"Amounts in different currencies are not added together; the company-currency column uses the exchange rate stored on each invoice.",
		"list_route": {"doctype": "Sales Invoice", "filters": {"docstatus": 1, "posting_date": ["between", [q["date"]["from"], q["date"]["to"]]]}}}
	return {"text": f"Here is the sales total for {period}, per company and currency.", "card": card, "names": [], "titles": []}


def _outstanding_by_customer(q):
	rows = frappe.get_list("Sales Invoice", filters=[["docstatus", "=", 1], ["outstanding_amount", ">", 0],
		*_company_filter(dict(q, doctype="Sales Invoice"))[0]],
		fields=["customer", "customer_name", "currency", {"SUM": "outstanding_amount", "as": "total"}, {"COUNT": "*", "as": "n"}],
		group_by="customer, customer_name, currency", order_by="total desc", limit_page_length=q["limit"])
	if not rows:
		return {"text": "No customer has an outstanding balance on submitted Sales Invoices.", "names": [], "titles": []}
	card = {"type": "table", "title": "Outstanding by customer", "doctype": "Customer",
		"columns": [{"key": "name", "label": "Customer", "type": "link"}, {"key": "customer_name", "label": "Name"}, {"key": "currency", "label": "Currency"},
			{"key": "total", "label": "Outstanding", "type": "currency"}, {"key": "n", "label": "Invoices"}],
		"rows": [{"name": r["customer"], "customer_name": r["customer_name"], "currency": r["currency"], "total": flt(r["total"], 2), "n": r["n"],
			"_name": r["customer"], "_n": i + 1} for i, r in enumerate(rows)],
		"note": "From your ERPNext data: outstanding amounts of submitted Sales Invoices, per currency (not converted)."}
	return {"text": f"{len(rows)} customer{'s have' if len(rows) != 1 else ' has'} outstanding balances.", "card": card,
		"names": [r["customer"] for r in rows], "titles": [r["customer_name"] or r["customer"] for r in rows], "result_doctype": "Customer"}


def _jsonable(v):
	if isinstance(v, (datetime.date, datetime.datetime)):
		return str(v)
	if isinstance(v, float):
		return flt(v, 2)
	return v


def _display(v, df):
	if df and df.fieldtype == "Check":
		return "Yes" if v else "No"
	return _jsonable(v)
