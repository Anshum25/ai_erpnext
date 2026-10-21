"""Questions about what the chat may query or create. Every answer is the intersection of

1. what ERPNext supports (capability.metadata),
2. the explicit chatbot policy (capability.policy), and
3. what the current user may do (DocType/record/company permissions here; field permlevels via
   frappe.model.get_permitted_fields).

All functions fail closed: an unknown DocType, field, operator or operation raises CapabilityError
(a short, user-safe sentence). Nothing in this module reads or writes records.
"""

import frappe
from frappe.model import get_permitted_fields

from ai_erpnext.capability import metadata as M
from ai_erpnext.capability import policy as P
from ai_erpnext.mapping.normalize import clean_text, parse_date, parse_number

MAX_WHERE = 8
MAX_IN_VALUES = 20
MAX_TEXT = 100
AGGREGATE_FUNCTIONS = ("count", "sum", "avg", "min", "max")
DATE_BUCKETS = ("month", "year")


class CapabilityError(Exception):
	"""A request outside the capability policy. str(error) is safe to show to the user."""


# --------------------------------------------------------------------------------------------
# Query capability
# --------------------------------------------------------------------------------------------
def queryable_doctypes():
	return list(P.REGISTRY)


def is_queryable(doctype):
	"""Listed in the policy, installed, and with a deliberate company policy: either a company field (the user's
	companies apply) or an explicit 'shared' decision. A missing company field never means 'visible to everyone'."""
	if not (isinstance(doctype, str) and doctype in P.REGISTRY and M.doctype_exists(doctype)):
		return False
	return M.has_company_field(doctype) or P.REGISTRY[doctype].shared


def query_spec(doctype):
	if not is_queryable(doctype):
		raise CapabilityError("I can't look that up.")
	return P.REGISTRY[doctype]


def is_prohibited_field(info):
	"""Passwords, secrets, tokens, credentials, attachments and similar are never usable."""
	if info is None:
		return True
	if info.fieldtype in P.PROHIBITED_FIELDTYPES:
		return True
	name = info.fieldname.lower()
	return bool(set(name.split("_")) & P.PROHIBITED_NAME_TOKENS) or any(
		part in name for part in P.PROHIBITED_NAME_PARTS
	)


def refuse_lifecycle(operation):
	"""Lifecycle and raw-access verbs are refused whatever the DocType."""
	if (operation or "").strip().lower() in P.PROHIBITED_QUERY_OPERATIONS:
		raise CapabilityError("I can only read data; I can't change, submit, cancel, delete or pay anything.")


def check_operation(doctype, operation):
	"""The operation is a read the policy allows for the DocType; lifecycle verbs are always refused."""
	op = (operation or "").strip().lower()
	refuse_lifecycle(op)
	spec = query_spec(doctype)
	if op not in spec.operations:
		raise CapabilityError(f"I can't do “{op.replace('_', ' ')}” for {doctype}.")
	return spec


def _policy_fields(spec):
	fields = set(spec.list_fields) | set(spec.detail_fields) | {"name", "creation"}
	fields.update(
		f
		for f in (
			spec.date_field,
			spec.status_field,
			spec.party_field,
			spec.party_title_field,
			spec.amount_field,
		)
		if f
	)
	return fields


def allowed_fields(doctype, permitted_only=True):
	"""Fields of a queryable DocType the chat may expose: policy ∩ metadata ∩ not prohibited ∩ (by default) the user's field permissions."""
	spec = query_spec(doctype)
	listed = [
		f
		for f in dict.fromkeys(
			("name", *spec.list_fields, *spec.detail_fields, *sorted(_policy_fields(spec)))
		)
		if f in _policy_fields(spec)
	]
	fields = [f for f in listed if not is_prohibited_field(M.field_info(doctype, f))]
	if permitted_only:
		permitted = set(get_permitted_fields(doctype)) | {"name", "creation"}
		fields = [f for f in fields if f in permitted]
	return fields


