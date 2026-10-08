import frappe

def run():
    provider = frappe.db.get_single_value('AI Provider Settings', 'provider')
    model = frappe.db.get_single_value('AI Provider Settings', 'model')
    print(f"Provider: {provider}")
    print(f"Model: {model}")
