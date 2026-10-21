"""Safety tests for Groq/Prompt B2 extraction: adapter contract, error handling, logging,
no paid fallback, reprocessing/retry guards. No network calls: provider HTTP is mocked.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_extraction_safety
"""

import contextlib
import hashlib
import io
import json
import logging
from types import SimpleNamespace
from unittest import mock

import frappe
import httpx
import openai
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from ai_erpnext.ai.errors import USER_MESSAGES, AIProviderError, classify
from ai_erpnext.ai.openai_provider import OpenAIProvider
from ai_erpnext.constants import DocumentStatus, FieldStatus
from ai_erpnext.mapping.adapters import SalesInvoiceAdapter
from ai_erpnext.mapping.mapper import map_document
from ai_erpnext.pipeline import processor
from ai_erpnext.tests.fixtures import test_file
from ai_erpnext.validation.validator import validate_outcome

SENTINEL = "ZZSENTINEL-CUSTOMER-4471"  # must never appear in any log output
ORG = "org_01SECRETORGID"
GROQ_URL = "https://api.groq.com/openai/v1"
REQ = httpx.Request("POST", GROQ_URL + "/chat/completions")


def our_company():
	return frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
		"Global Defaults", "default_company"
	)


def b2_output(**overrides):
	"""Shape of a Prompt B2 response for a split-GST invoice our company issued (synthetic test values)."""
	data = {
		"document_type": "Sales Invoice",
		"supplier_name": our_company(),
		"customer_name": "AISAFE Customer",
		"invoice_number": "AISAFE-0001",
		"date": nowdate(),
		"due_date": None,
		"currency": None,
		"items": [
			{
				"item_code": None,
				"item_name": "AISAFE Widget",
				"description": "Blue, size L",
				"qty": 2,
				"uom": "No.",
				"rate": 100,
				"amount": 200,
			}
		],
		"subtotal": 200,
		"discount_amount": None,
		"taxes": [{"name": "CGST", "rate": 9, "amount": 18}, {"name": "SGST", "rate": 9, "amount": 18}],
		"tax_rate": None,
		"tax_amount": None,
		"total_amount": 236,
	}
	data.update(overrides)
	return data


def fake_completion(content, prompt_tokens=10, completion_tokens=5):
	return SimpleNamespace(
		choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
		usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
	)


def api_error(cls, status, message):
	return cls(message, response=httpx.Response(status, request=REQ), body={"error": {"message": message}})