def field_operators(doctype, fieldname):
	"""Operators a filter on this field may use (empty: the field can't be filtered)."""
	info = M.field_info(doctype, fieldname)
	if info is None or fieldname == "docstatus":
		return ()
	if fieldname == "name":
		return ("=", "!=", "in", "like")
	if info.fieldtype in M.TEXT_TYPES:
		return ("=", "!=", "like", "in", "not in")
	if info.fieldtype == "Link":
		return ("=", "!=", "in", "not in", "like")
	if info.fieldtype == "Select":
		return ("=", "!=", "in", "not in")
	if info.fieldtype in M.NUMERIC_TYPES:
		return ("=", "!=", ">", "<", ">=", "<=", "in", "between")
	if info.fieldtype in M.DATE_TYPES:
		return ("=", "!=", ">", "<", ">=", "<=", "between")
	if info.fieldtype == "Check":
		return ("=", "!=")
	return ()


def filterable_fields(doctype):
	return [f for f in allowed_fields(doctype) if field_operators(doctype, f)]


def measure_fields(doctype, date_ok=False):
	"""Numeric fields (and dates, for min/max) an aggregate may use."""
	types = M.NUMERIC_TYPES | (M.DATE_TYPES if date_ok else frozenset())
	return [
		f
		for f in allowed_fields(doctype)
		if f != "docstatus"
		and (M.field_info(doctype, f) or None)
		and M.field_info(doctype, f).fieldtype in types
	]


def group_fields(doctype):
	"""Fields results may be grouped by: Link/Select/Check fields (no free text, so no personal data)."""
	return [
		f
		for f in allowed_fields(doctype)
		if f != "name" and M.field_info(doctype, f).fieldtype in ("Link", "Select", "Check")
	]


def currency_source(doctype, fieldname):
	"""How amounts of a Currency-type field are denominated: None (not a currency amount), ("field", <fieldname>),
	("company", None) for company currency, or ("unknown", None) when the policy can't say, in which case
	the amount must not be summed."""
	info = M.field_info(doctype, fieldname)
	spec = P.REGISTRY[doctype]
	# a field the policy names in currency_map is an amount whatever its field type (ERPNext stores some values as Float)
	if not info or (info.fieldtype != "Currency" and fieldname not in spec.currency_map):
		return None
	source = spec.currency_map.get(fieldname) or spec.currency_field
	if source == "@company":
		return ("company", None)
	if source and M.field_info(doctype, source):
		return ("field", source)
	return ("unknown", None)


def child_tables(doctype):
	"""{table fieldname: allowlisted child fields} reachable through the parent (metadata-checked, field-permission-checked)."""
	spec = query_spec(doctype)
	tables = {}
	if spec.child:
		tables[spec.child[0]] = spec.child[1]
	if spec.taxes:
		tables["taxes"] = P.TAX_FIELDS
	tables.update(spec.extra_children)
	result = {}
	for table, fields in tables.items():
		child = M.child_doctype(doctype, table)
		if not child:
			continue
		permitted = set(get_permitted_fields(child, parenttype=doctype))
		result[table] = tuple(
			f for f in fields if f in permitted and not is_prohibited_field(M.field_info(child, f))
		)
	return result


def child_fields(doctype, table):
	tables = child_tables(doctype)
	if table not in tables:
		raise CapabilityError(f"I can't look into that part of {doctype} records.")
	return list(tables[table])


def link_fields(doctype):
	"""{link field: target DocType} for Link fields whose target is itself queryable (one hop only)."""
	return {
		f: M.link_target(doctype, f)
		for f in allowed_fields(doctype)
		if M.link_target(doctype, f) and is_queryable(M.link_target(doctype, f))
	}


def company_mode(doctype):
	"""'shared' (reviewed master data without a company field: role and User Permissions apply), 'required' (every record
	has a company, the user's companies apply) or 'optional' (records may have none). 'none' means not queryable."""
	if not M.has_company_field(doctype):
		return "shared" if P.REGISTRY.get(doctype) and P.REGISTRY[doctype].shared else "none"
	return "optional" if P.REGISTRY[doctype].optional_company else "required"


