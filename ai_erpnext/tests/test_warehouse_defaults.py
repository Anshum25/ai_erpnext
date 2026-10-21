"""Default warehouse resolution across ERPNext v16 schemas.

ERPNext v16 moved the default warehouse from Stock Settings (one value per site) to Company
(erpnext bd76c2a14). The mapper must work on both: it may only read fields the installed
schema has, a default must belong to the document's company, and a missing default is not an
error for documents that don't need a warehouse.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_warehouse_defaults
"""

from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.chat import requirements
from ai_erpnext.constants import FieldStatus
from ai_erpnext.mapping import warehouse as W
from ai_erpnext.mapping.warehouse import WarehouseMatcher, configured_default_warehouse

OTHER = "AIWD Other Co"


def schema(company_field, stock_settings_field):
	"""get_meta() of a site whose Company / Stock Settings do (not) have default_warehouse."""
	real = frappe.get_meta

	def get_meta(doctype, *args, **kwargs):
		if doctype in ("Company", "Stock Settings"):
			has = company_field if doctype == "Company" else stock_settings_field
			return SimpleNamespace(
				has_field=lambda f, has=has: has if f == "default_warehouse" else real(doctype).has_field(f)
			)
		return real(doctype, *args, **kwargs)

	return mock.patch.object(W.frappe, "get_meta", side_effect=get_meta)


class TestWarehouseDefaults(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company")
		abbr = frappe.get_cached_value("Company", cls.company, "abbr")
		cls.stores, cls.goods = f"Stores - {abbr}", f"Finished Goods - {abbr}"
		if not frappe.db.exists("Company", OTHER):
			currency = frappe.get_cached_value("Company", cls.company, "default_currency")
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": OTHER,
					"abbr": "AIWD",
					"default_currency": currency,
					"country": frappe.db.get_single_value("System Settings", "country") or "India",
					"create_chart_of_accounts_based_on": "Standard Template",
					"chart_of_accounts": "Standard",
				}
			).insert()
		cls.other_stores = "Stores - AIWD"

	def set_default(self, warehouse, company=None):
		"""Configure the default the way the installed ERPNext stores it."""
		company = company or self.company
		if frappe.get_meta("Company").has_field("default_warehouse"):
			frappe.db.set_value("Company", company, "default_warehouse", warehouse)
			frappe.clear_document_cache("Company", company)
		else:
			frappe.db.set_single_value("Stock Settings", "default_warehouse", warehouse)

	# ---- schema compatibility ------------------------------------------------------------------
	def test_no_default_warehouse_field_anywhere_does_not_crash(self):
		"""The CI failure: Stock Settings has no default_warehouse on current ERPNext v16."""
		original = frappe.db.get_single_value

		def single_value(doctype, field, *args, **kwargs):
			if field == "default_warehouse":
				raise AssertionError("must not read a field the schema doesn't have")
			return original(doctype, field, *args, **kwargs)

		with (
			schema(company_field=False, stock_settings_field=False),
			mock.patch.object(W.frappe.db, "get_single_value", side_effect=single_value),
		):
			self.assertIsNone(configured_default_warehouse(self.company))
			res = WarehouseMatcher(self.company).resolve(None)
		self.assertEqual(res.status, FieldStatus.NOT_PROVIDED)
		self.assertIn("Not required unless stock is updated", res.message)

	def test_company_default_is_read_when_company_has_the_field(self):
		with (
			schema(company_field=True, stock_settings_field=False),
			mock.patch.object(W.frappe, "get_cached_value", return_value=self.stores) as cached,
			mock.patch.object(W.frappe.db, "get_single_value", side_effect=AssertionError("old field")),
		):
			self.assertEqual(configured_default_warehouse(self.company), self.stores)
		cached.assert_called_once_with("Company", self.company, "default_warehouse")

	def test_old_stock_settings_default_is_still_supported(self):
		with (
			schema(company_field=False, stock_settings_field=True),
			mock.patch.object(W.frappe.db, "get_single_value", return_value=self.goods) as single,
		):
			self.assertEqual(configured_default_warehouse(self.company), self.goods)
		single.assert_called_once_with("Stock Settings", "default_warehouse")

	def test_no_company_means_no_default(self):
		with mock.patch.object(
			W.frappe, "get_meta", side_effect=AssertionError("no lookup without a company")
		):
			self.assertIsNone(configured_default_warehouse(None))
			self.assertEqual(WarehouseMatcher(None).resolve("").status, FieldStatus.NOT_PROVIDED)

	# ---- behaviour on the installed schema ----------------------------------------------------
	def test_explicit_warehouse_is_kept_and_validated(self):
		res = WarehouseMatcher(self.company).resolve("Finished Goods")
		self.assertEqual((res.status, res.name), (FieldStatus.MATCHED, self.goods))
		res = WarehouseMatcher(self.company).resolve(self.other_stores)  # another company's warehouse
		self.assertNotEqual(res.status, FieldStatus.MATCHED)
		self.assertFalse(res.matched)

	def test_valid_configured_default_is_used_and_marked_as_default(self):
		self.set_default(self.stores)
		res = WarehouseMatcher(self.company).resolve(None)
		self.assertEqual((res.status, res.name), (FieldStatus.DEFAULTED, self.stores))
		self.assertEqual(res.message, "Not on document; used the company's default warehouse")

	def test_no_default_is_a_clear_result_not_an_error(self):
		self.set_default(None)
		res = WarehouseMatcher(self.company).resolve(None)
		self.assertEqual((res.status, res.name), (FieldStatus.NOT_PROVIDED, None))

	def test_default_of_another_company_is_never_used(self):
		self.set_default(self.other_stores)  # old schema: one site-wide value that belongs to another company
		res = WarehouseMatcher(self.company).resolve(None)
		self.assertEqual(res.status, FieldStatus.NOT_PROVIDED)
		self.assertIsNone(res.name)

	def test_disabled_default_is_not_used(self):
		self.set_default(self.goods)
		frappe.db.set_value("Warehouse", self.goods, "disabled", 1)
		self.addCleanup(frappe.db.set_value, "Warehouse", self.goods, "disabled", 0)
		self.assertEqual(WarehouseMatcher(self.company).resolve(None).status, FieldStatus.NOT_PROVIDED)

	# ---- which documents need one ------------------------------------------------------------
	def test_only_stock_moving_documents_require_a_warehouse(self):
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "AIWD-STOCK",
				"item_name": "AIWD Stock",
				"item_group": ig,
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"is_sales_item": 1,
				"is_purchase_item": 1,
			}
		).insert()
		for doctype in ("Purchase Order", "Purchase Receipt", "Sales Order", "Delivery Note"):
			self.assertTrue(requirements.needs_warehouse(doctype, "AIWD-STOCK"), doctype)
		for doctype in ("Sales Invoice", "Purchase Invoice", "Quotation"):
			self.assertFalse(requirements.needs_warehouse(doctype, "AIWD-STOCK"), doctype)

	def test_invoice_mapping_without_warehouse_is_not_blocked(self):
		from ai_erpnext.tests.test_extraction_safety import b2_output, map_document, validate_outcome

		self.set_default(None)
		outcome = map_document(b2_output())
		self.assertNotIn("set_warehouse", outcome.mapped)
		report = validate_outcome(outcome)
		self.assertFalse([i for i in report.blocking if i.field == "warehouse"])
		bad = map_document(
			b2_output(warehouse="No Such Warehouse ZZ")
		)  # an explicit but unknown warehouse is asked about
		self.assertIn("warehouse", {i.field for i in validate_outcome(bad).blocking})
