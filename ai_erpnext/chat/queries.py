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

Field questions ("mobile number of Amit Patel", "tax breakdown of SINV-0001") are "details"
plans with requested fields. Requested names are mapped to allowlisted fields of the
resolved DocType (FIELD_CONCEPTS) and answered directly, field first, with a clear sentence
when the record has no value; only the requested fields are returned. Records are resolved
by exact ID, exact (case-insensitive) title, then whole-word/prefix matching of every typed
word; a single such match is used and named in the answer, several become a choice, and a
fuzzy (misspelt) match is never selected on its own.
"""

import datetime
import re

import frappe
from frappe.model import get_permitted_fields
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, nowdate, strip_html

from ai_erpnext.capability import metadata as meta_
from ai_erpnext.capability import service as capability
from ai_erpnext.capability.policy import (  # re-exported: existing callers import them from here
	REGISTRY,
	TAX_FIELDS,
	TXN_ITEMS,
	Spec,
)
from ai_erpnext.capability.service import CapabilityError
from ai_erpnext.chat import hr
from ai_erpnext.mapping.normalize import clean_text, normalize_name, parse_date

PAGE_SIZE = 10
MAX_PAGE = 20
MAX_OFFSET = 500
MAX_RANGE_DAYS = 3 * 366


# The capability layer owns the registry (explicit policy) and its errors; these names stay here for existing callers.
QueryError = CapabilityError  # user-facing reason a plan can't run (unsupported, not permitted, invalid)


class QueryClarify(Exception):
	"""An essential parameter is missing or ambiguous; ask the user.

	kind: "target" (which record: the reply is a name or ID), "pick" (options are records:
	{"label", "value": "<DocType>::<name>"}), "period" (options are {"label", "value": preset})
	or None (plain suggestions). plan: the validated query to complete with the answer."""

	def __init__(self, question, options=None, kind=None, plan=None):
		super().__init__(question)
		self.question = question
		self.options = options or []
		self.kind = kind
		self.plan = plan


# Words people use for a field -> candidate fields (first ones present in the DocType's
# allowlist win; "@..." are computed values). Only allowlisted fields can ever be returned.
FIELD_CONCEPTS = {
	"mobile": ("mobile_no",),
	"phone": ("phone", "mobile_no"),
	"email": ("email_id",),
	"address": ("primary_address",),
	"tax_id": ("tax_id",),
	"contact": ("customer_primary_contact", "supplier_primary_contact"),
	"group": ("customer_group", "supplier_group", "item_group"),
	"customer_group": ("customer_group",),
	"supplier_group": ("supplier_group",),
	"item_group": ("item_group",),
	"territory": ("territory",),
	"country": ("country",),
	"currency": ("currency", "default_currency"),
	"type": ("customer_type", "supplier_type", "stock_entry_type", "payment_type", "warehouse_type"),
	"status": ("status", "docstatus"),
	"items": ("@items",),
	"taxes": ("@taxes",),
	"outstanding": ("outstanding_amount", "@outstanding"),
	"total": ("grand_total",),
	"net_total": ("net_total",),
	"tax_total": ("total_taxes_and_charges",),
	"date": ("posting_date", "transaction_date"),
	"due_date": ("due_date",),
	"delivery_date": ("delivery_date",),
	"required_by": ("schedule_date",),
	"customer": ("customer_name", "customer"),
	"supplier": ("supplier_name", "supplier"),
	"party": ("customer_name", "supplier_name", "party_name"),
	"company": ("company", "company_name"),
	"warehouse": ("set_warehouse", "from_warehouse", "to_warehouse"),
	"uom": ("stock_uom",),
	"description": ("description",),
	"department": ("department",),
	"designation": ("designation",),
	"bill_no": ("bill_no",),
	"delivered": ("per_delivered",),
	"received": ("per_received",),
	"billed": ("per_billed",),
}
CONCEPT_LABELS = {
	"mobile": "mobile number",
	"phone": "phone number",
	"email": "email address",
	"tax_id": "tax ID",
	"outstanding": "outstanding amount",
	"total": "grand total",
	"items": "items",
	"taxes": "tax breakdown",
	"date": "date",
	"due_date": "due date",
	"delivery_date": "delivery date",
	"required_by": "required-by date",
	"contact": "primary contact",
	"warehouse": "warehouse",
	"tax_total": "total taxes and charges",
}
VIRTUAL = ("@items", "@taxes", "@outstanding")
# "auto": the user named a record without saying what it is (e.g. "mobile of Amit Patel").
ENTITY_DOCTYPES = ("Customer", "Supplier", "Contact")
DOCUMENT_DOCTYPES = (
	"Sales Invoice",
	"Purchase Invoice",
	"Sales Order",
	"Purchase Order",
	"Delivery Note",
	"Purchase Receipt",
	"Stock Entry",
	"Payment Entry",
)
MAX_CHOICES = 6

DATE_PRESETS = (
	"today",
	"yesterday",
	"this_week",
	"last_week",
	"this_month",
	"last_month",
	"this_quarter",
	"last_quarter",
	"this_year",
	"last_year",
	"last_7_days",
	"last_30_days",
)
DOCSTATUS = {"draft": 0, "submitted": 1, "cancelled": 2}
SORTS = ("newest", "oldest", "highest", "lowest")


# ------------------------------------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------------------------------------
def validate(plan):
	"""Normalise a router plan into a safe query dict, or raise QueryError/QueryClarify."""
	if not isinstance(plan, dict):
		raise QueryError("I couldn't understand that request.")
	op = clean_text(plan.get("operation"), 40) or "list"
	filters = plan.get("filters") if isinstance(plan.get("filters"), dict) else {}
	raw_doctype = clean_text(plan.get("doctype"), 60)
	op = _hr_operation(op, raw_doctype, plan)
	doctype = "Employee" if op in hr.OPERATIONS else _match_doctype(raw_doctype)
	auto = (
		not doctype
		and op == "details"
		and (not raw_doctype or raw_doctype.lower() in ("auto", "any", "record", "party", "contact person"))
	)
	if not doctype and not auto:
		raise QueryError(
			f"I can't look up “{raw_doctype or 'that'}” yet. I can search: {', '.join(REGISTRY)}."
		)
	if doctype and not frappe.has_permission(doctype, "read"):
		raise QueryError(f"You don't have permission to view {doctype} records.")
	spec = REGISTRY.get(doctype)
	capability.refuse_lifecycle(op)
	if spec:
		capability.check_operation(doctype, op)  # operations outside the policy fail closed

	q = {
		"doctype": doctype,
		"auto": auto,
		"operation": op,
		"company": _match_company(doctype, filters.get("company")) if doctype else None,
		"party": clean_text(filters.get("party"), 140) if spec and spec.party_field else None,
		"search": clean_text(filters.get("search"), 140),
		"name": clean_text(filters.get("name"), 140),
		"status": _match_status(doctype, filters.get("status")) if doctype else None,
		"docstatus": None,
		"date": None,
		"payment": None,
		"sort": plan.get("sort") if plan.get("sort") in SORTS else "newest",
		"limit": max(1, min(MAX_PAGE, int(flt(plan.get("limit")) or PAGE_SIZE))),
		"offset": max(0, min(MAX_OFFSET, int(flt(plan.get("offset"))))),
		"fields": None,
		"want": None,
		# "my employee details": the logged-in user's own Employee, resolved when the query runs (never stored as a name)
		"self": doctype == "Employee" and filters.get("self") is True,
	}
	if not q["search"]:
		# A name given as "party" (or as a title field) of a record that has no party field is the
		# record itself: "mobile of Amit Patel" -> Customer details matching "Amit Patel".
		named = [
			clean_text(filters.get(k), 140)
			for k in ("party", *(spec.search_fields if spec else ()), spec.title if spec else "")
		]
		q["search"] = next((v for v in named if v), None) if not q["party"] else None
	if op in hr.OPERATIONS:
		return hr.validate(plan, q, filters)
	ds = clean_text(filters.get("docstatus"), 20)
	if ds and ds.lower() in DOCSTATUS:
		q["docstatus"] = ds.lower()
	payment = (clean_text(filters.get("payment"), 20) or "").lower()
	if doctype in ("Sales Invoice", "Purchase Invoice") and (
		payment in ("unpaid", "overdue", "outstanding") or filters.get("outstanding") is True
	):
		q["payment"] = "overdue" if payment == "overdue" else "unpaid"

	requested = [
		clean_text(f, 60) for f in (plan.get("fields") if isinstance(plan.get("fields"), list) else [])[:10]
	]
	requested = [f for f in requested if f]
	if requested:
		if op == "details":
			q["want"] = requested  # mapped to real fields once the record (and its DocType) is known
			if doctype:
				requested_fields(doctype, requested)  # unsupported names fail now, before any lookup
		else:
			allowed = set(spec.detail_fields) | set(spec.list_fields)
			bad = [f for f in requested if f not in allowed and not _concept_fields(doctype, f, allowed)]
			if bad:
				raise QueryError(f"I can't show {', '.join(bad[:3])} for {doctype}.")
			permitted = set(get_permitted_fields(doctype)) | {"name"}
			mapped = [f if f in allowed else _concept_fields(doctype, f, allowed)[0] for f in requested]
			q["fields"] = [f for f in dict.fromkeys(mapped) if f in permitted and not f.startswith("@")]

	date = plan.get("date") if isinstance(plan.get("date"), dict) else None
	if date and spec:
		q["date"] = _resolve_dates(date, spec)
	_generic_parts(q, plan)
	if op == "sales_summary" and not q["date"]:
		raise QueryClarify(
			"For which period should I total the sales?",
			kind="period",
			plan=q,
			options=[
				{"label": "This month", "value": "this_month"},
				{"label": "Last month", "value": "last_month"},
				{"label": "This year", "value": "this_year"},
			],
		)
	if op in ("details", "summary") and not (q["name"] or q["search"] or q["party"] or q["self"]):
		what = doctype or "record"
		raise QueryClarify(f"Which {what} do you mean? Type its name or ID.", kind="target", plan=q)
	return q


# Words in a details plan that ask for HRMS data rather than an Employee field ("leave balance of Anshum").
_HR_FIELD_WORDS = {
	"leave_balance": re.compile(r"\bleaves?\b|\bbalance\b", re.I),
	"attendance": re.compile(r"\battendance\b", re.I),
}


def _hr_operation(op, raw_doctype, plan):
	"""The HR operation a plan really asks for: leave_balance/attendance as named, or an Employee details plan whose
	requested "fields" are leave or attendance (the router model sometimes plans "leave balance" as a field)."""
	if op in hr.OPERATIONS:
		return op
	if op != "details" or (raw_doctype or "").lower().rstrip("s") != "employee":
		return op
	words = " ".join(str(f) for f in (plan.get("fields") if isinstance(plan.get("fields"), list) else []))
	return next((name for name, regex in _HR_FIELD_WORDS.items() if words and regex.search(words)), op)


def _generic_parts(q, plan):
	"""Validated structured parts of a plan: where (filters), aggregate, child (table rows) and link (one hop).
	A part that can't be validated rejects the whole plan: it is never dropped, which would widen the query."""
	doctype, op = q["doctype"], q["operation"]
	parts = {
		k: plan.get(k)
		for k in ("where", "aggregate", "child", "link")
		if plan.get(k) not in (None, [], {}, "")
	}
	if not doctype:
		if parts:
			raise QueryError("Please say which kind of record you mean.")
		q.update(where=[], aggregate=None, child=None, link=None)
		return
	q["where"] = capability.normalize_where(doctype, parts.get("where"))
	q["overdue_days"] = None
	days = plan.get("overdue_days")
	if days not in (None, "", 0):
		days = int(flt(days)) if isinstance(days, (int, float, str)) and not isinstance(days, bool) else 0
		if doctype not in ("Sales Invoice", "Purchase Invoice") or not 1 <= days <= 3650:
			raise QueryError("I can only filter invoices by how many days they are overdue (1 to 3650).")
		q["overdue_days"] = days
		if op in ("list", "overdue", "unpaid"):
			op = q["operation"] = "overdue"
		else:
			q["payment"] = "overdue"
	if "aggregate" in parts and op in ("list", "count"):
		op = q["operation"] = "aggregate"
	q["aggregate"] = (
		capability.normalize_aggregate(doctype, parts.get("aggregate") or {"fn": "count"})
		if op == "aggregate"
		else None
	)
	if "aggregate" in parts and op != "aggregate":
		raise QueryError("That calculation can't be combined with this request.")
	q["child"] = capability.normalize_child(doctype, parts["child"]) if "child" in parts else None
	if op == "child_rows" and not q["child"]:
		raise QueryError("Which part of the records do you want to see (for example their items)?")
	q["link"] = capability.normalize_link(doctype, parts["link"]) if "link" in parts else None
	if q["link"] and op != "list":
		raise QueryError("Related records can only be shown next to a list.")
	if q["child"] and op not in ("list", "count", "child_rows", "aggregate"):
		raise QueryError("That can't be combined with a lookup inside the rows.")
	if op == "aggregate" and q["aggregate"]["period"] and not q["date"]:
		raise QueryClarify(
			"For which period should I group them?",
			kind="period",
			plan=q,
			options=[
				{"label": "This year", "value": "this_year"},
				{"label": "Last year", "value": "last_year"},
				{"label": "Last 30 days", "value": "last_30_days"},
			],
		)


