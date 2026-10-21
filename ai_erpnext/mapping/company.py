import frappe

from ai_erpnext.mapping.base import BaseMatcher, MatchResult
from ai_erpnext.mapping.normalize import clean_text


class CompanyMatcher(BaseMatcher):
	doctype = "Company"
	title_field = "company_name"
	extra_fields = ("default_currency",)

	def resolve(self, names) -> MatchResult:
		"""Try each extracted name; otherwise fall back to a safe ERPNext default."""
		names = [n for n in (clean_text(v) for v in names) if n]
		misses = []
		for name in names:
			result = self.match(name)
			if result.matched:
				return result
			if result.status == "AMBIGUOUS" and result.match_type != "fuzzy_suggestion":
				return result
			misses.append(name)

		default = self.default_company()
		if default:
			reason = "No company on document; used default company"
			if misses:
				reason = f"Issuer '{misses[0]}' does not match any Company; used default company"
			res = MatchResult.defaulted(
				self.doctype, default, default, reason, input=misses[0] if misses else None
			)
			if misses:
				res.extra["unmatched_issuer"] = misses[0]
			return res

		companies = self._get_list({}, limit=20)
		if not companies:
			return MatchResult.not_found(
				self.doctype, names[0] if names else None, "No Company is accessible"
			)
		return MatchResult.ambiguous(
			self.doctype,
			names[0] if names else None,
			[self._candidate(c, 0.0) for c in companies],
			match_type="no_default",
			message="Several companies exist and none could be determined; please select one",
		)

	def default_company(self):
		permitted = {c["name"] for c in self._get_list({}, limit=500)}
		for candidate in (
			frappe.defaults.get_user_default("Company"),
			frappe.db.get_single_value("Global Defaults", "default_company"),
		):
			if candidate and candidate in permitted:
				return candidate
		if len(permitted) == 1:
			return next(iter(permitted))
		return None
