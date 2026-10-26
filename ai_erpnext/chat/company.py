"""“What is my company?”: the companies the user may work with, and which one is theirs.

Answered from the same allow-list the rest of the chat uses for companies (queries.usable_companies): the companies a
Company User Permission gives the user, or every company when none restricts them and they may read companies at all.
Nothing else is looked up, and no company is ever picked by the order of a query result:

- one usable company: that is the user's company;
- several, with a default company of the user's own (their user default or a default User Permission): that one, with
  the others they can also use;
- several without one: the choices, and which company new documents default to (the site-wide default), if it is
  one of them; the user is asked which one they mean.
"""

import re

import frappe

MAX_LISTED = 10

# "show me my company name", "what is our company?", "which company am I in", "list my companies"
MY_COMPANY_RE = re.compile(
	r"^\s*(?:please\s+)?(?:(?:show|tell|give|get|display|list)(?:\s+me)?\s+|what(?:'s|\s+is|\s+are)?\s+|which\s+(?:is\s+)?)?"
	r"(?:the\s+)?(?:name\s+of\s+)?(?:my|our|the\s+current|current|the\s+default|default|my\s+default|my\s+current)\s+"
	r"(?:company|companies|organi[sz]ation|firm)(?:'s)?(?:\s+names?)?(?:\s+is)?\s*[?.!]*\s*$"
	r"|^\s*(?:which|what)\s+compan(?:y|ies)\s+(?:am\s+i|do\s+i|can\s+i|i\s+am)\b[^?]{0,40}[?.!]*\s*$"
	r"|^\s*(?:show|list)(?:\s+me)?\s+(?:all\s+)?(?:the\s+)?companies(?:\s+i\s+can\s+(?:use|access|see))?\s*[?.!]*\s*$",
	re.I,
)


def asks(text):
	return bool(MY_COMPANY_RE.match(text or ""))


def is_company_doctype(value):
	return isinstance(value, str) and value.strip().lower() in ("company", "companies")


def own_default(allowed):
	"""The user's own default company (user default, or a User Permission marked default), if it is usable."""
	from frappe.defaults import get_defaults_for, get_user_permission_default

	user = frappe.session.user
	value = (get_defaults_for(user) or {}).get("company") or get_user_permission_default("Company", {})
	return value if value in allowed else None


def answer():
	"""{"text", "names"} for “what is my company”, from the user's usable companies only."""
	from ai_erpnext.chat.queries import usable_companies

	allowed = usable_companies()
	if not allowed:
		return {
			"text": "I can't see any company you have access to. Ask your administrator to give your user access to your "
			"company (a role that can read companies, or a Company User Permission).",
			"names": [],
		}
	if len(allowed) == 1:
		return {"text": f"Your company is {allowed[0]}.", "names": allowed}
	own = own_default(allowed)
	if own:
		others = [c for c in allowed if c != own]
		return {
			"text": f"Your default company is {own}. You can also use: {_listed(others)}.",
			"names": allowed,
		}
	site_default = frappe.defaults.get_global_default("company")
	text = f"You have access to {len(allowed)} companies: {_listed(allowed)}. None of them is set as your own default company"
	if site_default in allowed:
		text += f"; new documents default to {site_default}, the site-wide default company"
	text += ". Which company do you mean? If one of them is yours, your administrator can set it as your default company."
	return {"text": text, "names": allowed}


def _listed(names):
	shown = ", ".join(names[:MAX_LISTED])
	return shown + (f" and {len(names) - MAX_LISTED} more" if len(names) > MAX_LISTED else "")
