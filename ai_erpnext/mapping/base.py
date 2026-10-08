"""Matcher framework: resolve human-readable AI values to real ERPNext master records.

All lookups go through frappe.get_list, so the current user's permissions apply.
Nothing here creates master records. Only an unambiguous match at or above
AUTO_ACCEPT_CONFIDENCE is selected; everything else is returned as candidates.
"""

from dataclasses import asdict, dataclass, field

import frappe

from ai_erpnext.constants import AUTO_ACCEPT_CONFIDENCE, FieldStatus
from ai_erpnext.mapping.normalize import clean_text, normalize_name, sorted_tokens

try:
	from rapidfuzz import fuzz

	def similarity(a, b):
		return fuzz.token_sort_ratio(a, b)

except ImportError:  # pragma: no cover - rapidfuzz ships with ERPNext
	from difflib import SequenceMatcher

	def similarity(a, b):
		return SequenceMatcher(None, sorted_tokens(a), sorted_tokens(b)).ratio() * 100


FUZZY_SUGGESTION_SCORE = 80
MAX_CANDIDATES = 5
CANDIDATE_POOL = 50


@dataclass
class MatchResult:
	input: object = None
	doctype: str | None = None
	status: str = FieldStatus.NOT_PROVIDED
	matched: bool = False
	name: str | None = None
	display_name: str | None = None
	confidence: float = 0.0
	match_type: str = "not_provided"
	requires_review: bool = False
	candidates: list = field(default_factory=list)
	message: str | None = None
	extra: dict = field(default_factory=dict)

	def to_dict(self):
		data = asdict(self)
		if not data["extra"]:
			data.pop("extra")
		if not data["candidates"]:
			data.pop("candidates")
		return data

	@classmethod
	def from_dict(cls, data):
		known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
		return cls(**known)

	# Convenience constructors -------------------------------------------------
	@classmethod
	def found(cls, doctype, name, display_name, confidence, match_type, input=None, **extra):
		return cls(
			input=input,
			doctype=doctype,
			status=FieldStatus.MATCHED,
			matched=True,
			name=name,
			display_name=display_name,
			confidence=round(confidence, 4),
			match_type=match_type,
			extra=extra,
		)

	@classmethod
	def defaulted(cls, doctype, name, display_name, reason, input=None, confidence=1.0):
		return cls(
			input=input,
			doctype=doctype,
			status=FieldStatus.DEFAULTED,
			matched=True,
			name=name,
			display_name=display_name,
			confidence=confidence,
			match_type="default",
			message=reason,
		)

	@classmethod
	def not_found(cls, doctype, input, message=None, candidates=None):
		return cls(
			input=input,
			doctype=doctype,
			status=FieldStatus.NOT_FOUND,
			match_type="not_found",
			requires_review=True,
			candidates=candidates or [],
			message=message or f"No {doctype} found for '{input}'",
		)

	@classmethod
	def ambiguous(cls, doctype, input, candidates, match_type="ambiguous", message=None):
		return cls(
			input=input,
			doctype=doctype,
			status=FieldStatus.AMBIGUOUS,
			match_type=match_type,
			requires_review=True,
			candidates=candidates[:MAX_CANDIDATES],
			message=message or f"Multiple {doctype} records could match '{input}'",
		)

	@classmethod
	def manual(cls, doctype, name, display_name, input=None):
		return cls.found(doctype, name, display_name, 1.0, "manual", input=input)


