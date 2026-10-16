"""Deterministic handling of common chat messages, so they need no AI call.

Only messages that are unambiguous as a whole are handled here: greetings and thanks,
"open the 2nd one", "show more", "create a purchase invoice" (with nothing else), plain
list/count requests such as "show the last 10 sales invoices" or "how many draft purchase
invoices", and field questions about one named record: "what is the mobile number of Amit
Patel?", "Amit Patel's email", "which items are in Sales Invoice ACC-SINV-2026-00001?",
"what is the delivery date of this Sales Order?". Anything else goes to the router model as
before. A shortcut produces exactly the plan the router would, and the plan is validated
by chat.queries in the same way.

"this <document>" / "it" become {"ref": {"doctypes": [...]}}: the engine replaces it with the
record the user is looking at (the open form) or the single record shown last.
"""

import re

from ai_erpnext.chat.queries import DOCUMENT_DOCTYPES, ENTITY_DOCTYPES, REGISTRY, looks_like_id

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

def _plural(doctype):
	name = doctype.lower()
	if name.endswith("y"):
		return name[:-1] + "ies"
	return name + ("es" if name.endswith(("ch", "ss", "x")) else "s")


_PLURALS = {dt: _plural(dt) for dt in REGISTRY}
_PLURALS["BOM"] = "boms"
_DOCTYPE_ALT = "|".join(sorted({re.escape(n) for dt in REGISTRY for n in (dt.lower(), _PLURALS[dt])}, key=len, reverse=True))
_DOCTYPES = {n: dt for dt in REGISTRY for n in (dt.lower(), _PLURALS[dt])}

LIST_RE = re.compile(r"^\s*(?:please\s+)?(?:(?:show|list|display|get|give)(?:\s+me)?\s+)?(?:(?:the|my|all|all\s+the)\s+)?"
	r"(?:(?P<sort>last|latest|recent|newest|oldest|first)\s+)?(?P<limit>\d{1,2}\s+)?"
	r"(?:(?P<state>draft|submitted|cancelled|overdue|unpaid)\s+)?(?P<dt>" + _DOCTYPE_ALT + ")" + _END, re.I)
COUNT_RE = re.compile(r"^\s*(?:how\s+many|count(?:\s+the)?|number\s+of)\s+(?:(?P<state>draft|submitted|cancelled)\s+)?"
	r"(?:(?P<pay>unpaid|overdue|outstanding)\s+)?(?P<dt>" + _DOCTYPE_ALT + r")"
	r"(?:\s+(?:are\s+there|do\s+we\s+have|do\s+i\s+have|exist|(?:are|is)\s+(?:still\s+)?(?P<pay2>unpaid|overdue|outstanding)))?" + _END, re.I)

# ---- field questions -------------------------------------------------------------------------
# Field words (as people say them) -> the concept chat.queries maps to real fields per DocType.
FIELD_WORDS = {
	"mobile number": "mobile", "mobile no": "mobile", "mobile": "mobile", "cell number": "mobile", "phone number": "phone",
	"phone no": "phone", "phone": "phone", "contact number": "phone", "email address": "email", "email id": "email", "e-mail": "email",
	"email": "email", "outstanding amount": "outstanding", "outstanding balance": "outstanding", "outstanding": "outstanding",
	"balance": "outstanding", "amount due": "outstanding", "billing address": "address", "address": "address",
	"customer group": "customer_group", "supplier group": "supplier_group", "item group": "item_group", "territory": "territory",
	"tax id": "tax_id", "gstin": "tax_id", "gst number": "tax_id", "vat number": "tax_id", "pan": "tax_id", "currency": "currency",
	"payment status": "status", "status": "status", "due date": "due_date", "delivery date": "delivery_date", "posting date": "date",
	"invoice date": "date", "order date": "date", "required by date": "required_by", "required by": "required_by",
	"grand total": "total", "total amount": "total", "total": "total", "items": "items", "item list": "items", "line items": "items",
	"products": "items", "tax breakdown": "taxes", "tax details": "taxes", "taxes": "taxes", "supplier": "supplier",
	"customer": "customer", "target warehouse": "warehouse", "source warehouse": "warehouse", "warehouse": "warehouse",
	"primary contact": "contact", "company": "company",
}
# Concepts that make sense for a person or company name without a DocType ("mobile of Amit Patel").
ENTITY_CONCEPTS = {"mobile", "phone", "email", "address", "tax_id", "outstanding", "customer_group", "supplier_group", "territory",
	"contact", "currency"}
