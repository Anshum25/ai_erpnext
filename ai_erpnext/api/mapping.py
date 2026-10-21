import frappe
from frappe import _

from ai_erpnext.constants import DocumentStatus
from ai_erpnext.pipeline.review_service import apply_mapping, review_payload, set_overrides
from ai_erpnext.registry.document_registry import supported_doctypes


def get_entry(entry, ptype="write"):
	"""The caller's own entry. A missing and a foreign entry give the same answer; the has_permission
	hook (ai_erpnext.permissions) restricts entries to their owner, so System Managers do not see
	each other's extracted invoice data."""
	if not isinstance(entry, str) or not frappe.db.exists("AI Document Entry", entry):
		raise frappe.PermissionError(_("Not permitted"))
	doc = frappe.get_doc("AI Document Entry", entry)
	if not doc.has_permission(ptype):
		raise frappe.PermissionError(_("Not permitted"))
	return doc


def ensure_not_created(doc):
	if doc.status == DocumentStatus.CREATED and doc.get("created_document"):
		frappe.throw(
			_("{0} {1} was already created from this entry").format(doc.target_doctype, doc.created_document)
		)


@frappe.whitelist(methods=["POST"])
def get_review(entry: str):
	"""Return the stored review data (no recomputation)."""
	return review_payload(get_entry(entry, "read"))


@frappe.whitelist(methods=["POST"])
def remap(entry: str, target_doctype: str | None = None):
	"""Re-run master mapping + validation from the stored Extracted Data."""
	doc = get_entry(entry)
	if doc.status == DocumentStatus.CREATED and doc.get("created_document"):
		return review_payload(doc)
	if not doc.extracted_data:
		frappe.throw(_("No extracted data to map"))
	if target_doctype is not None:
		# The type comes from the browser: only a server-side registered document type is accepted, and only as a
		# confirmation. The target follows from the document's parties (detection/direction.py); it can't be chosen.
		if target_doctype and (
			not isinstance(target_doctype, str) or target_doctype not in supported_doctypes()
		):
			frappe.throw(_("Document type not supported"), frappe.ValidationError)
		doc.target_doctype = target_doctype or None
	apply_mapping(doc)
	if target_doctype and doc.target_doctype != target_doctype:
		frappe.throw(
			_(
				"This document is classified from who issued it and who it is for, so it can't be turned into a {0}."
			).format(target_doctype),
			frappe.ValidationError,
		)
	doc.save()
	return review_payload(doc)


@frappe.whitelist(methods=["POST"])
def apply_corrections(entry: str, corrections: str | dict):
	"""Store reviewer-selected masters/dates, then re-map and re-validate."""
	doc = get_entry(entry)
	ensure_not_created(doc)
	set_overrides(doc, corrections)
	apply_mapping(doc)
	doc.save()
	return review_payload(doc)


@frappe.whitelist(methods=["GET", "POST"])
def get_supported_doctypes():
	return supported_doctypes()
