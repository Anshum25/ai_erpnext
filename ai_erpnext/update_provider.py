import frappe

def execute():
    # 1. Update the DocType schema
    doc = frappe.get_doc("DocType", "AI Provider Settings")
    
    # Update provider options
    for f in doc.fields:
        if f.fieldname == "provider":
            f.options = "Gemini\nOpenAI\nGroq\nNvidia NIM\nOpenRouter"
            
    doc.save()
    frappe.db.commit()
    print("DocType AI Provider Settings updated")
    
    # 2. Set the OpenRouter details
    settings = frappe.get_single("AI Provider Settings")
    settings.provider = "OpenRouter"
    settings.api_key = "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"
    settings.base_url = "https://openrouter.ai/api/v1"
    settings.model = "qwen/qwen3-vl-235b-a22b-instruct"
    settings.save()
    frappe.db.commit()
    print("OpenRouter API Key and Settings configured successfully")