_FIELD_ALT = "|".join(sorted((re.escape(w) for w in FIELD_WORDS), key=len, reverse=True))
_ASK = r"^\s*(?:please\s+)?(?:(?:what|whats|what's|what\s+is|what\s+are|show(?:\s+me)?|give(?:\s+me)?|get(?:\s+me)?|tell\s+me|find|display|check)\s+)?(?:the\s+)?"
_TAIL = r"\s*[?.!]*\s*$"
FIELD_OF_RE = re.compile(_ASK + r"(?P<field>" + _FIELD_ALT + r")\s+(?:of|for|on|in)\s+(?P<target>.+?)" + _TAIL, re.I)
POSSESSIVE_RE = re.compile(_ASK + r"(?P<target>.+?)['’]s\s+(?:(?P<hint>customer|supplier|contact)\s+)?(?P<field>" + _FIELD_ALT
	+ r"|details|info|information)" + _TAIL, re.I)
ITEMS_IN_RE = re.compile(r"^\s*(?:which|what)\s+(?P<field>items|products)\s+(?:are|were|is)\s+(?:in|on)\s+(?P<target>.+?)" + _TAIL, re.I)
WHO_RE = re.compile(r"^\s*(?:who|which\s+(?:supplier|customer))\s+is\s+(?:the\s+)?(?P<field>supplier|customer)\s+(?:for|of|on)\s+(?P<target>.+?)" + _TAIL, re.I)
SUMMARY_RE = re.compile(r"^\s*(?:please\s+)?(?:(?:give|show)(?:\s+me)?\s+(?:an?\s+|the\s+)?summary\s+(?:of|for)|summari[sz]e)\s+(?:the\s+)?"
	r"(?P<kind>customer|supplier)\s+(?P<target>.+?)" + _TAIL, re.I)
DETAILS_RE = re.compile(r"^\s*(?:please\s+)?(?:show|give|get|display|find)(?:\s+me)?\s+(?:the\s+)?(?:details|info|information)\s+(?:of|for|on|about)\s+(?P<target>.+?)" + _TAIL, re.I)

# Words naming a DocType (or a family of them) at the start of a target: "customer Amit", "this invoice".
_TARGET_DOCTYPES = {n: [dt] for n, dt in _DOCTYPES.items()}
_TARGET_DOCTYPES.update({"invoice": ["Sales Invoice", "Purchase Invoice"], "invoices": ["Sales Invoice", "Purchase Invoice"],
	"order": ["Sales Order", "Purchase Order"], "receipt": ["Purchase Receipt"], "delivery": ["Delivery Note"], "bill": ["Purchase Invoice"],
	"vendor": ["Supplier"], "client": ["Customer"], "document": list(DOCUMENT_DOCTYPES), "record": list(DOCUMENT_DOCTYPES)})
