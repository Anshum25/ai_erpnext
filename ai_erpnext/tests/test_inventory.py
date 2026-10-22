"""The coverage inventory is generated from the real policy and must not claim more than exists.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_inventory
"""

import json
import tempfile
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.capability import inventory as I
from ai_erpnext.capability import policy as P
from ai_erpnext.capability import service as C
from ai_erpnext.chat import forms

DOCS = Path(__file__).resolve().parents[2] / "docs"


class TestInventory(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.data = I.build()
		cls.by_name = {}
		for e in cls.data["entries"]:
			cls.by_name.setdefault(e["doctype"], []).append(e)

	def test_every_registered_doctype_appears_exactly_once_with_capabilities(self):
		for dt in C.queryable_doctypes():  # the site's metadata: an uninstalled app's DocTypes don't exist
			entries = self.by_name.get(dt)
			self.assertEqual(len(entries or []), 1, dt)
			self.assertIn("caps", entries[0], dt)
			self.assertIn(entries[0]["status"], (I.IMPLEMENTED, I.IMPLEMENTED_TESTED), dt)

	def test_nothing_unregistered_is_reported_as_implemented(self):
		for e in self.data["entries"]:
			if e["doctype"] not in P.REGISTRY:
				self.assertNotIn(e["status"], (I.IMPLEMENTED, I.IMPLEMENTED_TESTED, I.PARTIAL), e["doctype"])
				self.assertNotIn("caps", e, e["doctype"])

	def test_tested_claims_are_only_made_where_a_test_manifest_names_them(self):
		for dt, verified in I.VERIFIED.items():
			self.assertIn(dt, P.REGISTRY, dt)
			self.assertLessEqual(
				verified,
				{"read", "details", "filter", "aggregate", "child", "link", "summary", "company"},
				dt,
			)
		self.assertLessEqual(I.CREATE_TESTED, set(forms.CREATABLE))
		for e in self.data["entries"]:
			for key in I.QUERY_CAPABILITIES:
				if "caps" in e and e["caps"][key] == I.IMPLEMENTED_TESTED:
					self.assertIn(
						key,
						I.VERIFIED.get(e["doctype"], set()),
						f"{e['doctype']}.{key} is reported as tested without a manifest entry",
					)

	def test_edit_reports_and_workflow_are_never_reported_as_implemented(self):
		for e in self.data["entries"]:
			if "caps" in e:
				self.assertEqual(
					(e["caps"]["edit"], e["caps"]["reports"], e["caps"]["workflow"]),
					(I.NOT_IMPLEMENTED,) * 3,
					e["doctype"],
				)

	def test_sensitive_and_consequential_doctypes_are_not_in_the_policy(self):
		self.assertFalse(set(I.BLOCKED_REASONS) & set(P.REGISTRY))
		blocked = {dt for dt, (status, _reason) in I.FRAMEWORK.items() if status == I.BLOCKED}
		self.assertFalse(blocked & set(P.REGISTRY))
		for dt in I.BLOCKED_REASONS:
			for e in self.by_name.get(dt, []):
				self.assertEqual(e["status"], I.BLOCKED, dt)

	def test_child_tables_and_settings_are_not_applicable_not_missing(self):
		child = next(e for e in self.data["entries"] if e["doctype"] == "Sales Invoice Item")
		self.assertEqual(child["status"], I.NOT_APPLICABLE)
		self.assertTrue(child["is_child"])

	def test_hrms_is_detected_not_assumed(self):
		self.assertEqual(self.data["hrms_installed"], "hrms" in frappe.get_installed_apps())
		text = I.render_markdown(self.data)
		self.assertIn(
			"HRMS installed: **no**" if not self.data["hrms_installed"] else "HRMS installed: **yes**", text
		)

	def test_markdown_is_complete_and_states_the_blockers(self):
		text = I.render_markdown(self.data)
		for dt in C.queryable_doctypes():
			self.assertIn(f"| {dt} |", text, dt)
		for needle in (
			"Legend",
			"Summary by module",
			"Standard reports",
			"Page context",
			"not implemented for any DocType",
			I.REPORT_BLOCKER[:40],
		):
			self.assertIn(needle, text)
		for module in (
			"Selling",
			"Buying",
			"Stock",
			"Accounting",
			"CRM",
			"Projects",
			"Support",
			"Manufacturing",
			"Quality Management",
			"Assets",
			"Subcontracting",
		):
			self.assertIn(f"| {module} |", text, module)

	def test_the_saved_inventory_lists_every_registered_doctype(self):
		self.assertTrue((DOCS / "coverage_inventory.md").exists())
		text = (DOCS / "coverage_inventory.md").read_text()
		data = json.loads((DOCS / "coverage_inventory.json").read_text())
		for dt in P.REGISTRY:
			self.assertIn(
				f"| {dt} |", text, f"{dt} is missing from docs/coverage_inventory.md: regenerate it"
			)
		self.assertEqual({e["doctype"] for e in data["entries"] if "caps" in e} >= set(P.REGISTRY), True)

	def test_write_inventory_writes_files_only_where_asked(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = I.write_inventory(tmp)
			self.assertEqual(Path(path), Path(tmp).resolve())
			self.assertTrue((Path(tmp) / "coverage_inventory.md").exists())
			self.assertTrue(json.loads((Path(tmp) / "coverage_inventory.json").read_text())["entries"])

	def test_page_context_statement_matches_the_code(self):
		from ai_erpnext.chat import engine, page_context

		self.assertTrue(callable(engine._view_context) and callable(page_context.parse))
		js = Path(__file__).resolve().parents[1] / "public" / "js"
		collector = (js / "ai_page_context.js").read_text()
		for needle in (
			"get_checked_items",
			"get_filters_for_args",
			"is_dirty",
			"query_report",
			"get_selected",
		):
			self.assertIn(needle, collector)
		self.assertFalse(P.REPORT_RESULTS_ENABLED)  # the report statement says results are never read
		for key in (
			"Current form and record",
			"List filters and selected rows",
			"Reports and report filters",
			"Unsaved form changes",
			"Stale-record detection",
		):
			self.assertIn(key, I.PAGE_CONTEXT)
