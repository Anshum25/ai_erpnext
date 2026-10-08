"""Resolve the AI's `document_type` text to a registered definition / ERPNext DocType."""

import frappe

from ai_erpnext.constants import DOCUMENT_TYPE_CONFIDENCE_THRESHOLD
from ai_erpnext.mapping.normalize import clean_text, normalize_name, parse_number
from ai_erpnext.registry.document_registry import all_definitions

_CONFIDENCE_KEYS = ("document_type_confidence", "confidence")


def detect_document_type(extracted: dict) -> dict:
	"""Return {input, target_doctype, registered, confidence, match_type, confident}."""
	raw = clean_text((extracted or {}).get("document_type"), 140)
	result = {
		"input": raw,
		"target_doctype": None,
		"registered": False,
		"confidence": 0.0,
		"match_type": "not_found",
		"confident": False,
	}
	if not raw:
		result["match_type"] = "not_provided"
		return result

	wanted = normalize_name(raw)
	for definition in all_definitions():
		if wanted == normalize_name(definition.key):
			result.update(target_doctype=definition.target_doctype, registered=True, confidence=1.0, match_type="exact")
			break
		if wanted in {normalize_name(a) for a in definition.aliases}:
			result.update(target_doctype=definition.target_doctype, registered=True, confidence=0.9, match_type="alias")
			break
	else:
		# A real ERPNext DocType we do not support yet (e.g. Purchase Invoice).
		doctype = frappe.db.get_value("DocType", {"name": raw, "istable": 0, "issingle": 0}, "name")
		if doctype:
			result.update(target_doctype=doctype, confidence=0.9, match_type="unregistered")

	ai_confidence = _ai_confidence(extracted)
	if ai_confidence is not None:
		result["ai_confidence"] = ai_confidence
		result["confidence"] = round(min(result["confidence"], ai_confidence), 4)

	if result["registered"] and not _has_minimum_structure(extracted):
		result["confidence"] = min(result["confidence"], 0.5)
		result["match_type"] += "+missing_structure"

	result["confident"] = bool(
		result["target_doctype"] and result["confidence"] >= DOCUMENT_TYPE_CONFIDENCE_THRESHOLD
	)
	return result


def _ai_confidence(extracted):
	for key in _CONFIDENCE_KEYS:
		value = parse_number(extracted.get(key)) if isinstance(extracted, dict) else None
		if value is not None:
			if value > 1:
				value = value / 100.0
			return max(0.0, min(1.0, value))
	return None


def _has_minimum_structure(extracted):
	items = extracted.get("items")
	return bool(isinstance(items, list) and items) or bool(extracted.get("customer_name"))
