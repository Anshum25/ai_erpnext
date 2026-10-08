"""Registry-driven entry point: extracted JSON -> MappingOutcome. No per-type branching."""

from dataclasses import dataclass, field

from ai_erpnext.constants import FieldStatus, MappingStatus
from ai_erpnext.detection.document_detector import detect_document_type
from ai_erpnext.registry.document_registry import DocumentDefinition, get_definition_for_doctype


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

	def results_dict(self):
		data = {"document_type": self.detection}
		data.update({k: v.to_dict() for k, v in self.results.items()})
		return data


def map_document(extracted: dict, target_doctype: str | None = None, overrides: dict | None = None) -> MappingOutcome:
	detection = detect_document_type(extracted or {})
	chosen = target_doctype or (detection["target_doctype"] if detection["confident"] else None)
	if target_doctype:
		detection = dict(detection, selected_by_user=target_doctype != detection["target_doctype"])

	outcome = MappingOutcome(detection=detection, target_doctype=chosen)
	if not chosen:
		outcome.message = (
			f"Document type '{detection['input']}' could not be determined confidently; select the Target DocType"
			if detection["input"]
			else "Document type not detected; select the Target DocType"
		)
		return outcome

	definition = get_definition_for_doctype(chosen)
	if not definition:
		outcome.message = f"Mapping for '{chosen}' is not supported yet"
		return outcome

	outcome.definition = definition
	outcome.canonical = definition.get_component("adapter").adapt(extracted or {})
	mapper = definition.get_component("mapper")
	outcome.mapped, outcome.results = mapper.map(outcome.canonical, overrides)

	needs_review = any(r.requires_review for r in outcome.results.values()) or any(
		outcome.results.get(f) is None or outcome.results[f].status not in FieldStatus.RESOLVED
		for f in definition.required_fields
	)
	outcome.status = MappingStatus.NEEDS_REVIEW if needs_review else MappingStatus.MAPPED
	return outcome
