from typing import ClassVar

import frappe

from ai_erpnext.capability.access import readable_value
from ai_erpnext.mapping.base import BaseMatcher, MatchResult
from ai_erpnext.mapping.normalize import clean_text


class CurrencyMatcher(BaseMatcher):
	doctype = "Currency"
	title_field = "currency_name"
	base_filters: ClassVar[dict] = {"enabled": 1}
	extra_fields = ("symbol",)

	def match(self, value) -> MatchResult:
		text = clean_text(value, 60)
		if not text:
			return MatchResult(doctype=self.doctype, input=value)

		rows = self._get_list({"name": text.upper()}, limit=1)
		if rows:
			return self._found(rows[0], 1.0, "exact_code", text)

		result = self.match_by_title(text)
		if result.matched:
			return result

		rows = self._get_list({"symbol": text}, limit=10)
		if len(rows) == 1:
			return self._found(rows[0], 0.9, "symbol", text)
		if len(rows) > 1:
			return MatchResult.ambiguous(
				self.doctype,
				text,
				[self._candidate(r, 0.5) for r in rows],
				match_type="symbol",
				message=f"Currency symbol '{text}' is used by several currencies",
			)
		return result

	def resolve(self, value, customer=None, company=None, party_doctype="Customer") -> MatchResult:
		if clean_text(value):
			return self.match(value)
		if customer:  # the counterparty (a Customer or a Supplier)
			cur = readable_value(party_doctype, customer, "default_currency")
			if cur:
				return MatchResult.defaulted(
					self.doctype,
					cur,
					cur,
					f"Not on document; used the {party_doctype.lower()}'s default currency",
				)
		if company:
			cur = frappe.get_cached_value("Company", company, "default_currency")
			if cur:
				return MatchResult.defaulted(
					self.doctype, cur, cur, "Not on document; used company's default currency"
				)
		return MatchResult(doctype=self.doctype, message="Currency could not be determined")