# --------------------------------------------------------------------------------------------
# Plan normalisation (model output is untrusted)
# --------------------------------------------------------------------------------------------
OP_SYNONYMS = {
	"==": "=",
	"eq": "=",
	"equals": "=",
	"is": "=",
	"ne": "!=",
	"<>": "!=",
	"neq": "!=",
	"not": "!=",
	"gt": ">",
	"greater": ">",
	"lt": "<",
	"less": "<",
	"gte": ">=",
	"ge": ">=",
	"lte": "<=",
	"le": "<=",
	"contains": "like",
	"like": "like",
	"search": "like",
	"in": "in",
	"not in": "not in",
	"nin": "not in",
	"between": "between",
	"range": "between",
}
FN_SYNONYMS = {
	"total": "sum",
	"average": "avg",
	"mean": "avg",
	"maximum": "max",
	"highest": "max",
	"largest": "max",
	"minimum": "min",
	"lowest": "min",
	"smallest": "min",
	"number": "count",
}
TRUE_WORDS, FALSE_WORDS = ("1", "true", "yes", "y"), ("0", "false", "no", "n")


def _op(raw):
	op = str(raw or "").strip().lower()
	op = OP_SYNONYMS.get(op, op)
	if op not in ("=", "!=", ">", "<", ">=", "<=", "in", "not in", "like", "between"):
		raise CapabilityError("That filter isn't supported.")
	return op


def _scalar(info, doctype, op, value):
	"""One filter value coerced to the field's type (CapabilityError if it doesn't fit)."""
	bad = CapabilityError(
		f"“{value}” isn't a valid value for {info.label}."
		if isinstance(value, (str, int, float))
		else f"That isn't a valid value for {info.label}."
	)
	if (
		isinstance(value, (dict, list, tuple, set))
		or value is None
		or (isinstance(value, bool) and info.fieldtype != "Check")
	):
		raise bad
	t = info.fieldtype
	if t in M.NUMERIC_TYPES:
		number = parse_number(value)
		if number is None:
			raise bad
		return number
	if t in M.DATE_TYPES:
		parsed = parse_date(value)
		if not parsed.ok:
			raise bad
		return str(frappe.utils.getdate(parsed.value))
	if t == "Check":
		word = str(value).strip().lower()
		if word in TRUE_WORDS:
			return 1
		if word in FALSE_WORDS:
			return 0
		raise bad
	text = clean_text(value, MAX_TEXT)
	if not text:
		raise bad
	if t == "Select":
		options = M.select_options(doctype, info.fieldname)
		match = next((o for o in options if o.lower() == text.lower()), None)
		if match is None:
			raise CapabilityError(f"“{text}” isn't one of the options for {info.label}.")
		return match
	return text.replace("%", "")


def normalize_where(doctype, where, table=None):
	"""Validate a list of {"field", "op", "value"} conditions against the policy; returns [[field, op, value], ...].

	With `table`, the conditions are on that allowlisted child table of `doctype`. Unknown fields, fields the
	user can't read, operators the field type doesn't allow and badly typed values raise CapabilityError: a
	condition is never dropped silently, because that would widen the query."""
	if where in (None, [], ()):
		return []
	if not isinstance(where, (list, tuple)) or len(where) > MAX_WHERE:
		raise CapabilityError(f"Please use at most {MAX_WHERE} filters.")
	if table:
		target, allowed = M.child_doctype(doctype, table), child_fields(doctype, table)
	else:
		target, allowed = doctype, filterable_fields(doctype)
	out = []
	for cond in where:
		if (
			isinstance(cond, (list, tuple)) and len(cond) == 3
		):  # already normalised (re-validation of a stored plan)
			cond = {"field": cond[0], "op": cond[1], "value": cond[2]}
		if not isinstance(cond, dict):
			raise CapabilityError("That filter isn't supported.")
		field = clean_text(cond.get("field"), 60)
		if not field or field not in allowed:
			raise CapabilityError(f"I can't filter {doctype} records by “{field or 'that'}”.")
		info = M.field_info(target, field)
		if is_prohibited_field(info):
			raise CapabilityError(f"I can't filter {doctype} records by “{field}”.")
		ops = field_operators(target, field) if not table else _child_operators(info)
		op = _op(cond.get("op"))
		if op not in ops:
			raise CapabilityError(f"“{op}” can't be used on {info.label}.")
		value = cond.get("value")
		if op in ("in", "not in"):
			items = (
				value
				if isinstance(value, (list, tuple))
				else [v for v in str(value or "").split(",") if v.strip()]
			)
			if not items or len(items) > MAX_IN_VALUES:
				raise CapabilityError(f"Please give between 1 and {MAX_IN_VALUES} values for {info.label}.")
			value = [_scalar(info, target, op, v) for v in items]
		elif op == "between":
			if not isinstance(value, (list, tuple)) or len(value) != 2:
				raise CapabilityError(f"“between” needs two values for {info.label}.")
			value = sorted(_scalar(info, target, op, v) for v in value)
		else:
			value = _scalar(info, target, op, value)
			if op == "like" and not value:
				raise CapabilityError(f"Please give a value to search {info.label} for.")
		out.append([field, op, value])
	return out


