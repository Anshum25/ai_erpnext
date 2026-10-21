"""Server-side conversation storage for the AI assistant.

The browser only ever sends user text, an uploaded file URL or the id/value of a button;
everything else (operation, extracted data, answers, acknowledgements) lives here or in
the linked AI Document Entry, owned by the user.

There is no per-user "current conversation" on the server. Each page (browser tab, window,
incognito session) holds the id of the conversation it shows, in memory:
- create: when the assistant is first opened on a page, or on "New chat", a new conversation
  is created (new id, empty state). Other conversations are left exactly as they are, so a
  user can have several open at once, in several browsers.
- resume: a conversation the user picks from the history is reopened by its id, after the
  ownership check; its messages and pending work continue there.
- list: the history shows the user's own conversations that have messages and haven't expired.
status is "Open" for usable conversations and "Closed" once expired (see cleanup).

A conversation id from the browser is untrusted: every access checks the owner, and a missing
or foreign id gets the same "Conversation not found." error. A message or poll is only ever
applied to the conversation it names; if that one is closed or expired, SessionClosed is
raised and nothing is redirected to another conversation.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, now_datetime

DOCTYPE = "AI Chat Session"
MAX_MESSAGES = 200
MAX_PROCESSED_IDS = 100
EXPIRY_DAYS = 7


NOT_FOUND = "Conversation not found."
EPHEMERAL_TEXT = "Your unsaved changes were shown here. They are not stored in the conversation."
HISTORY_LIMIT = 20
UNTITLED = "Conversation"  # title until the user's first message (text or attachment)


class SessionClosed(Exception):
	"""The conversation a request was sent for is closed or expired."""


def _owned(doc):
	# Role access is rechecked on every load: a user who is no longer allowed to use the chat (for example
	# no longer a System Manager) gets nothing back from conversations stored earlier.
	if doc.owner != frappe.session.user or not frappe.has_permission(DOCTYPE, "read"):
		raise frappe.PermissionError(_(NOT_FOUND))  # same answer as a missing id: nothing is revealed
	return doc


def _load(name):
	"""The user's own conversation `name`, or PermissionError (missing and foreign ids alike)."""
	if not isinstance(name, str) or not name or len(name) > 140 or not frappe.db.exists(DOCTYPE, name):
		raise frappe.PermissionError(_(NOT_FOUND))
	return _owned(frappe.get_doc(DOCTYPE, name))


def get_session(name=None):
	"""The conversation a page shows: the one it already has (its own id, ownership checked), or a
	new one. Never another existing conversation: a page without an id gets a fresh conversation
	even if the user has others (in another browser, or earlier ones in the history)."""
	if name:
		doc = _load(name)
		if _usable(doc):
			return doc
	return new_session()


def active_session(name):
	"""The conversation a message or poll was sent for; it must be the user's own, usable one."""
	doc = _load(name)
	if not _usable(doc):
		raise SessionClosed()
	return doc


def _usable(doc):
	return doc.status == "Open" and not _expired(doc)


def history(limit=HISTORY_LIMIT):
	"""The user's own conversations that can still be opened (metadata only, newest first).
	Conversations nobody wrote in yet (a page opened and left) are not listed."""
	cutoff = add_days(now_datetime(), -EXPIRY_DAYS)
	rows = frappe.get_all(
		DOCTYPE,
		filters={"owner": frappe.session.user, "last_activity": [">=", cutoff], "title": ["!=", UNTITLED]},
		fields=["name", "title", "last_activity"],
		order_by="last_activity desc",
		limit=limit,
	)
	return [{"session": r.name, "title": r.title, "last_activity": str(r.last_activity)} for r in rows]


def reopen(name):
	"""Resume a conversation the user picked from the history (its pending work included). Nothing
	else changes: conversations open in other pages stay as they are."""
	doc = _load(name)
	if _expired(doc):
		raise frappe.PermissionError(_(NOT_FOUND))
	if doc.status != "Open":  # closed by an earlier version of "New chat", still within its lifetime
		doc.status = "Open"
		doc.save()
	return doc


def new_session():
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"title": UNTITLED,
			"status": "Open",
			"messages": "[]",
			"state": "{}",
			"last_activity": now_datetime(),
		}
	)
	doc.insert()
	return doc


def close(doc):
	doc.status = "Closed"
	doc.save()


def lock(doc, active=False):
	"""Serialise concurrent requests for one conversation (double clicks, retries, polls, two
	pages showing the same conversation). active: it must still be usable once locked."""
	frappe.db.get_value(DOCTYPE, doc.name, "name", for_update=True)
	doc.reload()
	_owned(doc)
	if active and not _usable(doc):
		raise SessionClosed()
	return doc


def _expired(doc):
	return bool(doc.last_activity) and doc.last_activity < add_days(now_datetime(), -EXPIRY_DAYS)


def messages(doc):
	return json.loads(doc.messages or "[]")


def state(doc):
	"""The conversation state, with malformed parts dropped (never trusted blindly after a restore).

	Keys: pending (the one open question: id, kind, key, options; lookups also keep their plan and
	attempt count), forms (the guided record(s) being filled, outermost first), entry/phase (an
	invoice from an image), last_query, processed/replies (message ids already handled and the
	reply each produced), ask_sig (loop guard), seq (last message id)."""
	try:
		st = json.loads(doc.state or "{}")
	except ValueError:
		st = {}
	if not isinstance(st, dict):
		st = {}
	pending = st.get("pending")
	if pending is not None and not (
		isinstance(pending, dict)
		and isinstance(pending.get("id"), str)
		and isinstance(pending.get("kind"), str)
	):
		st["pending"] = None
	if not isinstance(st.get("forms") or [], list) or not all(
		isinstance(f, dict) and f.get("doctype") for f in st.get("forms") or []
	):
		st["forms"] = []
	if not isinstance(st.get("processed") or [], list):
		st["processed"] = []
	if not isinstance(st.get("replies") or {}, dict):
		st["replies"] = {}
	return st


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
	kept = set(st["processed"])
	st["replies"] = {k: v for k, v in (st.get("replies") or {}).items() if k in kept}
	# Messages that show browser-provided values (unsaved changes) are shown once and stored without them.
	stored = [
		{k: v for k, v in m.items() if k not in ("card", "ref", "ephemeral", "source")}
		| {"text": EPHEMERAL_TEXT}
		if m.get("ephemeral")
		else m
		for m in msgs
	]
	doc.messages = json.dumps(stored, default=str)
	doc.state = json.dumps(st, default=str)
	doc.last_activity = now_datetime()
	if doc.title == UNTITLED:
		first = next((m for m in msgs if m["role"] == "user"), None)
		if first:
			doc.title = (
				first.get("text") or (first.get("attachment") or {}).get("file_name") or "Attachment"
			)[:60]
	doc.save()


def cleanup_expired_sessions():
	"""Daily: close expired conversations and drop their message history. Every page that opens
	the assistant starts a conversation, so ones nobody wrote in are deleted after a day."""
	cutoff = add_days(now_datetime(), -EXPIRY_DAYS)
	for name in frappe.get_all(
		DOCTYPE, filters={"last_activity": ["<", cutoff], "status": "Open"}, pluck="name"
	):
		frappe.db.set_value(DOCTYPE, name, {"status": "Closed", "messages": "[]", "state": "{}"})
	frappe.db.delete(
		DOCTYPE,
		{
			"title": UNTITLED,
			"active_entry": ["is", "not set"],
			"last_activity": ["<", add_days(now_datetime(), -1)],
		},
	)
