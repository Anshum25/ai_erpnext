"""HR (HRMS) support for the chat: the user's own employee, leave balances, attendance and Leave Application drafts.

Leave and attendance need the HRMS app. Without it the chat says so instead of guessing; Employee itself is ERPNext's
and works either way.

Read-only questions are two query operations on the Employee policy (capability/policy.py), validated and dispatched by
chat/queries.py like every other plan:
- leave_balance: HRMS's own get_leave_details, the figures the Leave Application form shows. Allocations come from the
  Leave Ledger; "taken" counts only approved, submitted applications (HRMS writes ledger entries for those alone), so
  open (pending) and rejected applications are never deducted. Pending days are reported separately.
- attendance: submitted Attendance records of one employee in a bounded period, read with get_list.

Security:
- Data is only shown for exactly one employee, resolved on the server with permission-aware, company-scoped queries.
  "my" means the Employee whose user_id is the logged-in user; names and IDs from the model are only search terms.
  Several matches become a question, never a choice made here.
- HRMS re-checks access to leave balances (the employee themself, their leave approver or Employee read permission);
  attendance needs Attendance read permission and the User Permissions get_list applies.
- Leave Applications are created by the guided form (chat/forms.py) as Drafts with status Open. Status, docstatus and
  approval are never set from the chat. HRMS's own validate() runs on an unsaved copy before the preview, inside a
  savepoint that is always rolled back.
"""

import calendar
import copy
import datetime
import json
import re

import frappe
from frappe.utils import add_days, flt, getdate, nowdate, strip_html

from ai_erpnext.capability import metadata as M
from ai_erpnext.mapping.normalize import clean_text, normalize_name, parse_date

OPERATIONS = ("leave_balance", "attendance")
DOCTYPES = ("Leave Application", "Expense Claim")  # creatable HR DocTypes (they need HRMS)
NOT_INSTALLED = "Leave and attendance data come from the HRMS app, which isn't installed on this site."
NO_EMPLOYEE = (
	"Your user account isn't linked to an Employee record that you can access, so I don't know who “my” is. "
	"Ask HR to set your user on your Employee record, or tell me the employee's name or ID."
)
MAX_ATTENDANCE_ROWS = 62
MAX_PERIOD_DAYS = 366


def installed():
	return "hrms" in frappe.get_installed_apps() and M.doctype_exists("Leave Application")


# ------------------------------------------------------------------------------------------------
# Employees
# ------------------------------------------------------------------------------------------------
def my_employees():
	"""Employee records linked to the logged-in user that the user may read, active ones first."""
	from ai_erpnext.chat import queries

	if frappe.session.user == "Guest":
		return []
	try:
		rows = frappe.get_list(
			"Employee",
			filters=[["user_id", "=", frappe.session.user], *queries._scope("Employee")],
			fields=["name", "employee_name", "status"],
			order_by="creation asc",
			limit=5,
		)
	except frappe.PermissionError:
		frappe.local.message_log = []
		return []
	return sorted(rows, key=lambda r: r.status != "Active")


def default_employee():
	"""The user's own active employee when there is exactly one (a form default), else None."""
	rows = [r for r in my_employees() if r.status == "Active"]
	return rows[0].name if len(rows) == 1 else None


def employee_label(name):
	title = frappe.db.get_value("Employee", name, "employee_name") if name else None
	return f"{title} ({name})" if title and title != name else name


def self_employee(q):
	"""The logged-in user's Employee for a "my ..." question. Raises QueryError, or QueryClarify when several are linked."""
	from ai_erpnext.chat import queries

	rows = my_employees()
	active = [r for r in rows if r.status == "Active"] or rows
	if not active:
		raise queries.QueryError(NO_EMPLOYEE)
	if len(active) > 1:
		raise queries.QueryClarify(
			"Your user is linked to more than one employee. Which one do you mean?",
			kind="pick",
			plan=q,
			options=[
				queries._choice("Employee", {"name": r.name, "title": r.employee_name or r.name})
				for r in active
			],
		)
	return active[0].name