class TestExtractionSafety(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		cls.customer = (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "AISAFE Customer",
					"customer_group": group,
					"territory": territory,
				}
			)
			.insert()
			.name
		)
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		cls.item = (
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "AISAFE-ITEM",
					"item_name": "AISAFE Widget",
					"item_group": ig,
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"is_sales_item": 1,
				}
			)
			.insert()
			.name
		)
		cls.company_currency = frappe.get_cached_value(
			"Company",
			frappe.defaults.get_user_default("Company")
			or frappe.db.get_single_value("Global Defaults", "default_company"),
			"default_currency",
		)
		cls.image_url = test_file("png")

	# --- Prompt B2 contract ----------------------------------------------------------
	def test_prompt_b3_is_pinned_and_b2_is_kept(self):
		self.assertEqual(
			hashlib.sha256(processor.EXTRACTION_PROMPT.encode()).hexdigest(),
			processor.EXTRACTION_PROMPT_SHA256,
		)
		self.assertEqual(
			processor.EXTRACTION_PROMPT_SHA256,
			"425fb2c08ae86f5cf8213a615b0ac7f382a79894ac291ac11c858686a7842253",
		)
		self.assertEqual(processor.PROMPT_VERSION, "B3")
		# the benchmarked B2 text is kept unchanged for reference and rollback
		self.assertEqual(hashlib.sha256(processor.PROMPT_B2.encode()).hexdigest(), processor.PROMPT_B2_SHA256)
		self.assertEqual(
			processor.PROMPT_B2_SHA256, "2d7e2cbc95efe288be15a41aa63b96caa37fbf009e1a263e8437cf43cbab9a1f"
		)
		# every B2 key is still requested, plus the parties the direction classifier needs
		for key in (
			"document_type",
			"supplier_name",
			"customer_name",
			"invoice_number",
			'"date"',
			"due_date",
			"currency",
			"items",
			"subtotal",
			"discount_amount",
			"taxes",
			"tax_rate",
			"tax_amount",
			"total_amount",
			"issuer_name",
			"recipient_name",
			"original_invoice_number",
			"delivery_date",
			"bom_item_name",
			"material_request_type",
		):
			self.assertIn(key, processor.EXTRACTION_PROMPT, key)
		captured = {}

		class Capture:
			def extract_document_data(self, content, file_type, prompt):
				captured["prompt"] = prompt
				return {}

		processor.DocumentProcessor(Capture()).process_file(b"", "png")
		self.assertEqual(captured["prompt"], processor.EXTRACTION_PROMPT)
		for rule in (
			"DEBIT MEMO",
			"HSN or SAC",
			"Never merge description lines into item_name",
			"Never report one component's rate",
			"Never invent customers, invoice lines",
		):
			self.assertIn(rule, processor.EXTRACTION_PROMPT)

	def test_b2_output_is_compatible_with_adapter_and_maps(self):
		c = SalesInvoiceAdapter(None).adapt(b2_output())
		self.assertEqual(c["invoice_date"], nowdate())  # B2 "date" key
		self.assertEqual(c["tax_amount"], 36)  # summed from CGST + SGST components
		self.assertIsNone(c["tax_rate"])
		self.assertEqual(len(c["items"]), 1)
		self.assertEqual(c["items"][0]["description"], "Blue, size L")

		outcome = map_document(b2_output(currency=self.company_currency))
		report = validate_outcome(outcome)
		self.assertEqual(outcome.target_doctype, "Sales Invoice")
		self.assertEqual(outcome.mapped["customer"], self.customer)
		self.assertEqual(outcome.mapped["items"][0]["item_code"], self.item)
		self.assertEqual(outcome.mapped["items"][0]["uom"], "Nos")  # "No." resolved to the UOM master
		self.assertFalse(report.blocking, [i.code for i in report.blocking])
		self.assertFalse(
			report.data_errors, [i.code for i in report.data_errors]
		)  # split GST totals are consistent
		self.assertIn("taxes_not_mapped", [i.code for i in report.warnings])

	def test_debit_memo_heading_is_never_auto_classified(self):
		outcome = map_document(b2_output(document_type="Debit Memo"))
		self.assertIsNone(outcome.target_doctype)  # falls back to Needs Review + manual Target DocType
		self.assertIsNone(outcome.definition)

	def test_row_numbers_and_hsn_codes_do_not_hijack_item_matching(self):
		for bogus in ("1", "998314"):
			item = dict(b2_output()["items"][0], item_code=bogus)
			outcome = map_document(b2_output(items=[item]))
			self.assertEqual(outcome.results["item_1"].name, self.item, bogus)
			self.assertEqual(outcome.results["item_1"].match_type, "exact")

	def test_description_line_as_extra_item_is_not_silently_accepted(self):
		phantom = {
			"item_code": None,
			"item_name": "Blue, size L",
			"description": None,
			"qty": None,
			"uom": None,
			"rate": None,
			"amount": None,
		}
		outcome = map_document(b2_output(items=[b2_output()["items"][0], phantom]))
		report = validate_outcome(outcome)
		self.assertFalse(outcome.results["item_2"].matched)
		self.assertTrue({"item_not_found", "uom_unresolved"} & {i.code for i in report.blocking})
		self.assertTrue({"qty_missing", "rate_missing"} <= {i.code for i in report.data_errors})

	def test_invalid_uom_and_totals_are_rejected(self):
		item = dict(b2_output()["items"][0], uom="Zorkmid Bundles")
		report = validate_outcome(map_document(b2_output(items=[item])))
		self.assertIn("uom_unresolved", [i.code for i in report.blocking])
		report = validate_outcome(map_document(b2_output(total_amount=999)))
		self.assertIn("total_mismatch", [i.code for i in report.data_errors])

	def test_missing_masters_are_not_created(self):
		counts = {dt: frappe.db.count(dt) for dt in ("Customer", "Item", "UOM")}
		item = dict(b2_output()["items"][0], item_name="Nonexistent Gadget Q9", uom="Zorkmid Bundles")
		outcome = map_document(b2_output(customer_name="Nobody Buyer Q9", items=[item]))
		self.assertEqual(outcome.results["customer"].status, FieldStatus.NOT_FOUND)
		self.assertIsNone(outcome.mapped["customer"])
		self.assertEqual({dt: frappe.db.count(dt) for dt in counts}, counts)

	# --- provider error handling ---------------------------------------------------------
	def test_error_classification_is_safe(self):
		cases = [
			(
				api_error(
					openai.RateLimitError,
					429,
					f"Rate limit reached in organization {ORG} on tokens per minute (TPM)",
				),
				"rate_limited",
			),
			(
				api_error(
					openai.RateLimitError,
					429,
					f"Rate limit reached in organization {ORG} on tokens per day (TPD)",
				),
				"quota_exhausted",
			),
			(api_error(openai.InternalServerError, 503, "Service Unavailable"), "unavailable"),
			(api_error(openai.AuthenticationError, 401, f"Invalid API Key gsk_abc {ORG}"), "auth"),
			(api_error(openai.NotFoundError, 404, "model not found"), "config"),
			(api_error(openai.APIStatusError, 402, "Insufficient credits"), "billing"),
			(openai.APITimeoutError(request=REQ), "timeout"),
			(openai.APIConnectionError(request=REQ), "network"),
			(ValueError("Expecting value"), "invalid_response"),
			(
				api_error(openai.BadRequestError, 400, "json_validate_failed: Failed to generate JSON"),
				"invalid_response",
			),
			(api_error(openai.BadRequestError, 400, "image too large"), "bad_request"),
		]
		for exc, category in cases:
			err = classify(exc)
			self.assertEqual(err.category, category, repr(exc))
			self.assertEqual(str(err), USER_MESSAGES[category])
			self.assertNotIn("org_", str(err))
			self.assertNotIn("gsk_", str(err))
		self.assertEqual(
			USER_MESSAGES["rate_limited"], "AI request limit reached. Please wait and try again."
		)
		self.assertEqual(
			USER_MESSAGES["unavailable"], "AI service is temporarily unavailable. Please try again shortly."
		)
		self.assertEqual(
			USER_MESSAGES["auth"],
			"AI service configuration needs attention. Please contact the administrator.",
		)
		self.assertEqual(USER_MESSAGES["timeout"], "AI extraction timed out. Please retry.")

	def test_provider_retries_and_timeout_are_bounded(self):
		p = OpenAIProvider(
			api_key="test-key", base_url=GROQ_URL, model="qwen/qwen3.8-27b", provider_name="Groq"
		)
		self.assertEqual(p.client.max_retries, 2)
		self.assertEqual(p.client.timeout, 90)

	def test_provider_never_logs_response_content(self):
		p = OpenAIProvider(
			api_key="test-key", base_url=GROQ_URL, model="qwen/qwen3.8-27b", provider_name="Groq"
		)
		content = json.dumps(b2_output(customer_name=SENTINEL))
		out = io.StringIO()
		with (
			mock.patch.object(p.client.chat.completions, "create", return_value=fake_completion(content)),
			self.assertLogs("ai_erpnext", level="INFO") as logs,
			contextlib.redirect_stdout(out),
			contextlib.redirect_stderr(out),
		):
			data = p.extract_document_data(b"\x89PNG fake", "png", "prompt")
		self.assertEqual(data["customer_name"], SENTINEL)
		joined = "\n".join(logs.output) + out.getvalue()
		self.assertNotIn(SENTINEL, joined)
		self.assertNotIn("test-key", joined)
		self.assertIn("provider=Groq", joined)
		self.assertIn("model=qwen/qwen3.8-27b", joined)
		self.assertIn("status=ok", joined)

	def test_provider_rejects_non_object_json(self):
		p = OpenAIProvider(api_key="k", base_url=GROQ_URL, model="m", provider_name="Groq")
		for content in ('[{"a": 1}]', "not json", "```json\n{}\n```"):
			with mock.patch.object(
				p.client.chat.completions, "create", return_value=fake_completion(content)
			):
				with self.assertRaises(AIProviderError) as ctx:
					p.extract_document_data(b"x", "png", "prompt")
			self.assertEqual(ctx.exception.category, "invalid_response")

	# --- pipeline: safe failure, no fallback, no reprocessing --------------------------------
	def _entry(self, status):
		name = (
			frappe.get_doc({"doctype": "AI Document Entry", "status": "Processed", "file": self.image_url})
			.insert()
			.name
		)
		frappe.db.set_value("AI Document Entry", name, "status", status)
		return name

	@contextlib.contextmanager
	def _groq_settings(self):
		real = frappe.get_single
		settings = SimpleNamespace(
			provider="Groq",
			api_key="set",
			base_url=GROQ_URL,
			model="qwen/qwen3.8-27b",
			get_password=lambda field: "test-key",
		)
		with (
			mock.patch(
				"frappe.get_single",
				side_effect=lambda dt: settings if dt == "AI Provider Settings" else real(dt),
			),
			mock.patch.object(frappe.db, "commit"),
		):
			yield

	def test_pipeline_failure_is_safe_and_has_no_fallback(self):
		name = self._entry("Pending")
		error = api_error(
			openai.RateLimitError, 429, f"Rate limit reached in organization {ORG} ... customer {SENTINEL}"
		)
		created = []
		real_init = OpenAIProvider.__init__

		def tracking_init(self_, *a, **kw):
			created.append(kw.get("base_url"))
			real_init(self_, *a, **kw)

		out = io.StringIO()
		with (
			self._groq_settings(),
			mock.patch("openai.resources.chat.completions.Completions.create", side_effect=error) as create,
			mock.patch.object(OpenAIProvider, "__init__", tracking_init),
			mock.patch(
				"ai_erpnext.ai.gemini.GeminiProvider.__init__", side_effect=AssertionError("fallback used")
			),
			self.assertLogs("ai_erpnext", level="INFO") as logs,
			contextlib.redirect_stdout(out),
		):
			from ai_erpnext.pipeline.controller import run_ai_pipeline

			run_ai_pipeline(name)

		doc = frappe.get_doc("AI Document Entry", name)
		self.assertEqual(doc.status, "Failed")
		self.assertEqual(doc.error_log, USER_MESSAGES["rate_limited"])
		self.assertEqual(create.call_count, 1)  # SDK-level retries are inside this single call
		self.assertEqual(created, [GROQ_URL])  # exactly one provider, Groq; no fallback
		joined = "\n".join(logs.output) + out.getvalue()
		for secret in (ORG, SENTINEL, "test-key"):
			self.assertNotIn(secret, joined)
		self.assertIn("category=rate_limited", joined)

	def test_pipeline_success_logs_metadata_only(self):
		name = self._entry("Pending")
		content = json.dumps(b2_output(customer_name=SENTINEL))
		out = io.StringIO()
		with (
			self._groq_settings(),
			mock.patch(
				"openai.resources.chat.completions.Completions.create", return_value=fake_completion(content)
			),
			self.assertLogs("ai_erpnext", level="INFO") as logs,
			contextlib.redirect_stdout(out),
		):
			from ai_erpnext.pipeline.controller import run_ai_pipeline

			run_ai_pipeline(name)
		doc = frappe.get_doc("AI Document Entry", name)
		self.assertEqual(
			json.loads(doc.extracted_data)["customer_name"], SENTINEL
		)  # stored for review, not logged
		self.assertEqual(doc.target_doctype, "Sales Invoice")
		self.assertNotIn(SENTINEL, "\n".join(logs.output) + out.getvalue())

	def test_pipeline_does_not_reprocess_finished_entries(self):
		for status in (DocumentStatus.NEEDS_REVIEW, DocumentStatus.CREATED, "Failed"):
			name = self._entry(status)
			with (
				self._groq_settings(),
				mock.patch("openai.resources.chat.completions.Completions.create") as create,
			):
				from ai_erpnext.pipeline.controller import run_ai_pipeline

				run_ai_pipeline(name)
			create.assert_not_called()
			self.assertEqual(frappe.db.get_value("AI Document Entry", name, "status"), status)

	def test_retry_requeues_failed_entries_only_once(self):
		name = self._entry("Failed")
		from ai_erpnext.api.extraction import retry_extraction

		with mock.patch("frappe.enqueue") as enqueue:
			result = retry_extraction(name)
		self.assertEqual(result["status"], "Pending")
		self.assertEqual(enqueue.call_count, 1)
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], f"ai-extract-{name}")
		self.assertTrue(kwargs["deduplicate"])
		with self.assertRaises(frappe.ValidationError):
			retry_extraction(self._entry(DocumentStatus.NEEDS_REVIEW))

	def test_saving_pending_entry_enqueues_deduplicated_job(self):
		with mock.patch("frappe.enqueue") as enqueue:
			doc = frappe.get_doc(
				{"doctype": "AI Document Entry", "status": "Pending", "file": self.image_url}
			).insert()
		self.assertEqual(enqueue.call_args.kwargs["job_id"], f"ai-extract-{doc.name}")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
