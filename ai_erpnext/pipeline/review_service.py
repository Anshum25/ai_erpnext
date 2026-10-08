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

DATE_OVERRIDE_KEYS = ("posting_date", "due_date")


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
	outcome = map_document(extracted, target_doctype=doc.target_doctype or None, overrides=overrides)
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
		# Unregistered/unknown type: keep the pre-existing behaviour (legacy mapper)
		# so documents like Purchase Invoice still get the old prefilled form.
		doc.mapped_data = dump(_legacy_mapping(doc, outcome) or {})
		summary = ValidationReport().to_dict()
		summary.update(
			document_status=DocumentStatus.NEEDS_REVIEW,
			mapping_status=outcome.status,
			can_create=False,
			requires_review=True,
			legacy=bool(outcome.target_doctype),
			errors=[{"code": "unsupported_document_type", "message": outcome.message, "severity": "error", "category": "mapping", "field": "target_doctype"}],
		)
		doc.status = DocumentStatus.NEEDS_REVIEW
		doc.duplicate = 0
		doc.requires_review = 1

	doc.mapping_results = dump(results)
	doc.validation_results = dump(summary)
	return outcome, report


def _legacy_mapping(doc, outcome):
	if not outcome.target_doctype:
		return None
	try:
		from ai_erpnext.pipeline.mapper import ERPNextMapper

		extracted = dict(load_json(doc.extracted_data, {}) or {})
		extracted["document_type"] = outcome.target_doctype
		return ERPNextMapper().map_document(extracted)
	except Exception:
		frappe.log_error(title="AI legacy mapping failed")
		return None


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
		elif not (isinstance(results.get(key), dict) and results[key].get("doctype")):
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
		"source": {
			k: extracted.get(k)
			for k in ("invoice_number", "invoice_date", "due_date", "terms", "subtotal", "tax_rate", "tax_amount", "total_amount")
			if isinstance(extracted, dict)
		},
		"resolved_statuses": list(FieldStatus.RESOLVED),
	}
