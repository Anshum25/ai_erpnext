"""The Desk page the user is looking at, as a validated, bounded hint.

The browser sends a small JSON object (see public/js/ai_page_context.js): the route, and for a saved form the DocType, record,
`modified` timestamp, whether it has unsaved changes and which fields changed; for a list the filters, sort, page and selected
record names; for a report its name and filters. Everything in it is a *hint*, never proof of anything:

- the DocType must be in the capability policy; records are re-fetched on the server with permission-aware, company-scoped
  queries (an unreadable, missing or forged record gives the same result: the context is dropped and the chat behaves as if it
  had not been sent, revealing nothing);
- field names are intersected with the fields the user may read (policy ∩ metadata ∩ field permissions);
- list filters are translated by capability.service.normalize_where; a filter that can't be translated makes the whole list
  context "not usable for a filtered answer" and is named in the reply, never silently dropped;
- reports must be in capability.policy.REPORTS and permitted for the user. A report is never executed and its results are never
  read (REPORT_RESULTS_ENABLED is False); only its name and validated filters are described;
- sizes are bounded here as well as in the browser, and an oversized or malformed object is ignored.

Questions about the page ("explain this invoice", "compare the selected orders", "summarize the filtered list", "what did I
change?") are answered here from server data by deterministic code. No record value and no browser value is sent to the model;
only a few counts and the page kind reach the router (PageContext.summary). Browser values (unsaved changes) are always labelled
as unsaved, shown only to explain the form, never written anywhere, and not kept in the conversation history (messages marked
`ephemeral` are stored with a placeholder).
"""

import json
import re

import frappe
from frappe.utils import get_datetime, strip_html

from ai_erpnext.capability import metadata as M
from ai_erpnext.capability import policy as P
from ai_erpnext.capability import service as C
from ai_erpnext.capability.access import readable_names
from ai_erpnext.mapping.normalize import clean_text, parse_date

MAX_JSON = 6000
MAX_ROUTE_JSON = 1000
MAX_CHANGED = 10
MAX_SELECTED = 20
MAX_FILTERS = 8
MAX_REPORT_FILTERS = 12
MAX_VALUE = 140
MAX_ROWS_SHOWN = 25
DOCSTATUS_NAMES = {0: "draft", 1: "submitted", 2: "cancelled"}
FIELD_RE = re.compile(r"^[a-z_][a-z0-9_]{0,60}$")
UI_OPERATORS = {"=", "!=", ">", "<", ">=", "<=", "like", "in", "not in", "between"}
EPHEMERAL_TEXT = "Your unsaved changes were shown here. They are not stored in the conversation."


class PageContext:
	"""A validated page. Nothing here has been trusted before it was checked on the server."""

	def __init__(self, kind):
		self.kind = kind  # "form", "list", "report" or "other" (workspaces and every page we can only name)
		self.route = []
		self.unsupported = None  # (DocType, "form"|"list") of a page the chat can't read
		self.doctype = self.name = None
		self.route_only = False
		self.limitations = []  # things the user asked for that could not be honoured, in plain words
		# form
		self.modified = self.saved_modified = None
		self.stale = False
		self.dirty = None  # True / False / None (unknown)
		self.changed = {}  # field -> unsaved value (browser)
		self.changed_tables = []
		self.ignored = 0
		self.selected_rows = {}  # table -> [row names] (saved rows only)
		# list
		self.where, self.docstatus, self.sort = [], None, None
		self.filters_known = self.filters_ok = False
		self.selected = []  # authorised record names
		self.selected_dropped = 0
		self.selected_total = 0
		self.too_many = False
		# report
		self.report = self.report_about = None
		self.report_filters = {}
		self.supported = True

	@property
	def view(self):
		"""(doctype, name) of the open saved form, for “this Sales Order” (kept for the original route-only behaviour)."""
		return (self.doctype, self.name) if self.kind == "form" and self.name else None

	def summary(self):
		"""What the router model may know: page kind and counts only. Never a record name, value or filter value."""
		return {
			"page": self.kind,
			"doctype": self.doctype if self.kind in ("form", "list") else None,
			"selected": len(self.selected) or None,
			"unsaved_changes": bool(self.dirty) or None,
			"list_filters": len(self.where) or None,
		}


# --------------------------------------------------------------------------------------------
# Parsing and validation
# --------------------------------------------------------------------------------------------
def _load(context):
	if isinstance(context, str):
		if len(context) > MAX_JSON:
			return None
		try:
			context = json.loads(context)
		except ValueError:
			return None
	return context if isinstance(context, dict) else None


def _text(value, limit=MAX_VALUE):
	if value is None or isinstance(value, (dict, list, tuple, set)):
		return None
	if isinstance(value, bool):
		return "Yes" if value else "No"
	return clean_text(strip_html(str(value)), limit)


def _names(values, limit=MAX_SELECTED):
	if not isinstance(values, list):
		return []
	return list(
		dict.fromkeys(
			n for n in (clean_text(v, 140) if isinstance(v, str) else None for v in values[: limit * 3]) if n
		)
	)


def _readable(doctype, names):
	"""The names the current user may read: permission-aware and company-scoped, the same query the look-ups use."""
	from ai_erpnext.chat import queries

	names = list(dict.fromkeys(names))
	if not names or not frappe.has_permission(doctype, "read"):
		return set()
	return set(readable_names(doctype, [["name", "in", names], *queries._scope(doctype)], limit=len(names)))


def parse(context):
	"""PageContext for the browser's context object, or None (missing, malformed, unsupported, inaccessible)."""
	raw = _load(context)
	if raw is None:
		return None
	try:
		return _parse(raw)
	except (
		Exception
	):  # a hint must never break the chat; nothing about the failure is shown or logged with values
		frappe.local.message_log = []
		return None


def _parse(raw):
	route = raw.get("route") if isinstance(raw.get("route"), list) else []
	route = [clean_text(r, 140) for r in route[:3] if isinstance(r, str)]
	page = raw.get("page") if raw.get("v") == 2 and raw.get("page") in ("form", "list", "report") else None
	route_only = page is None
	if route_only:  # the original context: just the route
		if len(json.dumps(raw, default=str)) > MAX_ROUTE_JSON and set(raw) <= {"route"}:
			return None
		if not route or not route[0]:
			return None
		kind = {"Form": "form", "List": "list", "query-report": "report"}.get(route[0], "other")
		raw = {
			"doctype": route[1] if len(route) > 1 else None,
			"name": route[2] if kind == "form" and len(route) > 2 else None,
			"report": route[1] if kind == "report" and len(route) > 1 else None,
		}
	else:
		kind = page
	ctx = PageContext(kind)
	ctx.route_only = route_only
	ctx.route = route
	if kind == "other":
		return ctx  # any other Desk page (a workspace, the home page ...): only its name, to say what it is
	found = {"form": _form, "list": _list, "report": _report}[kind](ctx, raw)
	return found or _unavailable(kind, raw, route)


