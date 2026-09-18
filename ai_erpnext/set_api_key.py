import frappe

def set_api_key():
    settings = frappe.get_doc("AI Provider Settings")
    settings.provider = "Gemini"
    settings.api_key = "AQ.Ab8RN6JZs7-d9K-sdloZOfJZ_XhwE0N8tjz65ymTWMAMD9lwOw"
    settings.save()
    frappe.db.commit()
    print("API Key set successfully")
