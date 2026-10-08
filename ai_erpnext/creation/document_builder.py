"""Builds ERPNext documents from validated mapped JSON. Always creates Drafts.

Only whitelisted fields from the registry definition are copied, so nothing from the AI
output can set arbitrary fields (docstatus, owner, naming, accounts...).
"""

import frappe
from frappe import _


class BaseDocumentBuilder:
	def __init__(self, definition):
		self.definition = definition

	def build(self, mapped: dict, context: dict):
		d = self.definition
		doc = frappe.new_doc(d.target_doctype)
		for fieldname in d.supported_fields:
			if mapped.get(fieldname) not in (None, ""):
				doc.set(fieldname, mapped[fieldname])
		for row in mapped.get(d.items_table) or []:
			doc.append(d.items_table, {f: row[f] for f in d.supported_item_fields if row.get(f) not in (None, "")})
		self.prepare(doc, mapped, context)
		return doc

	def prepare(self, doc, mapped, context):
		"""Per-document hook for extra defaults."""

	def create_draft(self, mapped: dict, context: dict):
		frappe.has_permission(self.definition.target_doctype, "create", throw=True)
		doc = self.build(mapped, context)
		doc.docstatus = 0
		doc.insert()  # normal permission checks and ERPNext validations apply
		if doc.docstatus != 0:  # defensive: never auto-submit
			frappe.throw(_("AI-created documents must remain Draft"))
		return doc