def _unavailable(kind, raw, route):
	"""A form, list or report the chat can't use (a DocType outside the policy, a record or report the user can't access, a forged
	route). The page is then only named, so “what is this page about?” gets an answer instead of reaching the AI model. Nothing about
	any record is read; a DocType is named only when the user may read it."""
	page = PageContext("other")
	page.route = route
	page.route_only = True
	doctype = clean_text(raw.get("doctype"), 60) if kind in ("form", "list") else None
	if (
		doctype
		and isinstance(doctype, str)
		and frappe.db.exists("DocType", doctype)
		and not C.is_queryable(doctype)
		and frappe.has_permission(doctype, "read")
	):
		page.unsupported = (doctype, kind)
	return page


def _queryable(doctype):
	return isinstance(doctype, str) and C.is_queryable(doctype) and frappe.has_permission(doctype, "read")


def _form(ctx, raw):
	doctype, name = clean_text(raw.get("doctype"), 60), clean_text(raw.get("name"), 140)
	if not doctype or not name or name.startswith("new-") or not _queryable(doctype):
		return None
	if name not in _readable(doctype, [name]) or not frappe.has_permission(doctype, "read", name):
		return None  # missing, forbidden and forged records are indistinguishable
	ctx.doctype, ctx.name = doctype, name
	rows = frappe.get_list(doctype, filters={"name": name}, fields=["modified"], limit=1)
	ctx.saved_modified = rows[0]["modified"] if rows else None
	if not ctx.route_only:
		stamp = clean_text(raw.get("modified"), 40)
		if stamp:
			try:
				ctx.modified = get_datetime(stamp)
			except Exception:
				ctx.modified = None
			ctx.stale = bool(
				ctx.saved_modified and ctx.modified and get_datetime(ctx.saved_modified) != ctx.modified
			)
		ctx.dirty = raw.get("dirty") if isinstance(raw.get("dirty"), bool) else None
		if ctx.dirty:
			allowed = set(C.allowed_fields(doctype)) - {"name"}
			changed = raw.get("changed") if isinstance(raw.get("changed"), dict) else {}
			for field, value in list(changed.items())[:MAX_CHANGED]:
				text = _text(value)
				if isinstance(field, str) and FIELD_RE.match(field) and field in allowed and text is not None:
					ctx.changed[field] = text
				else:
					ctx.ignored += 1
			ctx.ignored += max(0, len(changed) - MAX_CHANGED)
			tables = C.child_tables(doctype)
			ctx.changed_tables = [
				t
				for t in (raw.get("changed_tables") if isinstance(raw.get("changed_tables"), list) else [])[
					:5
				]
				if t in tables
			]
		selected = raw.get("selected_rows") if isinstance(raw.get("selected_rows"), dict) else {}
		table = clean_text(selected.get("table"), 60)
		if table and table in C.child_tables(doctype):
			ctx.selected_rows = {table: _names(selected.get("rows"))}
	return ctx


def _list(ctx, raw):
	doctype = clean_text(raw.get("doctype"), 60)
	if not _queryable(doctype):
		return None
	ctx.doctype = doctype
	spec = P.REGISTRY[doctype]
	ctx.filters_known = not ctx.route_only and isinstance(raw.get("filters"), list)
	if not ctx.filters_known:
		ctx.limitations.append("I can't see this list's filters, so I can't summarize exactly what is shown.")
		return ctx
	ctx.filters_ok = True
	filters = raw["filters"]
	if len(filters) > MAX_FILTERS:
		ctx.filters_ok = False
		ctx.limitations.append(
			f"The list has more than {MAX_FILTERS} filters, which is more than I can apply."
		)
		filters = filters[:MAX_FILTERS]
	for item in filters:
		try:
			ctx_where, docstatus = _translate_filter(doctype, item)
		except C.CapabilityError as e:
			ctx.filters_ok = False
			ctx.limitations.append(str(e))
			continue
		if docstatus is not None:
			ctx.docstatus = docstatus
		if ctx_where:
			ctx.where.append(ctx_where)
	order = clean_text(raw.get("sort_by"), 60)
	direction = "desc" if str(raw.get("sort_order")).lower() != "asc" else "asc"
	if order:
		if order in (spec.date_field, "creation", "modified"):
			ctx.sort = "newest" if direction == "desc" else "oldest"
		elif order == spec.amount_field:
			ctx.sort = "highest" if direction == "desc" else "lowest"
		else:
			ctx.limitations.append("The list's sort order can't be reproduced here, so I use newest first.")
	total = raw.get("selected_count")
	names = _names(raw.get("selected"), MAX_SELECTED + 1)
	ctx.selected_total = (
		total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else len(names)
	)
	if ctx.selected_total > MAX_SELECTED or len(names) > MAX_SELECTED:
		ctx.too_many = True
		return ctx
	allowed = _readable(doctype, names)
	ctx.selected = [n for n in names if n in allowed]
	ctx.selected_dropped = len(names) - len(ctx.selected)
	return ctx


def _translate_filter(doctype, item):
	"""([field, op, value] | None, docstatus | None) for one Desk list filter, or CapabilityError (named, never dropped)."""
	if not isinstance(item, list) or len(item) not in (3, 4, 5):
		raise C.CapabilityError("A list filter was in a form I can't read.")
	if len(item) >= 4:
		if item[0] != doctype:
			raise C.CapabilityError(
				"The list is filtered on a field of a table inside the records, which I can't apply."
			)
		field, op, value = item[1], item[2], item[3]
	else:
		field, op, value = item
	op = str(op).strip().lower()
	field = clean_text(field, 60) if isinstance(field, str) else None
	label = M.field_info(doctype, field).label if field and M.field_info(doctype, field) else "a field"
	if op not in UI_OPERATORS:
		raise C.CapabilityError(
			f"The filter on {label} uses “{op}”, which I can't apply (supported: = != > < >= <= like in not in between)."
		)
	if field == "docstatus":
		try:
			status = int(value)
		except (TypeError, ValueError):
			status = None
		if op != "=" or status not in DOCSTATUS_NAMES:
			raise C.CapabilityError("The document status filter can't be applied.")
		return None, DOCSTATUS_NAMES[status]
	if op == "in" and isinstance(value, str):
		value = [v.strip() for v in value.split(",") if v.strip()]
	try:
		where = C.normalize_where(doctype, [{"field": field, "op": op, "value": value}])
	except C.CapabilityError as e:
		raise C.CapabilityError(f"The filter on {label} can't be applied: {e}") from e
	return where[0], None


