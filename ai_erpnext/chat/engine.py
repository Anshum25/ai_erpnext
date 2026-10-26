"""Conversation engine for the AI assistant.

Free-text messages that no deterministic rule recognises go to the router model (chat/llm.py), which only proposes a
plan; every plan is validated on the server, and the model's own text is checked before it is shown (chat/claims.py).
Document images go through the extraction pipeline; the extracted values are untrusted data, and what the document
becomes is decided by detection/direction.py and validated by the registry's mapper/validator. The next question is
always derived from those server-side results plus the user's recorded answers, so a refresh, retry or interrupted
request resumes from the same point.
"""

import json
import re
import uuid

import frappe
from frappe import _
from frappe.utils import flt, nowdate, strip_html

from ai_erpnext.capability.access import readable_exists
from ai_erpnext.chat import capabilities, forms, history, hr, masters, page_context, workflow
from ai_erpnext.chat import session as S
from ai_erpnext.constants import FieldStatus
from ai_erpnext.mapping.adapters import DocumentAdapter
from ai_erpnext.mapping.company import CompanyMatcher
from ai_erpnext.mapping.currency import CurrencyMatcher
from ai_erpnext.mapping.item import ItemMatcher
from ai_erpnext.mapping.normalize import clean_text, parse_date
from ai_erpnext.mapping.uom import UOMMatcher
from ai_erpnext.mapping.warehouse import WarehouseMatcher
from ai_erpnext.pipeline.review_service import apply_mapping, load_json, set_overrides

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TEXT = 2000

HELP = (
	"I can help in three ways:\n"
	"• Chat: ask me about ERPNext concepts and processes, e.g. “Explain what a Sales Invoice is”.\n"
	"• Look up your data (read-only): e.g. “Show me the last 10 Sales Invoices”, “List overdue invoices”, "
	"“How much sales did we make this month?”, “Find customer ABC”, “What is my company?”. I show up to 20 records per message "
	"(“Show more” continues).\n"
	"• Create records by answering a few questions: e.g. “Create a purchase invoice”, “New sales order”, “Create customer”. "
	"{creatable}\n"
	"• Create a draft from a document image or a one-page PDF (attach it with the paperclip): invoices, credit and debit notes, orders, quotations, "
	"delivery documents, goods receipts, material requests and bills of materials. I work out what it is for your company from "
	"who issued it and who it is addressed to.\n\n"
	"While we work on a document you can say “change customer to …”, “set posting date to 2026-10-15”, "
	"“row 2 item SKU001”, “why?”, “retry” or “cancel”. I never submit, cancel, delete or pay documents."
)
HR_HELP = (
	"\n\nHR (HRMS): “show my leave balance”, “leave balance for <employee>”, “my attendance this month”, “find employee "
	"<name>”, and “apply for leave” to create a Leave Application as a Draft (never submitted or approved by me)."
)
# The reply to a question outside ERPNext and business (a recipe, general programming): fixed text, never the model's answer.
OFF_TOPIC = (
	"I'm your ERPNext assistant, so I only help with ERPNext and your business: looking up your data, creating records, "
	"and explaining ERPNext and business processes (accounting, sales, buying, stock, HR). That question is outside what "
	"I can help with here. Say “help” for examples."
)
# A model answer with code is only shown for a question about ERPNext/Frappe or business data (customisation, reports...).
ERP_TOPIC_RE = re.compile(
	r"\b(?:erp(?:next)?|frappe|doctypes?|server\s+scripts?|client\s+scripts?|custom\s+fields?|reports?|print\s+formats?|"
	r"jinja|workflows?|invoices?|orders?|items?|customers?|suppliers?|stock|ledgers?|accounts?|employees?|hrms)\b",
	re.I,
)
GREETING = "Hi! Ask me about SKYDOTERP, look up your data, create records step by step (e.g. “create purchase invoice”), or attach a document image."

# A caption asking for a payment: the image is still read, but nothing is ever paid (only Drafts are created).
PAYMENT_CAPTION_RE = re.compile(
	r"\b(?:pay|record|make|post|process)\s+(?:a\s+|the\s+)?payments?\b|\bpayment\s+entry\b|\bpay\s+(?:this|it|the)\b",
	re.I,
)
# Asking to stop what is in progress: the whole message ("cancel", "cancel this", "stop creating it", "cancel the sales
# invoice", "never mind", "I changed my mind", common typos). Handled without an AI call (engine._cancel). A message naming
# a record ID ("cancel ACC-SINV-2026-00001") never matches: that is about an ERPNext document, which the chat never cancels.
_CANCEL_LEAD = r"^\s*(?:(?:please|ok|okay|no|actually|just|oh|sorry),?\s+)*"
_CANCEL_VERB = r"(?:cancel+|cancle|canlce|cancal|cansel|stop|abort|discard|quit|exit)"
_CANCEL_OTHER = (
	r"never\s*mind|forget\s+(?:it|about\s+it)|(?:i\s+(?:have\s+|'ve\s+)?)?changed\s+my\s+mind"
	r"|i\s+(?:don'?t|do\s+not)\s+want\s+(?:it|this|that|to\s+(?:create|continue|make)\s+(?:it|this|that))(?:\s+any\s*more)?"
)
_CANCEL_TAIL = r"(?:,?\s+(?:please|thanks|thank\s+you))?\s*[.!]*\s*$"
CANCEL_RE = re.compile(
	_CANCEL_LEAD + r"(?:" + _CANCEL_VERB + r"(?:\s+[a-z']+){0,5}|" + _CANCEL_OTHER + r")" + _CANCEL_TAIL, re.I
)
# The same request naming nothing else: with nothing in progress it is answered as such, never routed as a document request.
BARE_CANCEL_RE = re.compile(
	_CANCEL_LEAD
	+ r"(?:"
	+ _CANCEL_VERB
	+ r"(?:\s+(?:it|this|that|this\s+one|now|everything|all|creating(?:\s+(?:it|this|that))?"
	r"|the\s+(?:creation|process|workflow|form|draft|request|task)))?|" + _CANCEL_OTHER + r")" + _CANCEL_TAIL,
	re.I,
)
CANCELLED_EXTRACTION = "Cancelled in the chat before it was read; nothing was created."
RESTART_RE = re.compile(
	r"^\s*(?:please\s+)?(?:start\s+(?:over|again|from\s+scratch)|restart|begin\s+again|reset)\b", re.I
)
HELP_RE = re.compile(r"^\s*(help|\?)\s*$", re.I)
# While an invoice question is pending, these cues mean "a separate question", not an answer.
GENERAL_CUE_RE = re.compile(
	r"^\s*(show|list|count|how many|how much|what|which|who|when|explain|summari[sz]e|tell me|give me|display|hi|hello|hey|good (morning|afternoon|evening)|thanks|thank you)\b",
	re.I,
)
ACTION_RE = re.compile(
	r"\b(create|make|raise|add|submit|cancel|delete|remove|update|edit|amend|pay|post|approve)\b", re.I
)
RETRY_RE = re.compile(r"^\s*(retry|try again|re-?check|refresh)\b", re.I)
WHY_RE = re.compile(
	r"^\s*(why\b|what'?s wrong|what is wrong|why was .* rejected|show (the )?(errors|problems)|status\b)",
	re.I,
)
YES_RE = re.compile(
	r"^\s*(yes|y|yeah|yep|ok|okay|sure|confirm|go ahead|proceed|do it|create( it)?( the)?( draft)?|continue)\b",
	re.I,
)
NO_RE = re.compile(r"^\s*(no|n|nope)\b", re.I)
CREATE_INVOICE_RE = re.compile(r"\b(create|make|raise|generate|prepare|draft)\b.*\binvoice\b", re.I)
_CREATE_NAMES = "|".join(
	sorted(
		[
			"sales invoice",
			"purchase invoice",
			"sales order",
			"purchase order",
			"quotation",
			"delivery note",
			"purchase receipt",
			"material request",
			"stock entry",
			"customer",
			"supplier",
			"item",
			"lead",
			"issue",
			"leave application",
			*forms.DOCTYPE_ALIASES,
		],
		key=len,
		reverse=True,
	)
)
CREATE_DOC_RE = re.compile(
	r"^\s*(?:please\s+)?(?:create|make|raise|add|new|prepare|draft|generate|start)\s+(?:a\s+|an\s+|new\s+|draft\s+)*"
	r"(" + _CREATE_NAMES + r")s?\b",
	re.I,
)
# A creation request anywhere in a longer message ("can you make a new po for Acme").
CREATE_ANY_RE = re.compile(
	r"\b(?:create|make|raise|prepare|draft|generate|new)\s+(?:a\s+|an\s+|new\s+|draft\s+)*("
	+ _CREATE_NAMES
	+ r")s?\b",
	re.I,
)
CREATE_NEW_RE = re.compile(r"\b(create|add|make|new)\b", re.I)
USE_EXISTING_RE = re.compile(r"\buse (the )?existing\b", re.I)
FIELD_CORRECTION_RE = re.compile(
	r"^\s*(?:please\s+)?(?:change|set|update|use|make|correct)\s+(?:the\s+)?"
	r"(customer name|customer type|customer group|customer|supplier name|supplier type|supplier group|supplier|company|posting date|invoice date|"
	r"order date|transaction date|due date|delivery date|required by date|required by|valid till|date|currency|warehouse|territory|item code|item name|"
	r"item group|stock uom|uom|unit)"
	r"\s*(?:to|as|=|:|is)?\s+(.+?)\s*$",
	re.I,
)
ROW_CORRECTION_RE = re.compile(
	r"^\s*(?:please\s+)?(?:change|set|update|use)?\s*(?:row|line|item)\s*#?(\d+)\s+(item|uom|unit)\s*(?:to|as|=|:|is)?\s+(.+?)\s*$",
	re.I,
)

# Field words -> a logical key, translated to the open document's own fieldnames by _correction_key.
KEY_ALIASES = {
	"posting date": "@date",
	"invoice date": "@date",
	"order date": "@date",
	"transaction date": "@date",
	"date": "@date",
	"due date": "@second",
	"delivery date": "@second",
	"required by date": "@second",
	"required by": "@second",
	"valid till": "@second",
	"customer": "@party:Customer",
	"supplier": "@party:Supplier",
	"company": "company",
	"currency": "currency",
	"warehouse": "warehouse",
}
MASTER_ALIASES = {
	"customer name": "customer_name",
	"customer type": "customer_type",
	"customer group": "customer_group",
	"supplier name": "supplier_name",
	"supplier type": "supplier_type",
	"supplier group": "supplier_group",
	"territory": "territory",
	"item code": "item_code",
	"item name": "item_name",
	"item group": "item_group",
	"stock uom": "stock_uom",
	"uom": "stock_uom",
	"unit": "stock_uom",
}


# Loop guard: the same question (kind, field and wording) asked this many times in a row,
# each time right after the user answered it, is replaced by a recovery message that says
# what is still missing and how to get out (the question stays open; nothing is discarded).
LOOP_THRESHOLD = 3
LOOP_RECOVERY = (
	"I still don't have a value I can use here, so let's do this differently. {question}\n"
	"Pick one of the options or type the exact value. You can also say “cancel” to stop, or “start over” to begin again."
)
# A lookup question ("Which customer do you mean?") is asked again at most this many times.
MAX_QUERY_ATTEMPTS = 2


