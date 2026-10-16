"""Row-level access to AI Document Entry.

The DocType grants System Manager read/write on every row, which would let one System Manager
open another user's extracted invoice data in Desk (list, form, reports, REST). These hooks
(registered in hooks.py, so no migration is needed) narrow that to the entry's owner. Controller
hooks can only deny, never grant: role permissions still apply first.

Administrator is not restricted (Frappe never applies these hooks to it).
"""

import frappe

DOCTYPE = "AI Document Entry"


def has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if doc.is_new() or ptype == "create":  # the owner is set on insert; role permissions already gated creation
		return True
	return doc.owner == user


def permission_query_conditions(user=None, doctype=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	return f"`tab{DOCTYPE}`.`owner` = {frappe.db.escape(user)}"
