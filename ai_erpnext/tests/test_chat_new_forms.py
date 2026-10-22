"""Guided creation added on 2026-10-22: Employee, Opportunity, Project, Task (saved when confirmed), Supplier Quotation,
Work Order, Timesheet and the restricted Journal Entry (Drafts), and the capability catalog that describes them.

The router model is mocked; everything else runs against the site database in a rolled-back transaction. Every created
record is asserted from the database (docstatus, values), and every refusal asserts that nothing was written.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_new_forms
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from ai_erpnext.api import chat
from ai_erpnext.capability import service as C
from ai_erpnext.capability.service import CapabilityError
from ai_erpnext.chat import capabilities, forms

ROUTE = "ai_erpnext.chat.llm.route"
EXCLUDED_JE_TYPES = ["Bank", "Cash", "Receivable", "Payable", "Stock"]


def create_plan(doctype):
	return {"mode": "create", "doctype": doctype, "values": {}, "items": []}


class TestNewForms(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company")
		cls.currency = frappe.get_cached_value("Company", cls.company, "default_currency")
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		for code, stock in (("AINF-BUY", 0), ("AINF-RM", 1), ("AINF-FG", 1)):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_group": ig,
					"stock_uom": "Nos",
					"is_stock_item": stock,
					"is_purchase_item": 1,
					"is_sales_item": 1,
				}
			).insert()
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": "AINF-FG",
				"company": cls.company,
				"quantity": 1,
				"is_active": 1,
				"is_default": 1,
				"items": [{"item_code": "AINF-RM", "qty": 2, "rate": 5}],
			}
		).insert()
		bom.submit()
		cls.bom = bom.name
		cls.supplier = (
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "AINF Parts Supplier",
					"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
				}
			)
			.insert()
			.name
		)
		cls.customer = (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "AINF Prospect Buyer",
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				}
			)
			.insert()
			.name
		)
		cls.employee = (
			frappe.get_doc(
				{
					"doctype": "Employee",
					"first_name": "AINF Meera",
					"last_name": "Iyer",
					"gender": "Female",
					"date_of_birth": "1992-04-01",
					"date_of_joining": "2024-01-01",
					"company": cls.company,
					"status": "Active",
				}
			)
			.insert()
			.name
		)
		accounts = frappe.get_all(
			"Account",
			filters={
				"company": cls.company,
				"is_group": 0,
				"disabled": 0,
				"root_type": "Expense",
				"account_currency": cls.currency,
				"account_type": ["not in", EXCLUDED_JE_TYPES],
			},
			pluck="name",
			order_by="name",
			limit=2,
		)
		cls.debit_account, cls.credit_account = accounts
		cls.cash_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "is_group": 0, "account_type": ["in", ["Cash", "Bank"]]},
			"name",
		)
		for email, role in (
			("ainf-accounts@example.com", "Accounts User"),
			("ainf-sales@example.com", "Sales User"),
		):
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": email.split("@")[0],
						"send_welcome_email": 0,
						"roles": [{"role": "AI Assistant User"}, {"role": role}],
					}
				).insert(ignore_permissions=True)

	def setUp(self):
		# IntegrationTestCase rolls back per class: each test also gets its own savepoint (what it creates is undone after it)
		frappe.set_user("Administrator")
		frappe.db.savepoint("ainf_test")
		self.addCleanup(frappe.db.rollback, save_point="ainf_test")
		self.route = mock.patch(ROUTE, return_value={"mode": "chat", "reply": "ok"}).start()
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.addCleanup(frappe.set_user, "Administrator")
		self.s = chat.new_conversation()["session"]

	# helpers ------------------------------------------------------------------------------------
	def say(self, text, plan=None):
		if plan is not None:
			self.route.return_value = plan
		return chat.send_message(session=self.s, text=text)

	def pending(self):
		return json.loads(frappe.db.get_value("AI Chat Session", self.s, "state") or "{}").get("pending")

	def click(self, value):
		q = self.pending()
		return chat.send_message(
			session=self.s, action=json.dumps({"question_id": q["id"], "value": value, "label": value})
		)

	def texts(self, n=4):
		return " ".join(m.get("text") or "" for m in chat.get_session(self.s)["messages"][-n:])

	def fill(self, answers=None, rows=(), limit=60):
		"""Answer the form's questions (header fields from `answers` by key, row fields from `rows` in order; anything else
		optional is skipped) until the preview. Returns the pending confirmation question."""
		answers, rows, current = dict(answers or {}), list(rows), None
		for _ in range(limit):
			q = self.pending()
			self.assertIsNotNone(q, self.texts())
			if q["kind"] == "form_confirm":
				return q
			if q["kind"] == "form_more_rows":
				current = None
				self.click("__more__" if rows else "__done__")
				continue
			key = q["key"]
			if key.startswith(forms.ROW_PREFIX):
				if current is None:
					current = rows.pop(0)
				value = current.get(key[len(forms.ROW_PREFIX) :], "__skip__")
			else:
				value = answers.get(key, "__skip__")
			if value == "__skip__":
				self.click("__skip__")
			else:
				self.say(str(value))
		self.fail(f"never reached the preview: {self.texts()}")

	def confirm(self):
		r = self.click("__create__")
		card = next(
			(m["card"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created"), None
		)
		self.assertIsNotNone(card, self.texts())
		return frappe.get_doc(card["doctype"], card["name"])

	def preview(self):
		"""The card of the preview shown with the pending confirmation."""
		messages = chat.get_session(self.s)["messages"]
		return next(
			m["card"] for m in reversed(messages) if (m.get("card") or {}).get("type") == "form_preview"
		)

	# masters saved when confirmed -------------------------------------------------------------------
	def test_employee_is_saved_active_only_after_confirmation(self):
		before = frappe.db.count("Employee")
		self.say("create employee", create_plan("Employee"))
		q = self.fill(
			{
				"first_name": "AINF Kiran",
				"last_name": "Rao",
				"gender": "Male",
				"date_of_birth": "1995-02-11",
				"date_of_joining": "2026-10-01",
			}
		)
		self.assertEqual(q["kind"], "form_confirm")
		self.assertIn("saved immediately", self.texts(1))
		labels = {r["label"]: r["value"] for r in self.preview()["rows"]}
		self.assertEqual(labels["Full name"], "AINF Kiran Rao")  # computed by ERPNext on the unsaved copy
		self.assertEqual(frappe.db.count("Employee"), before)  # the preview wrote nothing
		doc = self.confirm()
		self.assertEqual(
			(doc.employee_name, doc.gender, str(doc.date_of_birth), doc.status, doc.company),
			("AINF Kiran Rao", "Male", "1995-02-11", "Active", self.company),
		)

	def test_an_employee_born_in_the_future_is_refused_and_correctable(self):
		before = frappe.db.count("Employee")
		self.say("create employee", create_plan("Employee"))
		for key, value in (
			("first_name", "AINF Future"),
			("last_name", "__skip__"),
			("gender", "Female"),
			("date_of_birth", str(add_days(nowdate(), 5))),
			("date_of_joining", "2026-10-01"),
			("department", "__skip__"),
			("designation", "__skip__"),
		):
			self.assertEqual(self.pending()["key"], key)
			self.click(value) if value == "__skip__" else self.say(value)
		self.assertIn("didn't accept", self.texts())
		self.assertIn("Nothing was created", self.texts())
		self.assertEqual(frappe.db.count("Employee"), before)
		self.say("change date of birth to 1994-03-03")
		self.assertEqual(self.pending()["kind"], "form_confirm")
		self.assertEqual(self.confirm().date_of_birth.isoformat(), "1994-03-03")

	def test_a_user_without_hr_rights_cannot_start_an_employee(self):
		frappe.set_user("ainf-accounts@example.com")
		self.s = chat.new_conversation()["session"]
		self.say("create employee", create_plan("Employee"))
		self.assertIn("don't have permission to create Employee", self.texts())
		self.assertIsNone(self.pending())

	def test_opportunity_for_an_existing_customer(self):
		self.say("create opportunity", create_plan("Opportunity"))
		self.fill({"party_name": "AINF Prospect Buyer", "expected_closing": "2026-12-31"})
		doc = self.confirm()
		self.assertEqual(
			(doc.opportunity_from, doc.party_name, doc.company, str(doc.expected_closing)),
			("Customer", self.customer, self.company, "2026-12-31"),
		)

	def test_project_then_a_task_under_it(self):
		self.say("create project", create_plan("Project"))
		self.fill({"project_name": "AINF Warehouse Move", "expected_end_date": "2026-12-15"})
		project = self.confirm()
		self.assertEqual((project.project_name, project.company), ("AINF Warehouse Move", self.company))
		self.say("create task", create_plan("Task"))
		self.fill(
			{"subject": "AINF Pack shelves", "project": "AINF Warehouse Move", "exp_end_date": "2026-11-30"}
		)
		task = self.confirm()
		self.assertEqual(
			(task.subject, task.project, str(getdate(task.exp_end_date))),
			("AINF Pack shelves", project.name, "2026-11-30"),
		)

	# drafts -------------------------------------------------------------------------------------------
	def test_supplier_quotation_is_a_draft(self):
		self.say("create supplier quotation", create_plan("Supplier Quotation"))
		q = self.pending()
		self.assertEqual(q["key"], "supplier")
		self.say("AINF Parts Supplier")
		self.click("__skip__")  # valid till
		self.say("AINF-BUY")
		self.say("4")
		self.say("12.5")
		self.click("__done__")
		self.assertEqual(self.pending()["kind"], "form_confirm")
		doc = self.confirm()
		self.assertEqual(
			(doc.docstatus, doc.supplier, doc.items[0].item_code, doc.items[0].qty),
			(0, self.supplier, "AINF-BUY", 4),
		)

	def test_work_order_draft_takes_the_default_bom_and_its_materials(self):
		self.say("create work order", create_plan("Work Order"))
		self.fill({"production_item": "AINF-FG", "qty": "3"})
		labels = {r["label"]: r["value"] for r in self.preview()["rows"]}
		self.assertEqual(labels["BOM (the item's default)"], self.bom)
		doc = self.confirm()
		self.assertEqual(
			(doc.docstatus, doc.production_item, doc.bom_no, doc.qty), (0, "AINF-FG", self.bom, 3)
		)
		self.assertEqual([(r.item_code, r.required_qty) for r in doc.required_items], [("AINF-RM", 6)])

	def test_only_items_with_a_default_bom_can_be_manufactured(self):
		self.say("create work order", create_plan("Work Order"))
		self.say("AINF-BUY")  # a non-stock item without a BOM
		self.assertEqual(self.pending()["key"], "production_item")
		self.assertIn("couldn't find", self.texts())

	def test_timesheet_draft_from_start_time_and_hours(self):
		self.say("create timesheet", create_plan("Timesheet"))
		self.assertEqual(self.pending()["key"], "employee")
		self.say("AINF Meera")  # a partial name is offered as a choice, never picked on its own
		self.assertEqual(self.pending()["key"], "employee")
		self.say("AINF Meera Iyer")
		self.assertEqual(self.pending()["key"], "row__activity_type")
		self.say("Planning")
		self.say("2026-10-20")  # a date without a time is not enough: a start time is never made up
		self.assertEqual(self.pending()["key"], "row__from_time")
		self.assertIn("HH:MM", self.texts())
		self.say("2026-10-20 09:30")
		self.say("2.5")
		self.click("__skip__")  # project
		self.click("__done__")
		labels = {r["label"]: r["value"] for r in self.preview()["rows"]}
		self.assertEqual(labels["Total hours"], "2.5")
		doc = self.confirm()
		log = doc.time_logs[0]
		self.assertEqual(
			(doc.docstatus, doc.employee, log.activity_type, str(log.from_time), str(log.to_time), log.hours),
			(0, self.employee, "Planning", "2026-10-20 09:30:00", "2026-10-20 12:00:00", 2.5),
		)

	# journal entry (approved restricted design) -------------------------------------------------------
	def je_lines(self, *lines):
		return [{"account": a, "side": side, "amount": amount} for a, side, amount in lines]

	def test_balanced_journal_entry_is_a_draft_with_only_account_lines(self):
		before = frappe.db.count("GL Entry")
		self.say("create journal entry", create_plan("Journal Entry"))
		self.fill(
			{"user_remark": "AINF reclass"},
			rows=self.je_lines((self.debit_account, "Debit", "150"), (self.credit_account, "Credit", "150")),
		)
		labels = {r["label"]: r["value"] for r in self.preview()["rows"]}
		self.assertEqual(labels["Total debit"], f"150.0 {self.currency}")
		doc = self.confirm()
		self.assertEqual(
			(doc.docstatus, doc.voucher_type, doc.company, doc.user_remark),
			(0, "Journal Entry", self.company, "AINF reclass"),
		)
		self.assertEqual(
			sorted(
				(r.account, r.debit_in_account_currency, r.credit_in_account_currency, r.party_type or None)
				for r in doc.accounts
			),
			sorted([(self.debit_account, 150, 0, None), (self.credit_account, 0, 150, None)]),
		)
		self.assertEqual(frappe.db.count("GL Entry"), before)  # a Draft posts nothing

	def test_a_repeated_confirmation_creates_one_journal_entry(self):
		before = frappe.db.count("Journal Entry")
		self.say("create journal entry", create_plan("Journal Entry"))
		self.fill(
			rows=self.je_lines((self.debit_account, "Debit", "10"), (self.credit_account, "Credit", "10"))
		)
		q = self.pending()
		action = json.dumps(
			{"question_id": q["id"], "value": "__create__", "label": "Create draft Journal Entry"}
		)
		chat.send_message(session=self.s, action=action, client_id="aqnf-1")
		chat.send_message(
			session=self.s, action=action, client_id="aqnf-1"
		)  # the browser retrying the same request
		chat.send_message(
			session=self.s, action=action, client_id="aqnf-2"
		)  # a second click on the old button
		self.assertEqual(frappe.db.count("Journal Entry"), before + 1)

	def test_an_unbalanced_journal_entry_is_never_created(self):
		before = frappe.db.count("Journal Entry")
		self.say("create journal entry", create_plan("Journal Entry"))
		self.click("__skip__")  # remark
		for account, side, amount in (
			(self.debit_account, "Debit", "100"),
			(self.credit_account, "Credit", "60"),
		):
			self.say(account)
			self.say(side)
			self.say(amount)
			if account == self.debit_account:
				self.click("__more__")
		self.click("__done__")
		self.assertIn("differ by 40.0", self.texts())
		self.assertEqual(self.pending()["kind"], "form_more_rows")
		self.say("row 2 amount 100")
		self.click("__done__")
		self.assertEqual(self.pending()["kind"], "form_confirm")
		self.say("cancel")
		self.assertEqual(frappe.db.count("Journal Entry"), before)

	def test_cash_and_bank_accounts_are_not_offered_for_journal_lines(self):
		self.say("create journal entry", create_plan("Journal Entry"))
		self.click("__skip__")
		q = self.pending()
		self.assertEqual(q["key"], "row__account")
		self.assertNotIn(self.cash_account, [o["value"] for o in q["options"]])
		self.say(self.cash_account)
		self.assertEqual(self.pending()["key"], "row__account")
		self.assertIn("couldn't find", self.texts())

	def test_journal_lines_can_never_carry_party_or_bank_fields(self):
		doc = {
			"doctype": "Journal Entry",
			"company": self.company,
			"posting_date": nowdate(),
			"voucher_type": "Journal Entry",
			"accounts": [
				{"account": self.debit_account, "debit_in_account_currency": 1, "party_type": "Customer"}
			],
		}
		with self.assertRaises(CapabilityError):
			C.assert_creation_safe("Journal Entry", doc)
		doc["accounts"][0].pop("party_type")
		self.assertTrue(C.assert_creation_safe("Journal Entry", doc))

	def test_a_user_without_accounting_rights_cannot_draft_a_journal_entry(self):
		frappe.set_user("ainf-sales@example.com")
		self.assertFalse(frappe.has_permission("Journal Entry", "create"))
		self.s = chat.new_conversation()["session"]
		self.say("create journal entry", create_plan("Journal Entry"))
		self.assertIn("don't have permission to create Journal Entry", self.texts())
		self.assertIsNone(self.pending())

	# the catalog ---------------------------------------------------------------------------------------
	def test_the_catalog_describes_exactly_what_the_forms_can_do(self):
		catalog = capabilities.catalog()
		for dt in forms.creatable():
			create = catalog[dt]["create"]
			self.assertTrue(create and create["confirmation"], dt)
			self.assertEqual(create["result"], "saved" if dt in forms.MASTERS else "draft", dt)
			self.assertTrue(catalog[dt]["modifies_data"], dt)
		for dt in ("Payment Entry", "Contact", "GL Entry"):
			self.assertIsNone(catalog[dt]["create"], dt)
		self.assertTrue(catalog["Payment Entry"]["query"]["read_only"])
		self.assertIn("pay", catalog["Payment Entry"]["never"])
		summary = capabilities.capability_summary()
		self.assertNotIn("no new Stock Entries", summary)  # the old text refused a supported action
		for dt in ("Employee", "Journal Entry", "Stock Entry", "Work Order"):
			self.assertIn(dt, summary)
		self.assertIn("Employee", capabilities.creation_sentence())

	def test_help_lists_what_can_be_created(self):
		r = self.say("help")
		text = " ".join(m.get("text") or "" for m in r["messages"])
		for dt in ("Employee", "Journal Entry", "Material Request", "Stock Entry", "Supplier Quotation"):
			self.assertIn(dt, text)

	def test_an_unsupported_creation_says_why(self):
		before = frappe.db.count("Payment Entry")
		self.say("create a payment entry", create_plan("Payment Entry"))
		self.assertIn("can't create Payment Entry records", self.texts())
		self.assertIn("reviewed form", self.texts())
		self.assertEqual(frappe.db.count("Payment Entry"), before)