class Ctx:
	def __init__(self, doc):
		self.doc = doc
		self.msgs = S.messages(doc)
		self.st = S.state(doc)
		self.start_seq = int(self.st.get("seq") or 0)
		self.loaded_seq = (
			self.start_seq
		)  # messages up to here were stored by earlier requests (rechecked when shown again)
		self.view = None  # (doctype, name) of the form the user has open, if readable
		self.page = None  # the validated Desk page (chat/page_context.py): form, list or report; a hint, rechecked on the server
		self.reasking = False  # True while re-asking a pending question after an unrelated message
		# The question the user is replying to now (loop guard: only a re-ask of it counts as a repeat).
		self.answering_sig = (self.st.get("ask_sig") or {}).get("sig") if self.st.get("pending") else None

	def say(self, text=None, **extra):
		return S.add_message(self.doc, self.msgs, self.st, "assistant", text, **extra)

	def user(self, text=None, **extra):
		return S.add_message(self.doc, self.msgs, self.st, "user", text, **extra)

	def ask(self, kind, text, key=None, options=None, input=None, **extra):
		options = list(options or [])
		sig = f"{kind}|{key}|{re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()}"
		last = self.st.get("ask_sig") or {}
		repeat = sig == last.get("sig") == self.answering_sig and not self.reasking
		count = (
			int(last.get("count") or 1) + 1
			if repeat
			else (int(last.get("count") or 1) if sig == last.get("sig") else 1)
		)
		self.answering_sig = None  # several questions in one reply: only the first can be a repeat
		self.st["ask_sig"] = {"sig": sig, "count": count}
		cancel = self._cancel_choice(kind, options)
		if count >= LOOP_THRESHOLD:
			from ai_erpnext.ai.errors import get_ai_logger

			get_ai_logger().warning(f"AI-CHAT loop_detected kind={kind} repeats={count}")
			text = LOOP_RECOVERY.format(question=text)
			if not cancel and not any(o.get("value") == "__cancel__" for o in options):
				options.append({"label": "Cancel", "value": "__cancel__", "style": "secondary"})
		q = {"id": uuid.uuid4().hex[:12], "kind": kind, "key": key, "options": options, "input": input}
		if cancel:
			q["cancel"] = cancel
		self.st["pending"] = q
		self.say(text, question={k: v for k, v in q.items() if k != "key"}, **extra)
		return q

	def _cancel_choice(self, kind, options):
		"""The Cancel shown with every question of a workflow (and of a lookup), in the same choice area as its options,
		unless the question already offers one. "__cancel_all__" always cancels the whole workflow, also from a question of
		a record created on the way (whose own "__cancel__" only goes back to the outer document)."""
		if any(str(o.get("label") or "").strip().lower() == "cancel" for o in options):
			return None
		if kind.startswith("query_"):
			return {"label": "Cancel", "value": "__cancel__"}
		if workflow.kind(self.st):
			return {"label": "Cancel", "value": "__cancel_all__"}
		return None

	def save(self):
		workflow.sync(self)
		workflow.current(self)  # a workflow from before workflows had ids gets one, stored with the state
		S.save(self.doc, self.msgs, self.st)

	def payload(self, since=None):
		since = self.start_seq if since is None else int(since or 0)
		pending = self.st.get("pending")
		return {
			"session": self.doc.name,
			"messages": history.recheck([m for m in self.msgs if m["id"] > since], self.loaded_seq),
			"busy": self.st.get("phase") == "extracting",
			"pending_id": pending["id"] if pending else None,
			"workflow": workflow.describe(self),  # the Cancel control of the workflow in progress, if any
		}


# --------------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------------
def greet_if_new(ctx):
	if not ctx.msgs:
		ctx.say(GREETING)


def handle_message(ctx, text=None, file_url=None, action=None, client_id=None, context=None):
	client_id = clean_text(client_id, 64)
	processed = ctx.st.setdefault("processed", [])
	replies = ctx.st.setdefault("replies", {})
	if client_id and client_id in processed:
		# Duplicate delivery of the same message (network retry, double click): nothing is processed
		# again; the reply produced the first time is returned so the browser can show it.
		if client_id in replies:
			ctx.start_seq = int(replies[client_id])
		return
	if client_id:
		processed.append(client_id)
		replies[client_id] = ctx.start_seq
	ctx.page = page_context.parse(context)
	ctx.view = ctx.page.view if ctx.page else None

	text = clean_text(text, MAX_TEXT) if text else None
	action = _parse_action(action)
	if action is False:
		ctx.say("That button press couldn't be read. Please try again.")
		return _reask_if_pending(ctx)

	if file_url:
		attachment = _attachment(file_url)
		ctx.user(
			text,
			attachment={"file_name": attachment["file_name"], "file_url": attachment["file_url"]}
			if attachment["ok"]
			else None,
		)
		if not attachment["ok"]:
			ctx.say(attachment["error"])
			return
		return _on_image(ctx, attachment, text)

	if action and action.get("command"):
		command = clean_text(action.get("command"), 40)
		if command == "more":
			ctx.user("Show more")
			return _more(ctx)
		if command == "cancel":  # the Cancel control of a workflow, bound to that workflow's id
			ctx.user("Cancel")
			return _cancel(ctx, workflow_id=clean_text(action.get("workflow_id"), 40) or None)
		if command == "say":  # suggestion buttons send ordinary text
			text = clean_text(action.get("value"), MAX_TEXT)
			if text:
				ctx.user(text)
				return _on_text(ctx, text)
		return
	if action:
		label = clean_text(action.get("label"), 200) or clean_text(action.get("value"), 200)
		ctx.user(label)
		return _on_action(ctx, action)

	if not text:
		return
	ctx.user(text)
	return _on_text(ctx, text)


def _view_context(context):
	"""(doctype, name) of the ERPNext form the user has open, when it is a record the chat can read.
	The browser only sends its route (and, from the page-aware widget, more); everything is re-checked in chat/page_context.py."""
	page = page_context.parse(context)
	return page.view if page else None


def _parse_action(action):
	"""A button press from the browser: dict, JSON object string, None (no action) or False (unreadable)."""
	if not action:
		return None
	if isinstance(action, dict):
		return action
	try:
		action = json.loads(action) if isinstance(action, str) and len(action) <= 4000 else None
	except ValueError:
		return False
	return action if isinstance(action, dict) else False


def poll(ctx):
	"""Advance an extraction that finished in the background worker."""
	if ctx.st.get("phase") != "extracting":
		return
	entry = _entry(ctx)
	if entry and entry.status == "Pending":
		_resume_if_lost(ctx, entry)
	if not entry or entry.status == "Pending":
		return
	if entry.status == "Failed":
		ctx.st["phase"] = "failed"
		ctx.ask(
			"extraction_failed",
			f"I couldn't read the invoice. {entry.error_log or ''}".strip(),
			options=[{"label": "Retry", "value": "__retry__"}, {"label": "Cancel", "value": "__cancel__"}],
		)
		return
	ctx.st["phase"] = "collecting"
	_announce(ctx, entry)
	advance(ctx)


STALE_EXTRACTION_S = 180  # a Pending entry without a queued or running job for this long was lost
LOST_EXTRACTION = "Reading the invoice didn't finish (the background job stopped). Please retry."


def _resume_if_lost(ctx, entry):
	"""Resume an extraction whose background job disappeared (worker restart, crashed job).

	A running job is left alone. A job that vanished or crashed is queued again once
	(deduplicated, so never twice in parallel). If it is lost again, or sits in the queue
	without a worker picking it up, the entry is marked Failed and the user gets the normal
	Retry/Cancel choice instead of an endless "reading the invoice".
	"""
	from frappe.utils import now_datetime, time_diff_in_seconds
	from frappe.utils.background_jobs import get_job_status
	from rq.job import JobStatus

	from ai_erpnext.ai.errors import get_ai_logger
	from ai_erpnext.pipeline.controller import enqueue_extraction, extraction_job_id

	job = get_job_status(extraction_job_id(entry.name))
	if job == JobStatus.STARTED:
		return  # still running; bounded by the job timeout
	requeued = ctx.st.get("requeued") == entry.name
	since = ctx.st.get("requeued_at") if requeued else entry.modified
	if time_diff_in_seconds(now_datetime(), since) < STALE_EXTRACTION_S:
		return
	if job != JobStatus.QUEUED and not requeued:
		ctx.st.update({"requeued": entry.name, "requeued_at": str(now_datetime())})
		get_ai_logger().warning(f"AI-CHAT extraction_requeued doc={entry.name} job_status={job}")
		enqueue_extraction(entry.name)
		return
	get_ai_logger().warning(f"AI-CHAT extraction_lost doc={entry.name} job_status={job}")
	entry.status = "Failed"
	entry.error_log = LOST_EXTRACTION
	entry.save()


# --------------------------------------------------------------------------------------------
# Input routing
# --------------------------------------------------------------------------------------------
def _on_image(ctx, attachment, text):
	if text and PAYMENT_CAPTION_RE.search(text):
		ctx.say(
			"I never record or make payments. I'll read the document and suggest the draft it becomes; nothing is paid or submitted."
		)
	if forms.active(ctx) or (
		ctx.st.get("phase") in ("extracting", "collecting", "failed") and ctx.st.get("entry")
	):
		ctx.st["queued_file"] = attachment["file_url"]
		ctx.ask(
			"replace_operation",
			"You're still working on another document. Do you want to cancel it and start on this new one?",
			options=[
				{"label": "Start the new document", "value": "__new__"},
				{"label": "Keep working on the current one", "value": "__keep__"},
			],
		)
		return
	_start_extraction(ctx, attachment["file_url"])


def _on_action(ctx, action):
	pending = ctx.st.get("pending")
	if not pending or action.get("question_id") != pending["id"]:
		ctx.say("That question is no longer active.")
		if pending:
			_reask(ctx)
		return
	if action.get("value") == "__cancel_all__":
		return _cancel(ctx)
	return _answer(ctx, pending, clean_text(action.get("value"), 200), from_text=False)


def _on_text(ctx, text):
	if CANCEL_RE.match(text) and (
		workflow.kind(ctx.st) or ctx.st.get("pending") or BARE_CANCEL_RE.match(text)
	):
		return _cancel(ctx)
	if RESTART_RE.match(text):
		return _restart(ctx)
	if HELP_RE.match(text):
		ctx.say(HELP.format(creatable=capabilities.creation_sentence()) + (HR_HELP if hr.installed() else ""))
		return _reask_if_pending(ctx)
	if RETRY_RE.match(text) and ctx.st.get("entry"):
		return _retry(ctx)
	if WHY_RE.match(text) and ctx.st.get("entry") and ctx.st.get("phase") not in (None, "done", "cancelled"):
		return _explain(ctx)

	if ctx.st.get("master"):
		m = FIELD_CORRECTION_RE.match(text)
		if m and m.group(1).lower() in MASTER_ALIASES:
			return _master_correction(ctx, MASTER_ALIASES[m.group(1).lower()], m.group(2))
	if ctx.st.get("phase") == "collecting" and not ctx.st.get("master"):
		m = ROW_CORRECTION_RE.match(text)
		if m:
			row, what, value = int(m.group(1)), m.group(2).lower(), m.group(3)
			key = f"item_{row}" if what == "item" else f"item_{row}_uom"
			return _answer(
				ctx,
				{"kind": "item" if what == "item" else "uom", "key": key},
				value,
				from_text=True,
				correction=True,
			)
		m = FIELD_CORRECTION_RE.match(text)
		if m and m.group(1).lower() in KEY_ALIASES:
			key = _correction_key(ctx, KEY_ALIASES[m.group(1).lower()])
			if not key:
				ctx.say(f"This document has no {m.group(1).lower()} I can change.")
				return _reask_if_pending(ctx)
			return _answer(ctx, {"kind": key, "key": key}, m.group(2), from_text=True, correction=True)

	if forms.active(ctx) and (forms.switch(ctx, text) or forms.correction(ctx, text)):
		return

	pending = ctx.st.get("pending")
	if pending and pending["kind"].startswith("query_"):
		# A lookup question is light: a new request replaces it instead of being forced into it.
		if _is_new_request(text):
			ctx.st["pending"] = None
			return _general(ctx, text)
		return _answer(ctx, pending, text, from_text=True)
	if pending:
		search_kinds = (
			"customer",
			"item",
			"uom",
			"company",
			"currency",
			"warehouse",
			"master_field",
			"form_field",
			"form_item",
			"form_warehouse",
		)
		from ai_erpnext.chat import shortcuts

		is_general = (
			GENERAL_CUE_RE.match(text)
			or CREATE_DOC_RE.match(text)
			or hr.APPLY_RE.match(text)
			or shortcuts.field_question(text)
			or (ctx.page and page_context.classify(text, ctx.page))
		) and not (
			(pending["kind"] in search_kinds and re.match(r"^\s*(find|search)\b", text, re.I))
			or forms.creates_new_for(ctx, pending, text)
		)
		if not is_general:
			return _answer(ctx, pending, text, from_text=True)
		_general(ctx, text)
		return _reask_if_pending(ctx)

	return _general(ctx, text)


def _is_new_request(text):
	"""True if a message is a request of its own rather than the name/ID a lookup question asked for."""
	from ai_erpnext.chat import capabilities, shortcuts

	if (
		"?" in text
		or len(text.split()) > 8
		or GENERAL_CUE_RE.match(text)
		or CREATE_DOC_RE.match(text)
		or hr.APPLY_RE.match(text)
		or ACTION_RE.match(text)
	):
		return True
	return bool(shortcuts.match(text) or capabilities.match(text))


def _reask_if_pending(ctx):
	pending = ctx.st.get("pending")
	if not pending:
		return
	if pending["kind"].startswith("query_"):
		ctx.st["pending"] = None  # lookup questions are not re-asked after another request
		return
	if forms.active(ctx):
		ctx.say(f"Back to the {ctx.st['forms'][-1]['doctype']}:")
	elif ctx.st.get("master"):
		ctx.say(f"Back to the new {ctx.st['master']['doctype'].lower()}:")
	else:
		ctx.say("Back to the invoice:")
	return _reask(ctx)


