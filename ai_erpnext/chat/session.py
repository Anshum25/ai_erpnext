"""Server-side conversation storage for the AI assistant.

The browser only ever sends user text, an uploaded file URL or the id/value of a button;
everything else (operation, extracted data, answers, acknowledgements) lives here or in
the linked AI Document Entry, owned by the user.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, now_datetime

DOCTYPE = "AI Chat Session"
MAX_MESSAGES = 200
MAX_PROCESSED_IDS = 100
EXPIRY_DAYS = 7


def _owned(doc):
	if doc.owner != frappe.session.user:
		raise frappe.PermissionError(_("This conversation belongs to another user."))
	return doc


def get_session(name=None):
	"""Return the user's conversation (the given one, or their latest open one, or a new one)."""
	if name:
		doc = _owned(frappe.get_doc(DOCTYPE, name))
		if doc.status == "Open" and not _expired(doc):
			return doc
	rows = frappe.get_all(DOCTYPE, filters={"owner": frappe.session.user, "status": "Open"},
		order_by="modified desc", pluck="name", limit=1)
	if rows:
		doc = _owned(frappe.get_doc(DOCTYPE, rows[0]))
		if not _expired(doc):
			return doc
		close(doc)
	return new_session()


def new_session():
	for name in frappe.get_all(DOCTYPE, filters={"owner": frappe.session.user, "status": "Open"}, pluck="name"):
		close(frappe.get_doc(DOCTYPE, name))
	doc = frappe.get_doc({"doctype": DOCTYPE, "title": "Conversation", "status": "Open",
		"messages": "[]", "state": "{}", "last_activity": now_datetime()})
	doc.insert()
	return doc


def close(doc):
	doc.status = "Closed"
	doc.save()


def lock(doc):
	"""Serialise concurrent requests for one conversation (double clicks, retries, polls)."""
	frappe.db.get_value(DOCTYPE, doc.name, "name", for_update=True)
	doc.reload()
	return _owned(doc)


def _expired(doc):
	return bool(doc.last_activity) and doc.last_activity < add_days(now_datetime(), -EXPIRY_DAYS)


def messages(doc):
	return json.loads(doc.messages or "[]")


def state(doc):
	return json.loads(doc.state or "{}")


def add_message(doc, msgs, st, role, text=None, **extra):
	st["seq"] = int(st.get("seq") or 0) + 1
	msg = {"id": st["seq"], "role": role, "ts": str(now_datetime())}
	if text:
		msg["text"] = text
	msg.update({k: v for k, v in extra.items() if v is not None})
	msgs.append(msg)
	return msg


def save(doc, msgs, st):
	if len(msgs) > MAX_MESSAGES:
		del msgs[: len(msgs) - MAX_MESSAGES]
	processed = st.get("processed") or []
	st["processed"] = processed[-MAX_PROCESSED_IDS:]
	doc.messages = json.dumps(msgs, default=str)
	doc.state = json.dumps(st, default=str)
	doc.last_activity = now_datetime()
	if doc.title == "Conversation":
		first = next((m.get("text") for m in msgs if m["role"] == "user" and m.get("text")), None)
		if first:
			doc.title = first[:60]
	doc.save()


def cleanup_expired_sessions():
	"""Daily: close expired conversations and drop their message history."""
	cutoff = add_days(now_datetime(), -EXPIRY_DAYS)
	for name in frappe.get_all(DOCTYPE, filters={"last_activity": ["<", cutoff], "status": "Open"}, pluck="name"):
		frappe.db.set_value(DOCTYPE, name, {"status": "Closed", "messages": "[]", "state": "{}"})