def _report(ctx, raw):
	name = clean_text(raw.get("report"), 140)
	spec = P.REPORTS.get(name) if name else None
	if not name or not frappe.db.exists("Report", name):
		return None
	try:
		doc = frappe.get_doc("Report", name)
		if (
			doc.disabled
			or not doc.is_permitted()
			or (spec and not frappe.has_permission(spec["ref_doctype"], "read"))
		):
			return None  # not available to this user: indistinguishable from not existing
	except Exception:
		frappe.local.message_log = []
		return None
	ctx.report = name
	ctx.supported = bool(spec)
	ctx.report_about = spec["about"] if spec else None
	if not spec:
		ctx.limitations.append("I don't have a description for this report.")
		return ctx
	filters = raw.get("filters") if isinstance(raw.get("filters"), dict) and not ctx.route_only else {}
	for key, value in list(filters.items())[:MAX_REPORT_FILTERS]:
		if not isinstance(key, str) or key not in spec["filters"]:
			ctx.limitations.append("A report filter was not recognised and is not described.")
			continue
		kind = spec["filters"][key]
		text = _text(value, 60)
		if text is None or text == "":
			continue
		if kind == "company":
			from ai_erpnext.chat.queries import usable_companies

			if text not in usable_companies(spec["ref_doctype"]):
				ctx.limitations.append(
					"The report is filtered on a company that is not available to you; that filter is not described."
				)
				continue
		elif kind == "date":
			if not parse_date(text).ok:
				ctx.limitations.append("A report date filter could not be read.")
				continue
			text = parse_date(text).value
		elif not re.match(r"^[\w .\-/]{1,60}$", text):
			continue
		ctx.report_filters[key] = text
	return ctx


# --------------------------------------------------------------------------------------------
# Questions about the page
# --------------------------------------------------------------------------------------------
_END = r"\s*[?.!]*\s*$"
_THIS = r"(?:this|the\s+current|the\s+open|current)"
_VERB_EXPLAIN = r"(?:explain|summari[sz]e|describe|walk\s+me\s+through|tell\s+me\s+about|give\s+me\s+(?:an?\s+)?(?:summary|overview)\s+of|what(?:'s|\s+is)\s+(?:in|on))"
EXPLAIN_RE = re.compile(
	r"^\s*(?:please\s+)?" + _VERB_EXPLAIN + r"\s+" + _THIS + r"(?:\s+(?P<noun>[a-z][a-z ]{0,30}?))?" + _END,
	re.I,
)
ITEMS_RE = re.compile(
	r"^\s*(?:please\s+)?(?:explain|describe|summari[sz]e|show|list|what\s+are)\s+(?:these|the|this)\s+(?:\w+\s+)?(?P<table>items?|item\s+rows?|rows|lines?|line\s+items?|taxes|tax\s+rows?)"
	r"(?:\s+(?:on|in|of)\s+" + _THIS + r"\s+[a-z ]{1,30})?" + _END,
	re.I,
)
FIELDS_RE = re.compile(
	r"^\s*(?:what\s+(?:do|does|are|is)\s+(?:these|the|this)\s+fields?(?:\s+mean)?|explain\s+(?:these|the|this)\s+fields?)(?:\s+(?:on|in)\s+"
	+ _THIS
	+ r"\s+[a-z ]{1,30})?"
	+ _END,
	re.I,
)
UNSAVED_RE = re.compile(
	r"^\s*(?:what\s+(?:did|have)\s+i\s+(?:change|changed|edit|edited)|show\s+(?:me\s+)?(?:my\s+)?(?:unsaved\s+)?changes|what(?:'s|\s+is)\s+(?:not\s+saved|unsaved)|what\s+(?:is|are)\s+(?:my\s+)?unsaved\s+changes)"
	+ _END,
	re.I,
)
SELECTED_RE = re.compile(r"\b(selected|checked|ticked|these|those)\b", re.I)
SELECTED_VERB_RE = re.compile(
	r"^\s*(?:please\s+)?(?:(?P<why>why)\b|(?P<compare>compare|contrast)\b|(?:summari[sz]e|explain|show|list|describe|check|review|total|sum)\b|what(?:'s|\s+is|\s+are)\b)",
	re.I,
)
LIST_RE = re.compile(
	r"^\s*(?:please\s+)?(?:(?:summari[sz]e|describe|explain|give\s+me\s+(?:an?\s+)?(?:summary|overview)\s+of|how\s+many|count|total|what(?:'s|\s+is)\s+in)\b.*\b(?:list|filtered|view|results?|filters?)\b|"
	r"what\s+(?:filters?|am\s+i\s+looking\s+at)\b.*)" + _END,
	re.I,
)
REPORT_RE = re.compile(
	r"^\s*(?:please\s+)?(?:(?:explain|describe|summari[sz]e|what\s+is|what(?:'s)|what\s+does)\s+"
	+ _THIS
	+ r"\s+report(?:\s+show)?|what\s+am\s+i\s+looking\s+at|"
	r"what\s+filters?(?:\s+are\s+(?:applied|set|active))?|what\s+(?:company|period|date)\s+is\s+(?:this|the)\s+report\s+for)"
	+ _END,
	re.I,
)
# "show my current list", "what is in this filtered list?", "count the items in my current list", "summarize the records currently visible"
CURRENT_LIST_RE = re.compile(
	r"\b(?:(?:my|this|the)\s+(?:current\s+|filtered\s+|open\s+)*(?:list|view|results)|(?:records?|rows|items|entries)\s+"
	r"(?:currently\s+)?(?:visible|shown|listed|on\s+(?:this|the|my)\s+(?:list|page|screen))|(?:currently\s+)?visible\s+(?:records?|rows|items))\b",
	re.I,
)
CURRENT_LIST_VERB_RE = re.compile(
	r"^\s*(?:please\s+)?(?:show|list|display|open|give\s+me|what(?:'s|\s+is|\s+are)|which|summari[sz]e|describe|explain|"
	r"count|how\s+many|total|tell\s+me)\b",
	re.I,
)
REPORT_ASK_RE = re.compile(
	r"^\s*(?:please\s+)?(?:summari[sz]e|explain|describe|show|give|tell|what|which|how|interpret|analy[sz]e|read|run|list)\b",
	re.I,
)
REPORT_RESULTS_RE = re.compile(
	r"\b(results?|numbers?|figures?|totals?|values?|rows?|data|shows?|says?|outcome|profit|balances?|amounts?|run|read|analy[sz]e|interpret)\b",
	re.I,
)
PENDING_WORDS = re.compile(
	r"\b(pending|overdue|unpaid|open|late|delayed|stuck|outstanding|not\s+(?:delivered|billed|paid|received))\b",
	re.I,
)
# “what is this page about”, “what page is this”, “explain this page”, “where am I”: any page, whatever its kind.
ABOUT_RE = re.compile(
	r"^\s*(?:please\s+)?(?:(?:what(?:'s|\s+is)?|whats|which|explain|describe|tell\s+me\s+about|summari[sz]e)\s+(?:is\s+)?(?:this|the\s+current|current)?\s*(?:page|screen)"
	r"(?:\s+(?:is\s+)?(?:about|for|showing))?|where\s+am\s+i|which\s+page\s+is\s+this|what\s+am\s+i\s+(?:looking\s+at|on))"
	+ _END,
	re.I,
)
LOOSE_ABOUT_RE = re.compile(r"\b(page|screen)\b", re.I)
# “what does this page show(s)”, “what is on this screen”, “tell me about this dashboard”: any short question about the page itself
PAGE_QUESTION_RE = re.compile(
	r"^\s*(?:please\s+)?(?:what|which|explain|describe|tell|show|summari[sz]e|where|can\s+you\s+(?:explain|describe|tell))\b.*?\b(?:this|current|the\s+current|the\s+open)\s+"
	r"(?:page|screen|dashboard|workspace|view)\b",
	re.I,
)
MAX_ABOUT_WORDS = 10