# --------------------------------------------------------------------------------------------
# General chat and read-only data queries
# --------------------------------------------------------------------------------------------
def _general(ctx, text):
	from ai_erpnext.ai.errors import AIProviderError
	from ai_erpnext.chat import capabilities, claims, company, llm, shortcuts

	# Questions about the page the user has open (“explain this invoice”, “compare the selected orders”) are answered from server data.
	if page_context.answer(ctx, text):
		_count_route(ctx, "page", "page")
		return
	# Unambiguous messages are handled without an AI call (see chat/shortcuts.py).
	plan = shortcuts.match(text, ctx.st.get("last_query"))
	# "apply for leave" is "create leave application"
	m = CREATE_DOC_RE.match(text) or hr.APPLY_RE.match(text)
	if plan is None and m and not text[m.end() :].strip(" .!?"):
		plan = {"mode": "create", "doctype": m.group(1)}  # "create purchase invoice" and nothing else
	elif plan is None and m and CONTEXT_CREATE_RE.match(text[m.end() :]):
		# "create a purchase order for this supplier" / "... for these items": the page supplies the party or the items
		rest = CONTEXT_CREATE_RE.match(text[m.end() :])
		plan = {
			"mode": "create",
			"doctype": m.group(1),
			"values": {"party": OPEN_RECORD} if rest.group("party") else {},
			"items": [{"item": SELECTED_RECORDS}] if rest.group("items") else [],
		}
	via = "shortcut" if plan else "model"
	if plan is None:
		try:
			plan = llm.route(
				ctx.msgs[:-1], text, ctx.st.get("last_query"), page=ctx.page.summary() if ctx.page else None
			)
		except AIProviderError as e:
			_count_route(ctx, "model", "error")
			if ctx.page and page_context.answer(
				ctx, text, loose=True
			):  # “what is this page about” needs no AI service
				return
			if m:  # creation works without the AI service: start the guided form with no prefill
				return forms.start(ctx, forms.match_doctype(m.group(1)))
			if hr.LEAVE_REQUEST_RE.search(text) and "?" not in text and not capabilities.match(text):
				# "apply for two days of casual leave" (a request, not a question): the form asks for the details
				return forms.start(ctx, "Leave Application")
			reason = CHAT_ERRORS.get(e.category, CHAT_ERRORS["unknown"])
			guidance = capabilities.answer(text)
			if (
				guidance
			):  # known ERPNext questions are answered without the AI service, and the user is told why
				ctx.say(
					f"{guidance}\n\n(Built-in guidance: {reason[0].lower() + reason[1:]})", source="guide"
				)
				return
			ctx.say(reason)
			return
	mode = plan["mode"]
	_count_route(ctx, via, mode)
	if mode == "chat" and via == "model" and page_context.asks_for_open_record(text, ctx.page):
		# The model can't see the open form and says so; the record's saved data is the answer the user asked for.
		return page_context.explain_open_record(ctx)
	if (
		mode == "chat"
		and via == "model"
		and "```" in (plan.get("reply") or "")
		and not ERP_TOPIC_RE.search(text)
	):
		mode = "off_topic"  # general programming help, whatever the model decided
	if mode == "off_topic":
		ctx.say(OFF_TOPIC)
	elif mode == "chat":
		reply = plan.get("reply") or "How can I help?"
		if via == "model":
			reply = claims.guard(reply, text, "chat")  # model text never states data or completed actions
		about = capabilities.match(text)
		if about and about[0] in ("how", "what", "after"):
			reply += "\n\n" + capabilities.footer(about[1])  # what the chat itself can do, from the registry
		ctx.say(reply, source="general")
	elif (
		mode == "clarify"
		and CREATE_ANY_RE.search(text)
		and forms.match_doctype(CREATE_ANY_RE.search(text).group(1))
	):
		# A creation request is never clarified by the model in free text: the guided form asks,
		# with the existing records as buttons and a separate "Create new".
		return _start_form(ctx, forms.match_doctype(CREATE_ANY_RE.search(text).group(1)))
	elif mode == "clarify":
		question = plan.get("question") or "Could you give me a bit more detail?"
		if via == "model":
			question = claims.guard(question, text, "clarify")
		previous = next((x for x in reversed(ctx.msgs[:-1]) if x["role"] == "assistant"), None)
		if previous and previous.get("source") == "clarify" and _same_text(previous.get("text"), question):
			# The model asked the same thing again although the user answered: don't repeat it.
			ctx.say(
				"I still can't tell exactly what you mean, so I won't ask the same question again. Please say it in one message, "
				"for example “mobile number of customer Amit Patel”, “status of Sales Order SAL-ORD-2026-00001” or "
				"“create purchase order”."
			)
			return
		ctx.say(question, source="clarify")
	elif mode == "create":
		doctype = forms.match_doctype(plan.get("doctype") or "")
		if not doctype:
			ctx.say(forms.not_creatable(clean_text(plan.get("doctype"), 60)))
			return
		values, items = _page_prefill(
			ctx,
			doctype,
			plan.get("values") if isinstance(plan.get("values"), dict) else None,
			plan.get("items") if isinstance(plan.get("items"), list) else None,
		)
		return _start_form(ctx, doctype, values=values, items=items)
	elif mode == "unsupported":
		if via == "model" and plan.get("reply"):
			plan = dict(plan, reply=claims.guard(plan["reply"], text, "unsupported"))
		if ctx.page and ctx.page.dirty:
			plan = dict(
				plan,
				reply=(plan.get("reply") or "I can't do that yet.")
				+ " This form also has unsaved changes. Editing from the chat isn't available; "
				"when it is, the form will first have to be saved or discarded and reloaded. Nothing you typed in the form was saved by me.",
			)
		ctx.say(
			(plan.get("reply") or "I can't do that yet.")
			+ " From the chat I can only create new records, after you confirm. "
			+ capabilities.creation_sentence()
			+ f" I never {', '.join(capabilities.NEVER)}."
		)
	elif mode == "open":
		_open_result(ctx, plan.get("index"))
	elif mode == "more":
		_more(ctx)
	elif mode == "records":
		_which_records(ctx, plan.get("limit"))
	elif mode == "company":
		_company(ctx)
	elif mode == "query":
		query = plan.get("query")
		if isinstance(query, dict) and company.is_company_doctype(query.get("doctype")):
			# "my company" planned as a Company query: answered from the user's own companies
			return _company(ctx)
		if isinstance(query, dict) and query.get("ref") == OPEN_RECORD:
			query = _open_record_query(ctx, query)
			if query is None:
				return
		elif isinstance(query, dict) and isinstance(query.get("ref"), dict):
			query = _resolve_ref(ctx, query)
		_run_query(ctx, query)
		_unapplied_page_filters(ctx, query)


def _which_records(ctx, limit=None):
	"""Records of no named kind ("show 100000 records"): ask which, never reuse the previous query's DocType or filters."""
	from ai_erpnext.chat import queries, shortcuts

	# a new topic: a reply such as "invoices" must not inherit the earlier filters
	ctx.st["last_query"] = None
	text = shortcuts.RECORDS_QUESTION
	if limit and limit > queries.MAX_PAGE:
		text += (
			f" Note: I show at most {queries.MAX_PAGE} records per message (you asked for {limit}); "
			"use “Show more” for the next ones, or open the list in ERPNext to see or export all of them."
		)
	ctx.say(
		text, suggestions=["Show customers", "Show sales invoices", "Show sales orders"], source="clarify"
	)


def _company(ctx):
	from ai_erpnext.chat import company

	answer = company.answer()
	if answer["names"]:
		ctx.say(answer["text"], source="erpnext", ref={"doctype": "Company", "names": answer["names"]})
	else:
		ctx.say(answer["text"])


def _start_form(ctx, doctype, values=None, items=None):
	if ctx.st.get("phase") in ("extracting", "collecting") or forms.active(ctx):
		current = ctx.st["forms"][-1]["doctype"] if forms.active(ctx) else "invoice"
		ctx.say(
			f"Please finish or cancel the current {current} first (say “cancel”)"
			+ (
				f", or say “I meant {doctype}” to switch and keep what you entered."
				if forms.active(ctx)
				else "."
			)
		)
		return _reask_if_pending(ctx)
	return forms.start(ctx, doctype, values=values, items=items)


def _same_text(a, b):
	norm = lambda v: re.sub(r"[^a-z0-9]+", " ", (v or "").lower()).strip()  # noqa: E731
	return bool(norm(a)) and norm(a) == norm(b)


def _resolve_ref(ctx, query):
	"""Replace “this Sales Order” / “it” with the record meant: the form the user has open, else
	the single record shown or created last. Unresolved: the plan asks which one."""
	from ai_erpnext.chat.queries import REGISTRY

	doctypes = [dt for dt in (query.get("ref") or {}).get("doctypes") or [] if dt in REGISTRY]
	query = {k: v for k, v in query.items() if k != "ref"}
	candidates = []
	if ctx.view:
		candidates.append(ctx.view)
	last = ctx.st.get("last_query") or {}
	if len(last.get("names") or []) == 1:
		candidates.append((last.get("doctype"), last["names"][0]))
	for created in (ctx.st.get("last_created"), ctx.st.get("created")):
		if isinstance(created, dict):
			candidates.append((created.get("doctype"), created.get("name")))
	for doctype, name in candidates:
		if doctype in doctypes and name:
			return dict(query, doctype=doctype, filters={**(query.get("filters") or {}), "name": name})
	return dict(query, doctype=doctypes[0] if len(doctypes) == 1 else "auto", filters={})


OPEN_RECORD, SELECTED_RECORDS = "open_record", "selected_records"
CONTEXT_CREATE_RE = re.compile(
	r"^\s*(?:for|from|to)\s+(?:(?P<party>(?:this|the\s+current|the\s+open|current)\s+(?:customer|supplier|client|vendor))"
	r"|(?P<items>(?:these|the\s+selected|selected)\s+(?:items|products|records|rows)))\s*[.!?]*\s*$",
	re.I,
)


def _open_record_query(ctx, query):
	"""A model plan that names "the open record" (an opaque reference, never a name): resolved here from the validated form.

	The open record is the target itself (same DocType: a name filter) or is linked from it (Bin.item_code -> the open
	Item, Sales Order.customer -> the open Customer: a filter on that Link field). Anything else is refused, never widened."""
	from ai_erpnext.capability import service as capability
	from ai_erpnext.chat.queries import REGISTRY, _match_doctype

	query = {k: v for k, v in query.items() if k != "ref"}
	if not ctx.view:
		ctx.say(
			"I can't see a record open on your screen that I can use. Open the record's form, or tell me its name."
		)
		return None
	open_doctype, open_name = ctx.view
	target = _match_doctype(query.get("doctype")) or open_doctype
	if target == open_doctype:
		return dict(
			query,
			doctype=target,
			filters={**(query.get("filters") or {}), "name": open_name, "search": None, "party": None},
		)
	if query.get("operation") in ("details", "summary"):
		ctx.say(
			f"The open record is a {open_doctype}, not a {target}. Tell me which {target.lower()} you mean."
		)
		return None
	spec = REGISTRY.get(target)
	try:
		links = capability.link_fields(target) if target in REGISTRY else {}
		filterable = set(capability.filterable_fields(target)) if target in REGISTRY else set()
	except capability.CapabilityError:
		links, filterable = {}, set()
	candidates = [f for f, dt in links.items() if dt == open_doctype and f in filterable]
	if spec and spec.party_field in candidates:
		candidates = [spec.party_field]
	if not candidates:
		ctx.say(f"{target} records aren't linked to the open {open_doctype}, so I can't use it here.")
		return None
	where = [
		w for w in (query.get("where") or []) if not (isinstance(w, dict) and w.get("field") == candidates[0])
	]
	filters = {k: v for k, v in (query.get("filters") or {}).items() if k not in ("party", "search", "name")}
	return dict(
		query,
		doctype=target,
		filters=filters,
		where=[*where, {"field": candidates[0], "op": "=", "value": open_name}],
	)


def _unapplied_page_filters(ctx, query):
	"""A model-planned list/count on the DocType of the open list: say that the list's own filters were not used."""
	page = ctx.page
	if not (
		page
		and page.kind == "list"
		and isinstance(query, dict)
		and page.filters_known
		and (page.where or page.docstatus)
	):
		return
	from ai_erpnext.chat.queries import _match_doctype

	if _match_doctype(query.get("doctype")) == page.doctype and query.get("operation", "list") in (
		"list",
		"count",
		"aggregate",
	):
		ctx.say(
			"Note: this answer doesn't use the filters of the list you have open. Ask “show my current list” for exactly what the list shows."
		)


def _page_prefill(ctx, doctype, values, items):
	"""Values for a new document taken from the page: the open Customer/Supplier as the party, the selected Items as rows.
	Only validated page context is used, and only when the open record is the kind of party the document needs."""
	values = dict(values or {})
	items = list(items or [])
	spec = forms.SPECS.get(doctype) or {}
	if values.get("party") == OPEN_RECORD or any(v == OPEN_RECORD for v in values.values()):
		values = {k: v for k, v in values.items() if v != OPEN_RECORD}
		party_field = forms._field(doctype, spec["party"]) if spec.get("party") else None
		needed = party_field["doctype"] if party_field else None
		if not ctx.view:
			ctx.say("I can't see a record open on your screen, so I'll ask for the party.")
		elif ctx.view[0] != needed:
			ctx.say(
				f"The open record is a {ctx.view[0]}, which isn't a {(needed or 'party').lower()} for a {doctype}, so I didn't use it."
			)
		else:
			values["party"] = ctx.view[1]
	if any(isinstance(i, dict) and i.get("item") == SELECTED_RECORDS for i in items):
		items = [i for i in items if not (isinstance(i, dict) and i.get("item") == SELECTED_RECORDS)]
		page = ctx.page
		if page and page.kind == "list" and page.doctype == "Item" and page.selected:
			items += [{"item": name} for name in page.selected]
		else:
			ctx.say("I can't see selected items on your screen, so I'll ask for the items.")
	return values, items


