"""Installation: ERPNext is a declared dependency and a clean install creates the full schema.

CI installs ERPNext and this app on a brand-new site and then runs `bench migrate` once
more; these tests check the result and that the schema hooks are idempotent.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_install_schema
"""

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext import setup_doctypes

DOCTYPES = ("AI Document Entry", "AI Provider Settings", "AI Chat Session")


class TestInstallSchema(IntegrationTestCase):
	def test_erpnext_is_required_and_installed_first(self):
		self.assertIn("erpnext", frappe.get_hooks("required_apps", app_name="ai_erpnext"))
		apps = frappe.get_installed_apps()
		self.assertLess(apps.index("erpnext"), apps.index("ai_erpnext"))
		for table in ("Sales Invoice", "Customer Group", "Fiscal Year"):
			self.assertTrue(frappe.db.table_exists(table), table)

	def test_install_hooks_are_registered(self):
		hooks = {
			key: frappe.get_hooks(key, app_name="ai_erpnext")
			for key in ("after_install", "after_migrate", "before_tests")
		}
		self.assertIn("ai_erpnext.setup_doctypes.after_install", hooks["after_install"])
		self.assertIn("ai_erpnext.setup_doctypes.after_migrate", hooks["after_migrate"])
		self.assertIn("ai_erpnext.tests.bootstrap.before_tests", hooks["before_tests"])
		events = frappe.get_hooks("doc_events", app_name="ai_erpnext")["AI Document Entry"]
		self.assertEqual(events["validate"], ["ai_erpnext.pipeline.controller.validate_ai_document"])

	def test_full_schema_exists(self):
		expected = {
			"AI Document Entry": [f["fieldname"] for f in setup_doctypes.AI_DOCUMENT_ENTRY["fields"]]
			+ [f for f, _, _ in setup_doctypes.AI_DOCUMENT_ENTRY_FIELDS],
			"AI Provider Settings": [f["fieldname"] for f in setup_doctypes.AI_PROVIDER_SETTINGS["fields"]]
			+ [f for f, _, _ in setup_doctypes.AI_PROVIDER_SETTINGS_FIELDS],
			"AI Chat Session": ["title", "status", "active_entry", "last_activity", "messages", "state"],
		}
		for doctype, fields in expected.items():
			meta = frappe.get_meta(doctype)
			self.assertEqual(meta.module, "AI", doctype)
			missing = [f for f in fields if not meta.has_field(f)]
			self.assertEqual(missing, [], doctype)
			if not meta.issingle:
				self.assertTrue(frappe.db.table_exists(doctype), doctype)
				self.assertEqual(
					[
						f
						for f in fields
						if f not in frappe.db.get_table_columns(doctype)
						and meta.get_field(f).fieldtype not in ("Section Break", "Column Break")
					],
					[],
					doctype,
				)
		providers = frappe.get_meta("AI Provider Settings").get_field("provider").options.split("\n")
		self.assertEqual([p for p in setup_doctypes.PROVIDERS if p not in providers], [])
		status = frappe.get_meta("AI Document Entry").get_field("status").options.split("\n")
		from ai_erpnext.constants import DocumentStatus

		self.assertEqual([s for s in DocumentStatus.ALL if s not in status], [])

	def test_schema_hooks_are_idempotent(self):
		before = {dt: frappe.db.get_value("DocType", dt, "modified") for dt in DOCTYPES}
		setup_doctypes.ensure_schema()
		setup_doctypes.ensure_schema()
		self.assertEqual({dt: frappe.db.get_value("DocType", dt, "modified") for dt in DOCTYPES}, before)

	def test_upgrade_adds_missing_fields_in_place(self):
		"""An older site (without the fallback fields or newer providers) is upgraded, not recreated."""
		doc = frappe.get_doc("DocType", "AI Provider Settings")
		doc.fields = [
			f
			for f in doc.fields
			if not f.fieldname.startswith("fallback")
			and f.fieldname not in ("base_url", "model", "text_model")
		]
		provider = next(f for f in doc.fields if f.fieldname == "provider")
		provider.options = "Gemini\nOpenAI"
		self.assertTrue(setup_doctypes._add_fields(doc, setup_doctypes.AI_PROVIDER_SETTINGS_FIELDS))
		self.assertTrue(setup_doctypes._add_select_options(provider, setup_doctypes.PROVIDERS))
		order = [f.fieldname for f in doc.fields]
		self.assertEqual(order[:4], ["provider", "api_key", "base_url", "model"])
		self.assertEqual(order[4:], [f for f, _, _ in setup_doctypes.AI_PROVIDER_SETTINGS_FIELDS[2:]])
		self.assertEqual(provider.options.split("\n"), list(setup_doctypes.PROVIDERS))
		self.assertFalse(
			setup_doctypes._add_fields(doc, setup_doctypes.AI_PROVIDER_SETTINGS_FIELDS)
		)  # second pass: nothing to do

	def test_fallback_defaults_are_safe(self):
		meta = frappe.get_meta("AI Provider Settings")
		self.assertEqual(meta.get_field("fallback_enabled").default, "0")
		self.assertEqual(meta.get_field("fallback_api_key").fieldtype, "Password")
		self.assertEqual(meta.get_field("fallback_provider").options.split("\n"), ["", "OpenAI"])
