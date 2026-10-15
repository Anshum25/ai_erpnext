import frappe

from ai_erpnext.mapping.base import BaseMatcher, MatchResult
from ai_erpnext.mapping.normalize import clean_text


def configured_default_warehouse(company):
	"""The default warehouse configured for `company`, read only from fields the installed ERPNext has.

	ERPNext v16 moved this setting from Stock Settings (one value for the whole site) to Company
	(`Company.default_warehouse`, erpnext commit bd76c2a14, 2026-10-08); a patch moves existing
	values to the company that owns the warehouse. Sites on an older v16 still have the Stock
	Settings field. The value is only a candidate: callers must check that it is an enabled,
	non-group warehouse of the same company that the user may read (WarehouseMatcher.resolve)."""
	if not company:
		return None  # a site-wide default could belong to any company
	if frappe.get_meta("Company").has_field("default_warehouse"):
		return frappe.get_cached_value("Company", company, "default_warehouse") or None
	if frappe.get_meta("Stock Settings").has_field("default_warehouse"):
		return frappe.db.get_single_value("Stock Settings", "default_warehouse") or None
	return None


class WarehouseMatcher(BaseMatcher):
	"""Warehouse matching, always scoped to the resolved company. Never invents a warehouse."""

	doctype = "Warehouse"
	title_field = "warehouse_name"
	base_filters = {"is_group": 0, "disabled": 0}

	def __init__(self, company=None):
		super().__init__({"company": company} if company else None)
		self.company = company

	def resolve(self, value) -> MatchResult:
		text = clean_text(value)
		if text:
			return self.match(text)

		default = configured_default_warehouse(self.company)
		if default and self.resolve_name(default):  # same company, enabled, not a group, readable
			return MatchResult.defaulted(
				self.doctype, default, default, "Not on document; used the company's default warehouse"
			)
		return MatchResult(
			doctype=self.doctype,
			message="Warehouse not detected. Not required unless stock is updated; ERPNext item defaults apply.",
		)