def _count_route(ctx, via, mode):
	"""Per-conversation counters of how free-text messages were routed (model calls vs shortcuts)."""
	from ai_erpnext.ai.errors import get_ai_logger

	metrics = ctx.st.setdefault("metrics", {})
	key = "model_routes" if via == "model" else "shortcut_routes"
	metrics[key] = int(metrics.get(key) or 0) + 1
	get_ai_logger().info(f"AI-CHAT routed via={via} mode={mode}")


# Chat-specific wording for AI router failures (the shared provider messages talk about extraction).
CHAT_ERRORS = {
	"timeout": "The AI service is responding slowly right now. Please try again in a moment.",
	"network": "I couldn't reach the AI service. Please check the connection and try again.",
	"unavailable": "The AI service is temporarily unavailable. Please try again in a moment.",
	# A 429 that names neither a daily quota nor credits: usually a short (per-minute) limit, but the provider doesn't always say which.
	"rate_limited": "The AI request limit was reached for now. Please wait about a minute and try again.",
	"quota_exhausted": "Today's AI usage limit has been reached, so chat and data questions are paused until it resets. "
	"You can still create records by typing, for example, “create purchase invoice”.",
	"auth": "The AI service isn't configured correctly. Please contact the administrator.",
	"config": "The AI service isn't configured correctly. Please contact the administrator.",
	"billing": "The AI service account needs attention. Please contact the administrator.",
	"invalid_response": "The AI couldn't produce a usable answer this time. Please rephrase your question or try again.",
	"bad_request": "The AI service couldn't process that request. Please rephrase it.",
	"request_too_large": "That request was too large for the AI service. Please ask a shorter question.",
	"unknown": "Something went wrong with the AI service. Please try again.",
}


def _run_query(ctx, raw_plan, page_of=None, prepared=None, attempts=0):
	from ai_erpnext.chat import queries

	try:
		q = page_of or prepared or queries.validate(raw_plan)
		result = queries.execute(q)
	except queries.QueryClarify as c:
		return _query_clarify(ctx, c, attempts)
	except queries.QueryError as e:
		ctx.say(str(e))
		return
	except frappe.PermissionError:
		frappe.local.message_log = []
		ctx.say("You don't have permission to view that.")
		return
	except Exception as e:
		frappe.local.message_log = []
		from ai_erpnext.ai.errors import get_ai_logger

		get_ai_logger().error(f"AI-CHAT query_failed error_type={type(e).__name__}")
		ctx.say("I couldn't run that query. Please try rephrasing it.")
		return
	if (
		result.get("not_found")
		and q["operation"] in ("details", *hr.OPERATIONS)
		and not _operation_in_progress(ctx)
	):
		if attempts < MAX_QUERY_ATTEMPTS:
			# Ask for the name again (bounded); the reply is applied to this same lookup.
			question = ctx.ask(
				"query_target",
				result["text"] + " Check the spelling and type the name or ID again, or ask something else.",
				input="text",
			)
			question["plan"], question["attempts"] = _retarget(q), attempts + 1
			return
		ctx.say(
			result["text"]
			+ " I've stopped searching for it. Try part of the name (for example “find customer Amit”), "
			"or open the list in ERPNext."
		)
		return
	plan = {
		k: v for k, v in q.items() if k != "pick"
	}  # a picked record name came from ERPNext, not from the user
	ctx.st["last_query"] = {
		"plan": plan,
		"names": result.get("names", []),
		"titles": result.get("titles", []),
		"doctype": result.get("result_doctype") or q["doctype"],
	}
	ctx.say(
		result["text"],
		card=result.get("card"),
		source="erpnext",
		ref={
			"doctype": result.get("result_doctype") or q["doctype"],
			"names": result.get("names", [])[: history.MAX_REFS],
			"requires": result.get("requires")
			or (
				[q["doctype"]]
				if q["doctype"] and q["doctype"] != (result.get("result_doctype") or q["doctype"])
				else []
			),
		},
	)


def _operation_in_progress(ctx):
	"""An invoice or guided form is waiting for an answer; lookups must not take its question over."""
	pending = ctx.st.get("pending")
	return bool(pending and not pending["kind"].startswith("query_")) and (
		forms.active(ctx) or bool(ctx.st.get("entry")) or bool(ctx.st.get("master"))
	)


def _retarget(q):
	return dict(q, name=None, search=None, party=None, pick=None)


def _query_clarify(ctx, c, attempts):
	"""Turn a lookup clarification into a question bound to the lookup, so the reply completes it."""
	if c.kind not in ("target", "pick", "period", "company") or not c.plan or _operation_in_progress(ctx):
		# Plain question (or an invoice/form question is open): no state; record names stay in the buttons/text.
		labels = [o["label"] if isinstance(o, dict) else o for o in c.options]
		suggestions = [o for o in c.options if isinstance(o, str)] or None
		text = c.question + (
			("\nMatches: " + "; ".join(labels) + ". Ask again with the full name or ID.")
			if c.kind == "pick"
			else ""
		)
		ctx.say(text, suggestions=suggestions, source="clarify" if c.kind != "pick" else None)
		return
	options = [o for o in c.options if isinstance(o, dict)]
	question = ctx.ask(
		f"query_{c.kind}", c.question, options=options, input="text" if c.kind != "period" else None
	)
	question["plan"], question["attempts"] = c.plan, attempts


ORDINAL_RE = re.compile(
	r"^\s*(?:no,?\s*)?(?:use\s+|pick\s+|choose\s+|take\s+)?(?:the\s+)?(?:number\s+|#\s*)?"
	r"(?P<n>\d{1,2}|first|second|third|fourth|fifth|sixth|last)(?:st|nd|rd|th)?(?:\s+(?:one|option|customer|supplier|item|record|contact|warehouse))?\s*[.!]*\s*$",
	re.I,
)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6}


# Questions whose typed answer may itself be a number or code: “1” is a value there, not option 1.
LITERAL_ANSWERS = {
	("form_item", "qty"),
	("form_item", "rate"),
	("master_field", "item_code"),
	("master_field", "customer_name"),
	("form_field", "item_code"),
	("form_field", "bill_no"),
	("form_field", "customer_name"),
	("form_field", "supplier_name"),
}


def ordinal_option(pending, text):
	"""The option a reply such as “2”, “the second one” or “no, use the second customer” picks."""
	if (pending.get("kind"), pending.get("key")) in LITERAL_ANSWERS:
		return None
	m = ORDINAL_RE.match(text or "")
	options = [o for o in pending.get("options") or [] if not str(o.get("value", "")).startswith("__")]
	if not m or not options:
		return None
	n = m.group("n").lower()
	index = len(options) if n == "last" else (_ORDINALS.get(n) or int(n))
	return options[index - 1]["value"] if 1 <= index <= len(options) else None


answer_term = forms.answer_term


def _answer_query(ctx, pending, value, from_text):
	from ai_erpnext.chat import queries

	q, attempts = dict(pending.get("plan") or {}), int(pending.get("attempts") or 0)
	if not q:
		return ctx.say("That lookup has expired. Please ask again.")
	if from_text:
		value = _option_value(pending, value) or ordinal_option(pending, value) or value
	if value == "__cancel__":
		ctx.say("OK, I've stopped that lookup.")
		return
	kind = pending["kind"]
	if kind == "query_company":
		# the reply names one of the user's companies: the same lookup continues with it (filters and DocType kept)
		try:
			q["company"] = queries._match_company(q["doctype"], answer_term(value) or value, plan=q)
		except queries.QueryClarify as c:
			if attempts < MAX_QUERY_ATTEMPTS:
				return _query_clarify(ctx, c, attempts + 1)
			return ctx.say(c.question.split(" Which")[0] + " Please ask again with the company's full name.")
		except queries.QueryError as e:
			return ctx.say(str(e))
		return _run_query(ctx, None, prepared=q, attempts=attempts)
	if kind == "query_period":
		if value not in queries.DATE_PRESETS:
			return _general(ctx, value)  # a period in other words: let the router read it
		q["date"] = queries.preset_dates(q["doctype"], value)
		return _run_query(ctx, None, prepared=q, attempts=attempts)
	if value in [o["value"] for o in pending.get("options") or []] and "::" in value:
		doctype, name = value.split("::", 1)  # one of the records offered (re-checked by the lookup)
		return _run_query(ctx, None, prepared=dict(q, pick=[doctype, name]), attempts=attempts)
	term = answer_term(value)
	if not term:
		return ctx.say("Please type the name or ID.")
	m = re.match(r"^(?P<dt>customer|supplier|contact|item|warehouse|employee)\s+(?P<rest>.+)$", term, re.I)
	if m and (q.get("auto") or q.get("doctype") == m.group("dt").title()):
		q.update(doctype=m.group("dt").title(), auto=False)
		term = m.group("rest")
	return _run_query(ctx, None, prepared=dict(_retarget(q), search=clean_text(term, 140)), attempts=attempts)


def _more(ctx):
	last = ctx.st.get("last_query")
	if not last or last["plan"]["operation"] in (
		"count",
		"details",
		"sales_summary",
		"aggregate",
		"summary",
		"outstanding_by_customer",
		"outstanding_by_supplier",
		*hr.OPERATIONS,
	):
		ctx.say("There's no list to continue. Ask me for some records first.")
		return
	q = dict(last["plan"], offset=last["plan"]["offset"] + last["plan"]["limit"], requested_limit=None)
	if q["offset"] > 500:
		ctx.say("That's as far as I can page in the chat. Please open the list in ERPNext for more.")
		return
	_run_query(ctx, None, page_of=q)


def _open_result(ctx, index):
	last = ctx.st.get("last_query") or {}
	names = last.get("names") or []
	try:
		index = int(index)
	except (TypeError, ValueError):
		index = 0
	offset = (last.get("plan") or {}).get("offset", 0)
	pos = index - 1 - offset if index > offset else index - 1
	if not names or pos < 0 or pos >= len(names):
		ctx.say("I can't tell which record you mean. Please give its number from the list shown, or its ID.")
		return
	doctype, name = last["doctype"], names[pos]
	if not frappe.has_permission(doctype, "read", name):
		ctx.say("You don't have permission to open that record.")
		return
	ctx.say(
		f"Here is {doctype} {name}.",
		card={
			"type": "created",
			"title": f"{doctype} {name}",
			"doctype": doctype,
			"name": name,
			"rows": [{"label": "ID", "value": name}],
			"action_label": f"Open {doctype}",
		},
		source="erpnext",
	)


def _correction_key(ctx, logical):
	"""The open document's fieldname for a field word ("date" -> transaction_date on an order), or None."""
	if not logical.startswith("@"):
		return logical
	entry = _entry(ctx)
	d = _definition(entry) if entry else None
	if not d:
		return None
	if logical == "@date":
		return d.date_field
	if logical == "@second":
		return d.second_date_field
	party = logical.split(":", 1)[1]
	return party.lower() if d.party_doctype == party else None


# --------------------------------------------------------------------------------------------
# Operation lifecycle
# --------------------------------------------------------------------------------------------
def _attachment(file_url):
	file_url = clean_text(file_url, 500)
	rows = (
		frappe.get_all(
			"File",
			filters={"file_url": file_url, "owner": frappe.session.user},
			fields=["name", "file_name", "file_url", "file_size"],
			order_by="creation desc",
			limit=1,
		)
		if file_url
		else []
	)
	if not rows:
		return {
			"ok": False,
			"file_name": None,
			"file_url": None,
			"error": "I couldn't access that attachment. Please upload it again.",
		}
	f = rows[0]
	ext = (f.file_name or "").rsplit(".", 1)[-1].lower()
	if ext == "pdf":
		return _pdf_attachment(f)
	if ext not in IMAGE_EXTENSIONS:
		return {
			**f,
			"ok": False,
			"error": "Please upload a JPG, PNG or WEBP image, or a one-page PDF, of the document.",
		}
	if (f.file_size or 0) > MAX_IMAGE_BYTES:
		return {**f, "ok": False, "error": "That image is larger than 10 MB. Please upload a smaller image."}
	from ai_erpnext.pipeline.controller import image_problem

	try:
		content = frappe.get_doc("File", f.name).get_content(encodings=())
	except Exception:
		frappe.local.message_log = []
		content = None
	problem = image_problem(
		content, ext
	)  # the bytes must really be an image of that format, whatever the name says
	if problem:
		return {**f, "ok": False, "error": problem}
	return {**f, "ok": True}