def _target(q):
	"""(employee, label) the plan is about, or a not-found result dict. Several matches raise QueryClarify (kind "pick")."""
	from ai_erpnext.chat import queries

	if q.get("pick") and q["pick"][0] != "Employee":
		raise queries.QueryError("You don't have permission to view that.")
	if q.get("pick") or q.get("name") or q.get("search"):
		resolved = queries._resolve_target(dict(q, auto=False), q.get("name") or q.get("search"))
		if isinstance(resolved, dict):
			return resolved
		_doctype, name, _exact = resolved
	else:
		name = self_employee(q)
	return name, employee_label(name)


# ------------------------------------------------------------------------------------------------
# Validation of query plans (called by queries.validate for the operations above)
# ------------------------------------------------------------------------------------------------
def validate(plan, q, filters):
	from ai_erpnext.chat import queries

	if not installed():
		raise queries.QueryError(NOT_INSTALLED)
	# No name or ID means the user's own employee; a name given in the same plan wins over "self".
	q["self"] = not (q["name"] or q["search"])
	q["leave_type"] = (
		clean_text(filters.get("leave_type"), 140) if q["operation"] == "leave_balance" else None
	)
	date = plan.get("date") if isinstance(plan.get("date"), dict) else None
	q["date"] = period(date, default="this_month" if q["operation"] == "attendance" else None)
	q.update(where=[], aggregate=None, child=None, link=None, overdue_days=None, fields=None, want=None)
	return q


def period(date, default=None):
	"""{"from", "to", "label"} of a plan's date part (a preset or from/to), the default preset, or None."""
	from ai_erpnext.chat import queries

	if not date and default:
		date = {"preset": default}
	if not date:
		return None
	preset = date.get("preset")
	if preset in queries.DATE_PRESETS:
		start, end = queries._preset_range(preset)
	else:
		start, end = parse_date(date.get("from")), parse_date(date.get("to") or date.get("from"))
		if not (start.ok and end.ok):
			raise queries.QueryClarify(
				"Which dates exactly? Please give them as YYYY-MM-DD, for example 2026-09-01 to 2026-09-30."
			)
		start, end = getdate(start.value), getdate(end.value)
	start, end = sorted((getdate(start), getdate(end)))
	if (end - start).days >= MAX_PERIOD_DAYS:
		raise queries.QueryError("Please choose a period of at most one year.")
	label = date.get("label") or (preset.replace("_", " ") if preset in queries.DATE_PRESETS else None)
	return {"from": str(start), "to": str(end), "label": label}


def execute(q):
	return leave_balance(q) if q["operation"] == "leave_balance" else attendance(q)


# ------------------------------------------------------------------------------------------------
# Leave balance
# ------------------------------------------------------------------------------------------------
def _num(value):
	return f"{flt(value, 2):g}"


def _as_of(q):
	"""The date a balance is reported on: today, or the requested period's end (past) or start (future)."""
	today = getdate(nowdate())
	if not q.get("date"):
		return today
	start, end = getdate(q["date"]["from"]), getdate(q["date"]["to"])
	return start if today < start else (end if today > end else today)


def _leave_types():
	try:
		return frappe.get_list("Leave Type", pluck="name", order_by="name asc", limit=100)
	except frappe.PermissionError:
		frappe.local.message_log = []
		return []


def match_leave_types(text, names):
	"""Leave types `text` names: exactly ("Casual Leave", "casual leave"), else by its words ("casual", "sick leaves")."""
	wanted = normalize_name(text)
	exact = [n for n in names if normalize_name(n) == wanted]
	if exact:
		return exact
	words = {re.sub(r"s$", "", w) for w in wanted.split()} - {"leave", "day", "holiday"}
	if not words:
		return []
	return [n for n in names if words <= {re.sub(r"s$", "", w) for w in normalize_name(n).split()}]


