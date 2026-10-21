"""Invoice lifecycle and pipeline safety (Phase 1 hardening).

- Every creation path makes a Draft: Document.submit is made to fail for the whole test,
  so any submit attempt anywhere in the flow fails the test.
- The extraction job only reads files the user may read, passes raw bytes to the provider,
  and never writes extracted data into the Error Log.
- A lost extraction job is resumed once, then reported with Retry/Cancel.
- Malformed button payloads and malformed router plans don't break the conversation.
No AI provider is called: providers are fakes.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_lifecycle_safety
"""

import json
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.model.document import Document
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime, nowdate

from ai_erpnext.api import chat
from ai_erpnext.chat import llm
from ai_erpnext.pipeline import controller
from ai_erpnext.pipeline.review_service import apply_mapping
from ai_erpnext.tests.fixtures import test_file

SENTINEL = "ZZLIFECYCLE-SENTINEL-9917"


class FakeExtractor:
	provider_name, model = "Groq", "fake"

	def __init__(self, data=None):
		self.data, self.calls = data or {}, []

	def extract_document_data(self, content, file_type, prompt):
		self.calls.append({"content_type": type(content), "file_type": file_type})
		return self.data


def no_submit(*args, **kwargs):
	raise AssertionError("submit() must never be called by the assistant")


