import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

def create_doctypes():
    create_ai_document_entry()
    create_client_script()
    create_ai_provider_settings()
    create_ai_entry_page()
    frappe.db.commit()
    print("DocTypes created successfully")

def create_ai_document_entry():
    if not frappe.db.exists("DocType", "AI Document Entry"):
        doc = frappe.get_doc({
            "doctype": "DocType",
            "name": "AI Document Entry",
            "module": "AI",
            "custom": 1,
            "autoname": "AI-DOC-.YYYY.-.#####",
            "fields": [
                {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Pending\nProcessed\nFailed\nSubmitted", "default": "Pending"},
                {"fieldname": "file", "label": "File", "fieldtype": "Attach"},
                {"fieldname": "target_doctype", "label": "Target DocType", "fieldtype": "Link", "options": "DocType"},
                {"fieldname": "extracted_data", "label": "Extracted Data", "fieldtype": "Code", "options": "JSON"},
                {"fieldname": "mapped_data", "label": "Mapped Data", "fieldtype": "Code", "options": "JSON"},
                {"fieldname": "error_log", "label": "Error Log", "fieldtype": "Text"}
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}]
        })
        doc.insert()

def create_client_script():
    if not frappe.db.exists("Client Script", "AI Document Entry UI"):
        doc = frappe.get_doc({
            "doctype": "Client Script",
            "name": "AI Document Entry UI",
            "dt": "AI Document Entry",
            "module": "AI",
            "script": '''
frappe.ui.form.on('AI Document Entry', {
    refresh: function(frm) {
        if (frm.doc.status === 'Processed' && frm.doc.mapped_data) {
            frm.add_custom_button(__('Review & Create Document'), function() {
                // Route to the target document with mapped data
                try {
                    let data = JSON.parse(frm.doc.mapped_data);
                    frappe.route_options = data;
                    frappe.set_route('Form', data.doctype, 'new-' + data.doctype.toLowerCase().replace(/ /g, '-'));
                } catch(e) {
                    frappe.msgprint("Error parsing mapped data.");
                }
            }).addClass('btn-primary');
        }
    }
});
'''
        })
        doc.insert()

def create_ai_provider_settings():
    if not frappe.db.exists("DocType", "AI Provider Settings"):
        doc = frappe.get_doc({
            "doctype": "DocType",
            "name": "AI Provider Settings",
            "module": "AI",
            "custom": 1,
            "issingle": 1,
            "fields": [
                {"fieldname": "provider", "label": "Provider", "fieldtype": "Select", "options": "Gemini\nOpenAI", "default": "Gemini"},
                {"fieldname": "api_key", "label": "API Key", "fieldtype": "Password"}
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}]
        })
        doc.insert()


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
	if status:
		options = [o for o in (status.options or "").split("\n") if o]
		missing = [s for s in DocumentStatus.ALL if s not in options]
		if missing:
			status.options = "\n".join(options + missing)
			changed = True

	for fieldname, definition, after in AI_DOCUMENT_ENTRY_FIELDS:
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
		doc.save()

	# The form UI now lives in public/js/ai_document_entry.js (app_include_js); the old
	# DB Client Script would add a second, unvalidated button.
	if frappe.db.get_value("Client Script", "AI Document Entry UI", "enabled"):
		frappe.db.set_value("Client Script", "AI Document Entry UI", "enabled", 0)
