"""Phase B: the open record and the current list through model-routed requests, context-aware creation, supplier-side
outstanding parity, returns, and the launcher permission flag.

The router model is mocked (it only returns plans); the opaque reference it may use ("open_record") is resolved on the
server from the validated page context, never from a name the model wrote. All values are asserted against fixtures.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_page_reference_and_parity
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from ai_erpnext.api import chat
from ai_erpnext.chat import queries

ROUTE = "ai_erpnext.chat.llm.route"
ROLES = ["Accounts User", "Sales User", "Purchase User", "Stock User"]


def run(plan):
	return queries.execute(queries.validate(plan))


class ReferenceCase(IntegrationTestCase):
	n = 0

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		try:
			cls.make_fixtures()
		except Exception:
			frappe.db.rollback()  # a failed setup must not leave half its fixtures committed for the next run
			raise

	@classmethod
	def make_fixtures(cls):
		frappe.set_user("Administrator")
		cls.company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
		cls.currency = frappe.get_cached_value("Company", cls.company, "default_currency")
		country = frappe.db.get_single_value("System Settings", "country") or "India"
		cls.other = "AIREF Other Company"
		if not frappe.db.exists("Company", cls.other):
			frappe.get_doc({"doctype": "Company", "company_name": cls.other, "abbr": "AIREFO", "default_currency": cls.currency, "country": country,
				"create_chart_of_accounts_based_on": "Standard Template", "chart_of_accounts": "Standard"}).insert()
		cg = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		te = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		sg = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		cls.customer = frappe.get_doc({"doctype": "Customer", "customer_name": "AIREF Customer", "customer_group": cg, "territory": te}).insert().name
		cls.customer2 = frappe.get_doc({"doctype": "Customer", "customer_name": "AIREF Customer Two", "customer_group": cg, "territory": te}).insert().name
		cls.suppliers = {k: frappe.get_doc({"doctype": "Supplier", "supplier_name": f"AIREF Supplier {k}", "supplier_group": sg}).insert().name for k in "ABC"}
		for code, stock in (("AIREF-SVC", 0), ("AIREF-STK", 1)):
			frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": f"{code} name", "item_group": ig, "stock_uom": "Nos",
				"is_stock_item": stock, "is_sales_item": 1, "is_purchase_item": 1, "valuation_rate": 5}).insert()
		cls.warehouse = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "AIREF Stores", "company": cls.company}).insert().name
		# stock: 12 units of AIREF-STK received into AIREF Stores
		se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": cls.company,
			"items": [{"item_code": "AIREF-STK", "qty": 12, "t_warehouse": cls.warehouse, "basic_rate": 5}]}).insert()
		se.submit()  # test data only: the assistant never submits
		# Sales Invoices of the customer: 2 submitted (outstanding 100 + 250), 1 draft
		cls.si = []
		for rate, submit in ((100, True), (250, True), (999, False)):
			si = frappe.get_doc({"doctype": "Sales Invoice", "customer": cls.customer, "company": cls.company, "posting_date": nowdate(),
				"due_date": nowdate(), "items": [{"item_code": "AIREF-SVC", "qty": 1, "rate": rate}]}).insert()
			if submit:
				si.submit()
			cls.si.append(si.name)
		cls.credit_note = frappe.get_doc({"doctype": "Sales Invoice", "customer": cls.customer, "company": cls.company, "posting_date": nowdate(),
			"is_return": 1, "items": [{"item_code": "AIREF-SVC", "qty": -1, "rate": 40}]}).insert()
		cls.credit_note.submit()
		cls.so = [frappe.get_doc({"doctype": "Sales Order", "customer": c, "company": cls.company, "transaction_date": nowdate(),
			"delivery_date": add_days(nowdate(), 3), "items": [{"item_code": "AIREF-SVC", "qty": 1, "rate": 10, "delivery_date": add_days(nowdate(), 3)}]}).insert().name
			for c in (cls.customer, cls.customer, cls.customer2)]
		# Purchase Invoices: supplier A 300 + 200 (one partly offset by a standalone debit note of 50), supplier B 70 in the other
		# company, supplier C a cancelled 999 and a paid-in-full draft that never counts
		cls.pi = {}
		for key, supplier, company, rate in (("A1", "A", cls.company, 300), ("A2", "A", cls.company, 200), ("B1", "B", cls.other, 70), ("C1", "C", cls.company, 999)):
			pi = frappe.get_doc({"doctype": "Purchase Invoice", "supplier": cls.suppliers[supplier], "company": company, "posting_date": nowdate(),
				"bill_no": f"AIREF-{key}", "items": [{"item_code": "AIREF-SVC", "qty": 1, "rate": rate}]})
			if company == cls.other:
				pi.items[0].expense_account = frappe.db.get_value("Account", {"company": cls.other, "root_type": "Expense", "is_group": 0}, "name")
				pi.items[0].cost_center = frappe.db.get_value("Company", cls.other, "cost_center")
			pi.insert()
			pi.submit()
			cls.pi[key] = pi.name
		frappe.get_doc("Purchase Invoice", cls.pi["C1"]).cancel()
		debit = frappe.get_doc({"doctype": "Purchase Invoice", "supplier": cls.suppliers["A"], "company": cls.company, "posting_date": nowdate(),
			"is_return": 1, "bill_no": "AIREF-DN1", "items": [{"item_code": "AIREF-SVC", "qty": -1, "rate": 50}]}).insert()
		debit.submit()
		cls.debit_note = debit.name

	def setUp(self):
		frappe.set_user("Administrator")
		mock.patch("frappe.enqueue").start()
		self.route = mock.patch(ROUTE, return_value={"mode": "chat", "reply": "general answer"}).start()
		self.addCleanup(mock.patch.stopall)
		self.s = chat.new_conversation()["session"]

	def form(self, doctype, name):
		return {"v": 2, "page": "form", "route": ["Form", doctype, name], "doctype": doctype, "name": name,
			"modified": str(frappe.db.get_value(doctype, name, "modified")), "dirty": False}

	def list_page(self, doctype="Sales Invoice", filters=None, selected=None):
		ctx = {"v": 2, "page": "list", "route": ["List", doctype, "List"], "doctype": doctype, "filters": filters or []}
		if selected:
			ctx.update(selected=selected, selected_count=len(selected))
		return ctx

	def say(self, text, context=None, plan=None):
		if plan is not None:
			self.route.return_value = plan
		r = chat.send_message(session=self.s, text=text, context=json.dumps(context) if context is not None else None)
		return [m for m in r["messages"] if m["role"] == "assistant"]

	def text(self, msgs):
		return " ".join(m.get("text") or "" for m in msgs)

	def card_names(self, msgs):
		return [r["_name"] for m in msgs if (m.get("card") or {}).get("rows") for r in m["card"]["rows"] if r.get("_name")]

	def state(self):
		return json.loads(frappe.db.get_value("AI Chat Session", self.s, "state"))


class TestOpenRecordReference(ReferenceCase):
	def test_outstanding_for_this_customer_through_the_model(self):
		plan = {"mode": "query", "query": {"doctype": "Customer", "operation": "details", "fields": ["outstanding"], "ref": "open_record"}}
		msgs = self.say("What is the total outstanding amount for this customer?", self.form("Customer", self.customer), plan)
		text = self.text(msgs)
		# 100 + 250 submitted, the 40 credit note nets it down; the draft (999) never counts
		self.assertIn("310.0", text)
		self.assertIn("AIREF Customer", text)
		self.assertNotIn("999", text)
		self.assertNotIn(self.customer, json.dumps(self.route.call_args.kwargs.get("page") or {}))  # the model never got the name

	def test_stock_for_this_item_follows_the_link_to_the_open_record(self):
		plan = {"mode": "query", "query": {"doctype": "Bin", "operation": "list", "ref": "open_record"}}
		msgs = self.say("Show stock for this item.", self.form("Item", "AIREF-STK"), plan)
		card = next(m["card"] for m in msgs if m.get("card"))
		self.assertEqual([(r["item_code"], r["warehouse"], r["actual_qty"]) for r in card["rows"]], [("AIREF-STK", self.warehouse, 12)])

	def test_latest_sales_orders_of_this_customer(self):
		plan = {"mode": "query", "query": {"doctype": "Sales Order", "operation": "list", "sort": "newest", "ref": "open_record"}}
		msgs = self.say("Show the latest sales orders for this customer", self.form("Customer", self.customer), plan)
		self.assertEqual(sorted(self.card_names(msgs)), sorted(self.so[:2]))

	def test_no_open_record_or_an_unrelated_one_never_widens_the_query(self):
		plan = {"mode": "query", "query": {"doctype": "Sales Order", "operation": "list", "ref": "open_record"}}
		msgs = self.say("Show the sales orders for this customer", None, plan)
		self.assertFalse(self.card_names(msgs))
		self.assertIn("open", self.text(msgs).lower())
		msgs = self.say("Show the sales orders for this one", self.form("Warehouse", self.warehouse), plan)  # no link from Sales Order to Warehouse
		self.assertFalse(self.card_names(msgs))

	def test_a_forged_or_inaccessible_open_record_is_not_used(self):
		plan = {"mode": "query", "query": {"doctype": "Customer", "operation": "details", "fields": ["outstanding"], "ref": "open_record"}}
		forged = dict(self.form("Customer", self.customer), name="AIREF No Such Customer")
		msgs = self.say("outstanding of this customer?", forged, plan)
		self.assertNotIn("310", self.text(msgs))
		user = "airef-restricted@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "R", "send_welcome_email": 0,
				"roles": [{"role": r} for r in ["System Manager", *ROLES]]}).insert(ignore_permissions=True)
			frappe.get_doc({"doctype": "User Permission", "user": user, "allow": "Customer", "for_value": self.customer2, "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
		frappe.set_user(user)
		self.s = chat.new_conversation()["session"]
		msgs = self.say("outstanding of this customer?", self.form("Customer", self.customer), plan)
		self.assertNotIn("310", self.text(msgs))

	def test_follow_up_keeps_the_record_and_a_page_change_switches_it(self):
		plan = {"mode": "query", "query": {"doctype": "Sales Order", "operation": "list", "ref": "open_record"}}
		self.say("sales orders of this customer", self.form("Customer", self.customer), plan)
		msgs = self.say("and for this customer?", self.form("Customer", self.customer2), plan)
		self.assertEqual(self.card_names(msgs), [self.so[2]])


class TestContextAwareCreation(ReferenceCase):
	def form_values(self):
		return self.state()["forms"][-1]["values"]

	def test_create_a_quotation_for_this_customer(self):
		plan = {"mode": "create", "doctype": "Quotation", "values": {"party": "open_record"}, "items": []}
		self.say("Make a quotation for this customer.", self.form("Customer", self.customer), plan)
		self.assertEqual(self.form_values()["party_name"], self.customer)

	def test_a_supplier_is_never_put_into_a_customer_field(self):
		plan = {"mode": "create", "doctype": "Sales Invoice", "values": {"party": "open_record"}, "items": []}
		msgs = self.say("Create a Sales Invoice for this one.", self.form("Supplier", self.suppliers["A"]), plan)
		self.assertNotIn("customer", self.form_values())
		self.assertIn("isn't a customer", self.text(msgs))

	def test_create_a_purchase_order_for_this_supplier_without_the_ai_service(self):
		from ai_erpnext.ai.errors import AIProviderError

		self.route.side_effect = AIProviderError("quota_exhausted")
		self.say("Create a Purchase Order for this supplier", self.form("Supplier", self.suppliers["A"]))
		self.assertEqual(self.form_values()["supplier"], self.suppliers["A"])

	def test_create_a_purchase_receipt_for_these_items(self):
		plan = {"mode": "create", "doctype": "Purchase Receipt", "values": {}, "items": [{"item": "selected_records"}]}
		self.say("Create a Purchase Receipt for these items.", self.list_page("Item", selected=["AIREF-STK", "AIREF-SVC"]), plan)
		form = self.state()["forms"][-1]
		queued = [q["text"] for q in form["queued_items"]] + ([form["draft_item"]["item_code"]] if (form.get("draft_item") or {}).get("item_code") else [])
		self.assertEqual(sorted(queued), ["AIREF-STK", "AIREF-SVC"])  # the selected items, each still asked for its quantity

	def test_the_open_record_is_rechecked_before_it_is_used(self):
		plan = {"mode": "create", "doctype": "Quotation", "values": {"party": "open_record"}, "items": []}
		self.say("Make a quotation for this customer.", dict(self.form("Customer", self.customer), name="AIREF Not A Customer"), plan)
		self.assertNotIn("party_name", self.form_values())


class TestCurrentList(ReferenceCase):
	def filtered(self):
		return self.list_page(filters=[["Sales Invoice", "customer", "=", self.customer], ["Sales Invoice", "docstatus", "=", 1]])

	def test_show_my_current_list_with_its_filters(self):
		for phrase in ("Show my current list with its applied filters", "Show my current list.", "What is in this filtered list?",
			"Summarize the records currently visible."):
			msgs = self.say(phrase, self.filtered())
			text = self.text(msgs)
			if "Summarize" in phrase:
				self.assertIn("3", text, phrase)  # count of the submitted invoices of the customer (2 + credit note)
				continue
			self.assertEqual(sorted(self.card_names(msgs)), sorted([self.si[0], self.si[1], self.credit_note.name]), phrase)
			if "filters" in phrase:
				self.assertIn("customer", text.lower())
		self.route.assert_not_called()  # answered from the page, no model call

	def test_count_the_records_in_my_current_list(self):
		msgs = self.say("Count the items in my current list", self.filtered())
		self.assertIn("There are 3 Sales Invoice records", self.text(msgs))

	def test_an_unapplicable_filter_is_explained_not_dropped(self):
		page = self.list_page(filters=[["Sales Invoice", "remarks", "like", "%x%"]])
		msgs = self.say("Show my current list", page)
		self.assertFalse(self.card_names(msgs))
		self.assertIn("can't", self.text(msgs))

	def test_a_model_query_on_the_open_list_says_the_page_filters_were_not_applied(self):
		plan = {"mode": "query", "query": {"doctype": "Sales Invoice", "operation": "list", "limit": 20}}
		msgs = self.say("list the invoices from last week that are big", self.filtered(), plan)
		self.assertIn("doesn't use the filters of the list you have open", self.text(msgs))


class TestSupplierOutstanding(ReferenceCase):
	def rows(self, result):
		return {(r["_name"], r["currency"]): r["total"] for r in (result.get("card") or {}).get("rows", [])}

	def test_outstanding_by_supplier_is_net_per_currency_and_excludes_cancelled(self):
		result = run({"doctype": "Purchase Invoice", "operation": "outstanding_by_supplier"})
		rows = self.rows(result)
		self.assertEqual(rows[(self.suppliers["A"], self.currency)], 450)  # 300 + 200 - 50 debit note
		self.assertEqual(rows[(self.suppliers["B"], self.currency)], 70)
		self.assertNotIn(self.suppliers["C"], {k[0] for k in rows})  # cancelled
		self.assertEqual(result["card"]["doctype"], "Supplier")

	def test_outstanding_by_supplier_follows_company_permissions(self):
		user = "airef-co@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "C", "send_welcome_email": 0,
				"roles": [{"role": r} for r in ["System Manager", *ROLES]]}).insert(ignore_permissions=True)
			frappe.get_doc({"doctype": "User Permission", "user": user, "allow": "Company", "for_value": self.company, "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
		frappe.set_user(user)
		rows = self.rows(run({"doctype": "Purchase Invoice", "operation": "outstanding_by_supplier"}))
		self.assertIn(self.suppliers["A"], {k[0] for k in rows})
		self.assertNotIn(self.suppliers["B"], {k[0] for k in rows})  # only in the other company

	def test_payments_reduce_the_outstanding(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		pe = get_payment_entry("Purchase Invoice", self.pi["A2"])
		pe.reference_no, pe.reference_date = "AIREF-PAY", nowdate()
		pe.insert()
		pe.submit()  # test data only
		self.assertEqual(self.rows(run({"doctype": "Purchase Invoice", "operation": "outstanding_by_supplier"}))[(self.suppliers["A"], self.currency)], 250)

	def test_empty_result_and_customer_parity(self):
		result = run({"doctype": "Purchase Invoice", "operation": "outstanding_by_supplier", "filters": {"company": self.other}, "where": [
			{"field": "supplier", "op": "=", "value": self.suppliers["A"]}]})
		self.assertIn("No supplier", result["text"])
		customers = self.rows(run({"doctype": "Sales Invoice", "operation": "outstanding_by_customer"}))
		self.assertEqual(customers[(self.customer, self.currency)], 310)  # 100 + 250 - 40 credit note

	def test_a_supplier_summary_with_values(self):
		result = run({"doctype": "Supplier", "operation": "summary", "filters": {"search": "AIREF Supplier A"}})
		rows = {(r["document"], r["state"]): r for r in result["card"]["rows"]}
		invoices = rows[("Purchase Invoice", "Submitted")]
		self.assertEqual((invoices["n"], invoices["total"], invoices["outstanding"]), (3, 450, 450))


class TestReturns(ReferenceCase):
	def test_returns_are_filterable(self):
		result = run({"doctype": "Sales Invoice", "operation": "list", "where": [{"field": "is_return", "op": "=", "value": 1}],
			"filters": {"party": "AIREF Customer"}})
		self.assertEqual(result["names"], [self.credit_note.name])
		result = run({"doctype": "Sales Invoice", "operation": "list", "where": [{"field": "is_return", "op": "=", "value": 0}],
			"filters": {"party": "AIREF Customer"}, "limit": 20})
		self.assertNotIn(self.credit_note.name, result["names"])

	def test_credit_and_debit_notes_by_their_names(self):
		msgs = self.say("show credit notes")
		self.assertIn(self.credit_note.name, self.card_names(msgs))
		msgs = self.say("show debit notes")
		self.assertIn(self.debit_note, self.card_names(msgs))
		self.route.assert_not_called()

	def test_totals_say_that_returns_count_negative(self):
		result = run({"doctype": "Sales Invoice", "operation": "aggregate", "aggregate": {"fn": "sum", "field": "grand_total"},
			"filters": {"docstatus": "submitted", "party": "AIREF Customer"}})
		self.assertEqual(result["card"]["rows"][0]["value"], 310)
		self.assertIn("count as negative amounts", result["card"]["note"])


class TestLauncherFlag(IntegrationTestCase):
	def test_the_boot_flag_follows_the_chat_permission(self):
		from ai_erpnext.boot import boot_session

		frappe.set_user("Administrator")
		boot = frappe._dict()
		boot_session(boot)
		self.assertTrue(boot.ai_chat_enabled)
		user = "airef-noaccess@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "N", "send_welcome_email": 0, "roles": [{"role": "Accounts User"}]}).insert(ignore_permissions=True)
		frappe.set_user(user)
		boot = frappe._dict()
		boot_session(boot)
		self.assertFalse(boot.ai_chat_enabled)
		frappe.set_user("Administrator")

	def test_the_widget_reads_the_flag_and_still_relies_on_the_server(self):
		from pathlib import Path

		source = (Path(__file__).resolve().parents[1] / "public" / "js" / "ai_bot_v2.js").read_text()
		self.assertIn("frappe.boot.ai_chat_enabled", source)
		self.assertIn('"ai_erpnext.boot.boot_session"', (Path(__file__).resolve().parents[1] / "hooks.py").read_text())