def _concept_fields(doctype, word, allowed=None):
	"""Allowlisted fields of `doctype` for a requested word ("mobile", "mobile_no", "Mobile No")."""
	spec = REGISTRY[doctype]
	allowed = set(allowed or (set(spec.detail_fields) | set(spec.list_fields)))
	key = normalize_name(word).replace(" ", "_")
	if key in allowed:
		return [key]
	candidates = FIELD_CONCEPTS.get(key) or FIELD_CONCEPTS.get(_CONCEPT_ALIASES.get(key, ""), ())
	out = []
	for f in candidates:
		if f == "@items" and spec.child:
			out.append(f)
		elif f == "@taxes" and spec.taxes:
			out.append(f)
		elif f == "@outstanding" and doctype in ("Customer", "Supplier"):
			out.append(f)
		elif f in allowed:
			out.append(f)
	if key in ("phone",) and out:
		return out  # both phone and mobile are shown when both exist
	return out[:1]


_CONCEPT_ALIASES = {
	"mobile_no": "mobile",
	"mobile_number": "mobile",
	"cell": "mobile",
	"phone_number": "phone",
	"contact_number": "phone",
	"telephone": "phone",
	"email_id": "email",
	"email_address": "email",
	"mail": "email",
	"gstin": "tax_id",
	"gst": "tax_id",
	"vat": "tax_id",
	"pan": "tax_id",
	"tax_number": "tax_id",
	"balance": "outstanding",
	"outstanding_amount": "outstanding",
	"due": "outstanding",
	"outstanding_balance": "outstanding",
	"amount_due": "outstanding",
	"grand_total": "total",
	"amount": "total",
	"value": "total",
	"tax": "taxes",
	"tax_breakdown": "taxes",
	"tax_details": "taxes",
	"item": "items",
	"products": "items",
	"lines": "items",
	"posting_date": "date",
	"transaction_date": "date",
	"invoice_date": "date",
	"order_date": "date",
	"schedule_date": "required_by",
	"required_date": "required_by",
	"target_warehouse": "warehouse",
	"source_warehouse": "warehouse",
	"set_warehouse": "warehouse",
	"primary_contact": "contact",
	"billing_address": "address",
	"state": "status",
	"vendor": "supplier",
	"client": "customer",
	"stock_uom": "uom",
	"unit": "uom",
	"payment_status": "status",
}


def requested_fields(doctype, words):
	"""Fields (and computed values) of `doctype` for the requested words; QueryError if one isn't available."""
	fields, bad = [], []
	for word in words or []:
		found = _concept_fields(doctype, word)
		if found:
			fields += found
		else:
			bad.append(word)
	if bad:
		raise QueryError(f"I can't show {', '.join(bad[:3])} for {doctype} records.")
	return list(dict.fromkeys(fields))


def _match_doctype(value):
	value = (clean_text(value, 60) or "").lower().rstrip("s")
	for dt in REGISTRY:
		if value in (dt.lower(), dt.lower().rstrip("s")):
			return dt
	return None


