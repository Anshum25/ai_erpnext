import frappe
from frappe import _
from frappe.utils import cint

from ai_erpnext.api.mapping import ensure_not_created, get_entry
from ai_erpnext.constants import DocumentStatus
from ai_erpnext.pipeline.review_service import apply_mapping, review_payload, set_overrides


@frappe.whitelist(methods=["POST"])
def create_document(entry, corrections=None, acknowledge_errors=0, acknowledge_duplicate=0):
	"""Re-validate and create the target document as a Draft. Never submits."""
	doc = get_entry(entry)
	# Serialise confirmations of the same entry (double click, two tabs, client retry):
	# the row lock makes a concurrent request wait, then see created_document and stop.
	frappe.db.get_value("AI Document Entry", doc.name, "name", for_update=True)
	doc.reload()
	ensure_not_created(doc)
	if corrections:
		set_overrides(doc, corrections)

	# Always recompute; never trust previously stored mapping/validation.
	outcome, report = apply_mapping(doc)
	blocked = None
	if not outcome.definition:
		blocked = outcome.message or _("Document type not supported")
	elif report.blocking:
		blocked = _("Resolve the highlighted fields before creating the document")
	elif report.duplicate and not cint(acknowledge_duplicate):
		blocked = _("Possible duplicate found. Confirm it is not a duplicate to continue.")
	elif report.data_errors and not cint(acknowledge_errors):
		blocked = _("Validation errors found. Acknowledge them to create a draft you will correct in ERPNext.")

	if blocked:
		doc.save()
		return {"created": False, "message": blocked, "review": review_payload(doc)}

	from ai_erpnext.creation.transaction import totals_note

	builder = outcome.definition.get_component("builder")
	new_doc = builder.create_draft(outcome.mapped, {"entry": doc.name, "canonical": outcome.canonical})

	doc.created_document = new_doc.name
	doc.status = DocumentStatus.CREATED
	doc.requires_review = 0
	doc.save()
	return {"created": True, "doctype": new_doc.doctype, "name": new_doc.name, "docstatus": new_doc.docstatus, "label": outcome.definition.display,
		"totals_note": totals_note(new_doc, outcome.canonical, outcome.definition), "review": review_payload(doc)}