def leave_balance(q):
	from hrms.hr.doctype.leave_application.leave_application import (
		get_leave_allocation_records,
		get_leave_details,
	)

	from ai_erpnext.chat import queries

	target = _target(q)
	if isinstance(target, dict):
		return target
	employee, who = target
	as_of = _as_of(q)
	wanted = None
	if q.get("leave_type"):
		found = match_leave_types(q["leave_type"], _leave_types())
		if not found:
			raise queries.QueryError(
				f"There's no leave type matching “{q['leave_type']}”. Leave types: {', '.join(_leave_types()) or 'none you can see'}."
			)
		if len(found) > 1:
			raise queries.QueryError(
				f"“{q['leave_type']}” matches several leave types: {', '.join(found)}. Please name one."
			)
		wanted = found[0]
	try:
		details = get_leave_details(employee, as_of)
	except frappe.PermissionError:
		frappe.local.message_log = []
		raise queries.QueryError("You don't have permission to see this employee's leave balance.") from None
	balances = details.get("leave_allocation") or {}
	periods = get_leave_allocation_records(employee, as_of)
	result = {"names": [employee], "titles": [who], "result_doctype": "Employee"}
	date_text = f"as on {as_of}" + (
		f" ({q['date']['label']})" if q.get("date") and q["date"]["label"] else ""
	)
	if wanted:
		if wanted not in balances:
			lwp = frappe.db.get_value("Leave Type", wanted, "is_lwp")
			reason = (
				f"{wanted} is leave without pay, so it has no balance."
				if lwp
				else f"{who} has no {wanted} allocation covering {as_of}, so I can't calculate a balance for it. "
				"HR allocates leave with a Leave Allocation or a Leave Policy Assignment."
			)
			return dict(result, text=reason)
		balances = {wanted: balances[wanted]}
	if not balances:
		return dict(
			result,
			text=f"{who} has no leave allocations covering {as_of}, so there is no leave balance to show. "
			"HR allocates leave with a Leave Allocation or a Leave Policy Assignment.",
		)
	rows = []
	for leave_type, b in sorted(balances.items()):
		alloc = periods.get(leave_type) or {}
		rows.append(
			{
				"leave_type": leave_type,
				"period": f"{alloc.get('from_date')} to {alloc.get('to_date')}" if alloc else "",
				"allocated": flt(b["total_leaves"], 2),
				"taken": flt(b["leaves_taken"], 2),
				"pending": flt(b["leaves_pending_approval"], 2),
				"expired": flt(b["expired_leaves"], 2),
				"remaining": flt(b["remaining_leaves"], 2),
			}
		)
	note = (
		f"From HRMS, {date_text}: the same figures as the Leave Application form. Taken counts approved, submitted leave "
		"in the allocation period (including approved leave still to come); applications pending approval are shown "
		"separately and are not deducted; rejected applications never count."
	)
	if len(rows) == 1:
		r = rows[0]
		text = (
			f"{who} has {_num(r['remaining'])} day(s) of {r['leave_type']} remaining {date_text}, "
			f"for the allocation period {r['period']}: {_num(r['allocated'])} allocated, {_num(r['taken'])} taken"
			+ (f", {_num(r['expired'])} expired" if r["expired"] else "")
			+ "."
			+ (
				f" {_num(r['pending'])} more day(s) are pending approval and not deducted yet."
				if r["pending"]
				else ""
			)
		)
	else:
		text = f"Leave balance of {who} {date_text}, per leave type:"
	card = {
		"type": "table",
		"title": f"Leave balance: {who}",
		"doctype": None,
		"source_doctype": "Employee",
		"columns": [
			{"key": "leave_type", "label": "Leave type"},
			{"key": "period", "label": "Allocation period"},
			{"key": "allocated", "label": "Allocated"},
			{"key": "taken", "label": "Taken (approved)"},
			{"key": "pending", "label": "Pending approval"},
			{"key": "expired", "label": "Expired"},
			{"key": "remaining", "label": "Remaining"},
		],
		"rows": rows,
		"note": note,
	}
	return dict(result, text=text, card=card)