def _pdf_attachment(f):
	"""A single-page PDF continues as a PNG of its page (a new private file of the same user): see pipeline/pdf.py."""
	from ai_erpnext.pipeline import pdf

	if (f.file_size or 0) > pdf.MAX_PDF_BYTES:
		return {**f, "ok": False, "error": "That PDF is larger than 10 MB. Please attach a smaller file."}
	try:
		content = frappe.get_doc("File", f.name).get_content(encodings=())
	except Exception:
		frappe.local.message_log = []
		content = None
	problem = pdf.problem(content)
	if problem:
		return {**f, "ok": False, "error": problem}
	stem = (f.file_name or "document").rsplit(".", 1)[0][:100]
	page = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{stem}-page-1.png",
			"content": pdf.render(content),
			"is_private": 1,
		}
	).insert()
	return {
		"name": page.name,
		"file_name": f.file_name,  # shown as the user's own attachment
		"file_url": page.file_url,  # what is read
		"file_size": page.file_size,
		"ok": True,
	}


def _start_extraction(ctx, file_url):
	frappe.flags.ai_chat = True  # suppress the form-oriented "queued" popup
	entry = frappe.get_doc({"doctype": "AI Document Entry", "file": file_url, "status": "Pending"}).insert()
	ctx.st.update(
		{
			"entry": entry.name,
			"phase": "extracting",
			"pending": None,
			"master": None,
			"acks": {},
			"company_confirmed": None,
			"doctype_confirmed": False,
			"queued_file": None,
			"created": None,
		}
	)
	ctx.doc.active_entry = entry.name
	workflow.begin(ctx, "image")
	ctx.say("Thanks. I'm reading the invoice now; this usually takes a few seconds.")


def _cancel(ctx, workflow_id=None):
	"""Cancel the workflow (or lookup question) in progress. Deterministic, no AI call, and idempotent: a repeated Cancel finds
	nothing in progress and says so. workflow_id: from the Cancel control; one of a workflow that already ended (finished,
	cancelled or replaced) never cancels the current one. Only collected answers are discarded: records created on the way
	stay and are named (chat/workflow.py)."""
	ctx.st["ask_sig"] = ctx.answering_sig = None
	wf = workflow.current(ctx)
	if workflow_id and wf and wf["id"] != workflow_id:
		ctx.say(
			"That Cancel belonged to an earlier task, which has already ended. The current one is unchanged."
		)
		return _reask_if_pending(ctx)
	ctx.st["queued_file"] = None  # an image waiting to replace the cancelled work is dropped with it
	pending = ctx.st.get("pending")
	if not wf and pending and pending["kind"].startswith("query_"):
		ctx.st["pending"] = None
		ctx.say("OK, I've stopped that lookup.")
		return
	if wf and wf["kind"] == "form":
		return forms.cancel(ctx)
	if wf:
		created = list(wf.get("created") or [])
		_stop_extraction(ctx)
		ctx.st.update({"phase": "cancelled", "pending": None, "master": None})
		ctx.say(
			workflow.cancelled_text(None, created) + " Attach another document whenever you're ready.",
			links=workflow.links(created) or None,
		)
		return
	ctx.st["pending"] = None
	users = [m["id"] for m in ctx.msgs if m["role"] == "user"]
	done = workflow.finished_text(ctx, users[-2] if len(users) > 1 else 0)
	if done:
		ctx.say(done, links=workflow.links([ctx.st["finished"]]))
		return
	ctx.say("There's nothing in progress to cancel.")


def _stop_extraction(ctx):
	"""A cancelled image workflow whose document is still being read: an extraction job that hasn't started must not run.
	The job skips entries that are no longer Pending, and one already running doesn't store its result (pipeline/controller.py)."""
	if ctx.st.get("phase") != "extracting":
		return
	entry = _entry(ctx)
	if entry and entry.status == "Pending" and not entry.created_document:
		entry.status = "Failed"
		entry.error_log = CANCELLED_EXTRACTION
		entry.save()


def _restart(ctx):
	"""“Start over”: the current record begins again from its first question; nothing is created."""
	ctx.st["pending"] = None
	ctx.st["ask_sig"] = ctx.answering_sig = None
	if forms.active(ctx):
		return forms.restart(ctx)
	entry = _entry(ctx) if ctx.st.get("entry") else None
	if entry and ctx.st.get("phase") == "collecting" and not entry.created_document:
		# Drop the answers given so far; the extracted invoice is kept (no new AI request).
		entry.review_overrides = None
		apply_mapping(entry)
		entry.save()
		ctx.st.update({"master": None, "acks": {}, "company_confirmed": None, "doctype_confirmed": False})
		ctx.say(
			"Starting this invoice over: your earlier answers are cleared; what was read from the image is kept."
		)
		return advance(ctx)
	ctx.st["last_query"] = None
	ctx.say("OK, starting fresh. What would you like to do?")


def _retry(ctx):
	phase = ctx.st.get("phase")
	entry = _entry(ctx)
	if phase == "failed" and entry:
		from ai_erpnext.api.extraction import retry_extraction

		frappe.flags.ai_chat = True
		retry_extraction(entry.name)
		ctx.st.update({"phase": "extracting", "pending": None})
		ctx.say("Retrying. I'm reading the invoice again.")
		return
	if phase == "collecting":
		ctx.st["pending"] = None
		ctx.say("Checking again.")
		return advance(ctx)
	ctx.say("There's nothing to retry right now.")


def _explain(ctx):
	entry = _entry(ctx)
	if not entry:
		ctx.say("There's no invoice in this conversation yet.")
		return
	if entry.status == "Failed":
		ctx.say(
			f"The last extraction failed: {entry.error_log or 'unknown error'}. Say “retry” to try again."
		)
		return
	validation = load_json(entry.validation_results, {}) or {}
	errors = [e["message"] for e in validation.get("errors", [])]
	warnings = [w["message"] for w in validation.get("warnings", [])]
	if not errors and not warnings:
		ctx.say("There are no open problems with this invoice.")
	else:
		ctx.say(
			"\n".join(
				["Open problems:"]
				+ [f"• {e}" for e in errors]
				+ (["Notes:"] + [f"• {w}" for w in warnings] if warnings else [])
			)
		)
	if ctx.st.get("pending"):
		_reask(ctx)


def _reask(ctx):
	pending = ctx.st.get("pending")
	if pending:
		ctx.st["pending"] = None
		ctx.reasking = True  # the same question again after an unrelated message is not a loop
		try:
			if forms.active(ctx):
				return forms.next_step(ctx)
			if ctx.st.get("master"):
				return _master_next(ctx)
			return advance(ctx)
		finally:
			ctx.reasking = False


def _entry(ctx):
	name = ctx.st.get("entry")
	if not name or not frappe.db.exists("AI Document Entry", name):
		return None
	entry = frappe.get_doc("AI Document Entry", name)
	if entry.owner != frappe.session.user:
		raise frappe.PermissionError(_("This invoice belongs to another user."))
	return entry


# --------------------------------------------------------------------------------------------
# Planning: decide the next question from server-side classification + mapping + validation
# --------------------------------------------------------------------------------------------
def _definition(entry):
	"""The registry definition the entry's current classification chose, or None."""
	from ai_erpnext.registry.document_registry import get_definition

	cls = _classification(entry)
	return get_definition(cls.get("target_key")) if cls.get("status") == "ok" else None


def _classification(entry):
	results = load_json(entry.mapping_results, {}) or {}
	return ((results.get("document_type") or {}).get("classification")) or {}


def _label(entry_or_definition):
	d = (
		entry_or_definition
		if hasattr(entry_or_definition, "target_doctype") and hasattr(entry_or_definition, "key")
		else _definition(entry_or_definition)
	)
	return d.display if d else "document"


def advance(ctx):
	entry = _entry(ctx)
	if not entry or ctx.st.get("phase") != "collecting":
		return
	if entry.created_document:
		ctx.st.update({"phase": "done", "pending": None})
		ctx.say(
			f"This document was already created as {entry.target_doctype} {entry.created_document}.",
			card=_created_card(entry.target_doctype, entry.created_document),
		)
		return
	if ctx.st.get("master"):
		return _master_next(ctx)

	apply_mapping(entry)
	entry.save()
	definition = _definition(entry)
	if not definition:
		return _q_classification(ctx, entry, _classification(entry))

	results = load_json(entry.mapping_results, {}) or {}
	validation = load_json(entry.validation_results, {}) or {}
	mapped = load_json(entry.mapped_data, {}) or {}
	question = _plan(ctx, entry, results, validation, mapped, definition)
	if not question:
		_ask_confirm(ctx, entry, mapped, validation, definition)


def _q_classification(ctx, entry, cls):
	"""The document's type or direction isn't proven: ask the one question that can prove it, or stop."""
	from ai_erpnext.mapping.mapper import company_context

	message = cls.get("message") or "I can't tell what this document is."
	usable, _default = company_context()
	cancel = {"label": "Cancel", "value": "__cancel__", "style": "secondary"}
	if cls.get("ask") == "company" or (cls.get("ask") == "role" and not cls.get("company")):
		first = [c for c in cls.get("company_candidates") or [] if c in usable]
		options = [{"label": c, "value": c} for c in dict.fromkeys(first + list(usable))][:20]
		text = (
			message
			if cls.get("ask") == "company"
			else f"{message} Which of your companies is this document for?"
		)
		return ctx.ask("company", text, key="company", options=[*options, cancel])
	if cls.get("ask") == "role":
		company, kind = cls["company"], (cls.get("kind") or "document").replace("_", " ")
		options = [
			{"label": f"{company} is the seller (we supplied the goods or services)", "value": "seller"},
			{"label": f"{company} is the buyer (we received the goods or services)", "value": "buyer"},
		]
		if len(usable) > 1:
			options.append(
				{"label": "It's for another of my companies", "value": "__company__", "style": "secondary"}
			)
		return ctx.ask(
			"role",
			f"{message} Before I create anything I need to know which side of this {kind} {company} is on.",
			key="document_role",
			options=[*options, cancel],
		)
	supported = "invoices, credit and debit notes, purchase and sales orders, quotations, delivery documents, goods receipts, material requests and bills of materials"
	return ctx.ask(
		"blocked",
		f"{message} Nothing was created. I can read {supported}; I decide what each becomes from who issued it "
		"and who it is for.",
		options=[cancel],
	)


def _plan(ctx, entry, results, validation, mapped, d):
	errors = validation.get("errors", [])
	blocking = {}
	for e in errors:
		if e.get("category") == "mapping":
			blocking.setdefault(e.get("field"), []).append(e)

	party_key = d.party_doctype.lower() if d.party_doctype else None
	if party_key and party_key in blocking:
		return _q_master_choice(ctx, party_key, results.get(party_key) or {}, doctype=d.party_doctype)
	# Possible duplicate: ask as soon as the party is known, before any other effort.
	acks = ctx.st.setdefault("acks", {})
	dups = sorted(dup["name"] for dup in validation.get("duplicates") or [])
	if dups and not set(dups) <= set(acks.get("duplicate") or []):
		reasons = "\n".join(f"• {dup['name']}: {dup['reason']}" for dup in validation["duplicates"])
		return ctx.ask(
			"duplicate",
			f"This document may already exist in ERPNext:\n{reasons}",
			options=[
				{"label": "It's not a duplicate; continue", "value": "__ack__"},
				{"label": "Cancel", "value": "__cancel__"},
			],
			links=[{"label": f"Open {name}", "doctype": d.target_doctype, "name": name} for name in dups],
		)
	if "bom_item" in blocking:
		return _q_master_choice(ctx, "bom_item", results.get("bom_item") or {}, doctype="Item")
	for i in range(1, len(mapped.get("items") or []) + 1):
		if f"item_{i}" in blocking:
			return _q_master_choice(ctx, f"item_{i}", results.get(f"item_{i}") or {}, row=i, doctype="Item")
		if f"item_{i}_uom" in blocking:
			return _q_uom(ctx, i, results, mapped, blocking[f"item_{i}_uom"])
	for key in (d.date_field, d.second_date_field):
		if key and key in blocking:
			return _q_date(
				ctx,
				key,
				results.get(key) or {},
				blocking[key],
				posting=mapped.get(d.date_field),
				definition=d,
			)
	if "currency" in blocking:
		return _q_currency(ctx, mapped, blocking["currency"], d)
	if "warehouse" in blocking:
		return _q_simple(
			ctx,
			"warehouse",
			"warehouse",
			f"{blocking['warehouse'][0]['message']}. Which warehouse should I use?",
			_warehouse_options(mapped),
		)
	if "apply_discount_on" in blocking:
		return ctx.ask(
			"discount_base",
			f"{blocking['apply_discount_on'][0]['message']} Is the discount taken off before tax (on the net total) "
			"or after tax (on the grand total)?",
			key="apply_discount_on",
			options=[
				{"label": "Before tax (net total)", "value": "Net Total"},
				{"label": "After tax (grand total)", "value": "Grand Total"},
				{"label": "Cancel", "value": "__cancel__", "style": "secondary"},
			],
		)
	if "return_against" in blocking:
		res = results.get("return_against") or {}
		options = [{"label": c["display_name"], "value": c["name"]} for c in res.get("candidates") or []][:5]
		options += [
			{
				"label": "Create it without linking the original invoice",
				"value": "__none__",
				"style": "secondary",
			},
			{"label": "Cancel", "value": "__cancel__", "style": "secondary"},
		]
		return ctx.ask(
			"return_against",
			f"{blocking['return_against'][0]['message']}. Which original {d.target_doctype} does this note return? "
			"You can type its ID.",
			key="return_against",
			options=options,
			input="text",
		)
	if "material_request_type" in blocking:
		res = results.get("material_request_type") or {}
		return ctx.ask(
			"material_request_type",
			f"{res.get('message') or 'The purpose is missing'}. What is this material request for?",
			key="material_request_type",
			options=[{"label": c["display_name"], "value": c["name"]} for c in res.get("candidates") or []],
		)
	other = [e for field, errs in blocking.items() for e in errs]
	if other:
		return ctx.ask(
			"blocked",
			"I can't continue because:\n"
			+ "\n".join(f"• {e['message']}" for e in other)
			+ "\nYou can correct this in ERPNext and say “retry”, or cancel.",
			options=[{"label": "Retry", "value": "__retry__"}, {"label": "Cancel", "value": "__cancel__"}],
		)

	warnings = {w["code"] for w in validation.get("warnings", [])}
	if "taxes_not_mapped" in warnings and not acks.get("taxes"):
		return _q_taxes(ctx, entry, mapped, d)
	data_codes = sorted({e["code"] for e in errors if e.get("category") == "data"})
	if data_codes and not set(data_codes) <= set(acks.get("data_errors") or []):
		msgs = [e["message"] for e in errors if e.get("category") == "data"]
		return ctx.ask(
			"data_errors",
			"Some values on the document don't add up:\n"
			+ "\n".join(f"• {m}" for m in msgs)
			+ "\nI won't change the numbers. You can correct them now (for example “row 1 item …”), or create the draft and fix it in ERPNext before submitting.",
			options=[
				{"label": "Create the draft anyway; I'll fix it in ERPNext", "value": "__ack__"},
				{"label": "Cancel", "value": "__cancel__"},
			],
		)
	return None


