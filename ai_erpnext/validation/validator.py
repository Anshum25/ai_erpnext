"""Registry-driven validation entry point."""

from ai_erpnext.validation.base import ValidationReport


def validate_outcome(outcome) -> ValidationReport:
	"""Validate a MappingOutcome using its definition's validator."""
	report = ValidationReport()
	if not outcome.definition:
		report.error("unsupported_document_type", outcome.message or "Document type not supported")
		return report
	return outcome.definition.get_component("validator").validate(outcome)
