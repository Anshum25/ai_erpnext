"""Opt-in chat access through the "AI Assistant User" role (setup_doctypes.ensure_assistant_role).

The role only opens the chat: what a user can see or create still comes from their own ERPNext roles, User Permissions
and company restrictions. Users with neither the role nor System Manager can't use the chat at all, and only System
Managers can read the provider settings. Everything is created in the test transaction and rolled back.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_assistant_role
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from ai_erpnext import boot, setup_doctypes
from ai_erpnext.api import chat

ROUTE = "ai_erpnext.chat.llm.route"
ROLE = setup_doctypes.ASSISTANT_ROLE
OTHER_COMPANY = "AIR Other Co"


class TestAssistantRole(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		setup_doctypes.ensure_assistant_role()
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company")
		currency = frappe.db.get_value("Company", cls.company, "default_currency")
		if not frappe.db.exists("Company", OTHER_COMPANY):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": OTHER_COMPANY,
					"abbr": "AIRO",
					"default_currency": currency,
					"country": frappe.db.get_value("Company", cls.company, "country"),
					"create_chart_of_accounts_based_on": "Standard Template",
					"chart_of_accounts": "Standard",
				}
			).insert()
		customer = (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "AIR Shared Buyer",
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				}
			)
			.insert()
			.name
		)
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "AIR-ITEM",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 1,
			}
		).insert()
		cls.invoices = {}
		for company in (cls.company, OTHER_COMPANY):
			cls.invoices[company] = (
				frappe.get_doc(
					{
						"doctype": "Sales Invoice",
						"customer": customer,
						"company": company,
						"posting_date": nowdate(),
						"due_date": nowdate(),
						"items": [{"item_code": "AIR-ITEM", "qty": 1, "rate": 10}],
					}
				)
				.insert()
				.name
			)
		cls.users = {
			"assistant": cls.make_user("air-assistant@example.com", [ROLE, "Accounts User"], cls.company),
			"assistant2": cls.make_user("air-assistant2@example.com", [ROLE, "Accounts User"], cls.company),
			"plain": cls.make_user("air-plain@example.com", ["Accounts User"], cls.company),
		}
		frappe.cache.delete_value("user_permissions")

	@classmethod
	def make_user(cls, email, roles, company):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0],
					"send_welcome_email": 0,
					"roles": [{"role": r} for r in roles],
				}
			).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": email,
				"allow": "Company",
				"for_value": company,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		return email

	def setUp(self):
		self.route = mock.patch(ROUTE, return_value={"mode": "chat", "reply": "ok"}).start()
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.addCleanup(frappe.set_user, "Administrator")

	def as_user(self, key):
		frappe.set_user(self.users[key])

	def say(self, session, text, plan):
		self.route.return_value = plan
		return chat.send_message(session=session, text=text)

	# --- setup ---------------------------------------------------------------------------------
	def test_the_role_opens_only_the_chat_doctypes(self):
		rows = frappe.get_all(
			"DocPerm", filters={"role": ROLE}, fields=["parent", "read", "create", "delete", "if_owner"]
		)
		by_parent = {r.parent: r for r in rows}
		self.assertEqual(set(by_parent), {"AI Chat Session", "AI Document Entry"})
		self.assertTrue(all(r.if_owner for r in rows))
		self.assertEqual(by_parent["AI Document Entry"].delete, 0)  # extraction entries are the audit trail

	def test_ensure_runs_once_and_never_restores_removed_rows(self):
		with mock.patch.object(setup_doctypes.frappe, "get_doc") as get_doc:
			setup_doctypes.ensure_assistant_role()  # the role exists: nothing is touched
		get_doc.assert_not_called()
		self.assertEqual(frappe.db.count("DocPerm", {"role": ROLE, "parent": "AI Chat Session"}), 1)

	# --- access --------------------------------------------------------------------------------
	def test_without_the_role_the_chat_is_closed(self):
		self.as_user("plain")
		with self.assertRaises(frappe.PermissionError):
			chat.new_conversation()
		self.assertEqual(chat.list_conversations(), [])
		info = frappe._dict()
		boot.boot_session(info)
		self.assertFalse(info.ai_chat_enabled)

	def test_with_the_role_the_launcher_and_chat_are_available(self):
		self.as_user("assistant")
		info = frappe._dict()
		boot.boot_session(info)
		self.assertTrue(info.ai_chat_enabled)
		self.assertTrue(chat.new_conversation()["session"])

	def test_a_role_user_sees_only_their_permitted_companies(self):
		self.as_user("assistant")
		s = chat.new_conversation()["session"]
		r = self.say(
			s,
			"show sales invoices",
			{"mode": "query", "query": {"doctype": "Sales Invoice", "operation": "list", "limit": 20}},
		)
		payload = json.dumps(r)
		self.assertIn(self.invoices[self.company], payload)
		self.assertNotIn(self.invoices[OTHER_COMPANY], payload)
		r = self.say(
			s,
			f"show sales invoice {self.invoices[OTHER_COMPANY]}",
			{
				"mode": "query",
				"query": {
					"doctype": "Sales Invoice",
					"operation": "details",
					"filters": {"name": self.invoices[OTHER_COMPANY]},
				},
			},
		)
		self.assertNotIn("AIR Shared Buyer", json.dumps(r["messages"][-1]))

	def test_a_role_user_cannot_create_what_erpnext_does_not_allow_them(self):
		self.as_user("assistant")
		self.assertFalse(frappe.has_permission("Purchase Order", "create"))  # Accounts User only
		before = frappe.db.count("Purchase Order")
		s = chat.new_conversation()["session"]
		r = self.say(s, "create purchase order", {"mode": "create", "doctype": "Purchase Order"})
		self.assertIn("don't have permission", r["messages"][-1]["text"])
		self.assertEqual(frappe.db.count("Purchase Order"), before)

	def test_conversations_of_another_role_user_are_not_found(self):
		self.as_user("assistant")
		s = chat.new_conversation()["session"]
		self.say(s, "hello", {"mode": "chat", "reply": "hi"})
		self.as_user("assistant2")
		with self.assertRaises(frappe.PermissionError):
			chat.open_conversation(s)
		self.assertNotIn(s, [c["session"] for c in chat.list_conversations()])

	def test_provider_settings_stay_system_manager_only(self):
		for key in ("assistant", "plain"):
			self.assertFalse(frappe.has_permission("AI Provider Settings", "read", user=self.users[key]))
