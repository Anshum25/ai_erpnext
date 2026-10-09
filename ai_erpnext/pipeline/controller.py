import frappe
import json
from ai_erpnext.pipeline.processor import DocumentProcessor
from ai_erpnext.pipeline.mapper import ERPNextMapper
from ai_erpnext.ai.gemini import GeminiProvider

def process_ai_document(doc, method):
    # This runs synchronously when user clicks save.
    if doc.status == "Pending" and doc.file:
        # Enqueue the actual heavy AI lifting to a background worker. The job id is
        # deduplicated so saving a Pending entry twice cannot start two AI calls.
        frappe.enqueue(
            "ai_erpnext.pipeline.controller.run_ai_pipeline",
            doc_name=doc.name,
            timeout=1500,
            enqueue_after_commit=True,
            job_id=f"ai-extract-{doc.name}",
            deduplicate=True,
        )
        if not frappe.flags.get("ai_chat"):  # the chat shows its own progress message
            frappe.msgprint("Document queued for AI processing in the background. Please refresh the page in a few seconds.")


SAFE_GENERIC_ERROR = "AI extraction failed. Please retry or contact the administrator."
SAFE_MAPPING_ERROR = "Automatic master mapping failed. Please review the entry and select the values manually."


def run_ai_pipeline(doc_name):
    import frappe
    import json
    from ai_erpnext.ai.errors import AIProviderError, get_ai_logger
    from ai_erpnext.pipeline.processor import DocumentProcessor
    from ai_erpnext.ai.gemini import GeminiProvider

    # Metadata only: document names, statuses and error categories. Never extracted
    # content, provider responses, keys or raw exception text (see ai/errors.py).
    logger = get_ai_logger()
    doc = frappe.get_doc("AI Document Entry", doc_name)

    # Re-delivered or stale jobs must not re-extract (and spend quota) or touch an
    # entry that already produced a document.
    if doc.status != "Pending" or doc.get("created_document"):
        logger.info(f"AI-PIPELINE skip doc={doc.name} status={doc.status}")
        return

    try:
        logger.info(f"AI-PIPELINE start doc={doc.name}")

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
                model=settings.model,
                provider_name=settings.provider,
            )
        else:
            raise AIProviderError("config")

        # Exactly one provider is used; there is deliberately no fallback provider.
        processor = DocumentProcessor(provider)
        extracted = processor.process_file(file_content, file_doc.file_name.split('.')[-1])

        doc.extracted_data = json.dumps(extracted, indent=4)
        doc.error_log = None

        # Registry-driven master mapping + validation. Sets target_doctype, mapped_data,
        # mapping_results, validation_results and the review status. A mapping failure
        # must never discard the extraction, so it is handled separately.
        from ai_erpnext.pipeline.review_service import apply_mapping
        try:
            apply_mapping(doc)
        except Exception as map_error:
            logger.error(f"AI-PIPELINE mapping_failed doc={doc.name} error_type={type(map_error).__name__}")
            frappe.log_error(title=f"AI mapping failed for {doc.name}")
            doc.status = "Needs Review"
            doc.requires_review = 1
            doc.error_log = SAFE_MAPPING_ERROR
        logger.info(f"AI-PIPELINE done doc={doc.name} status={doc.status}")

    except AIProviderError as e:
        logger.warning(f"AI-PIPELINE failed doc={doc.name} category={e.category} http_status={e.status_code}")
        doc.status = "Failed"
        doc.error_log = e.user_message
    except Exception as e:
        logger.error(f"AI-PIPELINE failed doc={doc.name} error_type={type(e).__name__}")
        doc.status = "Failed"
        doc.error_log = SAFE_GENERIC_ERROR

    # Save the document without triggering hooks again (to avoid infinite loop)
    doc.flags.ignore_permissions = True
    doc.flags.ignore_links = True
    doc.flags.ignore_validate = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()