# ------------------------------------------------------------------------------------------------
# Attendance
# ------------------------------------------------------------------------------------------------
def attendance(q):
	from ai_erpnext.chat import queries

	if not frappe.has_permission("Attendance", "read"):
		raise queries.QueryError("You don't have permission to view attendance records.")
	target = _target(q)
	if isinstance(target, dict):
		return target
	employee, who = target
	d = q["date"]
	base = [
		["employee", "=", employee],
		["attendance_date", "between", [d["from"], d["to"]]],
		*queries._scope("Attendance"),
	]
	counts = frappe.get_list(
		"Attendance",
		filters=[*base, ["docstatus", "=", 1]],
		fields=["status", {"COUNT": "*", "as": "n"}],
		group_by="status",
		order_by="status asc",
	)
	drafts = frappe.get_list("Attendance", filters=[*base, ["docstatus", "=", 0]], pluck="name", limit=500)
	fields = queries._permitted(
		"Attendance", ["name", "attendance_date", "status", "leave_type", "late_entry", "early_exit"]
	)
	rows = frappe.get_list(
		"Attendance",
		filters=[*base, ["docstatus", "=", 1]],
		fields=fields,
		order_by="attendance_date asc",
		limit=MAX_ATTENDANCE_ROWS + 1,
	)
	span = f"from {d['from']} to {d['to']}" + (f" ({d['label']})" if d["label"] else "")
	result = {
		"names": [employee],
		"titles": [who],
		"result_doctype": "Employee",
		"requires": ["Attendance"],
	}
	draft_note = (
		f" {len(drafts)} draft attendance record(s) in this period are not counted until they are submitted."
		if drafts
		else ""
	)
	if not counts:
		return dict(result, text=f"No submitted attendance is recorded for {who} {span}.{draft_note}")
	summary = ", ".join(f"{r['n']} {r['status']}" for r in counts)
	more = len(rows) > MAX_ATTENDANCE_ROWS
	rows = rows[:MAX_ATTENDANCE_ROWS]
	columns = [
		{
			"key": f,
			"label": frappe.get_meta("Attendance").get_label(f),
			**({"type": "date"} if f == "attendance_date" else {}),
		}
		for f in fields
		if f != "name"
	]
	card = {
		"type": "table",
		"title": f"Attendance: {who}",
		"doctype": None,
		"source_doctype": "Employee",
		"columns": columns,
		"rows": [
			{
				f: (
					("Yes" if r.get(f) else "")
					if f in ("late_entry", "early_exit")
					else queries._jsonable(r.get(f))
				)
				for f in fields
				if f != "name"
			}
			for r in rows
		],
		"note": "From your ERPNext data. Only submitted attendance is counted."
		+ (
			f" Showing the first {MAX_ATTENDANCE_ROWS} days; ask for a shorter period to see the rest."
			if more
			else ""
		),
	}
	return dict(
		result,
		text=f"Attendance of {who} {span}: {summary} (submitted records).{draft_note}",
		card=card,
	)


# ------------------------------------------------------------------------------------------------
# Leave Application drafts (the guided form in chat/forms.py calls these)
# ------------------------------------------------------------------------------------------------
SELF_WORDS = {"me", "myself", "i", "self", "my self", "mine"}


def form_hints(form, values):
	"""Values for a new Leave Application that aren't plain fields: "days" (a duration the user named, offered as a
	to-date choice) and "me" as the employee (the default employee applies)."""
	values = dict(values or {})
	days = values.pop("days", None)
	try:
		days = int(flt(days)) if days not in (None, "") else None
	except (TypeError, ValueError):
		days = None
	if days and 1 <= days <= 366:
		form["days"] = days
	if normalize_name(str(values.get("employee") or "")) in SELF_WORDS:
		values.pop("employee")
	return values


def derived_values(values):
	"""Fields HRMS's own form fills from the employee: the leave approver (needed when HR Settings make it mandatory)."""
	from hrms.hr.doctype.leave_application.leave_application import get_employee_leave_approver

	approver = get_employee_leave_approver(values["employee"]) if values.get("employee") else None
	return {"leave_approver": approver} if approver else {}


def to_date_options(form):
	values = form["values"]
	if not values.get("from_date"):
		return []
	start = values["from_date"]
	options = []
	if form.get("days") and form["days"] > 1:
		end = add_days(start, form["days"] - 1)
		options.append({"label": f"{end} ({form['days']} calendar days from {start})", "value": str(end)})
	options.append({"label": f"Same day ({start})", "value": start})
	return options