def has_company(doctype):
	return bool(frappe.get_meta(doctype).has_field("company"))


def permitted_companies(doctype=None):
	"""The companies the user's Company User Permissions allow (for `doctype`), or None when none applies.

	This is how ERPNext itself scopes a user to companies. It is deliberately *not* "the Company records the user can list":
	other User Permissions apply to Company records too (since v16 a Company links to its default warehouse, so a Warehouse
	User Permission hides companies), which would wrongly leave a warehouse-restricted user with no company at all.
	None means no company restriction: role permissions and get_list's own User Permission checks still apply."""
	from frappe.permissions import get_user_permissions

	rows = (get_user_permissions(frappe.session.user) or {}).get("Company") or []
	allowed = sorted(
		{
			r.get("doc")
			for r in rows
			if r.get("doc") and (not r.get("applicable_for") or r.get("applicable_for") == doctype)
		}
	)
	return allowed or None


def usable_companies(doctype=None):
	"""Names of the companies the user may work with: their permitted companies, or every company when no Company User
	Permission restricts them and they may read companies at all."""
	allowed = permitted_companies(doctype)
	if allowed is not None:
		return allowed
	if not frappe.has_permission("Company", "read"):
		return []
	return frappe.get_all("Company", pluck="name", order_by="name asc", limit=0)


def _match_company(doctype, value):
	value = clean_text(value, 140)
	if not value:
		return None
	if not has_company(doctype):
		return None  # masters such as Customer or Item are shared by all companies
	allowed = usable_companies(doctype)
	if allowed:
		# names and abbreviations of companies already known to be usable (not sensitive): get_all, so unrelated
		# User Permissions on Company records (e.g. its default warehouse) can't hide them
		rows = frappe.get_all(
			"Company", filters={"name": ["in", allowed]}, fields=["name", "company_name", "abbr"], limit=0
		)
		match = [
			r.name
			for r in rows
			if value.lower() in (r.name.lower(), (r.company_name or "").lower(), (r.abbr or "").lower())
		]
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
	allowed = permitted_companies(q["doctype"])
	if allowed is None:
		return [], None
	spec = REGISTRY.get(q["doctype"])
	if spec and spec.optional_company:  # records without a company (e.g. a Lead) are not tied to any company
		return [["company", "in", [*allowed, ""]]], None
	return [["company", "in", allowed or [""]]], None


def _match_status(doctype, value):
	value = clean_text(value, 40)
	spec = REGISTRY[doctype]
	if not value or not spec.status_field:
		return None
	options = [
		o for o in (frappe.get_meta(doctype).get_field(spec.status_field).options or "").split("\n") if o
	]
	return next((o for o in options if o.lower() == value.lower()), None)


def preset_dates(doctype, preset):
	"""The date filter of a named period (a button answer to “for which period?”)."""
	return _resolve_dates({"preset": preset}, REGISTRY[doctype])


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
			raise QueryClarify(
				"Which dates exactly? Please give them as YYYY-MM-DD, for example 2026-09-01 to 2026-09-30."
			)
		start, end = getdate(start.value), getdate(end.value)
	if start > end:
		start, end = end, start
	if (end - start).days > MAX_RANGE_DAYS:
		raise QueryError("That date range is too long. Please choose up to 3 years.")
	return {
		"field": field,
		"from": str(start),
		"to": str(end),
		"label": preset.replace("_", " ") if preset in DATE_PRESETS else None,
	}


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
def _recheck(q):
	"""Plans also come back from conversation state ("show more", the answer to a lookup question):
	re-apply the allowlist and the read permission before running one."""
	doctype = q.get("doctype")
	if doctype is None and not (q.get("auto") and q.get("operation") == "details"):
		raise QueryError("I couldn't understand that request.")
	if doctype is not None:
		spec = REGISTRY.get(doctype)
		if not spec or q.get("operation") not in spec.operations or not capability.is_queryable(doctype):
			raise QueryError("I couldn't understand that request.")
		if not frappe.has_permission(doctype, "read"):
			raise QueryError(f"You don't have permission to view {doctype} records.")
		# structured parts come back from conversation state too: validate them again, never trust a stored plan
		q["where"] = capability.normalize_where(doctype, q.get("where"))
		q["aggregate"] = (
			capability.normalize_aggregate(doctype, q["aggregate"]) if q.get("aggregate") else None
		)
		days = q.get("overdue_days")
		q["overdue_days"] = (
			int(days)
			if isinstance(days, (int, float))
			and not isinstance(days, bool)
			and 1 <= days <= 3650
			and doctype in ("Sales Invoice", "Purchase Invoice")
			else None
		)
		q["child"] = capability.normalize_child(doctype, q["child"]) if q.get("child") else None
		q["link"] = capability.normalize_link(doctype, q["link"]) if q.get("link") else None
		allowed = set(spec.detail_fields) | set(spec.list_fields)
		q["fields"] = [f for f in q.get("fields") or [] if f in allowed] or None
	q["limit"] = max(1, min(MAX_PAGE, int(flt(q.get("limit")) or PAGE_SIZE)))
	q["offset"] = max(0, min(MAX_OFFSET, int(flt(q.get("offset")))))
	return q


def execute(q):
	q = _recheck(q)
	op = q["operation"]
	if op == "count":
		return _count(q)
	if op == "aggregate":
		return _aggregate(q)
	if op == "summary":
		return _party_summary(q)
	if op == "child_rows":
		return _child_rows(q)
	if op == "details":
		return _details(q)
	if op == "sales_summary":
		return _sales_summary(q)
	if op == "outstanding_by_customer":
		return _outstanding_by_party(q, "Sales Invoice", "Customer")
	if op == "outstanding_by_supplier":
		return _outstanding_by_party(q, "Purchase Invoice", "Supplier")
	if op in hr.OPERATIONS:
		if not hr.installed():
			raise QueryError(hr.NOT_INSTALLED)
		return hr.execute(q)
	return _list(q)


def _base_filters(q):
	spec = REGISTRY[q["doctype"]]
	filters, or_filters, notes = [], [], []
	subset = q["operation"] if q["operation"] in ("overdue", "unpaid") else q.get("payment")
	if subset:
		filters += [["docstatus", "=", 1], ["outstanding_amount", ">", 0], ["is_return", "=", 0]]
		notes.append("submitted, with an outstanding amount")
		if subset == "overdue":
			filters.append(["due_date", "<", nowdate()])
			notes.append(f"due before {nowdate()}")
	elif q["docstatus"]:
		filters.append(["docstatus", "=", DOCSTATUS[q["docstatus"]]])
		notes.append(q["docstatus"])
	elif q.get(
		"all_states"
	):  # set by the page context only (a list or form shows every state), never taken from a plan
		pass
	elif frappe.get_meta(q["doctype"]).is_submittable:
		filters.append(["docstatus", "<", 2])
		notes.append("draft and submitted (cancelled excluded)")
	for fieldname, op, value in spec.default_filters:
		if not any(w[0] == fieldname for w in q.get("where") or []):
			filters.append([fieldname, op, value])
	if subset == "overdue" and q.get("overdue_days"):
		cutoff = add_days(nowdate(), -int(q["overdue_days"]))
		filters.append(["due_date", "<", cutoff])
		notes.append(f"overdue by more than {int(q['overdue_days'])} days (due before {cutoff})")
	company_filters, company_note = _company_filter(q)
	filters += company_filters
	if company_note:
		notes.append(company_note)
	if q["status"]:
		filters.append([spec.status_field, "=", q["status"]])
		notes.append(f"status {q['status']}")
	if q["party"]:
		or_filters += [[spec.party_field, "like", f"%{q['party']}%"]] + (
			[[spec.party_title_field, "like", f"%{q['party']}%"]] if spec.party_title_field else []
		)
		notes.append(f"{spec.party_field} matching “{q['party']}”")
	if q["search"]:
		or_filters += [[f, "like", f"%{q['search']}%"] for f in spec.search_fields]
		notes.append(f"matching “{q['search']}”")
	for fieldname, op, value in q.get("where") or []:
		filters.append([fieldname, op, f"%{value}%" if op == "like" else value])
		notes.append(
			f"{meta_.field_info(q['doctype'], fieldname).label.lower()} {op if op != 'like' else 'contains'} {_show(value)}"
		)
	child_filters, child_note = _child_parent_filter(q)
	filters += child_filters
	if child_note:
		notes.append(child_note)
	if q["date"]:
		d = q["date"]
		filters.append([d["field"], "between", [d["from"], d["to"]]])
		label = (
			"created"
			if d["field"] == "creation"
			else frappe.get_meta(q["doctype"]).get_label(d["field"]).lower()
		)
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
		ftype = (
			"link"
			if f == "name"
			else (
				"currency"
				if df and df.fieldtype in ("Currency", "Float")
				else ("date" if df and df.fieldtype in ("Date", "Datetime") else "text")
			)
		)
		cols.append({"key": f, "label": label, "type": ftype})
	return cols


