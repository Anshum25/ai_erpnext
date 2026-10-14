"""Deterministic handling of common chat messages, so they need no AI call.

Only messages that are unambiguous as a whole are handled here: greetings and thanks,
"open the 2nd one", "show more", "create a purchase invoice" (with nothing else), and
plain list/count requests such as "show the last 10 sales invoices" or "how many draft
purchase invoices". Anything with extra detail (a party, a period, a field) goes to the
router model as before. A shortcut produces exactly the plan the router would, and the
plan is validated by chat.queries in the same way.
"""

import re

from ai_erpnext.chat.queries import REGISTRY

GREETING_REPLY = ("Hello! How can I help? You can ask about ERPNext, look up your data, create records step by step "
	"(e.g. “create purchase invoice”), or attach an invoice image.")
THANKS_REPLY = "You're welcome! Anything else I can help with?"

_END = r"\s*[.!?]*\s*$"
GREETING_RE = re.compile(r"^\s*(hi|hello|hey|hiya|greetings|good (morning|afternoon|evening))(\s+there)?" + _END, re.I)
THANKS_RE = re.compile(r"^\s*((ok(ay)?|great|cool|perfect),?\s+)?(thanks|thank you|thx|ty)(\s+(a lot|so much|very much))?" + _END, re.I)
MORE_RE = re.compile(r"^\s*(show\s+more|more|next|next\s+page|load\s+more|see\s+more)" + _END, re.I)
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
OPEN_RE = re.compile(r"^\s*(?:please\s+)?open\s+(?:the\s+|number\s+|no\.?\s*|#\s*)?(?P<n>\d{1,3}|" + "|".join(ORDINALS) + r"|last)"
	r"(?:st|nd|rd|th)?(?:\s+(?:one|record|row|result|entry|invoice|order))?" + _END, re.I)

_PLURALS = {dt: dt.lower() + ("" if dt.endswith("y") else "s") for dt in REGISTRY}
_PLURALS.update({"Stock Entry": "stock entries", "Payment Entry": "payment entries"})
_DOCTYPE_ALT = "|".join(sorted({re.escape(n) for dt in REGISTRY for n in (dt.lower(), _PLURALS[dt])}, key=len, reverse=True))
_DOCTYPES = {n: dt for dt in REGISTRY for n in (dt.lower(), _PLURALS[dt])}

LIST_RE = re.compile(r"^\s*(?:please\s+)?(?:(?:show|list|display|get|give)(?:\s+me)?\s+)?(?:(?:the|my|all|all\s+the)\s+)?"
	r"(?:(?P<sort>last|latest|recent|newest|oldest|first)\s+)?(?P<limit>\d{1,2}\s+)?"
	r"(?:(?P<state>draft|submitted|cancelled|overdue|unpaid)\s+)?(?P<dt>" + _DOCTYPE_ALT + ")" + _END, re.I)
COUNT_RE = re.compile(r"^\s*(?:how\s+many|count(?:\s+the)?|number\s+of)\s+(?:(?P<state>draft|submitted|cancelled)\s+)?"
	r"(?P<dt>" + _DOCTYPE_ALT + r")(?:\s+(?:are\s+there|do\s+we\s+have|do\s+i\s+have|exist))?" + _END, re.I)
_LIST_VERB_RE = re.compile(r"^\s*(?:please\s+)?(show|list|display|get|give|last|latest|recent|newest|oldest)\b", re.I)


def match(text, last_query=None):
	"""A plan dict the engine can run directly, or None to ask the router model."""
	if GREETING_RE.match(text):
		return {"mode": "chat", "reply": GREETING_REPLY}
	if THANKS_RE.match(text):
		return {"mode": "chat", "reply": THANKS_REPLY}
	if MORE_RE.match(text) and last_query:
		return {"mode": "more"}
	m = OPEN_RE.match(text)
	if m and last_query and last_query.get("names"):
		n = m.group("n").lower()
		if n == "last":
			index = (last_query.get("plan") or {}).get("offset", 0) + len(last_query["names"])
		else:
			index = ORDINALS.get(n) or int(n)
		return {"mode": "open", "index": index}
	m = COUNT_RE.match(text)
	if m:
		return _query(m, "count")
	m = LIST_RE.match(text)
	# a bare "sales invoices" without a verb or "last" is too vague to treat as a request
	if m and _LIST_VERB_RE.match(text):
		return _query(m, "list")
	return None


def _query(m, operation):
	doctype = _DOCTYPES[m.group("dt").lower()]
	spec = REGISTRY[doctype]
	state = (m.group("state") or "").lower()
	query = {"doctype": doctype, "operation": operation, "filters": {}}
	if state in ("overdue", "unpaid"):
		if state not in spec.operations:
			return None  # e.g. "overdue customers": let the router explain
		query["operation"] = state
	elif state:
		query["filters"]["docstatus"] = state
	if operation == "list":
		sort = (m.groupdict().get("sort") or "").lower()
		query["sort"] = "oldest" if sort in ("oldest", "first") else "newest"
		if m.groupdict().get("limit"):
			query["limit"] = int(m.group("limit"))
	return {"mode": "query", "query": query}
