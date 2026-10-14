"""Test-site bootstrap (the app's before_tests hook).

The tests need what any working ERPNext site has: a company with a chart of accounts, a
fiscal year for today, and the standard groups, territories and UOMs. On a site where the
ERPNext setup wizard has already been completed this does nothing. On a fresh site (CI) it
completes the wizard once with a throw-away test company.
"""

import frappe
from frappe.utils import getdate, nowdate

TEST_COMPANY = "AI Test Company"


def before_tests():
	frappe.clear_cache()
	if not frappe.db.a_row_exists("Company"):
		_complete_setup_wizard()
	_ensure_fiscal_year()
	frappe.db.commit()  # nosemgrep: test-site bootstrap must persist across test classes


def _complete_setup_wizard():
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	year = getdate(nowdate()).year
	result = setup_complete({
		"language": "English",
		"country": "India",
		"timezone": "Asia/Kolkata",
		"currency": "INR",
		"company_name": TEST_COMPANY,
		"company_abbr": "AITC",
		"chart_of_accounts": "Standard",
		"fy_start_date": f"{year}-01-01",
		"fy_end_date": f"{year}-12-31",
		"setup_demo": 0,
	})
	# setup_complete reports failures in its return value instead of raising
	if not frappe.db.exists("Company", TEST_COMPANY):
		raise RuntimeError(f"ERPNext setup wizard did not create the test company: {result}")
	frappe.db.set_default("company", TEST_COMPANY)
	frappe.db.set_single_value("Global Defaults", "default_company", TEST_COMPANY)


def _ensure_fiscal_year():
	"""Tests post documents dated today; make sure a fiscal year covers it."""
	today = getdate(nowdate())
	if frappe.db.exists("Fiscal Year", {"year_start_date": ["<=", today], "year_end_date": [">=", today], "disabled": 0}):
		return
	frappe.get_doc({"doctype": "Fiscal Year", "year": str(today.year),
		"year_start_date": f"{today.year}-01-01", "year_end_date": f"{today.year}-12-31"}).insert()