def _is_about(text):
	return bool(
		ABOUT_RE.match(text) or (len(text.split()) <= MAX_ABOUT_WORDS and PAGE_QUESTION_RE.match(text))
	)


MAX_WORDS = 14


def classify(text, page):
	"""(intent, detail) for a message that is about the open page, else None (ordinary routing handles it)."""
	if not text or len(text.split()) > MAX_WORDS or len(text) > 200:
		return None
	if _is_about(text):
		return ("about", None)
	if page.kind == "other":
		card = _card_question(page, text)
		return ("card", card) if card else None
	if page.kind == "report":
		wants_results = bool(REPORT_RESULTS_RE.search(text) or re.search(r"\bsummari[sz]e\b", text, re.I))
		if REPORT_RE.match(text) or (EXPLAIN_RE.match(text) and "report" in text.lower()):
			return ("report", "results" if wants_results else None)
		# "summarize this report using the filters currently applied", "what are the results of this report?": answered from
		# the page (name and filters), with a plain statement that the report itself was not run.
		if REPORT_ASK_RE.search(text) and re.search(r"\b(?:this|the|current|open)\s+report\b", text, re.I):
			return ("report", "results" if wants_results else None)
		return None
	if page.kind == "list":
		verb = SELECTED_VERB_RE.match(text)
		if verb and SELECTED_RE.search(text) and not LIST_RE.match(text):
			sub = "why" if verb.group("why") else "compare" if verb.group("compare") else "summary"
			return ("selected", sub)
		if CURRENT_LIST_RE.search(text) and CURRENT_LIST_VERB_RE.match(text):
			if re.search(r"\b(count|how\s+many|number\s+of)\b", text, re.I):
				return ("list_count", None)
			if re.search(r"\b(summari[sz]e|describe|explain|overview|total)\b", text, re.I):
				return ("list", None)
			return ("show_list", "filters" if re.search(r"\bfilters?\b", text, re.I) else None)
		if LIST_RE.match(text):
			return (
				("filters", None)
				if re.search(r"\bfilters?\b", text, re.I)
				and not re.search(r"\b(summari|how many|count|total)", text, re.I)
				else ("list", None)
			)
		return None
	# form
	if UNSAVED_RE.match(text):
		return ("unsaved", None)
	m = ITEMS_RE.match(text)
	if m:
		return ("rows", "taxes" if m.group("table").lower().startswith(("tax",)) else "items")
	if FIELDS_RE.match(text):
		return ("fields", None)
	m = EXPLAIN_RE.match(text)
	if m:
		noun = (m.group("noun") or "").strip().lower()
		if re.search(r"\b(list|report|view|filters?)\b", noun):
			return None
		if noun and noun not in GENERIC_NOUNS and not all(w in page.doctype.lower() for w in noun.split()):
			return None  # “explain this customer” while an invoice is open: let the normal routing resolve it
		return ("explain", None)
	return None


GENERIC_NOUNS = {"record", "document", "doc", "form", "page", "screen", "one", "entry", "transaction"}


def answer(ctx, text, loose=False):
	"""Answer a question about the open page. True if handled (a reply was added), False for ordinary routing.
	loose: the AI service is unavailable, so any short message that mentions “this page” is answered from the page alone."""
	page = getattr(ctx, "page", None)
	if not page:
		return False
	found = classify(text, page)
	if not found and loose and len(text.split()) <= MAX_WORDS and LOOSE_ABOUT_RE.search(text):
		found = ("about", None)
	if not found:
		return False
	intent, detail = found
	handler = {
		"about": _about,
		"card": _card_answer,
		"explain": _explain,
		"rows": _rows,
		"fields": _fields,
		"unsaved": _unsaved,
		"selected": _selected,
		"list": _list_summary,
		"filters": _list_filters,
		"report": _report_explain,
		"show_list": _show_list,
		"list_count": _list_count,
	}[intent]
	try:
		handler(ctx, page, detail)
	except frappe.PermissionError:
		frappe.local.message_log = []
		ctx.say("You don't have permission to view that.")
	return True


STATUS_HELP = {
	"Draft": "not submitted yet; it has no accounting or stock effect and can still be edited",
	"Submitted": "submitted; it has taken effect and can't be edited (to change it, cancel and amend it in ERPNext)",
	"Cancelled": "cancelled; its effects were reversed",
	"Paid": "fully paid",
	"Unpaid": "submitted and not yet paid",
	"Partly Paid": "partly paid; some amount is still outstanding",
	"Overdue": "unpaid and past its due date",
	"Return": "a return (credit/debit note)",
	"Credit Note Issued": "a credit note was issued against it",
	"To Deliver and Bill": "submitted; it still has to be delivered and billed",
	"To Bill": "delivered but not billed yet",
	"To Deliver": "billed but not delivered yet",
	"To Receive and Bill": "submitted; it still has to be received and billed",
	"To Receive": "billed but not received yet",
	"Completed": "completed (delivered/received and billed)",
	"Closed": "closed manually",
	"On Hold": "on hold",
	"Open": "open and waiting for action",
	"Lost": "marked as lost",
	"Ordered": "an order was created from it",
	"Stopped": "stopped",
	"Expired": "past its valid-till date",
}


def _stale_note(ctx, page):
	if page.stale:
		ctx.say(
			f"Heads-up: your {page.doctype} {page.name} was saved again after this page was loaded. I'm answering from the latest saved version; "
			"reload the form to see it."
		)


def _saved(page, fields):
	"""The saved values of the open record, restricted to the fields the user may read."""
	from ai_erpnext.chat import queries

	allowed = set(C.allowed_fields(page.doctype))
	fields = [f for f in fields if f in allowed]
	if not fields:
		return {}
	rows = frappe.get_list(
		page.doctype,
		filters=[["name", "=", page.name], *queries._scope(page.doctype)],
		fields=fields,
		limit=1,
	)
	return rows[0] if rows else {}


def _run(ctx, plan):
	"""Run a validated query for the page. Every state is included (a list or form shows cancelled documents too)."""
	from ai_erpnext.chat import engine, queries

	q = queries.validate(plan)
	q["all_states"] = True
	return engine._run_query(ctx, None, prepared=q)


