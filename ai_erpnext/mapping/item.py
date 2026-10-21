from typing import ClassVar

import frappe

from ai_erpnext.mapping.base import CANDIDATE_POOL, BaseMatcher, MatchResult
from ai_erpnext.mapping.normalize import clean_text, normalize_name


class ItemMatcher(BaseMatcher):
	"""Item matching: item_code/barcode -> item_name hierarchy -> description -> fuzzy."""

	doctype = "Item"
	title_field = "item_name"
	# Templates (has_variants) cannot be used on transactions.
	base_filters: ClassVar[dict] = {"disabled": 0, "has_variants": 0}
	extra_fields = ("stock_uom", "is_stock_item")

	def match(self, value) -> MatchResult:
		if not isinstance(value, dict):
			value = {"item_name": value}
		code = clean_text(value.get("item_code"), 140)
		name = clean_text(value.get("item_name"))
		description = clean_text(value.get("description"))
		label = name or code or description

		if code:
			rows = self._get_list({"name": code}, limit=1)
			if rows:
				return self._with_input(self._found(rows[0], 1.0, "exact_code", code), label)
			barcode_parents = frappe.get_all(
				"Item Barcode", filters={"barcode": code}, pluck="parent", limit=2
			)
			rows = self._get_list({"name": ["in", barcode_parents]}, limit=2) if barcode_parents else []
			if len(rows) == 1:
				return self._with_input(self._found(rows[0], 0.97, "barcode", code), label)

		result = None
		if name:
			result = self.match_by_title(name)
			if result.matched or result.match_type in ("duplicate_names", "ambiguous_normalized"):
				return result

		if description and normalize_name(description) != normalize_name(name):
			desc_result = self._match_description(description)
			if desc_result and (desc_result.matched or result is None or not result.candidates):
				return self._with_input(desc_result, label)

		return result or MatchResult.not_found(self.doctype, label)

	def _match_description(self, description):
		# The extracted description may itself be the ERPNext item name.
		by_title = self.match_by_title(description)
		if by_title.matched:
			by_title.match_type = "description_as_name"
			by_title.confidence = min(by_title.confidence, 0.92)
			return by_title

		rows = self._get_list({"description": ["like", f"%{description[:120]}%"]}, limit=CANDIDATE_POOL)
		if len(rows) == 1:
			# Description containment is weaker evidence: suggest, do not auto-select.
			return MatchResult.ambiguous(
				self.doctype, description, [self._candidate(rows[0], 0.85)], match_type="description"
			)
		if len(rows) > 1:
			return self._ambiguous(rows, description, "description")
		return None

	@staticmethod
	def _with_input(result, label):
		result.input = label
		return result


def match_item(value):
	return ItemMatcher().match(value)
