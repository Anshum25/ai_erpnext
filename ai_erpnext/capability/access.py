"""Permission-aware record lookups.

frappe.db.get_value / frappe.db.exists / frappe.get_all ignore role permissions, User Permissions
(company, warehouse, customer ...) and field permission levels. Anything that decides what the user
sees or learns ("already exists", a price, a default) must go through these helpers instead, so the
chat never reveals the existence or the values of records the user can't read.
"""

import frappe


def readable_names(doctype, filters, limit=1):
	"""Names of the records matching `filters` that the current user may read (permission-aware get_list)."""
	try:
		return frappe.get_list(doctype, filters=filters, pluck="name", limit=limit)
	except frappe.PermissionError:
		frappe.local.message_log = []
		return []


def readable_exists(doctype, name_or_filters):
	"""True if a record exists AND the current user may read it. Same answer for 'missing' and 'hidden'."""
	filters = {"name": name_or_filters} if isinstance(name_or_filters, str) else name_or_filters
	return bool(readable_names(doctype, filters, limit=1))


def readable_value(doctype, name, fieldname):
	"""A field value of a record the user may read, or None (missing, hidden or field not permitted)."""
	from frappe.model import get_permitted_fields

	try:
		if fieldname not in get_permitted_fields(doctype):
			return None
		rows = frappe.get_list(doctype, filters={"name": name}, fields=[fieldname], limit=1)
	except frappe.PermissionError:
		frappe.local.message_log = []
		return None
	return rows[0].get(fieldname) if rows else None
