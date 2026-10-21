"""Glue between AI Document Entry and the registry-driven map -> validate -> create flow.

Extraction is not touched here: it only reads `extracted_data` produced by the existing
pipeline and writes mapped_data / mapping_results / validation_results / status.
"""

import json

import frappe
from frappe import _

from ai_erpnext.constants import DocumentStatus, FieldStatus
from ai_erpnext.mapping.mapper import map_document
from ai_erpnext.mapping.normalize import clean_text, parse_date
from ai_erpnext.validation.base import ValidationReport
from ai_erpnext.validation.validator import validate_outcome

DATE_OVERRIDE_KEYS = (
	"posting_date",
	"due_date",
	"transaction_date",
	"delivery_date",
	"schedule_date",
	"valid_till",
)
# Optional link overrides that have no mapping result until chosen (validated by the mapper).
EXTRA_LINK_OVERRIDE_KEYS = ("taxes_and_charges",)
# Reviewer answers with a fixed set of values (anything else is refused here, before mapping).
CHOICE_OVERRIDES = {
	"document_role": ("seller", "buyer"),
	"apply_discount_on": ("Net Total", "Grand Total"),
}


def load_json(value, default=None):
	if not value:
		return default
	if isinstance(value, (dict, list)):
		return value
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return default


def dump(value):
	return json.dumps(value, indent=4, default=str)


def evaluate(doc):
	"""Run detection, mapping and validation for an entry without saving it."""
	extracted = load_json(doc.extracted_data, {})
	if not isinstance(extracted, dict):
		extracted = {}
	overrides = load_json(doc.get("review_overrides"), {}) or {}
	# The stored target_doctype is an output of the classification, never an input: a Desk edit of the field can't
	# turn a document into another type.
	outcome = map_document(extracted, overrides=overrides)
	report = validate_outcome(outcome) if outcome.definition else None
	return outcome, report


def apply_mapping(doc):
	"""Evaluate an entry and write all review fields onto it (caller saves)."""
	outcome, report = evaluate(doc)
	doc.target_doctype = outcome.target_doctype
	results = outcome.results_dict()

	if report:
		doc.mapped_data = dump(outcome.mapped)
		summary = report.to_dict()
		summary["mapping_status"] = outcome.status
		doc.status = report.document_status
		doc.duplicate = int(report.duplicate)
		doc.requires_review = int(report.requires_review)
	else:
		# Unknown, unsupported, ambiguous or contradictory document: nothing can be created until the classification
		# question is answered (or never, for unsupported documents).
		doc.mapped_data = dump({})
		summary = ValidationReport().to_dict()
		summary.update(
			document_status=DocumentStatus.NEEDS_REVIEW,
			mapping_status=outcome.status,
			can_create=False,
			requires_review=True,
			errors=[
				{
					"code": "unsupported_document_type",
					"message": outcome.message,
					"severity": "error",
					"category": "mapping",
					"field": "target_doctype",
				}
			],
		)
		doc.status = DocumentStatus.NEEDS_REVIEW
		doc.duplicate = 0
		doc.requires_review = 1

	doc.mapping_results = dump(results)
	doc.validation_results = dump(summary)
	return outcome, report


def set_overrides(doc, corrections):
	"""Validate and merge reviewer corrections. Values are re-checked during mapping."""
	corrections = load_json(corrections, {}) or {}
	if not isinstance(corrections, dict):
		frappe.throw(_("Corrections must be an object"))
	results = load_json(doc.mapping_results, {}) or {}
	overrides = load_json(doc.get("review_overrides"), {}) or {}

	for key, value in corrections.items():
		key = clean_text(key, 60)
		if not key:
			continue
		value = clean_text(value, 140)
		if key in DATE_OVERRIDE_KEYS:
			if value and not parse_date(value).ok:
				frappe.throw(_("Invalid date for {0}: {1}").format(key, value))
		elif key in CHOICE_OVERRIDES:
			if value and value not in CHOICE_OVERRIDES[key]:
				frappe.throw(_("Invalid value for {0}").format(key))
		elif key == "company":
			from ai_erpnext.mapping.mapper import company_context

			if value and value not in company_context()[0]:
				frappe.throw(_("Company {0} cannot be used").format(value), frappe.PermissionError)
		elif key == "return_against":
			pass  # an invoice name (re-checked against the party, company and permissions by the mapper) or "__none__"
		elif key == "material_request_type":
			options = (
				frappe.get_meta("Material Request").get_field("material_request_type").options or ""
			).split("\n")
			if value and value not in options:
				frappe.throw(_("Invalid value for {0}").format(key))
		elif key not in EXTRA_LINK_OVERRIDE_KEYS and not (
			isinstance(results.get(key), dict) and results[key].get("doctype")
		):
			frappe.throw(_("Field {0} cannot be corrected").format(key))
		if value:
			overrides[key] = value
		else:
			overrides.pop(key, None)
	doc.review_overrides = dump(overrides)


def review_payload(doc, outcome=None, report=None):
	results = load_json(doc.mapping_results, {}) or {}
	extracted = load_json(doc.extracted_data, {}) or {}
	return {
		"name": doc.name,
		"status": doc.status,
		"target_doctype": doc.target_doctype,
		"created_document": doc.get("created_document"),
		"mapped": load_json(doc.mapped_data, {}),
		"results": results,
		"validation": load_json(doc.validation_results, {}),
		"overrides": load_json(doc.get("review_overrides"), {}) or {},
		"classification": ((results.get("document_type") or {}).get("classification")) or {},
		"source": {
			k: extracted.get(k)
			for k in (
				"document_type",
				"invoice_number",
				"date",
				"invoice_date",
				"due_date",
				"subtotal",
				"discount_amount",
				"tax_rate",
				"tax_amount",
				"total_amount",
			)
			if isinstance(extracted, dict)
		},
		"resolved_statuses": list(FieldStatus.RESOLVED),
	}
