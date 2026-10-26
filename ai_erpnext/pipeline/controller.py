import frappe


def extraction_job_id(doc_name):
	return f"ai-extract-{doc_name}"


def enqueue_extraction(doc_name):
	# The job id is deduplicated so saving a Pending entry twice cannot start two AI calls.
	frappe.enqueue(
		"ai_erpnext.pipeline.controller.run_ai_pipeline",
		doc_name=doc_name,
		timeout=1500,
		enqueue_after_commit=True,
		job_id=extraction_job_id(doc_name),
		deduplicate=True,
	)


def validate_ai_document(doc, method):
	"""The attached file must be one the user can already read.

	Frappe's on_update hook (attach_files_to_document) attaches an unattached File with the
	entry's URL to the entry, which would hand anyone who knows the URL of another user's
	private file read access to it. This check runs before that hook.
	"""
	if doc.file and (doc.is_new() or doc.has_value_changed("file")):
		try:
			_readable_file(doc.file, exclude_attached_to=doc.name)
		except FileAccessError:
			frappe.throw(
				frappe._("You don't have access to the attached file. Please upload it again."),
				frappe.PermissionError,
			)


def process_ai_document(doc, method):
	# This runs synchronously when user clicks save.
	if doc.status == "Pending" and doc.file:
		# Enqueue the actual heavy AI lifting to a background worker.
		enqueue_extraction(doc.name)
		if not frappe.flags.get("ai_chat"):  # the chat shows its own progress message
			frappe.msgprint(
				frappe._(
					"Document queued for AI processing in the background. Please refresh the page in a few seconds."
				)
			)


SAFE_GENERIC_ERROR = "AI extraction failed. Please retry or contact the administrator."
SAFE_MAPPING_ERROR = (
	"Automatic master mapping failed. Please review the entry and select the values manually."
)
SAFE_FILE_ERROR = "The attached file could not be read. Please attach the invoice image again."

# Background job: a few retries for outages and per-minute limits are fine (see ai/policy.py).
EXTRACTION_MAX_ATTEMPTS = 3
EXTRACTION_RETRY_BUDGET_S = 60


class FileAccessError(Exception):
	pass


class ImageContentError(Exception):
	"""The file's content is not a decodable PNG, JPEG or WEBP image (whatever its name says)."""


IMAGE_FORMATS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}
MAX_IMAGE_PIXELS = 50_000_000  # well above any scanned page; stops decompression bombs
SAFE_IMAGE_ERROR = "The attached file is not a readable JPG, PNG or WEBP image. Please attach a photo or scan of the document."


def image_problem(content, extension):
	"""None if `content` decodes as the image format its extension claims, else a user-safe reason.

	The extension alone is never trusted: the bytes must be a real PNG/JPEG/WEBP of that same format and of a sane size.
	PIL only parses the header and verifies the structure here; nothing in the file is executed."""
	import io

	from PIL import Image

	expected = IMAGE_FORMATS.get((extension or "").lower())
	if not expected or not isinstance(content, (bytes, bytearray)) or not content:
		return SAFE_IMAGE_ERROR
	try:
		with Image.open(io.BytesIO(content)) as img:
			if img.format != expected or img.width * img.height > MAX_IMAGE_PIXELS:
				return SAFE_IMAGE_ERROR
			img.verify()
	except Exception:
		return SAFE_IMAGE_ERROR
	return None


def run_ai_pipeline(doc_name):
	import json

	import frappe

	from ai_erpnext.ai import policy, providers
	from ai_erpnext.ai.errors import AIProviderError, get_ai_logger, log_safe_error
	from ai_erpnext.pipeline.processor import DocumentProcessor

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

		file_doc = _readable_file(doc.file)
		# Raw bytes: get_content() would otherwise try text encodings, and windows-1252
		# "decodes" many binary images into a str that cannot be sent as an image.
		file_content = file_doc.get_content(encodings=())
		file_type = (file_doc.file_name or doc.file).rsplit(".", 1)[-1]
		if image_problem(file_content, file_type):
			raise ImageContentError()  # never sent to a provider

		settings = providers.get_settings()

		def extract(provider, is_fallback):
			processor = DocumentProcessor(provider)
			return policy.call_with_retries(
				lambda: processor.process_file(file_content, file_type),
				max_attempts=1 if is_fallback else EXTRACTION_MAX_ATTEMPTS,
				budget_s=EXTRACTION_RETRY_BUDGET_S,
			)

		# One provider; the paid fallback is only considered for an exhausted Groq daily quota.
		extracted = providers.run("extract", extract, providers.primary(settings), settings)

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
			log_safe_error(f"AI mapping failed for {doc.name}", map_error, "AI Document Entry", doc.name)
			doc.status = "Needs Review"
			doc.requires_review = 1
			doc.error_log = SAFE_MAPPING_ERROR
		logger.info(f"AI-PIPELINE done doc={doc.name} status={doc.status}")

	except AIProviderError as e:
		logger.warning(f"AI-PIPELINE failed doc={doc.name} category={e.category} http_status={e.status_code}")
		doc.status = "Failed"
		doc.error_log = e.user_message
	except FileAccessError:
		logger.warning(f"AI-PIPELINE failed doc={doc.name} category=file_access")
		doc.status = "Failed"
		doc.error_log = SAFE_FILE_ERROR
	except ImageContentError:
		logger.warning(f"AI-PIPELINE failed doc={doc.name} category=not_an_image")
		doc.status = "Failed"
		doc.error_log = SAFE_IMAGE_ERROR
	except Exception as e:
		logger.error(f"AI-PIPELINE failed doc={doc.name} error_type={type(e).__name__}")
		doc.status = "Failed"
		doc.error_log = SAFE_GENERIC_ERROR

	# Cancelled in the chat while the provider was working (the entry is no longer Pending): the
	# result is not stored, so a cancelled workflow can't come back. A locking read sees the
	# latest committed status, not this job's snapshot from before the call.
	if frappe.db.get_value("AI Document Entry", doc.name, "status", for_update=True) != "Pending":
		logger.info(f"AI-PIPELINE discarded doc={doc.name} reason=no_longer_pending")
		return

	# The job runs as the user who queued it, and that user's write access was checked when
	# the Pending entry was saved (or retried). ignore_permissions only covers this system
	# write of the result fields; validation still runs. The status is never "Pending" here,
	# so the before_save hook does not queue the job again.
	doc.save(ignore_permissions=True)
	# The background job's result must be visible to the polling chat before the job ends.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit


def _readable_file(file_url, exclude_attached_to=None):
	"""The File record behind an attachment URL that the current user may read.

	Several File records can share one URL (attachments of the same content), so any one
	the user can read is accepted. Raises FileAccessError otherwise: an entry must never be
	used to read a file its user has no access to. exclude_attached_to skips Files attached
	to that AI Document Entry (they would grant access through the entry itself).
	"""
	if not file_url:
		raise FileAccessError()
	rows = frappe.get_all(
		"File",
		filters={"file_url": file_url, "is_folder": 0},
		fields=["name", "attached_to_doctype", "attached_to_name"],
		order_by="creation desc",
		limit=20,
	)
	for row in rows:
		if (
			exclude_attached_to
			and row.attached_to_doctype == "AI Document Entry"
			and row.attached_to_name == exclude_attached_to
		):
			continue
		file_doc = frappe.get_doc("File", row.name)
		if file_doc.has_permission("read"):
			return file_doc
	raise FileAccessError()