def _explain(ctx, page, _detail):
	spec = P.REGISTRY[page.doctype]
	_stale_note(ctx, page)
	from ai_erpnext.chat import engine

	engine._run_query(ctx, {"doctype": page.doctype, "operation": "details", "filters": {"name": page.name}})
	extra = []
	if spec.status_field and spec.status_field in C.allowed_fields(page.doctype):
		status = _saved(page, [spec.status_field]).get(spec.status_field)
		if status in STATUS_HELP:
			extra.append(f"Status “{status}” in ERPNext means: {STATUS_HELP[status]}.")
	from ai_erpnext.chat import capabilities

	guide = capabilities.GUIDE.get(page.doctype)
	if guide:
		extra.append(guide["what"])
	if extra:
		ctx.say(" ".join(extra), source="guide")
	if page.dirty:
		_unsaved(ctx, page, None, intro="This form also has unsaved changes. ")


def _rows(ctx, page, table):
	tables = C.child_tables(page.doctype)
	if table not in tables:
		ctx.say(f"{page.doctype} records have no {table} that I can show.")
		return
	_stale_note(ctx, page)
	chosen = page.selected_rows.get(table) or []
	if not chosen:
		_run(
			ctx,
			{
				"doctype": page.doctype,
				"operation": "child_rows",
				"where": [{"field": "name", "op": "=", "value": page.name}],
				"child": {"table": table},
			},
		)
	else:
		doc = frappe.get_doc(page.doctype, page.name)
		doc.check_permission("read")
		fields = C.child_fields(page.doctype, table)
		rows = [r for r in doc.get(table) if r.name in set(chosen)][:MAX_ROWS_SHOWN]
		meta = frappe.get_meta(M.child_doctype(page.doctype, table))
		if not rows:
			ctx.say(
				"The rows you selected aren't part of the saved document (new rows appear after you save)."
			)
		else:
			from ai_erpnext.chat import queries

			ctx.say(
				f"The {len(rows)} selected {table} row{'s' if len(rows) != 1 else ''} of {page.doctype} {page.name}.",
				source="erpnext",
				ref={"doctype": page.doctype, "names": [page.name], "requires": []},
				card={
					"type": "table",
					"title": f"{page.doctype} {page.name}: selected rows",
					"doctype": page.doctype,
					"columns": [{"key": f, "label": meta.get_label(f)} for f in fields],
					"rows": [
						{**{f: queries._jsonable(r.get(f)) for f in fields}, "_name": page.name, "_n": i + 1}
						for i, r in enumerate(rows)
					],
					"note": "From your ERPNext data (saved rows).",
				},
			)
	child = M.child_doctype(page.doctype, table)
	meta = frappe.get_meta(child)
	lines = []
	for f in C.child_fields(page.doctype, table):
		df = meta.get_field(f)
		if df:
			lines.append(f"• {df.label or f}: {strip_html(df.description or '').strip() or _type_help(df)}")
	ctx.say("What the columns mean:\n" + "\n".join(lines[:12]), source="guide")
	if table in page.changed_tables:
		ctx.say(f"The {table} on your form have unsaved changes; the rows above are the saved ones.")


def _type_help(df):
	t = df.fieldtype
	if t == "Link":
		return f"a link to a {df.options}"
	if t == "Select":
		options = [o for o in (df.options or "").split("\n") if o][:6]
		return "one of " + ", ".join(options) if options else "a choice"
	return {
		"Currency": "an amount",
		"Float": "a number",
		"Int": "a whole number",
		"Percent": "a percentage",
		"Date": "a date",
		"Datetime": "a date and time",
		"Check": "a yes/no tick",
		"Data": "text",
		"Small Text": "text",
		"Text": "text",
	}.get(t, t.lower())


def _fields(ctx, page, _detail):
	spec = P.REGISTRY[page.doctype]
	allowed = set(C.allowed_fields(page.doctype))
	meta = frappe.get_meta(page.doctype)
	rows = []
	for f in dict.fromkeys(spec.detail_fields):
		df = meta.get_field(f)
		if f == "name" or f not in allowed or not df:
			continue
		rows.append(
			{
				"field": df.label or f,
				"type": _type_help(df),
				"meaning": strip_html(df.description or "").strip()
				or ("Changed, not saved yet" if f in page.changed else ""),
			}
		)
	if not rows:
		ctx.say("I have no field descriptions for this document type.")
		return
	for r in rows:
		r["meaning"] = r["meaning"] or "-"
	ctx.say(
		f"The main fields of a {page.doctype}, as ERPNext defines them. Fields you may not see are left out.",
		source="guide",
		card={
			"type": "table",
			"title": f"{page.doctype} fields",
			"doctype": None,
			"columns": [
				{"key": "field", "label": "Field"},
				{"key": "type", "label": "Holds"},
				{"key": "meaning", "label": "Note"},
			],
			"rows": rows[:30],
			"note": "General ERPNext guidance from the field definitions, not data.",
		},
	)


def _unsaved(ctx, page, _detail, intro=""):
	if page.route_only or page.dirty is None:
		ctx.say("I can't tell whether this form has unsaved changes.")
		return
	if not page.dirty:
		ctx.say("This form has no unsaved changes.")
		return
	saved = _saved(page, list(page.changed))
	meta = frappe.get_meta(page.doctype)
	rows = [
		{
			"field": meta.get_label(f),
			"saved": "-" if saved.get(f) in (None, "") else str(saved.get(f)),
			"unsaved": v,
		}
		for f, v in page.changed.items()
	]
	if not rows and not page.changed_tables:
		ctx.say(
			intro
			+ "It has unsaved changes, but I can't see which fields changed. Nothing has been saved or changed in ERPNext."
		)
		return
	note = (
		"These values exist only in your browser. Nothing has been saved or changed in ERPNext, and I don't keep them in the conversation."
		+ (f" Tables with unsaved changes: {', '.join(page.changed_tables)}." if page.changed_tables else "")
		+ (
			f" {page.ignored} changed field{'s' if page.ignored != 1 else ''} could not be shown."
			if page.ignored
			else ""
		)
	)
	ctx.say(
		intro + f"Unsaved changes on {page.doctype} {page.name} (not saved in ERPNext):",
		ephemeral=True,
		source="erpnext",
		ref={"doctype": page.doctype, "names": [page.name], "requires": []},
		card={
			"type": "table",
			"title": "Unsaved changes",
			"doctype": None,
			"columns": [
				{"key": "field", "label": "Field"},
				{"key": "saved", "label": "Saved value"},
				{"key": "unsaved", "label": "Unsaved value (browser only)"},
			],
			"rows": rows,
			"note": note,
		}
		if rows
		else None,
	)


