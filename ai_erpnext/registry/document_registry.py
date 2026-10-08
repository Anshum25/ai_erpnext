"""AI Document Registry.

Each supported AI document type is described by one DocumentDefinition. The rest of the
pipeline (detection, mapping, validation, creation) looks components up here instead of
branching on the document type. Adding Purchase Invoice later means adding one
definition plus its adapter/mapper/validator/builder classes.
"""

from dataclasses import dataclass, field

import frappe


@dataclass(frozen=True)
class DocumentDefinition:
	key: str
	target_doctype: str
	adapter: str  # dotted path: raw extracted JSON -> canonical dict
	mapper: str  # dotted path: canonical dict -> mapped ERPNext JSON + mapping results
	validator: str  # dotted path: mapped JSON -> ValidationReport
	builder: str  # dotted path: mapped JSON -> unsaved ERPNext document
	aliases: tuple = ()
	# Canonical fields the adapter understands (documentation + review UI).
	extraction_schema: tuple = ()
	# Header fields the builder may set on the target document.
	supported_fields: tuple = ()
	# Child table name and the fields the builder may set on each row.
	items_table: str = "items"
	supported_item_fields: tuple = ()
	# Fields that must be resolved before a draft can be created.
	required_fields: tuple = ()
	required_item_fields: tuple = ()
	masters: dict = field(default_factory=dict)

	def get_component(self, kind):
		return frappe.get_attr(getattr(self, kind))(self)


_REGISTRY: dict[str, DocumentDefinition] = {}


def register(definition: DocumentDefinition):
	_REGISTRY[definition.key] = definition
	return definition


def get_definition(key) -> DocumentDefinition | None:
	_ensure_loaded()
	if not key:
		return None
	if key in _REGISTRY:
		return _REGISTRY[key]
	lowered = str(key).strip().lower()
	for definition in _REGISTRY.values():
		if lowered == definition.key.lower() or lowered == definition.target_doctype.lower():
			return definition
	return None


def get_definition_for_doctype(doctype) -> DocumentDefinition | None:
	_ensure_loaded()
	for definition in _REGISTRY.values():
		if definition.target_doctype == doctype:
			return definition
	return None


def all_definitions():
	_ensure_loaded()
	return list(_REGISTRY.values())


def supported_doctypes():
	return [d.target_doctype for d in all_definitions()]


_loaded = False


def _ensure_loaded():
	global _loaded
	if _loaded:
		return
	_loaded = True
	from ai_erpnext.registry import definitions  # noqa: F401  (registers built-ins)

	# Other apps can register definitions via the `ai_document_definitions` hook,
	# a list of dotted paths to modules that call register() on import.
	for path in frappe.get_hooks("ai_document_definitions") or []:
		frappe.get_module(path)
