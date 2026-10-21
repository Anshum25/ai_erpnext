"""Server-side check of text the router model wrote (modes chat, clarify and unsupported).

The model never receives ERPNext data and can't perform anything, so a model-written sentence that claims an action
("I've created the invoice", "it has been submitted") or states data ("you have 12 overdue invoices", "the outstanding
is ₹45,000", "SINV-2026-00012", "the report shows a profit of 3 lakh") is false by construction. Facts and actions only
ever reach the user from server code: results arrive as cards labelled with their source, created records as a
"created" card with a link. This module is the last check: a reply that makes such a claim is replaced by a fixed,
truthful sentence. Numbers and IDs the user typed themselves may be repeated.

Checks are deliberately narrow (completed-action phrasing, counts of business records, amounts with a currency, document
IDs, report results) so ordinary guidance ("submitting posts the invoice to the ledger", "step 2") passes unchanged.
"""

import re

from ai_erpnext.ai.errors import get_ai_logger

_DONE = (
	r"(?:created|made|submitted|saved|posted|paid|cancell?ed|deleted|removed|amended|recorded|booked|approved|updated|"
	r"added|generated|raised|sent|processed|issued|reconciled|imported|entered)"
)
# "I've created ...": always a claim.
ACTION_RE = re.compile(
	r"\b(?:i|we)(?:'ve|\s+have|'ll\s+have)?\s+(?:just\s+|now\s+|already\s+|successfully\s+)*" + _DONE + r"\b"
	r"|\bsuccessfully\s+" + _DONE + r"\b",
	re.I,
)
# "... has been submitted": a claim unless the clause is conditional ("once an invoice has been submitted, ...").
PASSIVE_RE = re.compile(
	r"\b(?:has|have|had)\s+(?:now\s+|already\s+|just\s+)?been\s+(?:successfully\s+)?" + _DONE + r"\b"
	r"|\b(?:is|are|was|were)\s+(?:now\s+)?successfully\s+"
	+ _DONE
	+ r"\b|\b(?:is|are)\s+now\s+"
	+ _DONE
	+ r"\b",
	re.I,
)
CONDITIONAL_RE = re.compile(
	r"\b(?:once|after|when|whenever|if|until|before|unless|until|as\s+soon\s+as)\b[^.!?;:]*$", re.I
)
_RECORDS = (
	r"(?:invoices?|bills?|orders?|customers?|suppliers?|vendors?|items?|products?|records?|entries|entry|receipts?|quotations?|quotes?|"
	r"notes?|payments?|leads?|opportunit(?:y|ies)|employees?|projects?|tasks?|issues?|tickets?|documents?|transactions?|requests?|boms?|"
	r"work\s+orders?|units?|pieces?|pcs)"
)
COUNT_RE = re.compile(r"\b\d[\d,]*(?:\.\d+)?\s+(?:[a-z-]+\s+){0,3}?" + _RECORDS + r"\b", re.I)
_CURRENCY_CODES = r"(?:INR|USD|EUR|GBP|AED|SAR|SGD|AUD|CAD|JPY|CNY|CHF|ZAR|NZD|KES|NGN|PKR|BDT|LKR|NPR|MYR|IDR|PHP|THB|QAR|OMR|KWD|BHD)"
AMOUNT_RE = re.compile(
	r"[₹$€£¥]\s?\d[\d,]*(?:\.\d+)?|\b(?:rs\.?|inr)\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\s?"
	+ _CURRENCY_CODES
	+ r"\b"
	r"|\b\d[\d,]*(?:\.\d+)?\s?(?:lakhs?|crores?|thousand|million|billion)\b"
	r"|\b(?:total|outstanding|balance|revenue|sales|profit|loss|amount|value|turnover|expenses?|due)\s+(?:of|is|was|are|were|stands\s+at|comes\s+to|:)\s+[₹$€£]?\s?\d",
	re.I,
)
DOC_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{2,}(?:-\d+)*\b")
REPORT_RE = re.compile(
	r"\b(?:i|we)(?:'ve|\s+have)?\s+(?:ran|run|executed|generated|pulled|opened)\s+(?:the\s+|your\s+|this\s+)?(?:[a-z]+\s+){0,3}reports?\b"
	r"|\breports?\s+(?:shows?|showed|returned|indicates?|lists?|found|says?)\b[^.\n]{0,80}\d",
	re.I,
)

ACTION_TEXT = (
	"I haven't created, changed, submitted or paid anything. I only create records through the guided steps, after you "
	"confirm, and you'd see a confirmation card with a link to the new record. I never submit, cancel, delete or pay."
)
DATA_TEXT = (
	"I can't give figures, counts or record IDs in a general answer: I haven't looked anything up in your ERPNext data for "
	"this. Ask me to show, count or total the records (for example “show overdue sales invoices” or “total sales this month”) "
	"and I'll fetch them from ERPNext."
)
REPORT_TEXT = (
	"I haven't run that report: I can describe a report and its filters, but I don't read report results. Ask me for the "
	"records instead (for example “unpaid sales invoices by customer”)."
)


def _numbers(text):
	return set(re.findall(r"\d[\d,]*(?:\.\d+)?", text or ""))


def check(reply, user_text=""):
	"""(reasons, ...) the reply makes claims it can't support: a subset of {"action", "data", "report"}."""
	if not reply:
		return set()
	user_numbers, user_upper = _numbers(user_text), (user_text or "").upper()
	reasons = set()
	if ACTION_RE.search(reply) or any(
		not CONDITIONAL_RE.search(reply[max(0, m.start() - 80) : m.start()])
		for m in PASSIVE_RE.finditer(reply)
	):
		reasons.add("action")
	if REPORT_RE.search(reply):
		reasons.add("report")
	for regex in (COUNT_RE, AMOUNT_RE):
		for m in regex.finditer(reply):
			figures = (
				_numbers(m.group(0)) - user_numbers
			)  # figures the user didn't give; "1 item" / "0 rows" are guidance
			if regex is COUNT_RE:
				figures = {f for f in figures if f not in ("0", "1")}
			if figures:
				reasons.add("data")
	for m in DOC_ID_RE.finditer(reply):
		if m.group(0).upper() not in user_upper:
			reasons.add("data")
	return reasons


def guard(reply, user_text="", mode="chat"):
	"""The reply to show: unchanged when it makes no unsupported claim, else a fixed truthful sentence."""
	reasons = check(reply, user_text)
	if not reasons:
		return reply
	# metadata only: the mode and the kinds of claim, never what the model wrote
	get_ai_logger().warning(f"AI-CHAT claim_blocked mode={mode} reasons={','.join(sorted(reasons))}")
	if "action" in reasons:
		return ACTION_TEXT
	if "report" in reasons:
		return REPORT_TEXT
	return DATA_TEXT