def _selected(ctx, page, kind):
	from ai_erpnext.chat import queries

	if page.too_many:
		ctx.say(
			f"You have {page.selected_total} rows selected. I can look at up to {MAX_SELECTED} at once; please narrow the selection."
		)
		return
	if not page.selected:
		ctx.say(
			"No rows are selected"
			+ (" that you can access" if page.selected_dropped else "")
			+ ". Tick the rows you mean in the list, or ask about the whole filtered list."
		)
		return
	doctype, spec = page.doctype, P.REGISTRY[page.doctype]
	allowed = set(C.allowed_fields(doctype)) - {"name"}
	if kind == "compare":
		fields = [f for f in dict.fromkeys(spec.detail_fields) if f in allowed][:9]
	elif kind == "why":
		fields = [
			f
			for f in (
				"status",
				"docstatus",
				"per_delivered",
				"per_billed",
				"per_received",
				"outstanding_amount",
				"due_date",
				"delivery_date",
				"schedule_date",
				"posting_date",
				"transaction_date",
				"grand_total",
				"currency",
			)
			if f in allowed
		][:9]
	else:
		fields = [f for f in spec.list_fields if f in allowed and f != "name"]
	plan = {
		"doctype": doctype,
		"operation": "list",
		"where": [{"field": "name", "op": "in", "value": page.selected}],
		"limit": MAX_SELECTED,
		"fields": fields,
		"sort": "oldest",
	}
	if page.selected_dropped:
		ctx.say(
			f"{page.selected_dropped} of the selected records aren't available to you, so I left them out."
		)
	_run(ctx, plan)
	if kind == "why":
		rows = frappe.get_list(
			doctype,
			filters=[["name", "in", page.selected], *queries._scope(doctype)],
			fields=["name", *fields],
			limit=MAX_SELECTED,
		)
		lines = [f"• {r['name']}: {reason}" for r in rows if (reason := _pending_reason(r))]
		ctx.say(
			"What the saved data says:\n"
			+ ("\n".join(lines) if lines else "None of the selected records shows anything pending."),
			source="erpnext",
			ref={"doctype": doctype, "names": page.selected, "requires": []},
		)
	elif kind == "summary":
		totals = _totals(doctype, [{"field": "name", "op": "in", "value": page.selected}], None)
		if totals:
			ctx.say(
				totals, source="erpnext", ref={"doctype": doctype, "names": page.selected, "requires": []}
			)


def _pending_reason(row):
	"""Facts from the record that explain why it isn't finished; nothing is inferred beyond what the fields say."""
	facts = []
	if row.get("docstatus") == 0 or row.get("status") == "Draft":
		facts.append("still a Draft (not submitted)")
	if row.get("docstatus") == 2 or row.get("status") == "Cancelled":
		return "cancelled"
	for field, word in (
		("per_delivered", "delivered"),
		("per_billed", "billed"),
		("per_received", "received"),
	):
		if row.get(field) is not None and row[field] < 100:
			facts.append(f"{row[field]:g}% {word}")
	if row.get("outstanding_amount") not in (None, 0) and row["outstanding_amount"] > 0:
		facts.append(f"outstanding {row['outstanding_amount']:g} {row.get('currency') or ''}".strip())
		if row.get("due_date"):
			facts.append(f"due {row['due_date']}")
	if row.get("status") in STATUS_HELP and row["status"] not in (
		"Draft",
		"Cancelled",
		"Paid",
		"Completed",
		"Closed",
	):
		facts.append(f"status “{row['status']}”: {STATUS_HELP[row['status']]}")
	return "; ".join(facts)


def _totals(doctype, where, docstatus):
	"""One sentence of totals (per currency) and a count by status for the records matching `where`, from validated aggregates."""
	from ai_erpnext.chat import queries

	spec = P.REGISTRY[doctype]
	parts = []
	if "aggregate" in spec.operations:
		if spec.amount_field and spec.amount_field in C.measure_fields(doctype):
			q = queries.validate(
				{
					"doctype": doctype,
					"operation": "aggregate",
					"where": _as_plan(where),
					"filters": {"docstatus": docstatus} if docstatus else {},
					"aggregate": {"fn": "sum", "field": spec.amount_field},
				}
			)
			q["all_states"] = not docstatus
			rows = queries.execute(q).get("card", {}).get("rows", [])
			label = frappe.get_meta(doctype).get_label(spec.amount_field)
			if rows:
				parts.append(
					f"Total {label.lower()}: "
					+ "; ".join(
						f"{r['value']:,.2f} {r.get('currency') or ''}".strip() + f" ({r['n']})" for r in rows
					)
					+ "."
				)
		if spec.status_field and spec.status_field in C.group_fields(doctype):
			q = queries.validate(
				{
					"doctype": doctype,
					"operation": "aggregate",
					"where": _as_plan(where),
					"filters": {"docstatus": docstatus} if docstatus else {},
					"aggregate": {"fn": "count", "group_by": spec.status_field},
				}
			)
			q["all_states"] = not docstatus
			rows = queries.execute(q).get("card", {}).get("rows", [])
			if rows:
				parts.append(
					"By status: "
					+ ", ".join(
						f"{r[spec.status_field]} {int(r['value'])}" for r in rows if r.get(spec.status_field)
					)
					+ "."
				)
	return " ".join(parts)


def _as_plan(where):
	return [w if isinstance(w, dict) else {"field": w[0], "op": w[1], "value": w[2]} for w in where]


def _list_filters(ctx, page, _detail):
	if not page.filters_known:
		ctx.say(
			"I can't see this list's filters."
			+ " Ask me about the records directly, for example “show unpaid sales invoices”."
		)
		return
	bits = [
		f"{frappe.get_meta(page.doctype).get_label(f).lower()} {op if op != 'like' else 'contains'} {v}"
		for f, op, v in page.where
	]
	if page.docstatus:
		bits.append(f"status {page.docstatus}")
	text = f"The {page.doctype} list " + (
		"is filtered by: " + "; ".join(bits) + "." if bits else "has no filters."
	)
	if page.limitations:
		text += " I couldn't apply: " + " ".join(page.limitations)
	ctx.say(text)


def _list_summary(ctx, page, _detail):
	from ai_erpnext.chat import engine, queries

	if not page.filters_known:
		ctx.say(page.limitations[0] + " Open the list again, or ask me about the records directly.")
		return
	if not page.filters_ok:
		ctx.say(
			"I can't summarize this filtered list exactly, because "
			+ " ".join(page.limitations)
			+ " Remove or change that filter, or ask me about the records directly."
		)
		return
	if page.limitations:
		ctx.say("Note: " + " ".join(page.limitations))
	plan = {
		"doctype": page.doctype,
		"operation": "count",
		"where": _as_plan(page.where),
		"filters": {"docstatus": page.docstatus} if page.docstatus else {},
	}
	q = queries.validate(plan)
	q["all_states"] = not page.docstatus
	engine._run_query(ctx, None, prepared=q)
	totals = _totals(page.doctype, page.where, page.docstatus)
	if totals:
		ctx.say(
			totals + " (all records matching the list's filters, not just the page shown)",
			source="erpnext",
			ref={"doctype": page.doctype, "names": [], "requires": []},
		)