class BaseMatcher:
	"""Generic name matcher over one master DocType.

	Subclasses set `doctype`, `title_field` and `base_filters`, and may override
	`match()` to add steps (item codes, aliases, defaults...).
	"""

	doctype: str = ""
	title_field: str = "name"
	base_filters: dict = {}
	extra_fields: tuple = ()

	def __init__(self, filters=None):
		self.filters = dict(self.base_filters)
		if filters:
			self.filters.update(filters)

	# Public API ------------------------------------------------------------------
	def match(self, value) -> MatchResult:
		text = clean_text(value)
		if not text:
			return MatchResult(doctype=self.doctype, input=value)
		return self.match_by_title(text)

	def resolve_name(self, name):
		"""Validate a user-supplied record name (manual correction)."""
		name = clean_text(name, 140)
		if not name:
			return None
		rows = self._get_list({"name": name}, limit=1)
		return rows[0] if rows else None

	# Matching hierarchy ------------------------------------------------------------
	def match_by_title(self, text) -> MatchResult:
		# 1 + 2. exact / case-insensitive title (DB comparison is case-insensitive)
		rows = self._get_list({self.title_field: text}, limit=CANDIDATE_POOL)
		if rows:
			exact = [r for r in rows if r[self.title_field] == text]
			if len(exact) == 1:
				return self._found(exact[0], 1.0, "exact", text)
			if len(exact) > 1:
				return self._ambiguous(exact, text, "duplicate_names")
			if len(rows) == 1:
				return self._found(rows[0], 0.98, "case_insensitive", text)
			return self._ambiguous(rows, text, "duplicate_names")

		# 3. exact record ID
		if self.title_field != "name":
			rows = self._get_list({"name": text}, limit=1)
			if rows:
				return self._found(rows[0], 0.97, "id_match", text)

		# 4 + 5. normalized and fuzzy over a bounded candidate pool
		pool = self._candidate_pool(text)
		return self._match_pool(text, pool)

	def _match_pool(self, text, pool, normalized_type="normalized") -> MatchResult:
		if not pool:
			return MatchResult.not_found(self.doctype, text)

		wanted, wanted_sorted = normalize_name(text), sorted_tokens(text)
		normalized = [
			r
			for r in pool
			if normalize_name(r[self.title_field]) == wanted
			or sorted_tokens(r[self.title_field]) == wanted_sorted
			or normalize_name(r["name"]) == wanted
		]
		if len(normalized) == 1:
			return self._found(normalized[0], 0.95, normalized_type, text)
		if len(normalized) > 1:
			return self._ambiguous(normalized, text, "ambiguous_normalized")

		scored = []
		for r in pool:
			score = max(similarity(wanted, normalize_name(r[self.title_field])), similarity(wanted, normalize_name(r["name"])))
			if score >= FUZZY_SUGGESTION_SCORE:
				scored.append((score, r))
		scored.sort(key=lambda s: s[0], reverse=True)
		if not scored:
			return MatchResult.not_found(self.doctype, text)

		candidates = [self._candidate(r, score / 100.0) for score, r in scored]
		# Fuzzy matches are suggestions only; a human confirms them.
		return MatchResult.ambiguous(
			self.doctype,
			text,
			candidates,
			match_type="fuzzy_suggestion",
			message=f"No exact {self.doctype} match for '{text}'. Closest: {candidates[0]['display_name']} ({candidates[0]['score']:.0%})",
		)

	# Helpers ----------------------------------------------------------------------
	def _fields(self):
		return list(dict.fromkeys(["name", self.title_field, *self.extra_fields]))

	def _get_list(self, filters, limit=CANDIDATE_POOL, or_filters=None):
		merged = dict(self.filters)
		merged.update(filters or {})
		return frappe.get_list(
			self.doctype,
			filters=merged,
			or_filters=or_filters,
			fields=self._fields(),
			limit_page_length=limit,
			order_by="modified desc",
		)

	def _candidate_pool(self, text):
		tokens = sorted({t for t in normalize_name(text).split() if len(t) >= 3}, key=len, reverse=True)[:4]
		if not tokens:
			return []
		or_filters = []
		for token in tokens:
			or_filters.append([self.doctype, self.title_field, "like", f"%{token}%"])
			if self.title_field != "name":
				or_filters.append([self.doctype, "name", "like", f"%{token}%"])
		return self._get_list({}, limit=CANDIDATE_POOL, or_filters=or_filters)

	def _display(self, row):
		return row.get(self.title_field) or row["name"]

	def _candidate(self, row, score):
		return {"name": row["name"], "display_name": self._display(row), "score": round(score, 4)}

	def _found(self, row, confidence, match_type, text):
		if confidence < AUTO_ACCEPT_CONFIDENCE:
			return MatchResult.ambiguous(self.doctype, text, [self._candidate(row, confidence)], match_type=match_type)
		return MatchResult.found(self.doctype, row["name"], self._display(row), confidence, match_type, input=text)

	def _ambiguous(self, rows, text, match_type):
		return MatchResult.ambiguous(self.doctype, text, [self._candidate(r, 1.0) for r in rows], match_type=match_type)
