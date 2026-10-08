"""Pure helpers that sanitise untrusted AI output (strings, numbers, dates)."""

import datetime
import re
import unicodedata

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

MAX_TEXT_LENGTH = 500


def clean_text(value, max_length=MAX_TEXT_LENGTH):
	"""Return a trimmed single-line string, or None for empty/non-scalar input."""
	if value is None or isinstance(value, (dict, list, tuple, set, bool)):
		return None
	text = str(value).replace("\x00", "")
	text = _SPACE_RE.sub(" ", text).strip()
	if not text:
		return None
	return text[:max_length]


def normalize_name(value):
	"""Lowercase, strip accents/punctuation and collapse whitespace for comparison."""
	text = clean_text(value)
	if not text:
		return ""
	text = unicodedata.normalize("NFKD", text)
	text = "".join(ch for ch in text if not unicodedata.combining(ch))
	text = _PUNCT_RE.sub(" ", text.lower())
	return _SPACE_RE.sub(" ", text).strip()


def sorted_tokens(value):
	return " ".join(sorted(normalize_name(value).split()))


def parse_number(value):
	"""Parse numbers such as 24.99, "1,234.50", "$ 24.99", "(5.00)". Returns float or None."""
	if value is None or isinstance(value, bool):
		return None
	if isinstance(value, (int, float)):
		return float(value)
	text = clean_text(value, 64)
	if not text:
		return None
	negative = text.startswith("(") and text.endswith(")")
	text = text.replace(",", "").replace(" ", "")
	match = _NUMBER_RE.search(text)
	if not match:
		return None
	number = float(match.group())
	if negative or text.startswith("-"):
		number = -abs(number)
	return number


_TEXT_DATE_FORMATS = (
	"%d %b %Y",
	"%d %B %Y",
	"%b %d, %Y",
	"%B %d, %Y",
	"%b %d %Y",
	"%B %d %Y",
	"%d-%b-%Y",
	"%d-%B-%Y",
	"%d.%m.%Y",
)


class DateParseResult:
	def __init__(self, value=None, ok=False, ambiguous=False, raw=None, fmt=None):
		self.value = value  # ISO string
		self.ok = ok
		self.ambiguous = ambiguous
		self.raw = raw
		self.fmt = fmt


def parse_date(value):
	"""Parse a date strictly. Numeric d/m vs m/d ambiguity is reported, never guessed."""
	raw = clean_text(value, 64)
	if not raw:
		return DateParseResult(raw=raw)

	try:
		return DateParseResult(datetime.date.fromisoformat(raw[:10]).isoformat(), ok=True, raw=raw, fmt="iso")
	except ValueError:
		pass

	for fmt in _TEXT_DATE_FORMATS:
		try:
			return DateParseResult(
				datetime.datetime.strptime(raw, fmt).date().isoformat(), ok=True, raw=raw, fmt=fmt
			)
		except ValueError:
			continue

	parts = re.split(r"[/\-]", raw)
	if len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[2]) == 4:
		a, b, year = int(parts[0]), int(parts[1]), int(parts[2])
		candidates = set()
		for day, month in ((a, b), (b, a)):
			try:
				candidates.add(datetime.date(year, month, day).isoformat())
			except ValueError:
				continue
		if len(candidates) == 1:
			return DateParseResult(candidates.pop(), ok=True, raw=raw, fmt="numeric")
		if len(candidates) > 1:
			return DateParseResult(raw=raw, ambiguous=True)

	return DateParseResult(raw=raw)