class TestLifecycleSafety(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
			"Global Defaults", "default_company"
		)
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "AILC Buyer",
				"customer_group": group,
				"territory": territory,
			}
		).insert()
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "AILC-ITEM",
				"item_name": "AILC Widget",
				"item_group": ig,
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 1,
				"is_purchase_item": 1,
			}
		).insert()
		for email in ("ailc-owner@example.com", "ailc-other@example.com"):
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": email.split("@")[0],
						"send_welcome_email": 0,
						"roles": [{"role": "System Manager"}],
					}
				).insert(ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self.enqueue = mock.patch("frappe.enqueue").start()
		for target in ("submit", "_submit"):
			mock.patch.object(Document, target, no_submit).start()
		self.addCleanup(mock.patch.stopall)

	def extraction(self, **extra):
		import random

		rate = random.randint(1000, 9999)  # unique total: the duplicate check compares customer/date/total
		data = {
			"document_type": "Sales Invoice",
			"supplier_name": self.company,
			"customer_name": "AILC Buyer",
			"invoice_number": "AILC-" + frappe.generate_hash(length=8),
			"date": nowdate(),
			"due_date": None,
			"currency": None,
			"items": [
				{
					"item_code": None,
					"item_name": "AILC Widget",
					"description": None,
					"qty": 1,
					"uom": "Nos",
					"rate": rate,
					"amount": rate,
				}
			],
			"subtotal": rate,
			"taxes": [],
			"tax_rate": None,
			"tax_amount": None,
			"total_amount": rate,
		}
		data.update(extra)
		return data

	def pending(self, session):
		return next(
			m["question"] for m in reversed(chat.get_session(session)["messages"]) if m.get("question")
		)

	# ---- draft only -------------------------------------------------------------------------
	def test_review_api_creates_a_draft(self):
		from ai_erpnext.api.creation import create_document

		entry = frappe.get_doc(
			{
				"doctype": "AI Document Entry",
				"status": "Processed",
				"file": test_file("png"),
				"extracted_data": json.dumps(self.extraction()),
			}
		).insert()
		apply_mapping(entry)
		entry.save()
		result = create_document(entry.name)
		self.assertTrue(result["created"], result.get("message"))
		self.assertEqual(result["docstatus"], 0)
		self.assertEqual(frappe.db.get_value(result["doctype"], result["name"], "docstatus"), 0)

	def test_second_create_never_duplicates_the_draft(self):
		from ai_erpnext.api.creation import create_document

		entry = frappe.get_doc(
			{
				"doctype": "AI Document Entry",
				"status": "Processed",
				"file": test_file("png"),
				"extracted_data": json.dumps(self.extraction()),
			}
		).insert()
		apply_mapping(entry)
		entry.save()
		before = frappe.db.count("Sales Invoice")
		first = create_document(entry.name)
		self.assertTrue(first["created"])
		with self.assertRaises(frappe.ValidationError):
			create_document(entry.name)  # double click, second tab, client retry
		self.assertEqual(frappe.db.count("Sales Invoice"), before + 1)
		with mock.patch("ai_erpnext.ai.providers.primary", side_effect=AssertionError("no AI call")):
			controller.run_ai_pipeline(entry.name)  # a re-delivered extraction job does nothing
		self.assertEqual(
			frappe.db.get_value("AI Document Entry", entry.name, "created_document"), first["name"]
		)

	def test_image_chat_flow_creates_a_draft(self):
		s = chat.new_conversation()["session"]
		with mock.patch("ai_erpnext.chat.llm.route", side_effect=AssertionError("no router call expected")):
			chat.send_message(
				session=s, text="Create a Sales Invoice from this image", file_url=test_file("png")
			)
			entry = frappe.get_doc(
				"AI Document Entry", frappe.db.get_value("AI Chat Session", s, "active_entry")
			)
			entry.extracted_data = json.dumps(self.extraction())
			apply_mapping(entry)
			entry.save()
			chat.poll(session=s, since=0)
			q = self.pending(s)
			self.assertEqual(q["kind"], "confirm")
			r = chat.send_message(
				session=s,
				action=json.dumps({"question_id": q["id"], "value": "__create__", "label": "Create"}),
			)
		card = next(m["card"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created")
		self.assertEqual(frappe.db.get_value("Sales Invoice", card["name"], "docstatus"), 0)

	def test_guided_form_ignores_docstatus_from_the_model(self):
		s = chat.new_conversation()["session"]
		plan = {
			"mode": "create",
			"doctype": "Sales Invoice",
			"values": {"docstatus": 1, "customer": "AILC Buyer", "posting_date": nowdate()},
			"items": [{"item": "AILC Widget", "qty": 2, "rate": 50}],
		}
		with mock.patch("ai_erpnext.chat.llm.route", return_value=plan):
			chat.send_message(
				session=s, text="create a sales invoice for AILC Buyer, 2 AILC Widget at 50, and submit it"
			)
		form = json.loads(frappe.db.get_value("AI Chat Session", s, "state"))["forms"][-1]
		self.assertNotIn("docstatus", form["values"])
		self.assertEqual(
			form["values"].get("customer"), frappe.db.get_value("Customer", {"customer_name": "AILC Buyer"})
		)
		transcript = []
		for _ in range(12):  # answer what is still asked until the confirmation
			m = next(m for m in reversed(chat.get_session(s)["messages"]) if m.get("question"))
			q, text = m["question"], m.get("text") or ""
			transcript.append(f"{q['kind']}: {text[:80]}")
			if q["kind"] == "form_confirm":
				break
			answer = (
				"__done__"
				if q["kind"] == "form_more_items"
				else "AILC-ITEM"
				if "item" in text.lower() and "which" in text.lower()
				else "2"
				if text.startswith("How many")
				else "50"
				if "rate" in text.lower()
				else next(
					(o["value"] for o in q.get("options") or [] if not str(o["value"]).startswith("__")), None
				)
			)
			self.assertIsNotNone(answer, "\n".join(transcript))
			chat.send_message(
				session=s, action=json.dumps({"question_id": q["id"], "value": answer, "label": answer})
			)
		self.assertEqual(q["kind"], "form_confirm", "\n".join(transcript))
		r = chat.send_message(
			session=s, action=json.dumps({"question_id": q["id"], "value": "__create__", "label": "Create"})
		)
		card = next(m["card"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created")
		self.assertEqual(frappe.db.get_value("Sales Invoice", card["name"], "docstatus"), 0)

	def test_document_builder_never_copies_docstatus(self):
		from ai_erpnext.registry.document_registry import get_definition

		definition = get_definition("Sales Invoice")
		self.assertNotIn("docstatus", definition.supported_fields)
		self.assertNotIn("docstatus", definition.supported_item_fields)

	# ---- extraction job ---------------------------------------------------------------------
	def _pending_entry(self, file_url, owner="Administrator"):
		frappe.set_user(owner)
		name = (
			frappe.get_doc({"doctype": "AI Document Entry", "status": "Processed", "file": file_url})
			.insert()
			.name
		)
		frappe.db.set_value("AI Document Entry", name, "status", "Pending")
		return name

	def _run(self, name, provider):
		with (
			mock.patch("ai_erpnext.ai.providers.primary", return_value=provider),
			mock.patch("ai_erpnext.ai.providers.get_settings", return_value=SimpleNamespace()),
			mock.patch.object(frappe.db, "commit"),
		):
			controller.run_ai_pipeline(name)
		return frappe.get_doc("AI Document Entry", name)

	def test_entry_cannot_use_another_users_private_file(self):
		other_file = test_file("png", owner="ailc-other@example.com")
		frappe.set_user("ailc-owner@example.com")
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": "AI Document Entry", "status": "Pending", "file": other_file}).insert()
		self.enqueue.assert_not_called()
		# Frappe would otherwise have attached the other user's file to this entry (and so shared it)
		self.assertFalse(
			frappe.db.exists("File", {"file_url": other_file, "attached_to_doctype": "AI Document Entry"})
		)

	def test_job_cannot_read_another_users_private_file(self):
		other_file = test_file("png", owner="ailc-other@example.com")
		name = self._pending_entry(
			test_file("jpg", owner="ailc-owner@example.com"), owner="ailc-owner@example.com"
		)
		frappe.db.set_value(
			"AI Document Entry", name, "file", other_file, update_modified=False
		)  # bypasses the save-time check
		provider = FakeExtractor({"customer_name": SENTINEL})
		doc = self._run(name, provider)  # the job runs as the user who queued it
		self.assertEqual(doc.status, "Failed")
		self.assertEqual(doc.error_log, controller.SAFE_FILE_ERROR)
		self.assertEqual(provider.calls, [])  # nothing was sent anywhere
		frappe.set_user("ailc-other@example.com")
		doc = self._run(
			self._pending_entry(other_file, owner="ailc-other@example.com"), FakeExtractor(self.extraction())
		)
		self.assertNotEqual(doc.status, "Failed")  # the file's owner may use it

	def test_job_result_save_runs_standard_validation(self):
		name = self._pending_entry(test_file("png"))
		with mock.patch("ai_erpnext.pipeline.controller.validate_ai_document") as validate:
			self._run(name, FakeExtractor(self.extraction()))
		self.assertEqual(validate.call_count, 1)  # no ignore_validate on the job's save

	def test_provider_receives_raw_bytes(self):
		import io

		from PIL import Image

		buf = io.BytesIO()
		Image.new("RGB", (32, 32), "white").save(
			buf, format="PNG"
		)  # bytes that windows-1252 happily "decodes"
		file_doc = frappe.get_doc(
			{"doctype": "File", "file_name": "ailc-white.png", "content": buf.getvalue(), "is_private": 1}
		).insert()
		provider = FakeExtractor(self.extraction())
		self._run(self._pending_entry(file_doc.file_url), provider)
		self.assertEqual(provider.calls, [{"content_type": bytes, "file_type": "png"}])

	def test_mapping_failure_error_log_has_no_document_data(self):
		name = self._pending_entry(test_file("png"))

		def failing_mapping(doc):
			customer = json.loads(doc.extracted_data)[
				"customer_name"
			]  # a local variable holding document data
			raise ValueError(f"cannot map {customer}")

		with mock.patch("ai_erpnext.pipeline.review_service.apply_mapping", failing_mapping):
			doc = self._run(name, FakeExtractor(self.extraction(customer_name=SENTINEL)))
		self.assertEqual(doc.status, "Needs Review")
		log = frappe.get_all(
			"Error Log", filters={"reference_name": name}, fields=["method", "error"], limit=1
		)
		self.assertTrue(log)
		self.assertIn("ValueError", log[0].error)
		self.assertIn("failing_mapping", log[0].error)  # the code path is kept for debugging
		self.assertNotIn(SENTINEL, log[0].error + (log[0].method or ""))

	# ---- resume -----------------------------------------------------------------------------
	def test_lost_extraction_is_requeued_once_then_offered_for_retry(self):
		from rq.job import JobStatus

		s = chat.new_conversation()["session"]
		chat.send_message(session=s, text="invoice", file_url=test_file("png"))
		entry = frappe.db.get_value("AI Chat Session", s, "active_entry")
		self.enqueue.reset_mock()
		status = mock.patch("frappe.utils.background_jobs.get_job_status").start()

		status.return_value = None
		self.assertTrue(chat.poll(session=s, since=0)["busy"])  # just queued: wait
		self.enqueue.assert_not_called()

		old = add_to_date(now_datetime(), minutes=-10)
		frappe.db.set_value("AI Document Entry", entry, "modified", old, update_modified=False)
		status.return_value = JobStatus.STARTED
		chat.poll(session=s, since=0)
		self.enqueue.assert_not_called()  # a running job is never duplicated

		status.return_value = None  # the job vanished
		self.assertTrue(chat.poll(session=s, since=0)["busy"])
		self.assertEqual(self.enqueue.call_count, 1)
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], f"ai-extract-{entry}")
		chat.poll(session=s, since=0)
		self.assertEqual(self.enqueue.call_count, 1)  # not again right away

		st = json.loads(frappe.db.get_value("AI Chat Session", s, "state"))
		st["requeued_at"] = str(old)
		frappe.db.set_value("AI Chat Session", s, "state", json.dumps(st))
		r = chat.poll(session=s, since=0)
		self.assertFalse(r["busy"])
		self.assertEqual(frappe.db.get_value("AI Document Entry", entry, "status"), "Failed")
		self.assertEqual(self.pending(s)["kind"], "extraction_failed")
		self.assertEqual(self.enqueue.call_count, 1)

	def test_job_stuck_in_queue_is_reported(self):
		from rq.job import JobStatus

		s = chat.new_conversation()["session"]
		chat.send_message(session=s, text="invoice", file_url=test_file("png"))
		entry = frappe.db.get_value("AI Chat Session", s, "active_entry")
		frappe.db.set_value(
			"AI Document Entry",
			entry,
			"modified",
			add_to_date(now_datetime(), minutes=-10),
			update_modified=False,
		)
		self.enqueue.reset_mock()
		with mock.patch("frappe.utils.background_jobs.get_job_status", return_value=JobStatus.QUEUED):
			r = chat.poll(session=s, since=0)
		self.assertFalse(r["busy"])  # no worker is picking it up: say so instead of waiting forever
		self.assertIn("didn't finish", " ".join(m.get("text") or "" for m in r["messages"]))
		self.enqueue.assert_not_called()

	# ---- malformed input ------------------------------------------------------------------
	def test_malformed_action_payload_is_answered_not_raised(self):
		s = chat.new_conversation()["session"]
		for action in ("{not json", "[1, 2]", '"text"', "x" * 5000):
			r = chat.send_message(session=s, action=action)
			self.assertIn("couldn't be read", r["messages"][-1]["text"])

	def test_malformed_router_plan_is_contained(self):
		from ai_erpnext.tests.test_chat_router_retry import FakeProvider

		s = chat.new_conversation()["session"]
		before = frappe.db.count("Sales Invoice")
		bad = {"mode": "create", "doctype": ["Sales Invoice"], "values": "docstatus=1", "items": {"x": 1}}
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=FakeProvider(bad)):
			r = chat.send_message(session=s, text="please make me something for AILC Buyer")
		self.assertIn("can't create that", r["messages"][-1]["text"])
		with mock.patch(
			"ai_erpnext.chat.llm.get_text_provider",
			return_value=FakeProvider({"mode": "query", "query": "DROP TABLE"}),
		):
			r = chat.send_message(session=s, text="show me everything about AILC")
		self.assertIn("couldn't understand", r["messages"][-1]["text"])
		self.assertEqual(frappe.db.count("Sales Invoice"), before)