def _permitted(doctype, fields):
	permitted = set(get_permitted_fields(doctype)) | {"name"}
	return [f for f in fields if f in permitted]


def _list(q):
	doctype, spec = q["doctype"], REGISTRY[q["doctype"]]
	fields = _permitted(doctype, q["fields"] or list(spec.list_fields))
	if q["operation"] in ("overdue", "unpaid"):
		fields = _permitted(
			doctype,
			[
				"name",
				spec.title,
				"posting_date",
				"due_date",
				"grand_total",
				"outstanding_amount",
				"currency",
				"status",
			],
		)
	if "name" not in fields:
		fields = ["name", *fields]
	link = q.get("link")
	if link and link["field"] not in fields:
		fields = [*fields, link["field"]]
	filters, or_filters, notes = _base_filters(q)
	rows = frappe.get_list(
		doctype,
		filters=filters,
		or_filters=or_filters or None,
		fields=fields,
		order_by=_order_by(q),
		offset=q["offset"],
		limit=q["limit"] + 1,
	)
	has_more = len(rows) > q["limit"]
	rows = rows[: q["limit"]]
	what = {"overdue": f"overdue {doctype}s", "unpaid": f"unpaid {doctype}s"}.get(
		q["operation"], f"{doctype} records"
	)
	if not rows:
		return {
			"text": f"I found no {what}"
			+ (" on this page." if q["offset"] else " matching that.")
			+ (f" Filters: {'; '.join(notes)}." if notes else ""),
			"names": [],
			"titles": [],
		}
	start = q["offset"] + 1
	summary = (
		f"I found {len(rows)}{'+' if has_more else ''} {what}"
		if not q["offset"]
		else f"Showing {what} {start}–{q['offset'] + len(rows)}"
	)
	columns = _columns(
		doctype,
		[
			f
			for f in fields
			if not (link and f == link["field"] and f not in (q["fields"] or spec.list_fields))
		],
	)
	table_rows = [
		{**{k: _jsonable(r.get(k)) for k in fields}, "_name": r["name"], "_n": start + i}
		for i, r in enumerate(rows)
	]
	if link:
		columns += _attach_link(doctype, link, rows, table_rows, q)
	card = {
		"type": "table",
		"title": doctype,
		"doctype": doctype,
		"columns": columns,
		"rows": table_rows,
		"note": "From your ERPNext data. "
		+ ("Filters: " + "; ".join(notes) + ". " if notes else "")
		+ f"Sorted {q['sort']} first.",
		"has_more": has_more,
	}
	return {
		"text": summary + ".",
		"card": card,
		"names": [r["name"] for r in rows],
		"titles": [str(r.get(spec.title) or "") for r in rows],
	}


def _show(value):
	if isinstance(value, (list, tuple)):
		parts = [str(v) for v in value]
		return " – ".join(parts) if len(parts) == 2 else ", ".join(parts)
	return f"“{value}”" if isinstance(value, str) else str(value)


# ------------------------------------------------------------------------------------------------
# Generic structured queries: child-table conditions, child rows, one-hop links, aggregates
# ------------------------------------------------------------------------------------------------
MAX_PARENT_SCAN = 500  # parents matched through a child-table condition
MAX_CHILD_PARENTS = 50  # parents whose rows "child_rows" shows
MAX_CHILD_ROWS = 100


def _child_filter(cond):
	fieldname, op, value = cond
	return [fieldname, op, f"%{value}%" if op == "like" else value]


def _child_parent_filter(q):
	"""[filters, note] limiting the parents to those having a child row that matches the child conditions.

	Parent-first: the child rows only yield candidate parent names; the parent query that follows applies
	the user's permissions, company scope and every other filter, so no row of an inaccessible parent is
	ever returned or counted."""
	child = q.get("child")
	if not child or not child["where"]:
		return [], None
	doctype, table = q["doctype"], child["table"]
	child_doctype = meta_.child_doctype(doctype, table)
	names = frappe.get_list(
		child_doctype,
		parent_doctype=doctype,
		pluck="parent",
		group_by="parent",
		order_by="parent desc",
		limit=MAX_PARENT_SCAN,
		filters=[
			["parenttype", "=", doctype],
			["parentfield", "=", table],
			*[_child_filter(c) for c in child["where"]],
		],
	)
	label = frappe.get_meta(doctype).get_field(table).label.lower()
	return [["name", "in", names or [""]]], "having " + label + " with " + " and ".join(
		f"{meta_.field_info(child_doctype, f).label.lower()} {op if op != 'like' else 'containing'} {_show(v)}"
		for f, op, v in child["where"]
	)


def _attach_link(doctype, link, rows, table_rows, q):
	"""Add the allowlisted fields of the linked record (one hop) to the rows; returns the new columns.

	The target is read with its own permission-aware query and company scope. A record the user can't
	read gives an empty cell, the same as a missing one: nothing reveals whether it exists."""
	target, key = link["target"], link["field"]
	values = sorted({r.get(key) for r in rows if r.get(key)})
	fields = [f for f in _permitted(target, link["fields"]) if f != "name"]
	found = {}
	if values and fields and frappe.has_permission(target, "read"):
		try:
			for r in frappe.get_list(
				target,
				filters=[["name", "in", values], *_scope(target)],
				fields=["name", *fields],
				limit=len(values),
			):
				found[r["name"]] = r
		except frappe.PermissionError:
			frappe.local.message_log = []
	label = frappe.get_meta(doctype).get_label(key)
	columns = []
	for f in fields:
		column = f"{key}.{f}"
		columns.append(
			{
				"key": column,
				"label": f"{label} › {frappe.get_meta(target).get_label(f)}",
				"type": _columns(target, [f])[0]["type"],
			}
		)
		for r, shown in zip(rows, table_rows, strict=True):
			shown[column] = _jsonable((found.get(r.get(key)) or {}).get(f))
	return columns


def _child_rows(q):
	doctype, child = q["doctype"], q["child"]
	table = child["table"]
	child_doctype = meta_.child_doctype(doctype, table)
	fields = [f for f in child["fields"] if f in set(get_permitted_fields(child_doctype, parenttype=doctype))]
	filters, or_filters, notes = _base_filters(q)
	parents = frappe.get_list(
		doctype,
		filters=filters,
		or_filters=or_filters or None,
		pluck="name",
		order_by=_order_by(q),
		limit=MAX_CHILD_PARENTS,
	)
	if not parents or not fields:
		return {
			"text": f"I found no {doctype} records with matching rows"
			+ (f" ({'; '.join(notes)})." if notes else "."),
			"names": [],
			"titles": [],
		}
	rows = frappe.get_list(
		child_doctype,
		parent_doctype=doctype,
		fields=["parent", *fields],
		order_by="parent desc, idx asc",
		filters=[
			["parent", "in", parents],
			["parenttype", "=", doctype],
			["parentfield", "=", table],
			*[_child_filter(c) for c in child["where"]],
		],
		limit=min(q["limit"] * 5, MAX_CHILD_ROWS) + 1,
		offset=q["offset"] * 5,
	)
	cap = min(q["limit"] * 5, MAX_CHILD_ROWS)
	has_more = len(rows) > cap
	rows = rows[:cap]
	if not rows:
		return {
			"text": f"I found no matching rows in {len(parents)} {doctype} record{'s' if len(parents) != 1 else ''}.",
			"names": [],
			"titles": [],
		}
	columns = [
		{"key": "parent", "label": doctype, "type": "link"},
		*({"key": f, "label": frappe.get_meta(child_doctype).get_label(f)} for f in fields),
	]
	names = list(dict.fromkeys(r["parent"] for r in rows))
	card = {
		"type": "table",
		"title": f"{doctype}: {frappe.get_meta(doctype).get_field(table).label}",
		"doctype": doctype,
		"columns": columns,
		"rows": [
			{
				"parent": r["parent"],
				**{f: _jsonable(r.get(f)) for f in fields},
				"_name": r["parent"],
				"_n": q["offset"] * 5 + i + 1,
			}
			for i, r in enumerate(rows)
		],
		"note": "From your ERPNext data. Rows of the "
		+ (f"{len(parents)}" if len(parents) < MAX_CHILD_PARENTS else f"first {MAX_CHILD_PARENTS}")
		+ f" {doctype} records you can access"
		+ (f" ({'; '.join(notes)})" if notes else "")
		+ ".",
		"has_more": has_more,
	}
	return {
		"text": f"I found {len(rows)}{'+' if has_more else ''} matching row{'s' if len(rows) != 1 else ''} in {len(names)} {doctype} record{'s' if len(names) != 1 else ''}.",
		"card": card,
		"names": names,
		"titles": [],
		"result_doctype": doctype,
	}


