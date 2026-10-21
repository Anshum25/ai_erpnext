"""Retention of extracted document data (AI Document Entry).

An entry keeps what the model read from an image (extracted_data) and what the mapping derived from it. Once the entry is
resolved (a document was created from it) that content is only needed for a while (review, questions about the draft).
After `document_retention_days` (AI Provider Settings; 0 = keep forever) a daily job clears it from resolved entries:

- only entries with status Created AND a created_document are touched: Pending, failed, under-review and every other
  unresolved entry keeps its data, and so does an entry a conversation still has open;
- the row itself, its owner, file link, status and created_document stay, so the link to the created record is kept;
- mapping_results is reduced to a names-free classification summary (kind of document, target, company, our role).
"""

import json

import frappe
from frappe.utils import add_days, cint, now_datetime

DOCTYPE = "AI Document Entry"
DEFAULT_DAYS = 180
PURGED_FIELDS = ("extracted_data", "mapped_data", "validation_results", "review_overrides", "error_log")
SUMMARY_KEYS = ("kind", "target_key", "target_doctype", "company", "our_role", "status")


def retention_days():
	if not frappe.db.exists("DocType", "AI Provider Settings"):
		return DEFAULT_DAYS
	value = frappe.db.get_single_value("AI Provider Settings", "document_retention_days")
	return DEFAULT_DAYS if value in (None, "") else cint(value)


def purge_resolved_entries(days=None):
	"""Clear extracted content of resolved entries older than `days`. Returns the names that were cleared."""
	days = retention_days() if days is None else cint(days)
	if days <= 0:
		return []
	cutoff = add_days(now_datetime(), -days)
	in_use = set(frappe.get_all("AI Chat Session", filters={"status": "Open", "active_entry": ["is", "set"]}, pluck="active_entry"))
	names = frappe.get_all(DOCTYPE, filters={"status": "Created", "created_document": ["is", "set"], "modified": ["<", cutoff],
		"extracted_data": ["is", "set"]}, pluck="name", limit=0)
	cleared = []
	for name in names:
		if name in in_use:
			continue
		results = frappe.db.get_value(DOCTYPE, name, "mapping_results")
		frappe.db.set_value(DOCTYPE, name, {**{f: None for f in PURGED_FIELDS}, "mapping_results": json.dumps(_summary(results))},
			update_modified=False)
		cleared.append(name)
	return cleared


def _summary(raw):
	try:
		data = json.loads(raw or "{}")
	except ValueError:
		data = {}
	cls = ((data.get("document_type") or {}).get("classification") or {}) if isinstance(data, dict) else {}
	return {"document_type": {"classification": {k: cls.get(k) for k in SUMMARY_KEYS}, "retention": "extracted content cleared"}}
