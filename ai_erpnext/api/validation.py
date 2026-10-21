import frappe

from ai_erpnext.api.mapping import get_entry
from ai_erpnext.pipeline.review_service import evaluate


@frappe.whitelist(methods=["POST"])
def validate_entry(entry: str):
	"""Dry-run mapping + validation for an entry; nothing is saved."""
	doc = get_entry(entry, "read")
	outcome, report = evaluate(doc)
	return {
		"target_doctype": outcome.target_doctype,
		"mapping_status": outcome.status,
		"message": outcome.message,
		"mapped": outcome.mapped,
		"validation": report.to_dict() if report else None,
	}