def _exact_list(ctx, page, what):
	"""True if the open list's filters can be reproduced exactly; otherwise says why and returns False (never an unfiltered answer)."""
	if not page.filters_known:
		ctx.say(
			page.limitations[0] + f" So I can't {what} exactly; ask me about the records directly instead."
		)
		return False
	if not page.filters_ok:
		ctx.say(
			f"I can't {what} exactly, because "
			+ " ".join(page.limitations)
			+ " Remove or change that filter, or ask me about the records directly."
		)
		return False
	return True


def _show_list(ctx, page, detail):
	"""The records the open list shows (its validated filters and sort; at most 20), optionally with the filters spelled out."""
	from ai_erpnext.chat import engine, queries

	if not _exact_list(ctx, page, "show this list"):
		return
	if page.limitations:
		ctx.say("Note: " + " ".join(page.limitations))
	plan = {
		"doctype": page.doctype,
		"operation": "list",
		"where": _as_plan(page.where),
		"filters": {"docstatus": page.docstatus} if page.docstatus else {},
		"sort": page.sort or "newest",
		"limit": 20,
	}
	q = queries.validate(plan)
	q["all_states"] = not page.docstatus
	engine._run_query(ctx, None, prepared=q)
	if detail == "filters":
		_list_filters(ctx, page, None)


def _list_count(ctx, page, _detail):
	from ai_erpnext.chat import engine, queries

	if not _exact_list(ctx, page, "count this list"):
		return
	q = queries.validate(
		{
			"doctype": page.doctype,
			"operation": "count",
			"where": _as_plan(page.where),
			"filters": {"docstatus": page.docstatus} if page.docstatus else {},
		}
	)
	q["all_states"] = not page.docstatus
	engine._run_query(ctx, None, prepared=q)


def _report_explain(ctx, page, detail):
	if not page.supported:
		ctx.say(
			f"You have the report “{page.report}” open. I don't have a description for it, and I can't read report results yet."
		)
		return
	if detail == "results":
		ctx.say(
			f"I haven't run “{page.report}” and can't read its results: report results aren't available to the chat. "
			"Here is what it shows and how it is filtered; for figures, ask me for the records instead (for example “unpaid sales invoices by customer”)."
		)
	filters = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in page.report_filters.items())
	text = (
		f"“{page.report}” {page.report_about} "
		+ (
			f"It is currently filtered by: {filters}. "
			if filters
			else "I can't see its filters. "
			if page.route_only
			else "It has no filters I can describe. "
		)
		+ "I can explain what a report shows, but I can't read or interpret its result rows yet."
	)
	if page.limitations:
		text += " " + " ".join(sorted(set(page.limitations)))
	ctx.say(text, source="guide")


def _about(ctx, page, _detail):
	"""What the open page is. A form, list or report gets its own answer; any other page is named and described in general terms."""
	if page.kind == "form":
		return _explain(ctx, page, None)
	if page.kind == "report":
		return _report_explain(ctx, page, None)
	if page.kind == "list":
		if page.filters_known and page.filters_ok:
			ctx.say(f"You are on the list of {page.doctype} records. Here is a summary of what it shows.")
			return _list_summary(ctx, page, None)
		ctx.say(
			f"You are on the list of {page.doctype} records."
			+ (" " + page.limitations[0] if page.limitations else "")
		)
		return None
	_other_about(ctx, page)


def _other_about(ctx, page):
	"""A workspace is described from its own definition (what its owner put on it, limited to what the user may see); any other
	page is only named. Nothing here comes from the browser except the page's name."""
	if page.unsupported:
		doctype, what = page.unsupported
		ctx.say(
			f"You are on the {'list' if what == 'list' else 'form'} of “{doctype}” records. That isn't one of the record types I can read from the chat, "
			"so I can't tell you what is on this page. I can look up other records, for example sales invoices, customers, items or stock levels; "
			"ask “what can you look up?” for the list."
		)
		return
	model = _workspace_model(page)
	if not model:
		ctx.say(
			"You are on a Desk page that I can only name, not read, so I can't tell you what is on it. Ask me about a specific record or list, "
			"or open a document or list and ask again."
		)
		return
	from ai_erpnext.chat import capabilities

	name = model["name"]
	lines = [f"You are on the “{name}” workspace. It shows:"]
	rows, doctypes = [], []
	for card in model["cards"]:
		value, reason, doctype = _card_value(card)
		doctypes += [doctype] if doctype else []
		rows.append({"label": card.label, "value": value or f"not available: {reason}"})
		lines.append(
			f"• {card.label}: {value}" if value else f"• {card.label} (I can't calculate it: {reason})"
		)
	lines += [f"• Chart: {title}" for title in model["charts"]]
	for section, labels in model["sections"]:
		lines.append(f"• {section}: {', '.join(labels)}")
	module = capabilities.MODULE_ALIASES.get(name.lower()) or (name if name in capabilities.MODULES else None)
	if module and module in capabilities.MODULE_WHAT:
		lines.append(capabilities.MODULE_WHAT[module])
	if rows:
		lines.append(
			"The numbers are calculated now from your ERPNext data, with your permissions; I can't read the charts."
		)
	extra = {}
	if rows:
		extra = {
			"source": "erpnext",
			"ref": {
				"doctype": doctypes[0] if doctypes else "",
				"names": [],
				"requires": sorted(set(doctypes)),
			},
			"card": {
				"type": "table",
				"title": f"{name}: numbers on this page",
				"doctype": None,
				"columns": [{"key": "label", "label": "Number"}, {"key": "value", "label": "Value"}],
				"rows": rows,
				"note": "Calculated now from your ERPNext data.",
			},
		}
		if not doctypes:
			extra = {}
	ctx.say("\n".join(lines), **({"source": "guide"} if not extra else extra))


MAX_WORKSPACE_BLOCKS = 200
MAX_SECTION_LINKS = 10
MAX_SECTIONS = 10


def _visible_workspace(name):
	"""The Workspace record when this user would see it in Desk (public with no role limits or a matching role, or their own), else None."""
	from frappe.utils import has_common

	if not isinstance(name, str) or not name or not frappe.db.exists("Workspace", name):
		return None
	doc = frappe.get_doc("Workspace", name)
	if doc.get("is_hidden"):
		return None
	if not doc.public and doc.get("for_user") != frappe.session.user:
		return None
	roles = [r.role for r in doc.get("roles") or []]
	if roles and not has_common(roles, frappe.get_roles()):
		return None
	return doc