def precheck(doc_dict):
	"""HRMS's validation of a Leave Application on an unsaved copy, before the preview.

	Returns {"ok": True, "total_leave_days", "balance", "company", "approver", "notes"} or {"ok": False, "message"}.
	Runs the same checks insert() runs (links, permissions, validate()) inside a savepoint that is always rolled back,
	so nothing is written; insert() remains the final authority."""
	from hrms.hr.doctype.leave_application.leave_application import get_leave_balance_on, is_lwp

	from ai_erpnext.chat import queries, requirements

	frappe.local.message_log = []
	frappe.db.savepoint("ai_chat_leave_check")
	try:
		doc = frappe.get_doc(copy.deepcopy(doc_dict))
		doc.docstatus = 0
		doc._action = "save"
		doc._set_defaults()
		doc._validate_links()  # also fetches company and department from the employee
		if doc.company not in queries.usable_companies("Leave Application"):
			return {
				"ok": False,
				"message": "This employee belongs to a company you can't create leave applications for.",
			}
		if not frappe.has_permission("Leave Application", "create", doc):
			return {
				"ok": False,
				"message": "You don't have permission to create a leave application for this employee.",
			}
		doc.run_method("before_validate")
		doc.run_method("validate")
		balance = (
			None
			if is_lwp(doc.leave_type)
			else get_leave_balance_on(
				doc.employee,
				doc.leave_type,
				doc.from_date,
				doc.to_date,
				consider_all_leaves_in_the_allocation_period=True,
			)
		)
		return {
			"ok": True,
			"total_leave_days": flt(doc.total_leave_days, 2),
			"balance": None if balance is None else flt(balance, 2),
			"company": doc.company,
			"approver": doc.leave_approver_name or doc.leave_approver or None,
			"notes": _notes(),
		}
	except Exception as e:
		return {"ok": False, "message": requirements.explain(e, "Leave Application")["message"]}
	finally:
		frappe.db.rollback(save_point="ai_chat_leave_check")
		frappe.local.message_log = []


def _notes():
	"""Warnings HRMS showed while validating (e.g. a blocked day, negative balance allowed), as plain text."""
	notes = []
	for raw in frappe.local.message_log or []:
		try:
			msg = json.loads(raw) if isinstance(raw, str) else raw
		except ValueError:
			msg = {"message": raw}
		text = strip_html(str((msg or {}).get("message") if isinstance(msg, dict) else msg)).strip()
		if text:
			notes.append(text[:300])
	return notes[:3]


def preview_rows(check):
	rows = [{"label": "Leave days (HRMS)", "value": _num(check["total_leave_days"])}]
	if check.get("balance") is not None:
		rows.append({"label": "Balance before this application", "value": _num(check["balance"])})
	if check.get("approver"):
		rows.append({"label": "Leave approver", "value": check["approver"]})
	rows.append({"label": "Status", "value": "Open (Draft, awaiting approval)"})
	return rows


def created_rows(doc):
	return [
		{"label": "ID", "value": doc.name},
		{"label": "Employee", "value": f"{doc.employee_name} ({doc.employee})"},
		{"label": "Leave type", "value": doc.leave_type},
		{"label": "Dates", "value": f"{doc.from_date} to {doc.to_date}"},
		{"label": "Leave days", "value": _num(doc.total_leave_days)},
		{"label": "Status", "value": f"Draft, {doc.status}"},
	]


# ------------------------------------------------------------------------------------------------
# Deterministic phrasings (chat/shortcuts.py): routed without an AI call
# ------------------------------------------------------------------------------------------------
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_PERIOD = (
	r"(?:this|last|previous|current)\s+(?:week|month|quarter|year)|today|yesterday|last\s+(?:7|30)\s+days"
	r"|(?:" + _MONTH_ALT + r")(?:\s+\d{4})?"
)
_PERIOD_RE = re.compile(r"^(?:the\s+)?(?:month\s+of\s+)?(?P<p>" + _PERIOD + r")$", re.I)
_TAIL = r"(?:\s+(?:for|in|during|of)\s+(?:the\s+)?(?P<period>" + _PERIOD + r"))?\s*[?.!]*\s*$"
_ASK = r"^\s*(?:please\s+)?(?:(?:show|check|get|tell|give|display|list|what\s+is|what's|whats|what\s+are)\s+(?:me\s+)?)?(?:the\s+)?"

