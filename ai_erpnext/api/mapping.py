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
		frappe.throw(_("{0} {1} was already created from this entry").format(doc.target_doctype, doc.created_document))


@frappe.whitelist(methods=["POST"])
def get_review(entry):
	"""Return the stored review data (no recomputation)."""
	return review_payload(get_entry(entry, "read"))


@frappe.whitelist(methods=["POST"])
def remap(entry, target_doctype=None):
	"""Re-run master mapping + validation from the stored Extracted Data."""
	doc = get_entry(entry)
	if doc.status == DocumentStatus.CREATED and doc.get("created_document"):
		return review_payload(doc)
	if not doc.extracted_data:
		frappe.throw(_("No extracted data to map"))
	if target_doctype is not None:
		# The type comes from the browser: only a server-side registered document type is accepted
		# (empty clears it, so detection decides again).
		if target_doctype and target_doctype not in supported_doctypes():
			frappe.throw(_("Document type not supported"), frappe.ValidationError)
		doc.target_doctype = target_doctype or None
	apply_mapping(doc)
	doc.save()
	return review_payload(doc)


@frappe.whitelist(methods=["POST"])
def apply_corrections(entry, corrections):
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
