"""Validation primitives. AI extraction is never treated as validation: every rule here
runs in Python against ERPNext data."""

from dataclasses import asdict, dataclass, field

from ai_erpnext.constants import DocumentStatus

# Issue categories decide what the reviewer must do before a draft can be created.
MAPPING = "mapping"  # must be fixed (unresolved master, invalid date, ...)
DATA = "data"  # inconsistent document data; reviewer may acknowledge and fix in ERPNext
DUPLICATE = "duplicate"  # possible duplicate; reviewer must explicitly acknowledge

ERROR = "error"
WARNING = "warning"


@dataclass
class Issue:
	code: str
	message: str
	severity: str = ERROR
	category: str = MAPPING
	field: str | None = None


@dataclass
class ValidationReport:
	issues: list = field(default_factory=list)
	duplicates: list = field(default_factory=list)

	def error(self, code, message, field=None, category=MAPPING):
		self.issues.append(Issue(code, message, ERROR, category, field))

	def warning(self, code, message, field=None, category=DATA):
		self.issues.append(Issue(code, message, WARNING, category, field))

	@property
	def errors(self):
		return [i for i in self.issues if i.severity == ERROR]

	@property
	def warnings(self):
		return [i for i in self.issues if i.severity == WARNING]

	@property
	def blocking(self):
		return [i for i in self.errors if i.category == MAPPING]

	@property
	def data_errors(self):
		return [i for i in self.errors if i.category == DATA]

	@property
	def duplicate(self):
		return any(i.category == DUPLICATE for i in self.errors)

	@property
	def document_status(self):
		if self.blocking or self.duplicate:
			return DocumentStatus.NEEDS_REVIEW
		if self.data_errors:
			return DocumentStatus.VALIDATION_FAILED
		if self.warnings:
			return DocumentStatus.READY_FOR_REVIEW
		return DocumentStatus.READY_TO_CREATE

	@property
	def requires_review(self):
		return self.document_status != DocumentStatus.READY_TO_CREATE

	def to_dict(self):
		return {
			"document_status": self.document_status,
			"requires_review": self.requires_review,
			"can_create": not self.blocking,
			"needs_acknowledgement": {"data_errors": bool(self.data_errors), "duplicate": self.duplicate},
			"duplicate": self.duplicate,
			"duplicates": self.duplicates,
			"errors": [asdict(i) for i in self.errors],
			"warnings": [asdict(i) for i in self.warnings],
		}


class BaseValidator:
	def __init__(self, definition):
		self.definition = definition

	def validate(self, outcome) -> ValidationReport:
		raise NotImplementedError
