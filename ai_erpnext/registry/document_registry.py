"""AI Document Registry.

Each supported AI document type is described by one DocumentDefinition. The rest of the pipeline (direction
classification, mapping, validation, creation) looks components and per-type semantics up here instead of branching on
the document type. A type is reachable only when detection/direction.TARGETS names its key AND it is registered here;
nothing else can make the pipeline create a document.
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
	label: str = ""  # what the user is shown ("Credit Note (Sales Invoice return)")
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
	# --- per-type semantics used by the shared transaction mapper / validator / builder ---
	party_field: str | None = None  # field on the target holding the counterparty (customer, supplier, party_name)
	party_doctype: str | None = None  # Customer or Supplier
	date_field: str | None = None  # posting_date or transaction_date
	second_date_field: str | None = None  # due_date, delivery_date, schedule_date, valid_till
	second_date_required: bool = False
	row_date_field: str | None = None  # the item-row copy of second_date_field (delivery_date, schedule_date)
	item_filters: dict = field(default_factory=dict)  # flags an item must have on this document (is_sales_item ...)
	tax_template_doctype: str | None = None
	supports_discount: bool = False
	is_return: bool = False
	warehouse: str | None = None  # None, "header" (set_warehouse, optional) or "rows" (every row needs one)
	currency: bool = True  # the target has a transaction currency
	rate_required: bool = True  # item rows must carry a price
	party_currency_check: bool = False  # mirror ERPNext's party-account currency rule (invoices)
	reference_field: str | None = None  # field for the source document number (bill_no, po_no, supplier_delivery_note)
	remarks_tag: bool = False  # tag remarks with the source number (duplicate detection without a dedicated field)
	duplicate_check: str | None = None  # "sales_invoice", "reference", "party_date_total", "bom"

	def get_component(self, kind):
		return frappe.get_attr(getattr(self, kind))(self)

	@property
	def display(self):
		return self.label or self.key


_REGISTRY: dict[str, DocumentDefinition] = {}


def register(definition: DocumentDefinition):
	_REGISTRY[definition.key] = definition
	return definition


def get_definition(key) -> DocumentDefinition | None:
	_ensure_loaded()
	if not key or not isinstance(key, str):
		return None
	if key in _REGISTRY:
		return _REGISTRY[key]
	lowered = key.strip().lower()
	for definition in _REGISTRY.values():
		if lowered == definition.key.lower():
			return definition
	return None


def get_definition_for_doctype(doctype) -> DocumentDefinition | None:
	"""The plain (non-return) definition of a target DocType."""
	_ensure_loaded()
	for definition in _REGISTRY.values():
		if definition.target_doctype == doctype and not definition.is_return:
			return definition
	return None


def all_definitions():
	_ensure_loaded()
	return list(_REGISTRY.values())


def supported_doctypes():
	"""Target DocTypes an image can become (each once, in registration order)."""
	return list(dict.fromkeys(d.target_doctype for d in all_definitions()))


def supported_keys():
	return [d.key for d in all_definitions()]


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