_TARGET_DT_ALT = "|".join(sorted((re.escape(n) for n in _TARGET_DOCTYPES), key=len, reverse=True))
REF_RE = re.compile(r"^(?:this|that|the\s+current|the\s+open|current|it)(?:\s+(?P<dt>" + _TARGET_DT_ALT + r"))?\s*$", re.I)
PREFIX_RE = re.compile(r"^(?:the\s+|a\s+|an\s+)?(?P<dt>" + _TARGET_DT_ALT + r")\s+(?:called\s+|named\s+|no\.?\s*|number\s+|#\s*)?(?P<term>.+)$", re.I)
# DocTypes whose name may introduce a record name ("customer Amit", "item AIL-BOTTLE"). The names of the other queryable DocTypes are common
# words ("project", "account", "task", "asset"), so there they only count in front of an ID (“task TASK-2026-00001”), never in front of a name.
NAME_PREFIX_DOCTYPES = (*DOCUMENT_DOCTYPES, *ENTITY_DOCTYPES, "Item", "Warehouse", "Employee")
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
	m = SUMMARY_RE.match(text)
	if m and len(m.group("target")) <= 140:
		return {"mode": "query", "query": {"doctype": m.group("kind").capitalize(), "operation": "summary", "filters": {"search": m.group("target").strip(" .?!\"'“”")}}}
	plan = field_question(text)
	if plan:
		return plan
	m = LIST_RE.match(text)
	# a bare "sales invoices" without a verb or "last" is too vague to treat as a request
	if m and _LIST_VERB_RE.match(text):
		return _query(m, "list")
	return None


def field_question(text):
	"""A details plan for a question about one record's field(s), or None."""
	for regex in (ITEMS_IN_RE, WHO_RE, FIELD_OF_RE, POSSESSIVE_RE, DETAILS_RE):
		m = regex.match(text)
		if m:
			break
	else:
		return None
	field = (m.groupdict().get("field") or "").lower()
	concept = FIELD_WORDS.get(field) if field not in ("details", "info", "information") else None
	hint = (m.groupdict().get("hint") or "").capitalize() or None
	target = re.sub(r"^\s*(?:the|a|an)\s+", "", m.group("target").strip(" .?!\"'“”"), flags=re.I)
	if not target or len(target) > 140:
		return None
	query = {"operation": "details", "filters": {}}
	if concept:
		query["fields"] = [concept]
	ref = REF_RE.match(target)
	if ref:
		dt = (ref.group("dt") or "").lower()
		query["ref"] = {"doctypes": _TARGET_DOCTYPES.get(dt) or list(REGISTRY)}
		return {"mode": "query", "query": query}
	if target.lower() in _TARGET_DOCTYPES or re.match(r"^(?:all|my|our|every)\b", target, re.I):
		return None  # "outstanding for customers", "status of my orders": not one record
	prefix = PREFIX_RE.match(target)
	doctypes = [hint] if hint else None
	if prefix:
		family, term = _TARGET_DOCTYPES[prefix.group("dt").lower()], prefix.group("term").strip()
		if not looks_like_id(term) and not set(family) <= set(NAME_PREFIX_DOCTYPES):
			prefix = None  # "Project Hub Ltd", "Account Managers Co": a name that merely starts with the word of another DocType
	if prefix:
		if len(family) == 1:
			doctypes, target = family, term
		elif looks_like_id(term):
			target = term  # "invoice ACC-SINV-2026-00001": the ID decides which DocType
		else:
			return None
	if doctypes:
		query["doctype"] = doctypes[0]
	elif looks_like_id(target) or concept is None or concept in ENTITY_CONCEPTS:
		query["doctype"] = "auto"  # an ID, or a person/company name (Customer, Supplier or Contact)
	else:
		return None  # e.g. "status of my order": the router can ask what is meant
	query["filters"]["search"] = target
	return {"mode": "query", "query": query}


def _query(m, operation):
	doctype = _DOCTYPES[m.group("dt").lower()]
	spec = REGISTRY[doctype]
	state = (m.group("state") or "").lower()
	query = {"doctype": doctype, "operation": operation, "filters": {}}
	pay = (m.groupdict().get("pay") or m.groupdict().get("pay2") or "").lower()
	if pay:
		if doctype not in ("Sales Invoice", "Purchase Invoice"):
			return None
		query["filters"]["payment"] = "overdue" if pay == "overdue" else "unpaid"
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
