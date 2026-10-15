"""Schema for the app's DocTypes.

The DocTypes are DB-level (custom=1) DocTypes, so they are not created by the standard
DocType sync. Everything here is idempotent and runs after install and after every
migrate: a clean site gets the full schema, an existing site only gets what is missing.
"""

import frappe

MODULE = "AI"

AI_DOCUMENT_ENTRY = {
	"doctype": "DocType",
	"name": "AI Document Entry",
	"module": MODULE,
	"custom": 1,
	"autoname": "AI-DOC-.YYYY.-.#####",
	"fields": [
		{"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Pending\nProcessed\nFailed\nSubmitted", "default": "Pending"},
		{"fieldname": "file", "label": "File", "fieldtype": "Attach"},
		{"fieldname": "target_doctype", "label": "Target DocType", "fieldtype": "Link", "options": "DocType"},
		{"fieldname": "extracted_data", "label": "Extracted Data", "fieldtype": "Code", "options": "JSON"},
		{"fieldname": "mapped_data", "label": "Mapped Data", "fieldtype": "Code", "options": "JSON"},
		{"fieldname": "error_log", "label": "Error Log", "fieldtype": "Text"},
	],
	"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
}

PROVIDERS = ("Gemini", "OpenAI", "Groq", "Nvidia NIM", "OpenRouter")

AI_PROVIDER_SETTINGS = {
	"doctype": "DocType",
	"name": "AI Provider Settings",
	"module": MODULE,
	"custom": 1,
	"issingle": 1,
	"fields": [
		{"fieldname": "provider", "label": "Primary AI Provider", "fieldtype": "Select", "options": "\n".join(PROVIDERS), "default": "Groq"},
		{"fieldname": "api_key", "label": "Primary API Key", "fieldtype": "Password"},
	],
	"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
}

AI_PROVIDER_SETTINGS_FIELDS = [
	# (fieldname, definition, insert_after)
	("base_url", {"label": "Base URL", "fieldtype": "Data",
		"description": "Leave empty for the provider's own endpoint (Groq, OpenRouter and Nvidia NIM endpoints are built in)."}, "api_key"),
	("model", {"label": "Model Name", "fieldtype": "Data",
		"description": "Leave empty for the provider default (Groq: qwen/qwen3.8-27b, OpenAI: gpt-4.1-mini, Gemini: gemini-3.6-flash). "
			"Required for OpenRouter and Nvidia NIM. Used for invoice image extraction."}, "base_url"),
	# Optional text-only model for the chat router (same provider and key). Empty: Model Name is used.
	("text_model", {"label": "Chat Model (Text Only)", "fieldtype": "Data",
		"description": "Optional. Used for chat routing so ordinary chat does not go to the image model. Empty: Model Name is used for both."}, "model"),
	# Paid fallback for an exhausted Groq daily quota. Off unless an administrator enables it.
	("fallback_section", {"label": "Fallback Provider", "fieldtype": "Section Break", "collapsible": 1}, "text_model"),
	("fallback_enabled", {"label": "Enable Fallback Provider", "fieldtype": "Check", "default": "0",
		"description": "Off by default. Only a Groq daily-quota error switches to the fallback, for one request. Rate limits, outages, "
			"invalid keys, missing credits and other errors never do. The fallback is a paid service: enable it only after its account has credits."},
		"fallback_section"),
	("fallback_provider", {"label": "Fallback AI Provider", "fieldtype": "Select", "options": "\nOpenAI", "depends_on": "eval:doc.fallback_enabled"},
		"fallback_enabled"),
	("fallback_model", {"label": "Fallback Model", "fieldtype": "Data", "default": "gpt-4.1-mini", "depends_on": "eval:doc.fallback_enabled"},
		"fallback_provider"),
	("fallback_base_url", {"label": "Fallback Base URL", "fieldtype": "Data", "depends_on": "eval:doc.fallback_enabled",
		"description": "Leave empty for OpenAI's own endpoint. Only needed for a compatible gateway."}, "fallback_model"),
	("fallback_api_key", {"label": "Fallback API Key", "fieldtype": "Password", "depends_on": "eval:doc.fallback_enabled"}, "fallback_base_url"),
	("fallback_daily_limit", {"label": "Fallback Daily Request Limit", "fieldtype": "Int", "default": "20", "depends_on": "eval:doc.fallback_enabled",
		"description": "Hard cap on fallback requests per site per day. 0 disables the fallback."}, "fallback_api_key"),
]

# Existing fields of the base definition whose presentation changed (fieldnames, values and
# stored keys are untouched, so upgraded sites keep their provider and keys).
AI_PROVIDER_SETTINGS_UPDATES = [
	("provider", {"label": "Primary AI Provider", "default": "Groq",
		"description": "Groq, OpenAI, OpenRouter and Nvidia NIM work for chat and invoice images; Gemini for invoice images only."}, None),
	("api_key", {"label": "Primary API Key", "description": "Stored encrypted. Never shown again after saving."}, None),
]


def ensure_schema():
	"""Create or upgrade every DocType of the app (idempotent)."""
	_create_if_missing(AI_DOCUMENT_ENTRY)
	ensure_ai_document_entry_schema()
	_create_if_missing(AI_PROVIDER_SETTINGS)
	ensure_ai_provider_settings_schema()
	ensure_ai_chat_session_doctype()


def create_doctypes():
	"""Kept for old manual setup instructions; same as ensure_schema()."""
	ensure_schema()
	frappe.db.commit()


