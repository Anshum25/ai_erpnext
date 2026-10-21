"""Fresh conversations, conversation history and strict per-user isolation.

"New chat" must start an empty conversation with its own id and state; earlier conversations
keep their messages and pending work and can be reopened; a message or poll is only applied to
the conversation it names, and only while that is the user's active one; no user can read or
change another user's conversation, whatever id they send. The router model is mocked.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_sessions
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from ai_erpnext.api import chat
from ai_erpnext.chat import session as S

NO_MODEL = AssertionError("this message must not reach the AI model")


class TestChatSessions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		sg = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		cls.supplier = (
			frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": "AIS Session Supplier", "supplier_group": sg}
			)
			.insert()
			.name
		)
		cls.users = {}
		for key in ("a", "b"):
			email = f"ais-user-{key}@example.com"
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": f"AIS {key.upper()}",
						"send_welcome_email": 0,
						"roles": [
							{"role": "System Manager"},
							{"role": "Purchase Manager"},
							{"role": "Sales User"},
						],
					}
				).insert(ignore_permissions=True)
			cls.users[key] = email

	def setUp(self):
		self.as_user("a")
		mock.patch("ai_erpnext.chat.llm.route", side_effect=NO_MODEL).start()
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.addCleanup(frappe.set_user, "Administrator")

	def as_user(self, key):
		frappe.set_user(self.users[key])

	def state(self, session):
		return json.loads(frappe.db.get_value("AI Chat Session", session, "state") or "{}")

	def messages(self, session):
		return json.loads(frappe.db.get_value("AI Chat Session", session, "messages") or "[]")

	def click(self, session, value):
		q = self.state(session)["pending"]
		return chat.send_message(
			session=session, action=json.dumps({"question_id": q["id"], "value": value, "label": value})
		)

	# ---- fresh conversations ---------------------------------------------------------------------
	def test_fresh_browser_starts_its_own_conversation(self):
		"""Reproduction: a second browser (incognito) of the same user opened the first browser's conversation."""
		first = chat.get_session()["session"]  # browser 1 opens the assistant
		chat.send_message(session=first, text="create purchase order")
		second = chat.get_session()["session"]  # browser 2: no conversation selected yet
		self.assertNotEqual(second, first)
		self.assertNotIn("create purchase order", json.dumps(chat.get_session(second)))

	def test_same_user_has_independent_simultaneous_conversations(self):
		first = chat.get_session()["session"]  # normal browser
		second = chat.get_session()["session"]  # incognito window, same account
		chat.send_message(session=first, text="create purchase order")
		chat.send_message(session=second, text="create sales order")
		self.assertEqual(self.state(first)["forms"][-1]["doctype"], "Purchase Order")
		self.assertEqual(self.state(second)["forms"][-1]["doctype"], "Sales Order")
		for s in (first, second):  # both stay usable; neither was closed by the other
			self.assertEqual(frappe.db.get_value("AI Chat Session", s, "status"), "Open")
			self.assertNotIn("closed", chat.poll(session=s, since=0))
		self.assertNotIn("create sales order", json.dumps(self.messages(first)))
		self.assertNotIn("create purchase order", json.dumps(self.messages(second)))

	def test_new_chat_is_a_fresh_conversation_and_the_old_one_is_kept(self):
		first = chat.new_conversation()["session"]
		chat.send_message(session=first, text="create purchase order")
		old_messages, old_state = self.messages(first), self.state(first)
		r = chat.new_conversation()
		second = r["session"]
		self.assertNotEqual(second, first)
		self.assertEqual(
			[(m["role"], m.get("text")) for m in r["messages"]], [("assistant", r["messages"][0]["text"])]
		)  # greeting only
		self.assertNotIn("create purchase order", json.dumps(r))
		self.assertEqual(self.state(second).get("forms") or [], [])
		self.assertIsNone(self.state(second).get("pending"))
		self.assertEqual(
			(self.messages(first), self.state(first)), (old_messages, old_state)
		)  # untouched, pending work kept
		self.assertEqual(frappe.db.get_value("AI Chat Session", first, "status"), "Open")
		self.assertIn(first, {c["session"] for c in chat.list_conversations()})  # still in the history

	def test_fresh_page_gets_a_new_conversation_even_with_older_ones(self):
		older = chat.new_conversation()["session"]
		chat.send_message(session=older, text="create purchase order")
		fresh = chat.get_session()  # a page that has no conversation yet
		self.assertNotEqual(fresh["session"], older)
		self.assertEqual(len(fresh["messages"]), 1)  # only the greeting
		self.assertEqual(
			chat.get_session(fresh["session"])["session"], fresh["session"]
		)  # the page keeps its own

	def test_polling_and_rerendering_do_not_create_conversations(self):
		s = chat.get_session()["session"]
		count = lambda: frappe.db.count("AI Chat Session", {"owner": self.users["a"]})  # noqa: E731
		before = count()
		for _ in range(3):
			chat.poll(session=s, since=0)
			chat.get_session(s)  # the panel reopened / the page re-rendered with its id
		self.assertTrue(chat.poll(session=None, since=0)["closed"])  # a poll without an id creates nothing
		chat.list_conversations()
		self.assertEqual(count(), before)

	# ---- the right conversation only ------------------------------------------------------------
	def test_message_for_a_closed_conversation_is_not_redirected(self):
		first = chat.new_conversation()["session"]
		chat.send_message(session=first, text="create purchase order")  # asks for the supplier
		second = chat.new_conversation()["session"]
		chat.send_message(session=second, text="create purchase receipt")
		frappe.db.set_value(
			"AI Chat Session", first, "last_activity", add_days(now_datetime(), -(S.EXPIRY_DAYS + 1))
		)
		S.cleanup_expired_sessions()  # the first conversation expires while a page still shows it
		before = {s: (self.messages(s), self.state(s)) for s in (first, second)}
		r = chat.send_message(session=first, text=self.supplier, client_id="late-1")
		self.assertEqual((r["closed"], r["messages"], r["session"]), (True, [], first))
		self.assertEqual(
			{s: (self.messages(s), self.state(s)) for s in (first, second)}, before
		)  # neither conversation changed
		self.assertEqual(chat.poll(session=first, since=0)["messages"], [])  # a late poll gets nothing either
		self.assertTrue(chat.poll(session=first, since=0)["closed"])

	def test_answers_stay_in_their_conversation_and_old_work_can_be_resumed(self):
		first = chat.new_conversation()["session"]
		chat.send_message(session=first, text="create purchase order")
		old_question = self.state(first)["pending"]
		second = chat.new_conversation()["session"]
		# a button of the old conversation's question is not an answer in the new one
		r = chat.send_message(
			session=second, action=json.dumps({"question_id": old_question["id"], "value": self.supplier})
		)
		self.assertIn("That question is no longer active.", r["messages"][-1]["text"])
		self.assertEqual(self.state(second).get("forms") or [], [])
		# reopening the first conversation from the history resumes its pending question, and the answer lands there
		r = chat.open_conversation(first)
		self.assertEqual((r["session"], r["pending_id"]), (first, old_question["id"]))
		self.assertEqual(
			frappe.db.get_value("AI Chat Session", second, "status"), "Open"
		)  # the other page is unaffected
		self.click(first, self.supplier)
		self.assertEqual(self.state(first)["forms"][-1]["values"]["supplier"], self.supplier)
		self.assertEqual(self.state(second).get("forms") or [], [])

	def test_duplicate_delivery_still_returns_the_original_reply(self):
		s = chat.new_conversation()["session"]
		first = chat.send_message(session=s, text="create purchase order", client_id="dup-s-1")
		seq = self.state(s)["seq"]
		again = chat.send_message(session=s, text="create purchase order", client_id="dup-s-1")
		self.assertEqual(again["messages"], first["messages"])
		self.assertEqual(self.state(s)["seq"], seq)
		self.assertEqual(len(self.state(s)["forms"]), 1)

	def test_history_lists_own_conversations_only(self):
		first = chat.new_conversation()["session"]
		chat.send_message(session=first, text="create purchase order")
		second = chat.new_conversation()["session"]
		rows = chat.list_conversations()
		by_id = {r["session"]: r for r in rows}
		self.assertEqual(by_id[first]["title"], "create purchase order")
		self.assertNotIn(second, by_id)  # nothing written there yet
		self.assertEqual(set(rows[0]), {"session", "title", "last_activity"})  # metadata only
		self.as_user("b")
		self.assertFalse({first, second} & {r["session"] for r in chat.list_conversations()})

	def test_cleanup_removes_only_conversations_nobody_used(self):
		empty = chat.get_session()["session"]  # a page opened and left
		used = chat.get_session()["session"]
		chat.send_message(session=used, text="create purchase order")
		image_only = chat.get_session()["session"]
		chat.send_message(
			session=image_only, file_url="/private/files/not-an-upload.png"
		)  # no text, refused attachment
		day_ago = add_days(now_datetime(), -2)
		for s in (empty, used, image_only):
			frappe.db.set_value("AI Chat Session", s, "last_activity", day_ago)
		S.cleanup_expired_sessions()
		self.assertFalse(frappe.db.exists("AI Chat Session", empty))
		self.assertTrue(frappe.db.exists("AI Chat Session", used))
		self.assertTrue(frappe.db.exists("AI Chat Session", image_only))  # titled by the attachment message
		self.assertEqual(frappe.db.get_value("AI Chat Session", image_only, "title"), "Attachment")

	def test_expired_conversations_are_not_listed_or_reopened(self):
		old = chat.new_conversation()["session"]
		chat.new_conversation()
		frappe.db.set_value(
			"AI Chat Session", old, "last_activity", add_days(now_datetime(), -(S.EXPIRY_DAYS + 1))
		)
		self.assertNotIn(old, {r["session"] for r in chat.list_conversations()})
		with self.assertRaises(frappe.PermissionError):
			chat.open_conversation(old)

	# ---- other users ----------------------------------------------------------------------------
	def test_another_user_can_neither_read_nor_change_a_conversation(self):
		mine = chat.new_conversation()["session"]
		chat.send_message(session=mine, text="create purchase order")
		before = (self.messages(mine), self.state(mine))
		self.as_user("b")
		attempts = {
			"get_session": lambda: chat.get_session(mine),
			"send_message": lambda: chat.send_message(session=mine, text="hello"),
			"poll": lambda: chat.poll(session=mine, since=0),
			"open_conversation": lambda: chat.open_conversation(mine),
		}
		for name, attempt in attempts.items():
			with self.assertRaises(frappe.PermissionError, msg=name) as ctx:
				attempt()
			self.assertEqual(str(ctx.exception), "Conversation not found.", name)
		with self.assertRaises(frappe.PermissionError) as ctx:
			chat.send_message(session="no-such-session", text="hello")
		self.assertEqual(
			str(ctx.exception), "Conversation not found."
		)  # same answer: existence isn't revealed
		self.as_user("a")
		self.assertEqual((self.messages(mine), self.state(mine)), before)

	def test_next_user_in_the_same_browser_never_gets_the_previous_users_conversation(self):
		mine = chat.new_conversation()["session"]
		chat.send_message(session=mine, text="create purchase order")
		self.as_user("b")  # logout / login as someone else
		theirs = chat.get_session()  # what the widget loads on open
		self.assertNotEqual(theirs["session"], mine)
		self.assertEqual(frappe.db.get_value("AI Chat Session", theirs["session"], "owner"), self.users["b"])
		self.assertNotIn("create purchase order", json.dumps(theirs))
		self.assertEqual(self.state(theirs["session"]).get("forms") or [], [])

	def test_pending_invoice_upload_is_not_inherited_by_a_new_chat(self):
		from ai_erpnext.tests.fixtures import test_file

		first = chat.new_conversation()["session"]
		chat.send_message(session=first, text="invoice", file_url=test_file("png", owner=self.users["a"]))
		self.assertTrue(self.state(first).get("entry"))
		second = chat.new_conversation()["session"]
		self.assertIsNone(self.state(second).get("entry"))
		self.assertFalse(chat.poll(session=second, since=0)["busy"])
		self.assertIsNone(frappe.db.get_value("AI Chat Session", second, "active_entry"))
