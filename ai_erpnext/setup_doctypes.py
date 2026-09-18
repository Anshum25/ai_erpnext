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