def _child_operators(info):
	t = info.fieldtype
	if t in M.TEXT_TYPES or t == "Link":
		return ("=", "!=", "like", "in", "not in")
	if t == "Select":
		return ("=", "!=", "in", "not in")
	if t in M.NUMERIC_TYPES:
		return ("=", "!=", ">", "<", ">=", "<=", "in", "between")
	if t in M.DATE_TYPES:
		return ("=", "!=", ">", "<", ">=", "<=", "between")
	if t == "Check":
		return ("=", "!=")
	return ()


def normalize_aggregate(doctype, agg):
	"""{"fn", "field", "group_by"} validated against the policy, or CapabilityError."""
	if not isinstance(agg, dict):
		raise CapabilityError("I couldn't understand which calculation you want.")
	check_operation(doctype, "aggregate")
	fn = str(agg.get("fn") or "count").strip().lower()
	fn = FN_SYNONYMS.get(fn, fn)
	if fn not in AGGREGATE_FUNCTIONS:
		raise CapabilityError("I can count, total, average, and find the smallest or largest value.")
	field = clean_text(agg.get("field"), 60)
	if fn != "count":
		allowed = measure_fields(doctype, date_ok=fn in ("min", "max"))
		if not field or field not in allowed:
			raise CapabilityError(
				f"I can't calculate the {fn if fn != 'avg' else 'average'} of “{field or 'that'}” for {doctype} records."
			)
		if (
			fn in ("sum", "avg", "min", "max")
			and (currency_source(doctype, field) or ("", None))[0] == "unknown"
		):
			raise CapabilityError(
				f"I can't {fn} {M.field_info(doctype, field).label} safely: its currency isn't recorded on the record."
			)
	else:
		field = None
	group_by = clean_text(agg.get("group_by"), 60) if agg.get("group_by") else None
	period = clean_text(agg.get("period"), 20) if agg.get("period") else None
	if group_by in DATE_BUCKETS:  # "group by month" is the period, with no other grouping
		if period and period != group_by:
			raise CapabilityError("Please group by one period only (month or year).")
		group_by, period = None, group_by
	if group_by and group_by not in group_fields(doctype):
		raise CapabilityError(f"I can't group {doctype} records by “{group_by}”.")
	if period and (period not in DATE_BUCKETS or not P.REGISTRY[doctype].date_field):
		raise CapabilityError(f"I can't group {doctype} records by “{period}”.")
	return {"fn": fn, "field": field, "group_by": group_by, "period": period}


