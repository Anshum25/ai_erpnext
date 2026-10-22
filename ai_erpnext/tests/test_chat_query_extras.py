"""Query additions of 2026-10-22: “my …” filters, strict and “open” statuses, items at or below their re-order level, and
the comparison of two periods. Every figure is asserted against records created here (rolled back after the class).

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_query_extras
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_months, nowdate

from ai_erpnext.api import chat
from ai_erpnext.chat import queries, shortcuts

ROUTE = "ai_erpnext.chat.llm.route"
USER = "aiqx-user@example.com"


def run(plan):
	return queries.execute(queries.validate(plan))


def cells(result):
	return [row for row in (result.get("card") or {}).get("rows", [])]


class TestQueryExtras(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company")
		if not frappe.db.exists("User", USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": USER,
					"first_name": "AIQX",
					"send_welcome_email": 0,
					"roles": [
						{"role": r}
						for r in (
							"AI Assistant User",
							"Projects User",
							"Sales User",
							"Stock User",
							"Accounts User",
						)
					],
				}
			).insert(ignore_permissions=True)
		cls.tasks = {}
		for subject, assigned, status in (
			("AIQX mine open", True, "Open"),
			("AIQX mine done", True, "Completed"),
			("AIQX not mine", False, "Open"),
		):
			task = frappe.get_doc(
				{"doctype": "Task", "subject": subject, "status": status, "company": cls.company}
			).insert()
			if assigned:
				frappe.db.set_value("Task", task.name, "_assign", json.dumps([USER]))
			cls.tasks[subject] = task.name
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 0}, "name")
		group = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 1}, "name")
		for code, level, group_level in (
			("AIQX-LOW", 10, False),
			("AIQX-OK", 5, False),
			("AIQX-GRP", 10, True),
		):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_group": ig,
					"stock_uom": "Nos",
					"is_stock_item": 1,
					"is_sales_item": 1,
					"valuation_rate": 1,
					"reorder_levels": [
						{
							"warehouse": cls.warehouse,
							"warehouse_group": group if group_level else None,
							"warehouse_reorder_level": level,
							"warehouse_reorder_qty": 20,
							"material_request_type": "Purchase",
						}
					],
				}
			).insert()
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": cls.company,
				"items": [
					{"item_code": "AIQX-OK", "qty": 20, "t_warehouse": cls.warehouse, "basic_rate": 1},
					{"item_code": "AIQX-LOW", "qty": 3, "t_warehouse": cls.warehouse, "basic_rate": 1},
				],
			}
		).insert()
		receipt.submit()
		cls.customer = (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "AIQX Compare Buyer",
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
				"item_code": "AIQX-SVC",
				"item_group": ig,
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		for posting, rate in ((nowdate(), 100), (nowdate(), 50), (add_months(nowdate(), -1), 60)):
			si = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"customer": cls.customer,
					"company": cls.company,
					"posting_date": posting,
					"set_posting_time": 1,
					"due_date": posting,
					"items": [{"item_code": "AIQX-SVC", "qty": 1, "rate": rate}],
				}
			).insert()
			si.submit()
		cls.my_order = cls.sales_order(USER)
		cls.other_order = cls.sales_order("Administrator")

	@classmethod
	def sales_order(cls, owner):
		frappe.set_user(owner)
		name = (
			frappe.get_doc(
				{
					"doctype": "Sales Order",
					"customer": cls.customer,
					"company": cls.company,
					"transaction_date": nowdate(),
					"delivery_date": nowdate(),
					"items": [{"item_code": "AIQX-SVC", "qty": 1, "rate": 10, "delivery_date": nowdate()}],
				}
			)
			.insert()
			.name
		)
		frappe.set_user("Administrator")
		return name

	def setUp(self):
		self.route = mock.patch(ROUTE, return_value={"mode": "chat", "reply": "ok"}).start()
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.addCleanup(frappe.set_user, "Administrator")

	# --- my ---------------------------------------------------------------------------------------
	def test_my_open_tasks_are_the_open_tasks_assigned_to_me(self):
		frappe.set_user(USER)
		s = chat.new_conversation()["session"]
		r = chat.send_message(session=s, text="show my open tasks")
		self.route.assert_not_called()  # deterministic: no AI call
		payload = json.dumps(r["messages"][-1])
		self.assertIn(self.tasks["AIQX mine open"], payload)
		self.assertNotIn(self.tasks["AIQX mine done"], payload)
		self.assertNotIn(self.tasks["AIQX not mine"], payload)
		self.assertIn("assigned to you", r["messages"][-1]["card"]["note"])  # the applied filter is stated

	def test_my_sales_orders_are_the_ones_i_created_and_all_is_all(self):
		frappe.set_user(USER)
		mine = set(
			run({"doctype": "Sales Order", "operation": "list", "filters": {"mine": True}, "limit": 20})[
				"names"
			]
		)
		self.assertIn(self.my_order, mine)
		self.assertNotIn(self.other_order, mine)
		every = set(run({"doctype": "Sales Order", "operation": "list", "limit": 20})["names"])
		self.assertTrue({self.my_order, self.other_order} <= every)

	def test_the_my_word_is_never_dropped_by_the_shortcut(self):
		plan = shortcuts.match("show my sales orders")
		self.assertEqual(plan["query"]["filters"].get("mine"), True)
		self.assertNotIn("mine", shortcuts.match("show all sales orders")["query"]["filters"])

	# --- statuses -----------------------------------------------------------------------------------
	def test_open_sales_orders_means_not_yet_finished(self):
		q = queries.validate({"doctype": "Sales Order", "operation": "list", "filters": {"status": "open"}})
		self.assertIsInstance(q["status"], list)
		self.assertIn("To Deliver and Bill", q["status"])
		self.assertNotIn("Completed", q["status"])
		self.assertEqual(
			queries.validate({"doctype": "Task", "operation": "list", "filters": {"status": "open"}})[
				"status"
			],
			"Open",
		)

	def test_an_unknown_status_is_refused_never_ignored(self):
		with self.assertRaises(queries.QueryError) as e:
			queries.validate(
				{"doctype": "Sales Invoice", "operation": "list", "filters": {"status": "sparkly"}}
			)
		self.assertIn("isn't a status of Sales Invoice", str(e.exception))
		self.assertIn("Overdue", str(e.exception))  # the real statuses are listed

	# --- re-order level -------------------------------------------------------------------------------
	def test_items_at_or_below_their_reorder_level(self):
		frappe.set_user(USER)
		plan = shortcuts.match("Which items are below their reorder level?")
		self.assertEqual(plan["query"], {"doctype": "Bin", "operation": "below_reorder"})
		result = run(dict(plan["query"], filters={"search": "AIQX"}))
		rows = {r["item"]: r for r in cells(result)}
		self.assertEqual(set(rows), {"AIQX-LOW"})  # 3 on hand <= 10; AIQX-OK has 20 > 5
		self.assertEqual((rows["AIQX-LOW"]["projected_qty"], rows["AIQX-LOW"]["level"]), (3, 10))
		self.assertIn("warehouse group was not checked", result["text"])  # AIQX-GRP: never reported wrongly

	# --- comparison of two periods -------------------------------------------------------------------
	def test_compare_sales_this_month_with_last_month(self):
		plan = shortcuts.match("compare sales this month with last month")
		self.assertEqual(plan["query"]["operation"], "compare")
		query = dict(plan["query"], filters={"party": "AIQX Compare Buyer"})
		result = run(query)
		(row,) = cells(result)
		self.assertEqual((row["a"], row["b"]), ("150.0 (2)", "60.0 (1)"))
		self.assertEqual(row["change"], "+90.0 (+150.0%)")
		self.assertIn("negative amounts", result["text"])

	def test_a_comparison_needs_two_periods(self):
		with self.assertRaises(queries.QueryClarify):
			queries.validate(
				{"doctype": "Sales Invoice", "operation": "compare", "date": {"preset": "this_month"}}
			)
		with self.assertRaises(queries.QueryError):
			queries.validate({"doctype": "Customer", "operation": "compare"})  # not an operation of Customer
