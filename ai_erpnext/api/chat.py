"""Conversational assistant API. All endpoints are POST (Frappe commits only on POST),
operate only on the caller's own conversation and lock it while working.

Three distinct operations (see chat/session.py):
- create: get_session() without an id (the page's first open) and new_conversation();
- resume: get_session(id) for the page's own conversation, open_conversation(id) for one the
  user picked from the history (ownership checked, nothing else changes);
- list: list_conversations().
send_message and poll act only on the conversation they name; if it is closed or expired they
answer {"closed": true} without touching any conversation, and poll never creates one."""

import frappe

from ai_erpnext.chat import engine
from ai_erpnext.chat import session as S


def _ctx(session=None, active=False):
	frappe.flags.ai_chat = True
	if active and session:
		return engine.Ctx(S.lock(S.active_session(session), active=True))
	return engine.Ctx(S.lock(S.get_session(session)))


def _closed(session):
	return {"session": session, "closed": True, "messages": [], "busy": False, "pending_id": None}


@frappe.whitelist(methods=["POST"])
def get_session(session=None):
	"""The page's conversation with all its messages: the one it names, or a new one when the page
	has none yet (also advances finished extractions)."""
	ctx = _ctx(session)
	engine.greet_if_new(ctx)
	engine.poll(ctx)
	ctx.save()
	return ctx.payload(since=0)


@frappe.whitelist(methods=["POST"])
def send_message(session=None, text=None, file_url=None, action=None, client_id=None, context=None):
	"""context: {"route": frappe.get_route()} of the page the user is on, so “this Sales Order” can be
	resolved; it is only a hint and is re-checked against the user's permissions."""
	try:
		ctx = _ctx(session, active=True)
	except S.SessionClosed:
		return _closed(session)
	engine.handle_message(ctx, text=text, file_url=file_url, action=action, client_id=client_id, context=context)
	ctx.save()
	return ctx.payload()


@frappe.whitelist(methods=["POST"])
def poll(session=None, since=0):
	if not session:
		return _closed(None)  # polling never creates a conversation
	try:
		ctx = _ctx(session, active=True)
	except S.SessionClosed:
		return _closed(session)
	engine.poll(ctx)
	ctx.save()
	return ctx.payload(since=since)


@frappe.whitelist(methods=["POST"])
def new_conversation():
	frappe.flags.ai_chat = True
	doc = S.new_session()
	ctx = engine.Ctx(doc)
	engine.greet_if_new(ctx)
	ctx.save()
	return ctx.payload(since=0)


@frappe.whitelist(methods=["POST"])
def list_conversations():
	"""The caller's own recent conversations (title and time only) for the history list."""
	return S.history()


@frappe.whitelist(methods=["POST"])
def open_conversation(session):
	"""Resume a conversation the caller picked from the history (ownership checked)."""
	frappe.flags.ai_chat = True
	ctx = engine.Ctx(S.lock(S.reopen(session)))
	engine.poll(ctx)
	ctx.save()
	return ctx.payload(since=0)
