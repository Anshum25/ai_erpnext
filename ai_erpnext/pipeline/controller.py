import frappe
import json
from ai_erpnext.pipeline.processor import DocumentProcessor
from ai_erpnext.pipeline.mapper import ERPNextMapper
from ai_erpnext.ai.gemini import GeminiProvider

def process_ai_document(doc, method):
    # This runs synchronously when user clicks save.
    if doc.status == "Pending" and doc.file:
        # Enqueue the actual heavy AI lifting to a background worker
        frappe.enqueue(
            "ai_erpnext.pipeline.controller.run_ai_pipeline", 
            doc_name=doc.name, 
            timeout=1500,
            enqueue_after_commit=True
        )
        frappe.msgprint("Document queued for AI processing in the background. Please refresh the page in a few seconds.")

def run_ai_pipeline(doc_name):
    import frappe
    import logging
    import os
    import json
    from ai_erpnext.pipeline.processor import DocumentProcessor
    from ai_erpnext.pipeline.mapper import ERPNextMapper
    from ai_erpnext.ai.gemini import GeminiProvider
    
    logger = logging.getLogger("ai_erpnext")
    if not logger.handlers:
        log_path = os.path.join(frappe.utils.get_bench_path(), "logs", "ai.log")
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)
        
    doc = frappe.get_doc("AI Document Entry", doc_name)
    
    try:
        logger.info(f"Starting AI processing for Document: {doc.name}")
        print(f"\n[AI PIPELINE]: Starting AI processing for Document: {doc.name}")
        
        file_doc = frappe.get_doc("File", {"file_url": doc.file})
        file_content = file_doc.get_content()
        
        settings = frappe.get_single("AI Provider Settings")
        if settings.provider == "Gemini" and settings.api_key:
            provider = GeminiProvider(api_key=settings.get_password("api_key"))
        elif settings.provider in ["OpenAI", "Groq", "Nvidia NIM", "OpenRouter"] and settings.api_key:
            from ai_erpnext.ai.openai_provider import OpenAIProvider
            provider = OpenAIProvider(
                api_key=settings.get_password("api_key"),
                base_url=settings.base_url,
                model=settings.model
            )
        else:
            raise Exception("AI Provider not properly configured.")
            
        processor = DocumentProcessor(provider)
        extracted = processor.process_file(file_content, file_doc.file_name.split('.')[-1])
        
        doc.extracted_data = json.dumps(extracted, indent=4)
        
        print("\n[AI PIPELINE]: Mapping extracted data to ERPNext Masters...")
        # Registry-driven master mapping + validation. Sets target_doctype, mapped_data,
        # mapping_results, validation_results and the review status. A mapping failure
        # must never discard the extraction, so it is handled separately.
        from ai_erpnext.pipeline.review_service import apply_mapping
        try:
            apply_mapping(doc)
        except Exception as map_error:
            logger.exception(f"Mapping failed for {doc.name}")
            frappe.log_error(title=f"AI mapping failed for {doc.name}")
            doc.status = "Needs Review"
            doc.requires_review = 1
            doc.error_log = f"Mapping failed: {map_error}"
        print(f"[AI PIPELINE]: Successfully processed AI Document Entry: {doc.name} -> {doc.status}\n")
        logger.info(f"Successfully mapped document {doc.name} -> {doc.status}")
        
    except Exception as e:
        print(f"\n[AI PIPELINE ERROR]: {str(e)}\n")
        logger.error(f"Error processing {doc.name}: {str(e)}")
        doc.status = "Failed"
        doc.error_log = str(e)
        
    # Save the document without triggering hooks again (to avoid infinite loop)
    doc.flags.ignore_permissions = True
    doc.flags.ignore_links = True
    doc.flags.ignore_validate = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()