def _q_master_choice(ctx, key, res, row=None, doctype="Customer"):
	label = doctype.lower() if key != "bom_item" else "finished item"
	shown = res.get("input") or ""
	where = f"Row {row}: " if row else ""
	if res.get("status") == "AMBIGUOUS" and res.get("match_type") != "fuzzy_suggestion":
		text = f"{where}Several {label}s could match “{shown}”. Which one is it?"
	elif res.get("candidates"):
		text = f"{where}I couldn't find the {label} “{shown}” in ERPNext. Is it one of these?"
	elif res.get("status") == "INVALID":
		text = f"{where}{res.get('message')}. Which {label} should I use?"
	elif not shown:
		text = f"{where}{res.get('message') or f'The {label} is not on the document'}. Which {label} is it?"
	else:
		text = f"{where}I couldn't find the {label} “{shown}” in ERPNext."
	text += f" Pick one, type a name to search, or create a new {doctype.lower()}."
	options = [
		{
			"label": c["display_name"] + (f" ({c['name']})" if c["name"] != c["display_name"] else ""),
			"value": c["name"],
		}
		for c in (res.get("candidates") or [])[:5]
	]
	options.append(
		{
			"label": f"Create new {doctype.lower()}" + (f" “{shown}”" if shown else ""),
			"value": "__create__",
			"style": "secondary",
		}
	)
	return ctx.ask(
		"item" if doctype == "Item" else doctype.lower(), text, key=key, options=options, input="text"
	)


def _q_uom(ctx, row, results, mapped, errors):
	item = (mapped.get("items") or [])[row - 1]
	res = results.get(f"item_{row}_uom") or {}
	stock_uom = item.get("stock_uom")
	if any(e["code"] == "uom_no_conversion" for e in errors):
		text = (
			f"Row {row}: the document's unit “{item.get('uom')}” has no conversion to the item's stock unit "
			f"“{stock_uom}” in ERPNext. Which unit should I use?"
		)
	else:
		text = f"Row {row}: I couldn't match the unit “{res.get('input') or ''}”. Which unit should I use?"
	options = []
	if item.get("item_code"):
		uoms = [
			stock_uom,
			*frappe.get_all(
				"UOM Conversion Detail",
				filters={"parent": item["item_code"], "parenttype": "Item"},
				pluck="uom",
			),
		]
		options = [{"label": u, "value": u} for u in dict.fromkeys(u for u in uoms if u)]
	return ctx.ask("uom", text, key=f"item_{row}_uom", options=options, input="text")


DATE_LABELS = {
	"posting_date": "posting date",
	"transaction_date": "document date",
	"due_date": "due date",
	"delivery_date": "delivery date",
	"schedule_date": "required-by date",
	"valid_till": "valid-till date",
}


def _q_date(ctx, key, res, errors, posting=None, definition=None):
	label = DATE_LABELS.get(key, "date")
	if any(e["code"] == "no_fiscal_year" for e in errors):
		text = (
			f"{errors[0]['message']} Which {label} should I use instead? If the document date is right, "
			"ask your administrator to add that Fiscal Year and then say “retry”."
		)
	elif any(e["code"].endswith("_before_date") or e["code"] == "due_before_posting" for e in errors):
		text = f"{errors[0]['message']}. Which {label} should I use?"
	elif res.get("status") == "NOT_PROVIDED" or not res.get("input"):
		text = f"ERPNext needs a {label} for this {definition.display if definition else 'document'}, and none is printed. Which {label} should I use?"
	else:
		text = f"{res.get('message') or errors[0]['message']}. Which {label} should I use?"
	first = definition.date_field if definition else "posting_date"
	options = (
		[{"label": f"Today ({nowdate()})", "value": nowdate()}]
		if key == first
		else ([{"label": f"Same as the document date ({posting})", "value": posting}] if posting else [])
	)
	return ctx.ask(key, text, key=key, options=options, input="date")


def _q_simple(ctx, kind, key, text, options):
	return ctx.ask(kind, text, key=key, options=options, input="text")


def _q_currency(ctx, mapped, errors, d):
	mismatch = next((e for e in errors if e["code"] == "party_currency_mismatch"), None)
	if mismatch:
		allowed = [c for c in [_required_party_currency(mapped, d)] if c and c != mapped.get("currency")]
		account = "receivable" if d.party_doctype == "Customer" else "payable"
		text = (
			f"{mismatch['message']} Options: use {allowed[0] if allowed else 'the account currency'} only if the document's "
			f"amounts really are in that currency (I won't convert them), or ask an accountant to add a {account} account "
			f"in {mapped.get('currency')} for this {d.party_doctype.lower()} (or enable multi-currency invoices in Accounts Settings) and then say “retry”."
		)
		options = [{"label": f"Use {c} (amounts unchanged)", "value": c} for c in allowed]
		options += [
			{"label": "Retry", "value": "__retry_check__", "style": "secondary"},
			{"label": "Cancel", "value": "__cancel__", "style": "secondary"},
		]
		return ctx.ask("currency", text, key="currency", options=options)
	return _q_simple(
		ctx,
		"currency",
		"currency",
		f"I need the document's currency. {errors[0]['message']}",
		_currency_options(mapped),
	)


def _required_party_currency(mapped, d):
	"""The currency ERPNext will accept for this party (same sources as its own check)."""
	from erpnext.accounts.party import get_party_account, get_party_gle_currency
	from erpnext.accounts.utils import get_account_currency

	party, company = mapped.get(d.party_field), mapped.get("company")
	if not (party and company):
		return None
	gle = get_party_gle_currency(d.party_doctype, party, company)
	if gle:
		return gle
	account = get_party_account(d.party_doctype, party, company)
	return get_account_currency(account) if account else None


def _currency_options(mapped):
	company_currency = (
		frappe.get_cached_value("Company", mapped.get("company"), "default_currency")
		if mapped.get("company")
		else None
	)
	return [{"label": company_currency, "value": company_currency}] if company_currency else []


def _warehouse_options(mapped):
	filters = {"is_group": 0, "disabled": 0}
	if mapped.get("company"):
		filters["company"] = mapped["company"]
	return [
		{"label": w, "value": w} for w in frappe.get_list("Warehouse", filters=filters, pluck="name", limit=8)
	]


def _q_taxes(ctx, entry, mapped, d):
	extracted = load_json(entry.extracted_data, {}) or {}
	canonical = DocumentAdapter(None).adapt(extracted)
	parts = [
		f"{t.get('name') or 'Tax'}{(' ' + str(t.get('rate')) + '%') if t.get('rate') not in (None, '') else ''}: {t.get('amount')}"
		for t in (extracted.get("taxes") or [])
		if isinstance(t, dict)
	]
	tax_text = (
		f"tax of {flt(canonical.get('tax_amount'), 2)}" if canonical.get("tax_amount") is not None else "tax"
	)
	if parts:
		tax_text += " (" + ", ".join(parts) + ")"
	template = d.tax_template_doctype
	templates = (
		frappe.get_list(
			template, filters={"company": mapped.get("company"), "disabled": 0}, pluck="name", limit=10
		)
		if mapped.get("company")
		else []
	)
	if templates:
		text = f"The document shows {tax_text}. Which {template} should I apply? ERPNext will calculate the tax rows."
		options = [{"label": t, "value": t} for t in templates]
	else:
		text = (
			f"The document shows {tax_text}, but there is no {template} for {mapped.get('company')}, "
			"so I can't add tax rows. The draft would be created without tax and its total would be lower than the document; "
			"add the taxes in ERPNext before submitting."
		)
		options = []
	options += [
		{"label": "Create the draft without taxes", "value": "__none__", "style": "secondary"},
		{"label": "Cancel", "value": "__cancel__"},
	]
	return ctx.ask("taxes", text, key="taxes_and_charges", options=options)


def _role_text(cls):
	role = cls.get("our_role")
	if role == "internal":
		return f"{cls.get('company')} (internal document)"
	return f"{cls.get('company')} is the {role}" + (
		" (your choice)" if cls.get("role_source") == "selected" else ""
	)


def _ask_confirm(ctx, entry, mapped, validation, d):
	extracted = load_json(entry.extracted_data, {}) or {}
	canonical = DocumentAdapter(None).adapt(extracted)
	cls = _classification(entry)
	rows = [
		{"label": "Source document", "value": _describe(cls)},
		{"label": "Your company", "value": _role_text(cls)},
		{"label": "Will create", "value": f"Draft {d.display}"},
	]
	if d.party_field:
		rows.append({"label": d.party_doctype, "value": mapped.get(d.party_field)})
	if d.target_doctype == "BOM":
		rows += [
			{"label": "Finished item", "value": mapped.get("item")},
			{"label": "Quantity", "value": mapped.get("quantity")},
		]
	if d.target_doctype == "Material Request":
		rows.append({"label": "Purpose", "value": mapped.get("material_request_type")})
	for key in (d.date_field, d.second_date_field):
		if key:
			rows.append(
				{"label": DATE_LABELS[key].capitalize(), "value": mapped.get(key) or "ERPNext default"}
			)
	if d.currency and d.target_doctype != "BOM":
		rows.append({"label": "Currency", "value": mapped.get("currency")})
	if mapped.get("set_warehouse"):
		rows.append({"label": "Warehouse", "value": mapped["set_warehouse"]})
	if d.is_return:
		rows.append({"label": "Original invoice", "value": mapped.get("return_against") or "Not linked"})
	if mapped.get("discount_amount"):
		rows.append(
			{
				"label": "Discount",
				"value": f"{flt(mapped['discount_amount'], 2)} on the {(mapped.get('apply_discount_on') or '').lower()}",
			}
		)
	if d.tax_template_doctype:
		rows.append(
			{
				"label": "Taxes",
				"value": mapped.get("taxes_and_charges")
				or ("None (add in ERPNext)" if canonical.get("tax_amount") else "None"),
			}
		)
	if d.rate_required:
		net = sum(abs(flt(i.get("qty"))) * flt(i.get("rate")) for i in mapped.get("items") or [])
		rows.append(
			{
				"label": "Net total (qty × rate)",
				"value": f"{flt(net, 2)} {mapped.get('currency') or ''}".strip(),
			}
		)
		rows.append(
			{
				"label": "Total on the document",
				"value": f"{canonical.get('total_amount')}"
				if canonical.get("total_amount") is not None
				else "Not printed",
			}
		)
	card = {
		"type": "draft_preview",
		"title": f"Draft {d.display}",
		"rows": rows,
		"items": [
			{
				"name": i.get("item_name") or i.get("item_code"),
				"code": i.get("item_code"),
				"qty": i.get("qty"),
				"uom": i.get("uom"),
				"rate": i.get("rate"),
				"amount": i.get("amount"),
			}
			for i in mapped.get("items") or []
		],
	}
	ctx.ask(
		"confirm",
		f"Everything required is resolved. Shall I create this {d.display} as a Draft? It will not be submitted.",
		options=[
			{"label": f"Create draft {d.target_doctype}", "value": "__create__"},
			{"label": "Cancel", "value": "__cancel__", "style": "secondary"},
		],
		card=card,
	)


