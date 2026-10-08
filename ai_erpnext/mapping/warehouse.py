import frappe

from ai_erpnext.mapping.base import BaseMatcher, MatchResult
from ai_erpnext.mapping.normalize import clean_text


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

		default = frappe.db.get_single_value("Stock Settings", "default_warehouse")
		if default and self.resolve_name(default):
			return MatchResult.defaulted(
				self.doctype, default, default, "Not on document; used Stock Settings default warehouse"
			)
		return MatchResult(
			doctype=self.doctype,
			message="Warehouse not detected. Not required unless stock is updated; ERPNext item defaults apply.",
		)
