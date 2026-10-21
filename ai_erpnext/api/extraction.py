import frappe
from frappe import _

from ai_erpnext.api.mapping import get_entry


@frappe.whitelist(methods=["POST"])
def retry_extraction(entry: str):
	"""Re-queue AI extraction for a Failed entry (e.g. after a rate limit or outage).

	Only Failed entries without a created document can be retried, and the job is
	deduplicated, so this cannot create documents or start parallel AI calls.
	"""
	doc = get_entry(entry)
	if doc.status != "Failed" or doc.get("created_document"):
		frappe.throw(_("Only failed extractions can be retried."))
	if not doc.file:
		frappe.throw(_("This entry has no file to extract."))
	doc.status = "Pending"
	doc.error_log = None
	doc.save()  # before_save hook enqueues the (deduplicated) extraction job
	return {"name": doc.name, "status": doc.status}
