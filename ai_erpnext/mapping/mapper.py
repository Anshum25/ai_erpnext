"""Registry-driven entry point: extracted JSON -> MappingOutcome. No per-type branching.

The target is decided by detection/direction.classify (kind of document x our company's role), never by the printed
title and never by a caller: a target_doctype passed in can only confirm the classification, not change it.
"""

from dataclasses import dataclass, field

import frappe

from ai_erpnext.constants import FieldStatus, MappingStatus
from ai_erpnext.detection import direction
from ai_erpnext.registry.document_registry import DocumentDefinition, get_definition

# Reviewer answers the classification itself reads (validated in review_service.set_overrides).
ROLE_KEY = "document_role"


@dataclass
class MappingOutcome:
	detection: dict
	target_doctype: str | None = None
	definition: DocumentDefinition | None = None
	canonical: dict = field(default_factory=dict)
	mapped: dict | None = None
	results: dict = field(default_factory=dict)
	status: str = MappingStatus.FAILED
	message: str | None = None
	classification: direction.Classification | None = None

	def results_dict(self):
		data = {"document_type": self.detection}
		data.update({k: v.to_dict() for k, v in self.results.items()})
		return data


def company_context():
	"""(usable companies, validated default company) of the current user."""
	from ai_erpnext.chat.queries import usable_companies

	usable = usable_companies()
	default = next((c for c in (frappe.defaults.get_user_default("Company"), frappe.db.get_single_value("Global Defaults", "default_company"))
		if c and c in usable), None)
	return usable, default


def classify(extracted, overrides=None):
	overrides = overrides or {}
	usable, default = company_context()
	return direction.classify(extracted or {}, usable, default_company=default, selected_company=overrides.get("company"),
		selected_role=overrides.get(ROLE_KEY))


def map_document(extracted: dict, target_doctype: str | None = None, overrides: dict | None = None) -> MappingOutcome:
	overrides = dict(overrides or {})
	cls = classify(extracted, overrides)
	detection = {"input": cls.source_type, "kind": cls.kind, "target_doctype": cls.target_doctype, "target_key": cls.target_key,
		"registered": cls.ok, "confidence": 1.0 if cls.ok else 0.0, "match_type": cls.status, "confident": cls.ok,
		"classification": cls.to_dict()}
	outcome = MappingOutcome(detection=detection, target_doctype=cls.target_doctype if cls.ok else None, classification=cls)
	if not cls.ok:
		outcome.message = cls.message
		return outcome
	if target_doctype and target_doctype != cls.target_doctype:
		outcome.target_doctype = None
		outcome.message = (f"This document is a {cls.target_doctype} for {cls.company} (decided from who issued it and who it is for), "
			f"not a {target_doctype}.")
		return outcome

	definition = get_definition(cls.target_key)
	outcome.definition = definition
	outcome.canonical = definition.get_component("adapter").adapt(extracted or {})
	outcome.canonical["source_kind"] = cls.kind
	outcome.canonical["tax_component_rate"] = _component_rate(extracted)
	mapper = definition.get_component("mapper")
	outcome.mapped, outcome.results = mapper.map(outcome.canonical, overrides, cls)

	needs_review = any(r.requires_review for r in outcome.results.values()) or any(
		outcome.results.get(f) is None or outcome.results[f].status not in FieldStatus.RESOLVED
		for f in definition.required_fields
	)
	outcome.status = MappingStatus.NEEDS_REVIEW if needs_review else MappingStatus.MAPPED
	return outcome


def _component_rate(extracted):
	"""Sum of the printed tax components' rates (CGST 9% + SGST 9% = 18%), when every component prints its rate."""
	from ai_erpnext.mapping.normalize import parse_number

	taxes = extracted.get("taxes") if isinstance(extracted, dict) else None
	if not isinstance(taxes, list) or not taxes:
		return None
	rates = [parse_number(t.get("rate")) for t in taxes if isinstance(t, dict)]
	return sum(rates) if rates and all(r is not None for r in rates) else None
