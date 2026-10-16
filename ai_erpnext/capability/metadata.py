"""What ERPNext supports for a DocType, read from Frappe's metadata (frappe.get_meta).

This layer only describes. It never grants: capability.policy decides what the chat may use and
capability.service intersects the two with the current user's permissions.

Caching: frappe.get_meta is already cached by Frappe and invalidated when a DocType or Custom Field
changes. The small summaries built here are cached on frappe.local, i.e. for one request/job only,
so a changed DocType can never be served stale across requests and nothing new is stored in the database.
"""

from dataclasses import dataclass

import frappe

NUMERIC_TYPES = frozenset({"Int", "Float", "Currency", "Percent"})
DATE_TYPES = frozenset({"Date", "Datetime"})
TEXT_TYPES = frozenset({"Data", "Small Text", "Text"})
LINK_TYPES = frozenset({"Link"})
SELECT_TYPES = frozenset({"Select"})
# Standard columns every DocType has that are not in meta.fields.
STANDARD = {"name": "Data", "creation": "Datetime", "modified": "Datetime", "docstatus": "Int"}


@dataclass(frozen=True)
class FieldInfo:
	fieldname: str
	fieldtype: str
	label: str
	options: str | None
	reqd: bool
	read_only: bool
	hidden: bool
	permlevel: int


def _cache():
	if not hasattr(frappe.local, "ai_capability_meta"):
		frappe.local.ai_capability_meta = {}
	return frappe.local.ai_capability_meta


def doctype_exists(doctype):
	"""True if the DocType is installed (metadata lookup; reveals nothing about records)."""
	if not isinstance(doctype, str) or not doctype or len(doctype) > 140:
		return False
	cache = _cache().setdefault("exists", {})
	if doctype not in cache:
		cache[doctype] = bool(frappe.db.exists("DocType", doctype))
	return cache[doctype]


def field_info(doctype, fieldname):
	"""FieldInfo for a field (standard columns included), or None if the DocType has no such field."""
	if not doctype_exists(doctype):
		return None
	if fieldname in STANDARD:
		return FieldInfo(fieldname, STANDARD[fieldname], fieldname.replace("_", " ").title() if fieldname != "name" else "ID", None, False, True, False, 0)
	df = frappe.get_meta(doctype).get_field(fieldname)
	if not df:
		return None
	return FieldInfo(df.fieldname, df.fieldtype, df.label or df.fieldname, df.options, bool(df.reqd), bool(df.read_only), bool(df.hidden), int(df.permlevel or 0))


def is_submittable(doctype):
	return bool(doctype_exists(doctype) and frappe.get_meta(doctype).is_submittable)


def has_company_field(doctype):
	return bool(doctype_exists(doctype) and frappe.get_meta(doctype).has_field("company"))


def link_target(doctype, fieldname):
	"""DocType a Link field points at, or None (not a Link field)."""
	info = field_info(doctype, fieldname)
	return info.options if info and info.fieldtype == "Link" and info.options else None


def select_options(doctype, fieldname):
	info = field_info(doctype, fieldname)
	if not info or info.fieldtype != "Select":
		return []
	return [o for o in (info.options or "").split("\n") if o]


def child_doctype(doctype, table):
	"""Child DocType behind a Table field of `doctype`, or None."""
	info = field_info(doctype, table)
	if info and info.fieldtype in ("Table", "Table MultiSelect") and info.options and doctype_exists(info.options):
		return info.options
	return None


def title_and_search_fields(doctype):
	if not doctype_exists(doctype):
		return None, []
	meta = frappe.get_meta(doctype)
	return meta.title_field, [f.strip() for f in (meta.search_fields or "").split(",") if f.strip()]