def _describe(cls):
	from ai_erpnext.detection.direction import KIND_LABELS

	text = KIND_LABELS.get(cls.get("kind")) or cls.get("source_type") or "Document"
	if cls.get("issuer"):
		text += f" issued by {cls['issuer']}"
	if cls.get("recipient"):
		text += f" to {cls['recipient']}"
	if not (cls.get("issuer") or cls.get("recipient")) and (cls.get("seller") or cls.get("buyer")):
		text += (
			f" from {cls.get('seller') or 'an unnamed seller'} to {cls.get('buyer') or 'an unnamed buyer'}"
		)
	if cls.get("source_type") and cls.get("source_type") != KIND_LABELS.get(cls.get("kind")):
		text += f" (titled “{cls['source_type']}”)"
	return text


# --------------------------------------------------------------------------------------------
# Answers
# --------------------------------------------------------------------------------------------
def _option_value(pending, text):
	"""The option a typed reply names: its label or value, or its position (“2”, “the second one”)."""
	wanted = (text or "").strip().lower()
	for o in pending.get("options") or []:
		if wanted in (str(o["value"]).lower(), str(o["label"]).lower()):
			return o["value"]
	term = (answer_term(text) or "").lower()
	for o in pending.get("options") or []:
		if term and term in (str(o["value"]).lower(), str(o["label"]).lower()):
			return o["value"]
	return ordinal_option(pending, text)


ROLE_RE = re.compile(
	r"\b(?P<role>seller|selling|sold|supplier|vendor|buyer|buying|bought|customer|purchaser)\b", re.I
)
_ROLE_WORDS = {
	"seller": "seller",
	"selling": "seller",
	"sold": "seller",
	"supplier": "seller",
	"vendor": "seller",
	"buyer": "buyer",
	"buying": "buyer",
	"bought": "buyer",
	"customer": "buyer",
	"purchaser": "buyer",
}


def _answer(ctx, pending, value, from_text, correction=False):
	kind = pending["kind"]
	if kind.startswith("query_"):
		ctx.st["pending"] = None
		return _answer_query(ctx, pending, value, from_text)
	if kind.startswith("form_") and forms.active(ctx):
		if from_text:
			value = _option_value(pending, value) or value
		ctx.st["pending"] = None
		return forms.answer(ctx, pending, value, from_text)
	if from_text and not correction:
		value = _option_value(pending, value) or value
		if NO_RE.match(value) and kind in (
			"confirm",
			"data_errors",
			"duplicate",
			"replace_operation",
			"master_confirm",
		):
			value = "__cancel__" if kind != "replace_operation" else "__keep__"
		elif YES_RE.match(value) and kind in (
			"confirm",
			"data_errors",
			"duplicate",
			"master_confirm",
			"use_existing",
		):
			value = {"confirm": "__create__", "master_confirm": "__create__", "use_existing": "__use__"}.get(
				kind, "__ack__"
			)
		elif kind == "role" and value not in ("seller", "buyer", "__company__", "__cancel__"):
			m = ROLE_RE.search(value or "")
			value = _ROLE_WORDS[m.group("role").lower()] if m else value
	if value == "__cancel__" and kind not in ("master_confirm", "master_field", "use_existing"):
		return _cancel(ctx)

	ctx.st["pending"] = None
	if kind == "replace_operation":
		if value == "__new__" and ctx.st.get("queued_file"):
			ctx.st["forms"] = []
			return _start_extraction(ctx, ctx.st["queued_file"])
		ctx.st["queued_file"] = None
		ctx.say("OK, let's continue with the current document.")
		return _resume(ctx)
	if kind == "extraction_failed" or kind == "blocked":
		return _retry(ctx)
	if kind == "role":
		if value == "__company__":
			from ai_erpnext.mapping.mapper import company_context

			return ctx.ask(
				"company",
				"Which of your companies is this document for?",
				key="company",
				options=[{"label": c, "value": c} for c in company_context()[0][:20]],
			)
		if value not in ("seller", "buyer"):
			ctx.say("Please choose whether your company is the seller or the buyer, or cancel.")
			return advance(ctx)
		ctx.say(f"Recording your company as the {value}.")
		return _set_overrides(ctx, {"document_role": value}, "role")
	if kind == "company":
		return _set_master(ctx, "company", value, CompanyMatcher(), "company", from_text=from_text)
	if kind in ("customer", "supplier", "item"):
		return _answer_master_choice(ctx, pending["key"], value, from_text)
	if kind == "uom":
		return _set_master(ctx, pending["key"], value, UOMMatcher(), "unit", from_text=from_text)
	if kind == "currency":
		if value == "__retry_check__":
			return advance(ctx)
		return _set_master(ctx, "currency", value, CurrencyMatcher(), "currency", from_text=from_text)
	if kind == "warehouse":
		mapped = load_json(_entry(ctx).mapped_data, {}) or {}
		return _set_master(
			ctx, "warehouse", value, WarehouseMatcher(mapped.get("company")), "warehouse", from_text=from_text
		)
	if kind in DATE_LABELS:
		return _set_date(ctx, kind, value)
	if kind == "discount_base":
		if value not in ("Net Total", "Grand Total"):
			ctx.say("Please choose before tax (net total) or after tax (grand total).")
			return advance(ctx)
		return _set_overrides(ctx, {"apply_discount_on": value}, "discount")
	if kind == "return_against":
		term = answer_term(value) if from_text else value
		return _set_overrides(ctx, {"return_against": term or ""}, "original invoice")
	if kind == "material_request_type":
		return _set_overrides(ctx, {"material_request_type": value}, "purpose")
	if kind == "taxes":
		if value == "__none__":
			ctx.st.setdefault("acks", {})["taxes"] = True
			return advance(ctx)
		return _set_overrides(ctx, {"taxes_and_charges": value}, "tax template")
	if kind == "data_errors":
		if value == "__ack__":
			validation = load_json(_entry(ctx).validation_results, {}) or {}
			ctx.st.setdefault("acks", {})["data_errors"] = sorted(
				{e["code"] for e in validation.get("errors", []) if e.get("category") == "data"}
			)
		return advance(ctx)
	if kind == "duplicate":
		if value == "__ack__":
			validation = load_json(_entry(ctx).validation_results, {}) or {}
			ctx.st.setdefault("acks", {})["duplicate"] = sorted(
				d["name"] for d in validation.get("duplicates") or []
			)
		return advance(ctx)
	if kind == "confirm":
		if value == "__create__":
			return _create_document(ctx)
		ctx.say("Please choose “Create draft” or “Cancel”.")
		ctx.st["pending"] = pending
		return
	if kind in ("master_field", "master_confirm", "use_existing"):
		return _answer_master(ctx, pending, value)
	return advance(ctx)


def _resume(ctx):
	if forms.active(ctx):
		return forms.next_step(ctx)
	if ctx.st.get("phase") == "collecting":
		return advance(ctx)


def _set_overrides(ctx, corrections, label):
	entry = _entry(ctx)
	try:
		set_overrides(entry, corrections)
	except (frappe.ValidationError, frappe.PermissionError):
		frappe.local.message_log = []
		ctx.say(f"That {label} isn't valid. Please try again.")
		return advance(ctx)
	apply_mapping(entry)
	entry.save()
	key = next(iter(corrections))
	res = (load_json(entry.mapping_results, {}) or {}).get(key) or {}
	if corrections[key] and res.get("status") == FieldStatus.INVALID:
		ctx.say(res.get("message") or f"That {label} can't be used.")
		set_overrides(entry, {key: ""})
		apply_mapping(entry)
		entry.save()
	return advance(ctx)


def _set_master(ctx, key, value, matcher, label, confirm_key=None, from_text=True):
	# Button values are exact record names; typed text must go through the matcher so that
	# duplicate names are reported as ambiguous instead of silently picking one record.
	row = None if from_text else matcher.resolve_name(value)
	if not row:
		res = matcher.match(value)
		if res.matched:
			row = {"name": res.name}
		elif res.candidates:
			ctx.say(
				f"“{value}” could be several {label}s: "
				+ ", ".join(c["name"] for c in res.candidates[:5])
				+ ". Please pick one."
			)
			return advance(ctx)
		else:
			row = matcher.resolve_name(value)  # an exact record ID typed by the user
			if not row:
				ctx.say(f"I couldn't find a {label} called “{value}”.")
				return advance(ctx)
	if confirm_key:
		ctx.st[confirm_key] = row["name"]
	ctx.say(f"Using {label} {row['name']}.")
	return _set_overrides(ctx, {key: row["name"]}, label)


def _set_date(ctx, key, value):
	if str(value).strip().lower() in ("today", "now"):
		value = nowdate()
	parsed = parse_date(value)
	if not parsed.ok:
		ctx.say(
			"Please give the date as YYYY-MM-DD."
			if not parsed.ambiguous
			else "That date could be read two ways. Please use YYYY-MM-DD."
		)
		return advance(ctx)
	ctx.say(f"Using {DATE_LABELS.get(key, 'date')} {parsed.value}.")
	return _set_overrides(ctx, {key: parsed.value}, "date")


def _matcher_for(key, entry):
	if key.startswith("item_") or key == "bom_item":
		return ItemMatcher(), "item"
	from ai_erpnext.mapping.transaction import PARTY_MATCHERS

	doctype = key.capitalize()
	return PARTY_MATCHERS[doctype](), doctype.lower()


def _answer_master_choice(ctx, key, value, from_text):
	entry = _entry(ctx)
	is_item = key.startswith("item_") or key == "bom_item"
	results = load_json(entry.mapping_results, {}) or {}
	res = results.get(key) or {}
	if from_text and forms.NONE_RE.match(value or ""):
		# "no" to the records offered is not a name to search for (nor to create)
		ctx.say(
			f"OK, none of these. Type the {'item' if is_item else key}'s name (or part of it) to search, choose "
			"“Create new …” to add a new one, or press Cancel to stop."
		)
		return advance(ctx)
	if value == "__create__" or (
		from_text
		and CREATE_NEW_RE.search(value)
		and len(value.split()) <= 6
		and ("new" in value.lower() or "create" in value.lower())
	):
		return _start_master(ctx, key, entry, res)
	if value.startswith("__create__:"):
		return _start_master(ctx, key, entry, res, name=value.split(":", 1)[1])
	matcher, label = _matcher_for(key, entry)
	row = None if from_text else matcher.resolve_name(value)  # buttons carry exact record names
	if row:
		ctx.say(f"Using {label} {row['name']}.")
		return _set_overrides(ctx, {key: row["name"]}, label)
	found = matcher.match({"item_name": value, "item_code": value} if is_item else value)
	if found.matched:
		ctx.say(f"Using {label} {found.name}.")
		return _set_overrides(ctx, {key: found.name}, label)
	if not found.candidates:
		row = matcher.resolve_name(value)  # an exact record ID typed by the user
		if row:
			ctx.say(f"Using {label} {row['name']}.")
			return _set_overrides(ctx, {key: row["name"]}, label)
	options = [
		{
			"label": c["display_name"] + (f" ({c['name']})" if c["name"] != c["display_name"] else ""),
			"value": c["name"],
		}
		for c in found.candidates[:5]
	]
	options.append(
		{"label": f"Create new {label} “{value}”", "value": f"__create__:{value}", "style": "secondary"}
	)
	text = (
		f"Several {label}s match “{value}”. Which one?"
		if found.candidates
		else f"I couldn't find a {label} matching “{value}”."
	)
	ctx.ask(
		"item" if is_item else label,
		text + " Pick one, search again, or create it.",
		key=key,
		options=options,
		input="text",
	)