LEAVE_BALANCE_RE = re.compile(
	_ASK + r"(?:(?P<my>my)\s+)?(?:(?P<type>[a-z]+(?:\s+[a-z]+)?)\s+)?leaves?\s+balances?"
	r"(?:\s+(?:for|of)\s+(?P<target>.+?))?" + _TAIL,
	re.I,
)
POSSESSIVE_BALANCE_RE = re.compile(
	_ASK + r"(?P<target>.+?)['’]s?\s+leaves?\s+balances?" + _TAIL,
	re.I,
)
HOW_MANY_RE = re.compile(
	r"^\s*(?:please\s+)?how\s+many\s+(?:(?P<type>[a-z]+(?:\s+[a-z]+)?)\s+)?leaves?(?:\s+days?)?"
	r"(?:\s+(?:do|have|does|did)\s+(?P<target>i|.+?)\s+(?:have|got|get)(?:\s+left)?|\s+(?:are|is)(?:\s+still)?)?"
	r"(?:\s+(?:left|remaining|available|pending))?(?:\s+(?:for|of)\s+(?P<target2>.+?))?" + _TAIL,
	re.I,
)
ATTENDANCE_RE = re.compile(
	_ASK + r"(?:(?P<my>my)\s+)?attendance(?:\s+(?:records?|summary|report|details))?"
	r"(?:\s+(?:for|of)\s+(?P<target>.+?))?" + _TAIL,
	re.I,
)
POSSESSIVE_ATTENDANCE_RE = re.compile(
	_ASK + r"(?P<target>.+?)['’]s?\s+attendance(?:\s+(?:records?|summary|report|details))?" + _TAIL,
	re.I,
)
MY_EMPLOYEE_RE = re.compile(
	_ASK + r"my\s+employee(?:\s+(?:details|record|profile|info|information|data|id))?\s*[?.!]*\s*$", re.I
)
EMPLOYEE_DETAILS_RE = re.compile(
	r"^\s*(?:please\s+)?(?:show|get|give|display|find)(?:\s+me)?\s+(?:the\s+)?employee\s+(?:details|info|information|record|profile)"
	r"\s+(?:of|for|about)\s+(?P<t>.+?)\s*[?.!]*\s*$",
	re.I,
)
FIND_EMPLOYEE_RE = re.compile(
	r"^\s*(?:please\s+)?(?:find|search(?:\s+for)?|look\s*up|show(?:\s+me)?|get)\s+(?:the\s+)?employee\s+(?:named\s+|called\s+)?(?P<t>.+?)\s*[?.!]*\s*$",
	re.I,
)
WHO_EMPLOYEE_RE = re.compile(r"^\s*who\s+is\s+(?:the\s+)?employee\s+(?P<t>.+?)\s*[?.!]*\s*$", re.I)
# "apply for leave": the Leave Application form (group 1 is the name forms.match_doctype knows).
APPLY_RE = re.compile(
	r"^\s*(?:please\s+)?(?:(?:i\s+)?(?:want|need|would\s+like|wish)\s+to\s+)?(?:apply|request)\s+(?:for\s+)?(?:a\s+|an\s+|some\s+)?(leave)s?\b",
	re.I,
)
# Any leave request, used only to open the form when the AI service is down ("apply for two days of casual leave").
LEAVE_REQUEST_RE = re.compile(r"\b(?:apply|request|take|book)\b.*\bleaves?\b", re.I)
_FILLER = {
	"remaining",
	"current",
	"available",
	"total",
	"full",
	"complete",
	"the",
	"our",
	"all",
	"employee",
	"employees",
}
_NOT_NAMES = {
	"details",
	"detail",
	"list",
	"records",
	"record",
	"all",
	"info",
	"information",
	"data",
	"profile",
}


