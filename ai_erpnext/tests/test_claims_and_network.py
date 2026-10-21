"""Model-authored text can't claim actions or data (chat/claims.py), and tests can't reach the network (network_guard.py).

The router model is mocked to return the kinds of replies a model could invent: "I've created the invoice", a count of
records, an amount, a document ID, a report's result. None of it may reach the user; ordinary guidance must pass.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_claims_and_network
"""

import json
import logging
import socket
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.api import chat
from ai_erpnext.chat import claims
from ai_erpnext.tests import network_guard

FABRICATED = {
	"I've created the Sales Invoice ACC-SINV-2026-00777 for you.": "action",
	"Your invoice has been submitted successfully and the payment was successfully recorded.": "action",
	"You have 12 overdue invoices totalling ₹45,000.": "data",
	"The total outstanding is 98,500 INR across your customers.": "data",
	"Open ACC-PINV-2026-00042 to see it.": "data",
	"I ran the Accounts Receivable report for you.": "report",
	"The Profit and Loss report shows a profit of 3 lakh.": "report",
}
GUIDANCE = [
	"To create a Purchase Order, open Buying > Purchase Order > New, choose the supplier and add the items.",
	"Once an invoice has been submitted, it can't be edited: cancel and amend it instead.",
	"A Sales Invoice needs at least 1 item. Step 2: add the items with quantity and rate.",
	"Submitting posts the invoice to the general ledger and updates the outstanding amount.",
]


class TestClaimChecks(IntegrationTestCase):
	def test_fabricated_claims_are_detected(self):
		for text, reason in FABRICATED.items():
			self.assertIn(reason, claims.check(text), text)

	def test_guidance_passes_unchanged(self):
		for text in GUIDANCE:
			self.assertEqual(claims.check(text), set(), text)
			self.assertEqual(claims.guard(text), text)

	def test_figures_the_user_typed_may_be_repeated(self):
		self.assertEqual(claims.check("You asked about 5 invoices; I can look them up for you.", "show me 5 invoices"), set())
		self.assertEqual(claims.check("ACC-SINV-2026-00001 is a sales invoice number.", "what is ACC-SINV-2026-00001"), set())
		self.assertIn("data", claims.check("ACC-SINV-2026-00002 is overdue.", "what is ACC-SINV-2026-00001"))

	def test_each_reason_gets_its_own_truthful_sentence(self):
		self.assertEqual(claims.guard("I've created it."), claims.ACTION_TEXT)
		self.assertEqual(claims.guard("You have 7 unpaid invoices."), claims.DATA_TEXT)
		self.assertEqual(claims.guard("I ran the Balance Sheet report."), claims.REPORT_TEXT)


class TestModelRepliesInTheChat(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.s = chat.new_conversation()["session"]

	def say(self, text, plan):
		with mock.patch("ai_erpnext.chat.llm.route", return_value=plan):
			r = chat.send_message(session=self.s, text=text)
		return [m for m in r["messages"] if m["role"] == "assistant"]

	def test_chat_replies_never_claim_a_creation(self):
		counts = {dt: frappe.db.count(dt) for dt in ("Sales Invoice", "Purchase Order")}
		msgs = self.say("make me an invoice and submit it", {"mode": "chat", "reply": "Done! I've created and submitted Sales Invoice ACC-SINV-2026-00099."})
		text = " ".join(m.get("text") or "" for m in msgs)
		self.assertNotIn("ACC-SINV-2026-00099", text)
		self.assertIn(claims.ACTION_TEXT, text)
		self.assertEqual(msgs[0].get("source"), "general")  # never labelled as ERPNext data
		self.assertFalse(any(m.get("card") for m in msgs))
		self.assertEqual({dt: frappe.db.count(dt) for dt in counts}, counts)

	def test_chat_replies_never_state_data(self):
		msgs = self.say("how are we doing on receivables?", {"mode": "chat", "reply": "You have 23 overdue invoices worth $12,400 in total."})
		text = " ".join(m.get("text") or "" for m in msgs)
		self.assertNotIn("12,400", text)
		self.assertIn(claims.DATA_TEXT, text)

	def test_clarify_and_unsupported_text_is_checked_too(self):
		msgs = self.say("which one?", {"mode": "clarify", "question": "Do you mean the 14 invoices of ACME or SINV-2026-00777?"})
		self.assertIn(claims.DATA_TEXT, msgs[0]["text"])
		msgs = self.say("delete the old invoices", {"mode": "unsupported", "reply": "I have deleted the 3 old invoices."})
		self.assertIn(claims.ACTION_TEXT, msgs[0]["text"])
		self.assertIn("I never submit, cancel, delete or pay", msgs[0]["text"])

	def test_report_results_are_never_claimed(self):
		msgs = self.say("what does the P&L say?", {"mode": "chat", "reply": "I ran the Profit and Loss Statement: the report shows a profit of 4.2 lakh."})
		self.assertIn(claims.REPORT_TEXT, msgs[0]["text"])

	def test_ordinary_guidance_is_shown_as_written(self):
		reply = GUIDANCE[0]
		msgs = self.say("how do I create a purchase order?", {"mode": "chat", "reply": reply})
		self.assertTrue(msgs[0]["text"].startswith(reply))

	def test_a_blocked_claim_is_logged_without_its_text(self):
		logger = logging.getLogger("ai_erpnext.claims_test")
		with mock.patch("ai_erpnext.chat.claims.get_ai_logger", return_value=logger), self.assertLogs(logger, level="WARNING") as logs:
			self.say("anything", {"mode": "chat", "reply": "I've created Sales Invoice ZZSECRET-2026-00001 for ZZ Customer."})
		line = " ".join(logs.output)
		self.assertIn("claim_blocked", line)
		self.assertNotIn("ZZSECRET", line)
		self.assertNotIn("ZZ Customer", line)

	def test_stored_conversation_keeps_only_the_safe_text(self):
		self.say("x", {"mode": "chat", "reply": "I've created Sales Invoice ACC-SINV-2026-00555."})
		stored = frappe.db.get_value("AI Chat Session", self.s, "messages")
		self.assertNotIn("ACC-SINV-2026-00555", stored)
		self.assertIn(claims.ACTION_TEXT, json.loads(stored)[-1]["text"])


class TestNetworkGuard(IntegrationTestCase):
	def test_the_guard_is_active_for_every_test(self):
		self.assertTrue(network_guard.installed())

	def test_outbound_connections_are_blocked(self):
		for address in (("203.0.113.10", 443), ("8.8.8.8", 53), ("2001:db8::1", 443, 0, 0)):
			family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
			with socket.socket(family, socket.SOCK_STREAM) as s, self.assertRaises(network_guard.OutboundNetworkBlocked):
				s.connect(address)

	def test_loopback_still_works(self):
		with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
			with self.assertRaises(ConnectionRefusedError):  # refused by the OS, not blocked by the guard
				s.connect(("127.0.0.1", 9))
		self.assertTrue(frappe.db.sql("select 1"))  # the test database (on loopback) is reachable

	def test_an_unmocked_provider_request_cannot_leave_the_machine(self):
		import httpx

		with self.assertRaises((network_guard.OutboundNetworkBlocked, httpx.ConnectError)) as caught:
			httpx.Client(trust_env=False).get("https://api.groq.com/openai/v1/models", timeout=5)
		self.assertIn("blocked", repr(caught.exception).lower() + str(getattr(caught.exception, "__cause__", "")).lower())