AGG_SQL = {"count": "COUNT", "sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX"}
AGG_WORDS = {"count": "Number of", "sum": "Total", "avg": "Average", "min": "Smallest", "max": "Largest"}
MAX_GROUPS = 20
MAX_BUCKET_ROWS = 120  # month/year buckets x groups


def _month_buckets(date):
	start, end = getdate(date["from"]), getdate(date["to"])
	cursor = get_first_day(start)
	while cursor <= end:
		yield cursor, get_last_day(cursor)
		cursor = add_months(cursor, 1)


def _aggregate(q):
	"""Count/sum/average/min/max over records the user can read, optionally grouped. Currency amounts are
	never added across currencies: the document currency (or the company, for company-currency fields) is
	always part of the grouping. Nothing but allowlisted fields and fixed functions reaches the database."""
	doctype, agg = q["doctype"], q["aggregate"]
	fn, measure, group, period = agg["fn"], agg["field"], agg["group_by"], agg["period"]
	filters, or_filters, notes = _base_filters(q)
	source = capability.currency_source(doctype, measure) if measure else None
	columns = []
	if source and source[0] == "field":
		columns.append(source[1])
	elif source and source[0] == "company":
		columns.append("company")
	bucket = period
	if group and group not in columns:
		columns.append(group)
	value = {AGG_SQL[fn]: measure or "*", "as": "value"}
	fields = [*columns, value, {"COUNT": "*", "as": "n"}]

	def run(extra):
		return frappe.get_list(
			doctype,
			filters=[*filters, *extra],
			or_filters=or_filters or None,
			fields=fields,
			group_by=", ".join(columns) or None,
			order_by="value desc",
			limit=MAX_GROUPS + 1,
		)

	results = []  # (bucket label, row)
	groups_label = [g for g in (group, bucket) if g]
	if bucket:
		date_field = REGISTRY[doctype].date_field
		spans = (
			list(_month_buckets(q["date"]))
			if bucket == "month"
			else [
				(datetime.date(y, 1, 1), datetime.date(y, 12, 31))
				for y in range(getdate(q["date"]["from"]).year, getdate(q["date"]["to"]).year + 1)
			]
		)
		for start, end in spans:
			lo, hi = max(start, getdate(q["date"]["from"])), min(end, getdate(q["date"]["to"]))
			label = f"{start:%Y-%m}" if bucket == "month" else f"{start:%Y}"
			results += [(label, r) for r in run([[date_field, "between", [str(lo), str(hi)]]])]
			if len(results) > MAX_BUCKET_ROWS:
				break
	else:
		results = [(None, r) for r in run([])]
	if not results or all(not r.get("n") for _l, r in results):
		return {
			"text": f"I found no {doctype} records to calculate that from"
			+ (f" ({'; '.join(notes)})." if notes else "."),
			"names": [],
			"titles": [],
		}
	truncated = len(results) > (MAX_BUCKET_ROWS if bucket else MAX_GROUPS)
	results = results[: MAX_BUCKET_ROWS if bucket else MAX_GROUPS]

	meta = frappe.get_meta(doctype)
	label = meta.get_label(measure) if measure else "records"
	cols, table_rows = [], []
	if bucket:
		cols.append({"key": "bucket", "label": "Month" if bucket == "month" else "Year"})
	for c in columns:
		cols.append(
			{
				"key": c,
				"label": "Currency"
				if source and source[0] == "field" and c == source[1]
				else meta.get_label(c),
			}
		)
	if source and source[0] == "company":
		cols.append({"key": "currency", "label": "Currency"})
	is_money = bool(source and source[0] != "unknown")
	cols.append(
		{
			"key": "value",
			"label": f"{AGG_WORDS[fn]} {label}".strip(),
			"type": "currency" if is_money else "text",
		}
	)
	if fn != "count":
		cols.append({"key": "n", "label": "Records"})
	for bucket_label, r in results:
		row = {c: r.get(c) for c in columns}
		if bucket:
			row["bucket"] = bucket_label
		if source and source[0] == "company":
			row["currency"] = frappe.get_cached_value("Company", r.get("company"), "default_currency")
		v = r.get("value")
		row["value"] = _jsonable(v) if not isinstance(v, float) else flt(v, 2)
		row["n"] = r.get("n")
		table_rows.append(row)
	title = f"{AGG_WORDS[fn]} {label}" + (
		f" by {' and '.join(g.replace('_', ' ') for g in groups_label)}" if groups_label else ""
	)
	card = {
		"type": "table",
		"title": f"{doctype}: {title}",
		"doctype": None,
		"source_doctype": doctype,
		"columns": cols,
		"rows": table_rows,
		"note": "From your ERPNext data"
		+ (f" ({'; '.join(notes)})" if notes else "")
		+ ". "
		+ ("Amounts in different currencies are never added together. " if is_money else "")
		+ (
			_RETURN_NOTE + " "
			if is_money
			and doctype in ("Sales Invoice", "Purchase Invoice")
			and not any(w[0] == "is_return" for w in q.get("where") or [])
			else ""
		)
		+ (f"Showing only the first {MAX_BUCKET_ROWS if bucket else MAX_GROUPS} rows. " if truncated else ""),
	}
	return {
		"text": f"{title} for {doctype} records" + (" (grouped)" if groups_label else "") + ".",
		"card": card,
		"names": [],
		"titles": [],
	}


def _count(q):
	filters, or_filters, notes = _base_filters(q)
	rows = frappe.get_list(
		q["doctype"], filters=filters, or_filters=or_filters or None, fields=[{"COUNT": "*", "as": "n"}]
	)
	n = int(rows[0]["n"]) if rows else 0
	return {
		"text": f"There {'is' if n == 1 else 'are'} {n} {q['doctype']} record{'' if n == 1 else 's'} you can access"
		+ (f" ({'; '.join(notes)})" if notes else "")
		+ ". From your ERPNext data.",
		"names": [],
		"titles": [],
	}


# ------------------------------------------------------------------------------------------------
# Record resolution
# ------------------------------------------------------------------------------------------------
DOC_ID_RE = re.compile(r"^[A-Za-z0-9]+(?:[-/.][A-Za-z0-9]+)*[-/.]\d+$")


def looks_like_id(term):
	"""True for naming-series IDs such as ACC-SINV-2026-00001 or PUR-ORD-2026-00012."""
	return bool(
		term and DOC_ID_RE.match(term) and any(c.isdigit() for c in term) and ("-" in term or "/" in term)
	)


def _title(doctype):
	spec = REGISTRY[doctype]
	return spec.title if spec.title and frappe.get_meta(doctype).has_field(spec.title) else "name"


def _words(*values):
	words = set()
	for v in values:
		words.update(normalize_name(v).split())
	return words