# --------------------------------------------------------------------------------------------
# Master creation sub-flow
# --------------------------------------------------------------------------------------------
def _start_master(ctx, key, entry, res, name=None):
	d = _definition(entry)
	if key.startswith("item_") or key == "bom_item":
		extracted_rows = DocumentAdapter(None).adapt(load_json(entry.extracted_data, {}) or {})
		if key == "bom_item":
			src, uom = extracted_rows.get("bom_item") or {}, (extracted_rows.get("bom_item") or {}).get("uom")
		else:
			row = int(key.split("_")[1])
			mapped = load_json(entry.mapped_data, {}) or {}
			src = extracted_rows["items"][row - 1] if len(extracted_rows["items"]) >= row else {}
			uom = ((mapped.get("items") or [{}] * row)[row - 1]).get("uom")
		flags = dict(d.item_filters) if d and d.item_filters else ({} if d else {"is_sales_item": 1})
		draft = masters.item_draft(
			key,
			name or src.get("item_name"),
			src.get("description"),
			uom,
			flags=flags or {"is_stock_item": 1},
		)
	elif key == "supplier":
		draft = masters.supplier_draft(key, name or res.get("input"))
	else:
		draft = masters.customer_draft(key, name or res.get("input"))
	if draft["unsupported_required"]:
		ctx.say(
			f"ERPNext requires fields I can't collect here ({', '.join(draft['unsupported_required'])}). "
			f"Please create the {draft['doctype']} in ERPNext, then pick it here."
		)
		return advance(ctx)
	if not frappe.has_permission(draft["doctype"], "create"):
		ctx.say(
			f"You don't have permission to create {draft['doctype']} records. Please pick an existing one."
		)
		return advance(ctx)
	existing = masters.find_existing(draft)
	ctx.st["master"] = draft
	if existing:
		draft["existing"] = existing
		ctx.ask(
			"use_existing",
			f"A {draft['doctype'].lower()} “{existing}” already exists. Should I use it instead of creating a new one?",
			options=[
				{"label": f"Use {existing}", "value": "__use__"},
				{"label": "Create a new one anyway", "value": "__new__", "style": "secondary"},
				{"label": "Back", "value": "__cancel__", "style": "secondary"},
			],
		)
		return
	ctx.say(f"OK, let's create a new {draft['doctype']}. I'll only ask for what ERPNext requires.")
	return _master_next(ctx)


MASTER_PROMPTS = {
	"customer_name": "What is the customer's name?",
	"supplier_name": "What is the supplier's name?",
	"item_code": "What item code should the new item have?",
	"item_group": "Which item group does it belong to?",
	"is_stock_item": "Is it a stock item (tracked in inventory) or a non-stock item / service?",
}
MASTER_GROUP_FIELD = {"Customer": "customer group", "Supplier": "supplier group", "Item": "item group"}


def _master_next(ctx):
	draft = ctx.st["master"]
	field = masters.next_field(draft)
	if field:
		spec = masters.FIELDS[draft["doctype"]][field]
		options = masters.field_options(draft["doctype"], field)
		prompt = MASTER_PROMPTS.get(field, f"Which {spec['label']} should I use?")
		if (
			field == "item_code"
			and draft["values"].get("item_name")
			and not readable_exists("Item", draft["values"]["item_name"])
		):
			options = [
				{
					"label": f"Use the item name: {draft['values']['item_name']}",
					"value": draft["values"]["item_name"],
				},
				*options,
			]
		ctx.ask(
			"master_field",
			prompt,
			key=field,
			options=options,
			input=None if spec["type"] in ("Select", "Check") else "text",
		)
		return
	ctx.ask(
		"master_confirm",
		f"Create this {draft['doctype']} in ERPNext? You can still change a value, for example "
		f"“set {MASTER_GROUP_FIELD[draft['doctype']]} to …”.",
		options=[
			{"label": f"Create {draft['doctype']}", "value": "__create__"},
			{"label": "Back", "value": "__cancel__", "style": "secondary"},
		],
		card={"type": "master_preview", "title": f"New {draft['doctype']}", "rows": masters.summary(draft)},
	)


def _master_correction(ctx, fieldname, value):
	draft = ctx.st["master"]
	if fieldname not in masters.FIELDS[draft["doctype"]]:
		ctx.say(f"That field isn't part of a {draft['doctype']}.")
		ctx.st["pending"] = None
		return _master_next(ctx)
	error = masters.set_field(draft, fieldname, value)
	ctx.say(error or "Updated.")
	ctx.st["pending"] = None
	return _master_next(ctx)


def _answer_master(ctx, pending, value):
	draft = ctx.st["master"]
	kind = pending["kind"]
	if value == "__cancel__":
		ctx.st["master"] = None
		ctx.say(f"OK, I won't create the {draft['doctype'].lower()}.")
		return advance(ctx)
	if kind == "use_existing":
		if value == "__use__" or USE_EXISTING_RE.search(value or ""):
			ctx.st["master"] = None
			ctx.say(f"Using existing {draft['doctype'].lower()} {draft['existing']}.")
			return _set_overrides(ctx, {draft["for_key"]: draft["existing"]}, draft["doctype"].lower())
		ctx.say(f"OK, let's create a new {draft['doctype']}.")
		return _master_next(ctx)
	if kind == "master_field":
		if USE_EXISTING_RE.search(value or "") and draft.get("existing"):
			return _answer_master(ctx, {"kind": "use_existing"}, "__use__")
		error = masters.set_field(draft, pending["key"], value)
		if error:
			ctx.say(error)
		return _master_next(ctx)
	if kind == "master_confirm":
		if value != "__create__":
			ctx.say(f"Please choose “Create {draft['doctype']}” or “Back”.")
			return _master_next(ctx)
		frappe.db.savepoint("ai_chat_master")
		try:
			name = masters.create(draft)
		except Exception as e:
			frappe.db.rollback(save_point="ai_chat_master")
			frappe.local.message_log = []
			ctx.say(f"ERPNext didn't accept the {draft['doctype'].lower()}: {_safe_error(e)}")
			return _master_next(ctx)
		ctx.st["master"] = None
		workflow.note_created(ctx, draft["doctype"], name)
		ctx.say(
			f"{draft['doctype']} {name} created.",
			card={
				"type": "created",
				"title": f"{draft['doctype']} created",
				"doctype": draft["doctype"],
				"name": name,
				"rows": [{"label": draft["doctype"], "value": name}],
				"action_label": f"Open {draft['doctype']}",
			},
		)
		return _set_overrides(ctx, {draft["for_key"]: name}, draft["doctype"].lower())


# --------------------------------------------------------------------------------------------
# Draft creation and announcements
# --------------------------------------------------------------------------------------------
def _create_document(ctx):
	from ai_erpnext.api.creation import create_document

	entry = _entry(ctx)
	acks = ctx.st.get("acks") or {}
	frappe.db.savepoint("ai_chat_create")
	try:
		result = create_document(
			entry.name,
			acknowledge_errors=1 if acks.get("data_errors") else 0,
			acknowledge_duplicate=1 if acks.get("duplicate") else 0,
		)
	except Exception as e:
		frappe.db.rollback(save_point="ai_chat_create")
		frappe.local.message_log = []
		return _creation_failed(ctx, _safe_error(e))
	if not result.get("created"):
		ctx.say(result.get("message") or "The draft couldn't be created yet.")
		return advance(ctx)
	doc = frappe.get_doc(result["doctype"], result["name"])
	notes = forms.erpnext_notes()
	if notes:
		ctx.say("ERPNext notes: " + " ".join(notes))
	workflow.finish(ctx, doc.doctype, doc.name)
	ctx.st.update({"phase": "done", "pending": None, "created": {"doctype": doc.doctype, "name": doc.name}})
	status = "Draft" if doc.docstatus == 0 else ("Submitted" if doc.docstatus == 1 else "Cancelled")
	label = result.get("label") or doc.doctype
	text = f"Your {label} draft has been created successfully. Open it to review and submit it yourself in ERPNext."
	if result.get("totals_note"):
		text += "\n" + result["totals_note"]
	ctx.say(text, card=_created_card(doc.doctype, doc.name, doc=doc, status=status))


_create_invoice = _create_document  # older name


def _creation_failed(ctx, reason):
	"""ERPNext rejected the draft. Re-plan first (validation may now ask a specific question);
	if nothing new comes up, explain and offer Retry/Cancel rather than the same confirmation."""
	ctx.say(f"ERPNext didn't accept the draft: {reason}")
	before = ctx.st.get("pending")
	advance(ctx)
	pending = ctx.st.get("pending")
	if pending and pending.get("kind") == "confirm":
		ctx.msgs.pop()  # drop the identical confirmation that advance() just added
		ctx.st["pending"] = None
		ctx.ask(
			"blocked",
			"Nothing was created. Correct the problem above (in ERPNext, or tell me the corrected value), then choose Retry.",
			options=[
				{"label": "Retry", "value": "__retry__"},
				{"label": "Cancel", "value": "__cancel__", "style": "secondary"},
			],
		)
	return before


PARTY_FIELDS = (
	("customer_name", "Customer"),
	("customer", "Customer"),
	("supplier_name", "Supplier"),
	("supplier", "Supplier"),
	("party_name", "Customer"),
)


def _created_card(doctype, name, doc=None, status=None):
	doc = doc or frappe.get_doc(doctype, name)
	status = status or ("Draft" if doc.docstatus == 0 else "Submitted" if doc.docstatus == 1 else "Cancelled")
	rows = [{"label": "ID", "value": name}]
	party = next(
		((label, doc.get(f)) for f, label in PARTY_FIELDS if doc.meta.has_field(f) and doc.get(f)), None
	)
	if party:
		rows.append({"label": party[0], "value": party[1]})
	if doctype == "BOM":
		rows.append({"label": "Item", "value": doc.get("item")})
	date = doc.get("posting_date") or doc.get("transaction_date")
	if date:
		rows.append({"label": "Posting date" if doc.get("posting_date") else "Date", "value": str(date)})
	if doc.meta.has_field("grand_total"):
		rows.append(
			{
				"label": "Grand total",
				"value": f"{flt(doc.get('grand_total'), 2)} {doc.get('currency') or ''}".strip(),
			}
		)
	rows.append({"label": "Status", "value": status})
	return {
		"type": "created",
		"title": f"{doctype} {name}",
		"doctype": doctype,
		"name": name,
		"action_label": f"Open {doctype}",
		"rows": rows,
	}


def _announce(ctx, entry):
	results = load_json(entry.mapping_results, {}) or {}
	validation = load_json(entry.validation_results, {}) or {}
	mapped = load_json(entry.mapped_data, {}) or {}
	canonical = DocumentAdapter(None).adapt(load_json(entry.extracted_data, {}) or {})
	cls = _classification(entry)
	d = _definition(entry)

	def row(label, key, value=None):
		r = results.get(key) or {}
		ok = r.get("status") in FieldStatus.RESOLVED
		status = "ok" if ok and r.get("status") == "MATCHED" else ("warn" if ok else "error")
		shown = value if value is not None else (r.get("display_name") or r.get("name") or r.get("input"))
		note = None if status == "ok" else (r.get("message") or ("Not found" if status == "error" else None))
		return {"label": label, "value": shown or "Not on document", "status": status, "note": note}

	rows = [
		{
			"label": "Document",
			"value": _describe(cls),
			"status": "ok" if cls.get("status") == "ok" else "warn",
		}
	]
	if d:
		rows.append(
			{"label": "For your company", "value": f"{_role_text(cls)}: a Draft {d.display}", "status": "ok"}
		)
		if d.party_doctype:
			rows.append(row(d.party_doctype, d.party_doctype.lower()))
		for key in (d.date_field, d.second_date_field):
			if key:
				rows.append(row(DATE_LABELS[key].capitalize(), key))
		if d.currency and d.target_doctype != "BOM":
			rows.append(row("Currency", "currency"))
	else:
		rows.append(
			{
				"label": "For your company",
				"value": cls.get("message") or "Not determined yet",
				"status": "warn",
			}
		)

	items = []
	for i, it in enumerate(mapped.get("items") or [], start=1):
		ir, ur = results.get(f"item_{i}") or {}, results.get(f"item_{i}_uom") or {}
		items.append(
			{
				"name": ir.get("input") or it.get("description"),
				"matched": it.get("item_code"),
				"status": "ok" if it.get("item_code") else "error",
				"qty": it.get("qty"),
				"uom": (
					f"{ur.get('input')} → {it.get('uom')}"
					if ur.get("input") and it.get("uom") and ur.get("input") != it.get("uom")
					else (it.get("uom") or ur.get("input"))
				),
				"rate": it.get("rate"),
				"amount": it.get("amount"),
			}
		)
	notes = [
		w["message"]
		for w in validation.get("warnings", [])
		if w["code"].startswith(
			("uom_", "currency_defaulted", "foreign_currency", "item_", "apply_discount_on")
		)
	]
	card = {
		"type": "summary",
		"title": f"{(d.display if d else 'Document')} {canonical.get('invoice_number') or ''}".strip(),
		"rows": rows,
		"items": items,
		"totals": {
			"subtotal": canonical.get("subtotal"),
			"discount": canonical.get("discount_amount"),
			"tax": canonical.get("tax_amount"),
			"total": canonical.get("total_amount"),
		},
		"notes": notes[:5],
	}
	matched_items = sum(1 for i in items if i["matched"])
	what = f"a {d.display} for your company" if d else "a document I can't classify yet"
	ctx.say(
		f"I've read the document: it is {what}. {len(items)} line item{'s' if len(items) != 1 else ''}, {matched_items} matched to ERPNext items. "
		"I'll ask only about what still needs your input.",
		card=card,
	)


def _safe_error(exc):
	text = strip_html(str(exc) or "").strip()
	return (text[:300] + "…") if len(text) > 300 else (text or "unknown error")
