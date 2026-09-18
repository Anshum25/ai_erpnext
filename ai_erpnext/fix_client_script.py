import frappe

def execute():
    if frappe.db.exists("Client Script", "AI Document Entry UI"):
        doc = frappe.get_doc("Client Script", "AI Document Entry UI")
        doc.enabled = 1
        doc.save()
        frappe.db.commit()
        print("Client Script enabled!")
    else:
        print("Client Script not found.")