def resolve_record(doctype, term, scope=None, exact_name=False):
	"""Find the record `term` names. Returns (rows, exact): rows are {"name", "title"} dicts the user
	may read (at most MAX_CHOICES + 1), exact tells whether they matched the ID/title as typed.

	Order: exact ID; exact title (case-insensitive, as the database compares); the same with
	punctuation and spacing normalised; then records where every typed word starts a word of the
	title, ID or a search field ("palmer production ltd" -> "Palmer Productions Ltd."). Misspellings
	are never matched here. ID-like terms (ACC-SINV-2026-00001) only match an ID or title exactly:
	word prefixes would let "...-00001" select "...-000012"."""
	spec, scope = REGISTRY[doctype], list(scope or [])
	title = _title(doctype)
	fields = list(dict.fromkeys(["name", title]))

	def rows_of(found):
		return [{"name": r["name"], "title": str(r.get(title) or r["name"])} for r in found]

	term = clean_text(term, 140)
	if not term:
		return [], False
	found = frappe.get_list(doctype, filters=[["name", "=", term], *scope], fields=fields, limit=1)
	if found or exact_name:
		return rows_of(found), True
	if title != "name":
		found = frappe.get_list(
			doctype, filters=[[title, "=", term], *scope], fields=fields, limit=MAX_CHOICES + 1
		)
		if found:
			return rows_of(found), True
	tokens = normalize_name(term).split()
	if not tokens or looks_like_id(term):
		return [], False
	searchable = [
		f
		for f in dict.fromkeys(["name", title, *spec.search_fields])
		if f == "name" or frappe.get_meta(doctype).has_field(f)
	]
	longest = max(tokens, key=len)
	pool = frappe.get_list(
		doctype,
		filters=scope or None,
		or_filters=[[f, "like", f"%{longest}%"] for f in searchable],
		fields=list(dict.fromkeys(fields + searchable)),
		limit=50,
		order_by="modified desc",
	)
	wanted = " ".join(tokens)
	same = [r for r in pool if wanted in (normalize_name(r.get(title)), normalize_name(r["name"]))]
	if same:
		return rows_of(same[: MAX_CHOICES + 1]), True
	prefix = [
		r
		for r in pool
		if all(any(w.startswith(t) for w in _words(*(r.get(f) for f in searchable))) for t in tokens)
	]
	return rows_of(prefix[: MAX_CHOICES + 1]), False


def _scope(doctype, company=None):
	return _company_filter({"doctype": doctype, "company": company})[0]


def _choice(doctype, row, with_doctype=False):
	label = row["title"] if row["title"] == row["name"] else f"{row['title']} ({row['name']})"
	return {"label": f"{doctype}: {label}" if with_doctype else label, "value": f"{doctype}::{row['name']}"}


def _kind_label(doctypes):
	if len(doctypes) > 3:
		return "record"
	names = [dt.lower() for dt in doctypes]
	return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _resolve(q, term):
	"""(doctype, name, exact) for the record the query names, or a not-found result dict.
	Raises QueryClarify (kind "pick") when several records match."""
	if q.get("auto"):
		candidates = DOCUMENT_DOCTYPES + ENTITY_DOCTYPES if looks_like_id(term) else ENTITY_DOCTYPES
		doctypes = [dt for dt in candidates if frappe.has_permission(dt, "read")]
		if not doctypes:
			raise QueryError("You don't have permission to look that up.")
	else:
		doctypes = [q["doctype"]]
	hits = []  # (doctype, row, exact)
	for dt in doctypes:
		rows, exact = resolve_record(
			dt, term, _scope(dt, q.get("company") if dt == q.get("doctype") else None)
		)
		hits += [(dt, r, exact) for r in rows]
	if any(h[2] for h in hits):
		hits = [h for h in hits if h[2]]
	if len({h[0] for h in hits}) > 1 and any(h[0] != "Contact" for h in hits):
		hits = [h for h in hits if h[0] != "Contact"]  # a customer's/supplier's own contact is the same party
	if not hits:
		return {
			"text": f"I couldn't find {'an' if _kind_label(doctypes)[0] in 'aeiou' else 'a'} {_kind_label(doctypes)} "
			f"matching “{term}” that you can access.",
			"names": [],
			"titles": [],
			"not_found": True,
		}
	if len(hits) == 1:
		return hits[0][0], hits[0][1]["name"], hits[0][2]
	many = len({h[0] for h in hits}) > 1
	what = _kind_label(sorted({h[0] for h in hits}, key=doctypes.index)) if many else hits[0][0].lower()
	more = len(hits) > MAX_CHOICES
	question = (
		f"I found {'more than ' + str(MAX_CHOICES) if more else len(hits)} records matching “{term}”"
		+ (f" ({what})" if many else f" ({what}s)")
		+ ". Which one do you mean?"
		+ (" Type more of the name to narrow it down." if more else "")
	)
	raise QueryClarify(
		question,
		kind="pick",
		plan=q,
		options=[_choice(dt, row, with_doctype=many) for dt, row, _exact in hits[:MAX_CHOICES]],
	)


# ------------------------------------------------------------------------------------------------
# Details and field answers
# ------------------------------------------------------------------------------------------------
def _resolve_target(q, term):
	"""(doctype, name, exact) of the record a details/summary request names, or a not-found result dict."""
	if q.get("pick"):
		doctype, name = q["pick"]
		if doctype not in REGISTRY or not frappe.has_permission(doctype, "read"):
			raise QueryError("You don't have permission to view that.")
		rows, _exact = resolve_record(doctype, name, _scope(doctype, q.get("company")), exact_name=True)
		if not rows:
			return {
				"text": f"That {doctype} isn't available to you any more.",
				"names": [],
				"titles": [],
				"not_found": True,
			}
		return (doctype, rows[0]["name"], True)
	return _resolve(q, term)


def _details(q):
	if q.get("self") and not (q["name"] or q["search"] or q["party"] or q.get("pick")):
		q = dict(q, pick=["Employee", hr.self_employee(q)])
	term = q["name"] or q["search"] or q["party"]
	resolved = _resolve_target(q, term)
	if isinstance(resolved, dict):
		return resolved
	doctype, name, exact = resolved
	spec, meta = REGISTRY[doctype], frappe.get_meta(doctype)
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	title = str(doc.get(_title(doctype)) or doc.name)
	lead = (
		""
		if exact or q.get("pick")
		else f"I found one {doctype.lower()} matching “{term}”: {_who(doctype, doc, title)}. "
	)
	if q.get("want"):
		return _field_answer(q, doctype, doc, title, lead)

	fields = _permitted(doctype, list(spec.detail_fields))
	if "name" in fields:
		fields.remove("name")
	card_rows = [
		{"label": meta.get_label(f), "value": _display(doc.get(f), meta.get_field(f))}
		for f in fields
		if doc.get(f) not in (None, "")
	]
	card = {
		"type": "record",
		"title": f"{doctype} {doc.name}",
		"doctype": doctype,
		"name": doc.name,
		"rows": card_rows,
		"action_label": f"Open {doctype}",
		"note": "From your ERPNext data.",
	}
	if spec.child and doc.get(spec.child[0]):
		card["items"] = _child_table(doctype, doc, spec.child[0], spec.child[1])
	if doctype in ("Customer", "Supplier"):
		total = _party_outstanding(doctype, doc.name, q.get("company"))
		if total is not None:
			card["rows"].append(
				{
					"label": f"Outstanding (submitted {_INVOICE[doctype]}s)",
					"value": total["summary"] or "Nothing outstanding",
				}
			)
	return {
		"text": lead + f"Here are the details of {doctype} {doc.name}.",
		"card": card,
		"names": [doc.name],
		"titles": [title],
		"result_doctype": doctype,
	}


_INVOICE = {"Customer": "Sales Invoice", "Supplier": "Purchase Invoice"}
_ORDER = {"Customer": "Sales Order", "Supplier": "Purchase Order"}
_PARTY_FIELD = {"Customer": "customer", "Supplier": "supplier"}