def _clean_target(text):
	"""The employee a phrase names: "anshum named employee" / "employee named Anshum" / "the employee Anshum" -> "anshum"."""
	text = (text or "").strip(" .?!\"'“”")
	text = re.sub(r"^(?:the\s+)?employees?\s+(?:named\s+|called\s+)?", "", text, flags=re.I)
	text = re.sub(r"\s+(?:named|called)?\s*employees?$", "", text, flags=re.I)
	text = re.sub(r"^(?:named|called)\s+", "", text, flags=re.I)
	return text.strip(" .?!\"'“”") or None


def _period_plan(text):
	"""A plan "date" part for a period phrase ("this month", "October", "October 2025"), or None."""
	m = _PERIOD_RE.match((text or "").strip())
	if not m:
		return None
	p = re.sub(r"\s+", " ", m.group("p").lower())
	p = re.sub(r"^(?:previous|current)\b", lambda x: "last" if x.group(0) == "previous" else "this", p)
	preset = p.replace(" ", "_")
	if preset in (
		"this_week",
		"last_week",
		"this_month",
		"last_month",
		"this_quarter",
		"last_quarter",
		"this_year",
		"last_year",
		"today",
		"yesterday",
		"last_7_days",
		"last_30_days",
	):
		return {"preset": preset}
	parts = p.split()
	month = _MONTHS.get(parts[0])
	if not month:
		return None
	today = getdate(nowdate())
	year = int(parts[1]) if len(parts) > 1 else (today.year if month <= today.month else today.year - 1)
	last = calendar.monthrange(year, month)[1]
	return {
		"from": str(datetime.date(year, month, 1)),
		"to": str(datetime.date(year, month, last)),
		"label": f"{calendar.month_name[month]} {year}",
	}


def _hr_query(operation, target, period_text, leave_type=None):
	filters = {}
	if target:
		cleaned = _clean_target(target)
		if not cleaned:
			return None
		if normalize_name(cleaned) in SELF_WORDS:
			filters["self"] = True
		else:
			filters["search"] = cleaned
	else:
		filters["self"] = True
	if leave_type:
		filters["leave_type"] = leave_type
	query = {"doctype": "Employee", "operation": operation, "filters": filters}
	if period_text:
		date = _period_plan(period_text)
		if not date:
			return None
		query["date"] = date
	return {"mode": "query", "query": query}


def _split_period(target, period_text):
	"""“attendance for October”: a target that is really a period."""
	if target and not period_text and _period_plan(target):
		return None, target
	return target, period_text


def match(text):
	"""A plan for an HR question the wording makes unambiguous, or None (the router model decides)."""
	text = text or ""
	for regex in (LEAVE_BALANCE_RE, HOW_MANY_RE, POSSESSIVE_BALANCE_RE):
		m = regex.match(text)
		if m:
			g = m.groupdict()
			leave_type = (g.get("type") or "").strip()
			if leave_type.lower() in _FILLER:
				leave_type = None
			target = g.get("target") or g.get("target2")
			if target and target.strip().lower() == "i":
				target = None
			target, period_text = _split_period(target, g.get("period"))
			return _hr_query("leave_balance", target, period_text, leave_type or None)
	for regex in (ATTENDANCE_RE, POSSESSIVE_ATTENDANCE_RE):
		m = regex.match(text)
		if m:
			target, period_text = _split_period(m.groupdict().get("target"), m.group("period"))
			return _hr_query("attendance", target, period_text)
	if MY_EMPLOYEE_RE.match(text):
		return {
			"mode": "query",
			"query": {"doctype": "Employee", "operation": "details", "filters": {"self": True}},
		}
	m = EMPLOYEE_DETAILS_RE.match(text) or WHO_EMPLOYEE_RE.match(text) or FIND_EMPLOYEE_RE.match(text)
	if m:
		target = _clean_target(m.group("t"))
		if not target or target.lower() in _NOT_NAMES:
			return None
		filters = {"self": True} if normalize_name(target) in SELF_WORDS else {"search": target}
		return {"mode": "query", "query": {"doctype": "Employee", "operation": "details", "filters": filters}}
	return None
