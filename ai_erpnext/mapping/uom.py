import re
from typing import ClassVar

from ai_erpnext.mapping.base import BaseMatcher, MatchResult
from ai_erpnext.mapping.normalize import clean_text, normalize_name

# Common abbreviations / synonyms -> list of ERPNext UOM names to try (first existing wins).
UOM_ALIASES = {
	"pc": ["Nos", "Unit"],
	"pcs": ["Nos", "Unit"],
	"piece": ["Nos", "Unit"],
	"pieces": ["Nos", "Unit"],
	"no": ["Nos"],
	"nos": ["Nos"],
	"number": ["Nos"],
	"each": ["Nos", "Unit"],
	"ea": ["Nos", "Unit"],
	"unit": ["Unit", "Nos"],
	"units": ["Unit", "Nos"],
	"kg": ["Kg"],
	"kgs": ["Kg"],
	"kilogram": ["Kg"],
	"kilograms": ["Kg"],
	"g": ["Gram"],
	"gm": ["Gram"],
	"gms": ["Gram"],
	"grams": ["Gram"],
	"l": ["Litre"],
	"ltr": ["Litre"],
	"liter": ["Litre"],
	"liters": ["Litre"],
	"litres": ["Litre"],
	"m": ["Meter"],
	"mtr": ["Meter"],
	"metre": ["Meter"],
	"metres": ["Meter"],
	"meters": ["Meter"],
	"hr": ["Hour"],
	"hrs": ["Hour"],
	"hours": ["Hour"],
	"box": ["Box"],
	"boxes": ["Box"],
	"sq ft": ["Square Foot"],
	"sqft": ["Square Foot"],
	"sq m": ["Square Meter"],
	"sqm": ["Square Meter"],
}

_IRREGULAR_PLURALS = {"feet": "foot", "inches": "inch", "boxes": "box"}


def singularize(text):
	words = []
	for word in text.split():
		if word in _IRREGULAR_PLURALS:
			word = _IRREGULAR_PLURALS[word]
		elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
			word = word[:-1]
		words.append(word)
	return " ".join(words)


class UOMMatcher(BaseMatcher):
	doctype = "UOM"
	title_field = "uom_name"
	base_filters: ClassVar[dict] = {"enabled": 1}

	def match(self, value) -> MatchResult:
		text = clean_text(value, 140)
		if not text:
			return MatchResult(doctype=self.doctype, input=value)

		result = self.match_by_title(text)
		if result.matched or result.match_type == "duplicate_names":
			return result

		singular = singularize(normalize_name(text))
		if singular and singular != normalize_name(text):
			rows = self._get_list({self.title_field: singular}, limit=2)
			if len(rows) == 1:
				return self._found(rows[0], 0.97, "singular_form", text)

		key = re.sub(r"[.\s]+", " ", text.lower()).strip()
		for target in UOM_ALIASES.get(key, []) + UOM_ALIASES.get(key.replace(" ", ""), []):
			rows = self._get_list({"name": target}, limit=1)
			if rows:
				res = self._found(rows[0], 0.92, "alias", text)
				res.message = f"'{text}' interpreted as UOM '{rows[0]['name']}'"
				return res

		return result


def match_uom(value):
	return UOMMatcher().match(value)