def normalize_child(doctype, child):
	"""{"table", "where", "fields"} for a query into an allowlisted child table of the parent, or CapabilityError."""
	if not isinstance(child, dict):
		raise CapabilityError("I couldn't understand which rows you want.")
	table = clean_text(child.get("table"), 60)
	allowed = child_fields(doctype, table)
	fields = [
		clean_text(f, 60) for f in (child.get("fields") if isinstance(child.get("fields"), list) else [])[:10]
	]
	bad = [f for f in fields if f and f not in allowed]
	if bad:
		raise CapabilityError(f"I can't show {', '.join(bad[:3])} from those rows.")
	return {
		"table": table,
		"where": normalize_where(doctype, child.get("where"), table=table),
		"fields": [f for f in dict.fromkeys(fields) if f] or [f for f in allowed[:6]],
	}


def normalize_link(doctype, link):
	"""{"field", "target", "fields"}: one hop through an allowlisted Link field to a queryable DocType."""
	if not isinstance(link, dict):
		raise CapabilityError("I couldn't understand which related record you want.")
	links = link_fields(doctype)
	field = clean_text(link.get("field"), 60)
	if field not in links:
		raise CapabilityError(f"I can't follow “{field or 'that'}” from {doctype} records.")
	target = links[field]
	target_fields = [f for f in allowed_fields(target) if f in P.REGISTRY[target].list_fields and f != "name"]
	wanted = [
		clean_text(f, 60) for f in (link.get("fields") if isinstance(link.get("fields"), list) else [])[:6]
	]
	bad = [f for f in wanted if f and f not in target_fields]
	if bad:
		raise CapabilityError(f"I can't show {', '.join(bad[:3])} of the related {target}.")
	return {
		"field": field,
		"target": target,
		"fields": [f for f in dict.fromkeys(wanted) if f] or target_fields[:3],
	}


# --------------------------------------------------------------------------------------------
# Creation capability (separate from query capability; Draft documents only)
# --------------------------------------------------------------------------------------------
ROW_FIELDS = frozenset(
	{"item_code", "qty", "rate", "warehouse", "schedule_date", "delivery_date", "s_warehouse", "t_warehouse"}
)
DOC_KEYS = frozenset({"doctype", "items", "set_posting_time"})


def creatable_doctypes():
	from ai_erpnext.chat import forms

	return tuple(forms.CREATABLE)


def is_creatable(doctype):
	return isinstance(doctype, str) and doctype in creatable_doctypes() and M.doctype_exists(doctype)


def creation_fields(doctype, extra=()):
	"""Top-level fields a chat-created `doctype` may set: the guided header fields, fixed values, and
	(per form) fields ERPNext made mandatory and the chat asked for."""
	from ai_erpnext.chat import forms

	if not is_creatable(doctype):
		raise CapabilityError(f"I can't create {doctype if isinstance(doctype, str) else 'that'}.")
	spec = forms.SPECS[doctype]
	names = {f["fieldname"] for f in spec["header"]} | set(spec.get("fixed", {})) | set(extra)
	return sorted(f for f in names if not _protected(doctype, f))


def _protected(doctype, fieldname):
	if fieldname in P.PROTECTED_CREATE_FIELDS:
		return True
	info = M.field_info(doctype, fieldname)
	return info is None or is_prohibited_field(info)


def reject_protected(doc):
	"""Lifecycle/ownership fields (docstatus, owner, workflow_state ...) are never set from chat input."""
	for key in doc:
		if key in P.PROTECTED_CREATE_FIELDS and key != "doctype":
			raise CapabilityError("I can't set that field when creating a record.")


def assert_creation_safe(doctype, doc, extra=()):
	"""Fail closed before insert(): only creatable DocTypes, only allowlisted fields, never lifecycle or
	ownership fields (docstatus, owner, workflow_state ...). A model-supplied value cannot authorise anything."""
	allowed = set(creation_fields(doctype, extra)) | DOC_KEYS
	for key in doc:
		if (key in P.PROTECTED_CREATE_FIELDS and key != "doctype") or key not in allowed:
			raise CapabilityError("I can't set that field when creating a record.")
	if doc.get("doctype") != doctype:
		raise CapabilityError("I can't create that.")
	for row in doc.get("items") or []:
		if not isinstance(row, dict) or set(row) - ROW_FIELDS:
			raise CapabilityError("I can't set that field on an item row.")
	return True