def _party_summary(q):
	"""A customer's (or supplier's) orders and invoices in one table: per document type, state and currency, the number of
	documents and their totals. Each source is a separate permission-aware, company-scoped query, so a document type the
	user can't read is left out (and said so), and amounts of different currencies are never added together."""
	resolved = _resolve_target(q, q["name"] or q["search"] or q["party"])
	if isinstance(resolved, dict):
		return resolved
	party_type, name, _exact = resolved
	if party_type not in _INVOICE:
		raise QueryError("A summary is available for customers and suppliers.")
	title = frappe.get_cached_value(party_type, name, _title(party_type)) or name
	table_rows, skipped, readable = [], [], []
	for doctype in (_ORDER[party_type], _INVOICE[party_type]):
		if not (
			doctype in REGISTRY
			and capability.is_queryable(doctype)
			and frappe.has_permission(doctype, "read")
		):
			skipped.append(doctype)
			continue
		readable.append(doctype)
		invoice = doctype == _INVOICE[party_type]
		fields = ["docstatus", "currency", {"COUNT": "*", "as": "n"}, {"SUM": "grand_total", "as": "total"}]
		if invoice:
			fields.append({"SUM": "outstanding_amount", "as": "outstanding"})
		scope = _company_filter({"doctype": doctype, "company": q.get("company")})[0]
		for r in frappe.get_list(
			doctype,
			filters=[[_PARTY_FIELD[party_type], "=", name], ["docstatus", "<", 2], *scope],
			fields=fields,
			group_by="docstatus, currency",
			order_by="docstatus desc, currency asc",
			limit=40,
		):
			table_rows.append(
				{
					"document": doctype,
					"state": "Submitted" if r["docstatus"] == 1 else "Draft",
					"currency": r["currency"],
					"n": r["n"],
					"total": flt(r["total"], 2),
					"outstanding": flt(r["outstanding"], 2) if invoice and r["docstatus"] == 1 else None,
				}
			)
	requires = [_ORDER[party_type], _INVOICE[party_type]]
	if not readable:
		raise QueryError(
			f"You don't have permission to view the orders or invoices of this {party_type.lower()}."
		)
	note = "From your ERPNext data. Cancelled documents are left out; amounts in different currencies are never added together."
	if skipped:
		note += f" Not included (no permission): {', '.join(skipped)}."
	if not table_rows:
		return {
			"text": f"{party_type} {title} has no {' or '.join(d.lower() + 's' for d in readable)} that you can access.",
			"names": [name],
			"titles": [str(title)],
			"result_doctype": party_type,
			"requires": requires,
		}
	cols = [
		{"key": "document", "label": "Document"},
		{"key": "state", "label": "State"},
		{"key": "currency", "label": "Currency"},
		{"key": "n", "label": "Documents"},
		{"key": "total", "label": "Total", "type": "currency"},
		{"key": "outstanding", "label": "Outstanding", "type": "currency"},
	]
	card = {
		"type": "table",
		"title": f"{party_type} {title}: orders and invoices",
		"doctype": None,
		"source_doctype": _INVOICE[party_type],
		"columns": cols,
		"rows": table_rows,
		"note": note,
	}
	return {
		"text": f"Here is the summary of {party_type.lower()} {title}.",
		"card": card,
		"names": [name],
		"titles": [str(title)],
		"result_doctype": party_type,
		"requires": requires,
	}


def _who(doctype, doc, title):
	return title if title == doc.name else f"{title} ({doc.name})"


def _child_table(doctype, doc, table, wanted):
	child_doctype = frappe.get_meta(doctype).get_field(table).options
	child_meta = frappe.get_meta(child_doctype)
	permitted = set(get_permitted_fields(child_doctype, parenttype=doctype))
	cols = [f for f in wanted if f in permitted]
	return {
		"columns": [{"key": f, "label": child_meta.get_label(f)} for f in cols],
		"rows": [{f: _jsonable(r.get(f)) for f in cols} for r in doc.get(table)[:25]],
	}


def _party_outstanding(doctype, party, company=None):
	"""Net outstanding of submitted invoices and notes of a customer/supplier, per currency, within the companies
	the user may read. None if the user can't read those invoices."""
	invoice = _INVOICE[doctype]
	if not frappe.has_permission(invoice, "read"):
		return None
	# net: standalone credit/debit notes carry a negative outstanding (see _outstanding_by_party)
	rows = frappe.get_list(
		invoice,
		filters=[
			[doctype.lower(), "=", party],
			["docstatus", "=", 1],
			["outstanding_amount", "!=", 0],
			*_company_filter({"doctype": invoice, "company": company})[0],
		],
		fields=["currency", {"SUM": "outstanding_amount", "as": "total"}, {"COUNT": "*", "as": "n"}],
		group_by="currency",
	)
	rows = [r for r in rows if flt(r["total"], 2)]
	return {
		"rows": rows,
		"invoice": invoice,
		"summary": "; ".join(f"{flt(r['total'], 2)} {r['currency']} in {r['n']} invoice(s)" for r in rows),
	}