def _workspace_model(page):
	"""{"name", "cards": [Number Card docs], "charts": [titles], "sections": [(title, [labels])]} of the open workspace, from its content."""
	route = page.route or []
	name = route[-1] if len(route) > 1 and route[0] == "Workspaces" else None
	doc = _visible_workspace(name)
	if not doc:
		return None
	try:
		blocks = json.loads(doc.content or "[]")
	except ValueError:
		blocks = []
	cards, charts, sections, wanted = [], [], [], []
	for block in (blocks if isinstance(blocks, list) else [])[:MAX_WORKSPACE_BLOCKS]:
		kind, data = (block.get("type"), block.get("data") or {}) if isinstance(block, dict) else (None, {})
		if kind == "number_card" and isinstance(data.get("number_card_name"), str):
			try:
				card = frappe.get_doc("Number Card", data["number_card_name"])
				if card.has_permission("read"):
					card.label = card.get("label") or card.name
					cards.append(card)
			except Exception:
				frappe.local.message_log = []
		elif kind == "chart" and isinstance(data.get("chart_name"), str):
			charts.append(data["chart_name"])
		elif kind == "card" and isinstance(data.get("card_name"), str):
			wanted.append(data["card_name"])
	groups, current = {}, None
	for link in doc.get("links") or []:
		if link.type == "Card Break":
			current = link.label
			groups.setdefault(current, [])
		elif (
			current is not None
			and not link.get("hidden")
			and link.link_type in ("DocType", "Report")
			and link.link_to
		):
			if link.link_type == "DocType" and not frappe.has_permission(link.link_to, "read"):
				continue
			if link.link_type == "Report":
				try:
					if not frappe.get_doc("Report", link.link_to).is_permitted():
						continue
				except Exception:
					frappe.local.message_log = []
					continue
			groups[current].append(link.label or link.link_to)
	for title in wanted[:MAX_SECTIONS]:
		if groups.get(title):
			sections.append((title, groups[title][:MAX_SECTION_LINKS]))
	return {"name": doc.name, "cards": cards, "charts": charts, "sections": sections}


DYNAMIC_VALUES = {
	'frappe.defaults.get_user_default("Company")': lambda: frappe.defaults.get_user_default("Company"),
	"frappe.defaults.get_user_default('Company')": lambda: frappe.defaults.get_user_default("Company"),
}
NUMBER_FUNCTIONS = {"Count": "count", "Sum": "sum", "Average": "avg", "Minimum": "min", "Maximum": "max"}


def _card_value(card):
	"""(text, reason, doctype) for a Number Card: calculated only for “Document Type” cards on DocTypes the policy allows, with the
	same validated aggregates as every other question. Custom-method and report cards are never run."""
	from ai_erpnext.chat import queries

	if card.type != "Document Type" or not card.document_type:
		return None, "it is calculated by a custom method or report, which I don't run", None
	doctype = card.document_type
	if card.get("parent_document_type"):
		return None, "it counts rows inside another document", doctype
	if not C.is_queryable(doctype) or not frappe.has_permission(doctype, "read"):
		return None, "its data isn't available to me", doctype
	try:
		filters = json.loads(card.filters_json or "[]")
		dynamic = json.loads(card.get("dynamic_filters_json") or "[]")
		if not isinstance(filters, list) or not isinstance(dynamic, list):
			raise ValueError
	except ValueError:
		return None, "its filters can't be read", doctype
	for item in dynamic[
		:MAX_FILTERS
	]:  # Desk evaluates these in the browser; only the user's default company is understood here
		expression = item[3] if isinstance(item, list) and len(item) == 4 else None
		if expression not in DYNAMIC_VALUES:
			return None, "it uses a dynamic filter I can't evaluate", doctype
		value = DYNAMIC_VALUES[expression]()
		if not value:
			return None, "it depends on a default company, which isn't set for you", doctype
		filters.append([item[0], item[1], item[2], value])
	where, docstatus = [], None
	try:
		for f in filters[:MAX_FILTERS]:
			w, status = _translate_filter(doctype, f)
			where += [w] if w else []
			docstatus = status or docstatus
		fn = NUMBER_FUNCTIONS.get(card.function)
		if not fn or len(filters) > MAX_FILTERS:
			raise C.CapabilityError("its calculation isn't supported")
		aggregate = {"fn": fn, "field": card.aggregate_function_based_on if fn != "count" else None}
		q = queries.validate(
			{
				"doctype": doctype,
				"operation": "aggregate",
				"where": _as_plan(where),
				"filters": {"docstatus": docstatus} if docstatus else {},
				"aggregate": aggregate,
			}
		)
		q["all_states"] = not docstatus
		rows = queries.execute(q).get("card", {}).get("rows", [])
	except C.CapabilityError as e:
		return None, str(e).rstrip("."), doctype
	if not rows:
		return "0" if fn == "count" else "no data", None, doctype
	if fn == "count":
		return f"{int(sum(r['value'] for r in rows)):,}", None, doctype
	return (
		"; ".join(
			f"{r['value']:,.2f}"
			+ (f" {r['currency']}" if r.get("currency") else "")
			+ (f" ({r['company']})" if len(rows) > 1 and r.get("company") else "")
			for r in rows
		),
		None,
		doctype,
	)


_STOP = {
	"what",
	"whats",
	"is",
	"are",
	"the",
	"show",
	"me",
	"tell",
	"give",
	"of",
	"a",
	"an",
	"how",
	"many",
	"much",
	"our",
	"my",
	"current",
	"now",
	"please",
	"do",
	"we",
	"have",
	"in",
	"on",
	"this",
	"page",
	"there",
	"total",
}


def _words(text):
	return [w for w in re.findall(r"[a-z0-9]+", text.lower())]


def _card_question(page, text):
	"""Name of the Number Card on the open workspace that the question asks for (“total stock value?”, “how many warehouses”), or None."""
	if len(text.split()) > 8:
		return None
	asked = {w for w in _words(text) if w not in _STOP}
	if not asked:
		return None
	model = _workspace_model(page)
	if not model:
		return None
	hits = [
		c
		for c in model["cards"]
		if asked <= {w for w in _words(c.label) if w not in _STOP}
		and (len(asked) > 1 or len(_words(c.label)) <= 3)
	]
	exact = [c for c in hits if asked == {w for w in _words(c.label) if w not in _STOP}]
	hits = exact or hits
	return hits[0].name if len(hits) == 1 else None


def _card_answer(ctx, page, name):
	model = _workspace_model(page)
	card = next((c for c in (model or {}).get("cards", []) if c.name == name), None)
	if not card:
		ctx.say("I can't see that number on this page any more.")
		return
	value, reason, doctype = _card_value(card)
	if value is None:
		ctx.say(f"“{card.label}” is shown on this page, but I can't calculate it: {reason}.")
		return
	ctx.say(
		f"{card.label}: {value}. This is the number the “{model['name']}” page shows, calculated now from your ERPNext data with your permissions.",
		source="erpnext",
		ref={"doctype": doctype, "names": [], "requires": [doctype]},
	)