def _create_if_missing(definition):
	if not frappe.db.exists("DocType", definition["name"]):
		frappe.get_doc(frappe.parse_json(frappe.as_json(definition))).insert()


def _add_fields(doc, fields):
	"""Add or update (fieldname, definition, insert_after) fields on a custom DocType. Returns True if changed."""
	changed = False
	by_name = {f.fieldname: f for f in doc.fields}
	for fieldname, definition, after in fields:
		field = by_name.get(fieldname)
		if field:
			for key, value in definition.items():
				if str(field.get(key) or "") != str(value):
					field.set(key, value)
					changed = True
			continue
		new_field = doc.append("fields", {"fieldname": fieldname, **definition})
		# move right after `after`
		doc.fields.remove(new_field)
		index = next((i for i, f in enumerate(doc.fields) if f.fieldname == after), len(doc.fields) - 1)
		doc.fields.insert(index + 1, new_field)
		by_name[fieldname] = new_field
		changed = True
	if changed:
		for i, f in enumerate(doc.fields, start=1):
			f.idx = i
	return changed


def _add_select_options(field, options):
	current = [o for o in (field.options or "").split("\n") if o]
	missing = [o for o in options if o not in current]
	if missing:
		field.options = "\n".join(current + missing)
	return bool(missing)


def ensure_ai_provider_settings_schema():
	if not frappe.db.exists("DocType", "AI Provider Settings"):
		return
	doc = frappe.get_doc("DocType", "AI Provider Settings")
	if not doc.custom:
		return
	provider = next((f for f in doc.fields if f.fieldname == "provider"), None)
	changed = _add_select_options(provider, PROVIDERS) if provider else False
	changed = _add_fields(doc, AI_PROVIDER_SETTINGS_FIELDS) or changed
	if _add_fields(doc, [u for u in AI_PROVIDER_SETTINGS_UPDATES if u[0] in {f.fieldname for f in doc.fields}]) or changed:
		doc.save()


# --------------------------------------------------------------------------------------
# Review/mapping fields on AI Document Entry (idempotent; run from after_migrate)
# --------------------------------------------------------------------------------------
AI_DOCUMENT_ENTRY_FIELDS = [
	# (fieldname, definition, insert_after)
	("created_document", {"label": "Created Document", "fieldtype": "Dynamic Link", "options": "target_doctype", "read_only": 1}, "target_doctype"),
	("requires_review", {"label": "Requires Review", "fieldtype": "Check", "default": "0", "read_only": 1}, "created_document"),
	("duplicate", {"label": "Possible Duplicate", "fieldtype": "Check", "default": "0", "read_only": 1}, "requires_review"),
	("mapping_results", {"label": "Mapping Results", "fieldtype": "Code", "options": "JSON", "read_only": 1}, "mapped_data"),
	("validation_results", {"label": "Validation Results", "fieldtype": "Code", "options": "JSON", "read_only": 1}, "mapping_results"),
	("review_overrides", {"label": "Review Corrections", "fieldtype": "Code", "options": "JSON", "read_only": 1, "hidden": 1}, "validation_results"),
]


def ensure_ai_document_entry_schema():
	if not frappe.db.exists("DocType", "AI Document Entry"):
		return
	from ai_erpnext.constants import DocumentStatus

	doc = frappe.get_doc("DocType", "AI Document Entry")
	if not doc.custom:
		return
	changed = False
	by_name = {f.fieldname: f for f in doc.fields}

	status = by_name.get("status")
	if status and _add_select_options(status, DocumentStatus.ALL):
		changed = True
	if _add_fields(doc, AI_DOCUMENT_ENTRY_FIELDS):
		changed = True

	if changed:
		doc.save()

	# The form UI now lives in public/js/ai_document_entry.js (app_include_js); the old
	# DB Client Script would add a second, unvalidated button.
	if frappe.db.get_value("Client Script", "AI Document Entry UI", "enabled"):
		frappe.db.set_value("Client Script", "AI Document Entry UI", "enabled", 0)


# --------------------------------------------------------------------------------------
# AI Chat Session: server-side state for the conversational assistant (idempotent)
# --------------------------------------------------------------------------------------
def ensure_ai_chat_session_doctype():
	if frappe.db.exists("DocType", "AI Chat Session"):
		return
	frappe.get_doc({
		"doctype": "DocType",
		"name": "AI Chat Session",
		"module": MODULE,
		"custom": 1,
		"autoname": "hash",
		"track_changes": 0,
		"fields": [
			{"fieldname": "title", "label": "Title", "fieldtype": "Data", "read_only": 1, "in_list_view": 1},
			{"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Open\nClosed", "default": "Open", "in_list_view": 1},
			{"fieldname": "active_entry", "label": "Active AI Document Entry", "fieldtype": "Link", "options": "AI Document Entry", "read_only": 1},
			{"fieldname": "last_activity", "label": "Last Activity", "fieldtype": "Datetime", "read_only": 1},
			{"fieldname": "messages", "label": "Messages", "fieldtype": "Long Text", "read_only": 1, "hidden": 1},
			{"fieldname": "state", "label": "State", "fieldtype": "Long Text", "read_only": 1, "hidden": 1},
		],
		# Same role as AI Document Entry; each user only sees their own conversations.
		"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "if_owner": 1}],
	}).insert()


def after_install():
	ensure_schema()


def after_migrate():
	ensure_schema()