def _value_text(doc, meta, fieldname):
	value, df = doc.get(fieldname), meta.get_field(fieldname)
	if fieldname == "docstatus":
		return {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(value)
	if value in (None, "") or (df and df.fieldtype in ("Currency", "Float", "Percent") and value is None):
		return None
	if df and df.fieldtype in ("Text Editor", "Small Text", "Text", "Long Text"):
		value = strip_html(str(value).replace("<br>", ", ").replace("<br/>", ", ")).strip(" ,\n")
		return value or None
	if df and df.fieldtype == "Currency":
		currency = doc.get("currency") or doc.get("paid_from_account_currency") or ""
		return f"{flt(value, 2)} {currency}".strip()
	if df and df.fieldtype == "Percent":
		return f"{flt(value, 2)}%"
	return str(_display(value, df))


def _field_answer(q, doctype, doc, title, lead):
	"""Answer exactly the requested fields, value first; missing values are said plainly."""
	meta = frappe.get_meta(doctype)
	permitted = set(get_permitted_fields(doctype)) | {"name", "docstatus"}
	is_master = doctype in ("Customer", "Supplier", "Contact", "Item", "Warehouse", "Employee")
	subject = f"{doctype.lower()} {_who(doctype, doc, title)}" if is_master else f"{doctype} {doc.name}"
	lines, card_rows, extra = [], [], {}
	for word in q["want"]:
		fields = requested_fields(doctype, [word])
		key = _CONCEPT_ALIASES.get(
			normalize_name(word).replace(" ", "_"), normalize_name(word).replace(" ", "_")
		)
		label = CONCEPT_LABELS.get(key) or (
			meta.get_label(fields[0]).lower() if not fields[0].startswith("@") else key.replace("_", " ")
		)
		for fieldname in fields:
			if fieldname == "@items":
				lines.append(_items_text(doctype, doc, subject, extra))
				continue
			if fieldname == "@taxes":
				lines.append(_taxes_text(doctype, doc, subject, extra))
				continue
			if fieldname == "@outstanding":
				total = _party_outstanding(doctype, doc.name, q.get("company"))
				if total is None:
					lines.append(f"{_INVOICE[doctype]} amounts aren't available to your account.")
				elif total["summary"]:
					lines.append(
						f"Outstanding for {subject}: {total['summary']} (submitted {total['invoice']}s you can access)."
					)
					card_rows.append({"label": "Outstanding", "value": total["summary"]})
				else:
					lines.append(
						f"{subject[0].upper() + subject[1:]} has no outstanding amount on submitted {total['invoice']}s you can access."
					)
				continue
			if fieldname not in permitted:
				lines.append(f"The {label} of this {doctype.lower()} isn't available to your account.")
				continue
			value = _value_text(doc, meta, fieldname)
			field_label = label if len(fields) == 1 else meta.get_label(fieldname).lower()
			if fieldname == "outstanding_amount" and doc.docstatus == 0:
				lines.append(f"{subject} is a Draft, so nothing is outstanding until it is submitted.")
			elif value is None:
				lines.append(_missing(doctype, doc, subject, field_label, fieldname))
			else:
				lines.append(f"The {field_label} of {subject} is {value}.")
			card_rows.append(
				{
					"label": meta.get_label(fieldname) if fieldname != "docstatus" else "Document status",
					"value": value or "Not recorded",
				}
			)
	text = lead + "\n".join(line[0].upper() + line[1:] if line else line for line in lines)
	card = {
		"type": "record",
		"title": f"{doctype} {doc.name}",
		"doctype": doctype,
		"name": doc.name,
		"rows": card_rows,
		"action_label": f"Open {doctype}",
		"note": "From your ERPNext data.",
		**extra,
	}
	return {"text": text, "card": card, "names": [doc.name], "titles": [title], "result_doctype": doctype}


def _missing(doctype, doc, subject, label, fieldname):
	text = f"{subject[0].upper() + subject[1:]} was found, but no {label} is recorded."
	if (
		doctype in ("Customer", "Supplier")
		and fieldname in ("mobile_no", "email_id", "phone")
		and frappe.has_permission("Contact", "read")
	):
		contact_fields = [
			f
			for f in ("mobile_no", "phone", "email_id")
			if f == fieldname or (fieldname == "mobile_no" and f == "phone")
		]
		permitted = set(get_permitted_fields("Contact"))
		contact_fields = [f for f in contact_fields if f in permitted]
		contacts = (
			frappe.get_list(
				"Contact",
				filters=[
					["Dynamic Link", "link_doctype", "=", doctype],
					["Dynamic Link", "link_name", "=", doc.name],
				],
				fields=["name", *contact_fields],
				limit=5,
			)
			if contact_fields
			else []
		)
		found = [(c["name"], c.get(f)) for c in contacts for f in contact_fields if c.get(f)]
		if found:
			text += (
				" Linked contact"
				+ ("s: " if len(found) > 1 else ": ")
				+ "; ".join(f"{n}: {v}" for n, v in found[:3])
				+ "."
			)
	return text


def _items_text(doctype, doc, subject, extra):
	rows = doc.get("items") or []
	if not rows:
		return f"{subject} has no item rows."
	spec = REGISTRY[doctype]
	extra["items"] = _child_table(doctype, doc, "items", spec.child[1])
	lines = [f"{subject} has {len(rows)} item row{'s' if len(rows) != 1 else ''}:"]
	for r in rows[:10]:
		amount = (
			f" at {flt(r.get('rate'), 2)} = {flt(r.get('amount'), 2)}"
			if r.get("rate") is not None and "rate" in spec.child[1]
			else ""
		)
		where = "".join(
			f" ({k.replace('_', ' ')}: {r.get(k)})"
			for k in ("warehouse", "s_warehouse", "t_warehouse")
			if r.get(k)
		)
		lines.append(
			f"• {flt(r.get('qty'))} {r.get('uom') or ''} × {r.get('item_name') or r.get('item_code')} ({r.get('item_code')}){amount}{where}"
		)
	if len(rows) > 10:
		lines.append(f"…and {len(rows) - 10} more (see the card).")
	return "\n".join(lines)


def _taxes_text(doctype, doc, subject, extra):
	rows = doc.get("taxes") or []
	currency = doc.get("currency") or ""
	if not rows:
		return f"{subject} has no tax rows (total taxes and charges: {flt(doc.get('total_taxes_and_charges'), 2)} {currency})."
	extra["items"] = _child_table(doctype, doc, "taxes", TAX_FIELDS)
	lines = [f"Tax breakdown of {subject}:"]
	for r in rows[:15]:
		rate = f" ({flt(r.get('rate'), 2)}%)" if flt(r.get("rate")) else ""
		lines.append(
			f"• {r.get('description') or r.get('account_head')}{rate}: {flt(r.get('tax_amount'), 2)} {currency}".rstrip()
		)
	lines.append(
		f"Total taxes and charges: {flt(doc.get('total_taxes_and_charges'), 2)} {currency}; grand total {flt(doc.get('grand_total'), 2)} {currency}."
	)
	return "\n".join(lines)


def _sales_summary(q):
	filters, or_filters, notes = _base_filters(dict(q, docstatus=q["docstatus"] or "submitted", status=None))
	rows = frappe.get_list(
		"Sales Invoice",
		filters=filters,
		or_filters=or_filters or None,
		fields=[
			"company",
			"currency",
			{"SUM": "grand_total", "as": "total"},
			{"SUM": "base_grand_total", "as": "base_total"},
			{"COUNT": "*", "as": "n"},
		],
		group_by="company, currency",
		order_by="company asc",
	)
	period = f"{q['date']['from']} to {q['date']['to']}"
	if not rows:
		return {
			"text": f"I found no {'; '.join(notes)} Sales Invoices for {period}.",
			"names": [],
			"titles": [],
		}
	table_rows = []
	for r in rows:
		company_currency = frappe.get_cached_value("Company", r["company"], "default_currency")
		table_rows.append(
			{
				"company": r["company"],
				"currency": r["currency"],
				"invoices": r["n"],
				"total": flt(r["total"], 2),
				"base_total": f"{flt(r['base_total'], 2)} {company_currency}",
			}
		)
	card = {
		"type": "table",
		"title": f"Sales {period}",
		"doctype": None,
		"columns": [
			{"key": "company", "label": "Company"},
			{"key": "currency", "label": "Invoice currency"},
			{"key": "invoices", "label": "Invoices"},
			{"key": "total", "label": "Total (invoice currency)", "type": "currency"},
			{"key": "base_total", "label": "Total in company currency"},
		],
		"rows": table_rows,
		"note": "From your ERPNext data. Grand totals of "
		+ "; ".join(notes)
		+ " Sales Invoices; credit notes count as negative amounts. "
		"Amounts in different currencies are not added together; the company-currency column uses the exchange rate stored on each invoice.",
		"list_route": {
			"doctype": "Sales Invoice",
			"filters": {"docstatus": 1, "posting_date": ["between", [q["date"]["from"], q["date"]["to"]]]},
		},
	}
	return {
		"text": f"Here is the sales total for {period}, per company and currency.",
		"card": card,
		"names": [],
		"titles": [],
	}


_RETURN_NOTE = "Credit notes (returns) count as negative amounts."
_PARTY = {
	"Customer": ("customer", "customer_name", "credit notes"),
	"Supplier": ("supplier", "supplier_name", "debit notes"),
}


def _outstanding_by_party(q, invoice, party_type):
	"""Net outstanding per customer (Sales Invoices) or supplier (Purchase Invoices) and currency.

	ERPNext keeps outstanding_amount on each submitted invoice current: payments and returns allocated against an invoice
	already reduce it, and a standalone credit/debit note carries a negative outstanding. So the net balance is the sum over
	submitted, non-cancelled invoices and notes, per currency (never converted), within the user's companies. Parties whose
	net is zero are left out."""
	field, title_field, notes = _PARTY[party_type]
	where = q.get("where") or []
	filters = [
		["docstatus", "=", 1],
		["outstanding_amount", "!=", 0],
		*_company_filter(dict(q, doctype=invoice))[0],
		*[[f, o, f"%{v}%" if o == "like" else v] for f, o, v in where],
	]
	rows = frappe.get_list(
		invoice,
		filters=filters,
		fields=[
			field,
			title_field,
			"currency",
			{"SUM": "outstanding_amount", "as": "total"},
			{"COUNT": "*", "as": "n"},
		],
		group_by=f"{field}, {title_field}, currency",
		order_by="total desc",
		limit=max(q["limit"] * 3, 60),
	)
	rows = [r for r in rows if abs(flt(r["total"])) >= 0.005][: q["limit"]]
	label = party_type.lower()
	if not rows:
		return {
			"text": f"No {label} has an outstanding balance on submitted {invoice}s you can access.",
			"names": [],
			"titles": [],
		}
	card = {
		"type": "table",
		"title": f"Outstanding by {label}",
		"doctype": party_type,
		"columns": [
			{"key": "name", "label": party_type, "type": "link"},
			{"key": title_field, "label": "Name"},
			{"key": "currency", "label": "Currency"},
			{"key": "total", "label": "Outstanding", "type": "currency"},
			{"key": "n", "label": "Documents"},
		],
		"rows": [
			{
				"name": r[field],
				title_field: r[title_field],
				"currency": r["currency"],
				"total": flt(r["total"], 2),
				"n": r["n"],
				"_name": r[field],
				"_n": i + 1,
			}
			for i, r in enumerate(rows)
		],
		"note": f"From your ERPNext data: net outstanding of submitted {invoice}s per currency (not converted); payments reduce it and "
		f"standalone {notes} count as negative. Cancelled documents are left out.",
	}
	return {
		"text": f"{len(rows)} {label}{'s have' if len(rows) != 1 else ' has'} outstanding balances.",
		"card": card,
		"names": [r[field] for r in rows],
		"titles": [r[title_field] or r[field] for r in rows],
		"result_doctype": party_type,
		"requires": [invoice],
	}


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
