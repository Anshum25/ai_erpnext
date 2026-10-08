"""Shared status vocabularies for the AI document mapping pipeline."""


class FieldStatus:
	"""Outcome of resolving a single extracted value to an ERPNext master/value."""

	MATCHED = "MATCHED"
	AMBIGUOUS = "AMBIGUOUS"
	NOT_FOUND = "NOT_FOUND"
	INVALID = "INVALID"
	DEFAULTED = "DEFAULTED"
	NOT_PROVIDED = "NOT_PROVIDED"

	RESOLVED = (MATCHED, DEFAULTED)


class MappingStatus:
	"""Overall result of the mapping step."""

	MAPPED = "MAPPED"
	NEEDS_REVIEW = "NEEDS_REVIEW"
	FAILED = "FAILED"


class DocumentStatus:
	"""Document-level review state, stored as labels in AI Document Entry.status."""

	READY_FOR_REVIEW = "Ready For Review"
	NEEDS_REVIEW = "Needs Review"
	VALIDATION_FAILED = "Validation Failed"
	READY_TO_CREATE = "Ready To Create"
	CREATED = "Created"

	# Pre-existing values of AI Document Entry.status, kept for compatibility.
	PENDING = "Pending"
	PROCESSED = "Processed"
	FAILED = "Failed"
	SUBMITTED = "Submitted"

	ALL = (
		PENDING,
		PROCESSED,
		FAILED,
		SUBMITTED,
		NEEDS_REVIEW,
		READY_TO_CREATE,
		VALIDATION_FAILED,
		READY_FOR_REVIEW,
		CREATED,
	)


# A match below this confidence is never auto-accepted.
AUTO_ACCEPT_CONFIDENCE = 0.90
# Minimum confidence for automatic document type detection.
DOCUMENT_TYPE_CONFIDENCE_THRESHOLD = 0.80
