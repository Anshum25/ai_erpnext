"""Conversational assistant API. All endpoints are POST (Frappe commits only on POST),
operate only on the caller's own conversation and lock it while working."""

import frappe

from ai_erpnext.chat import engine
from ai_erpnext.chat import session as S


def _ctx(session=None):
	frappe.flags.ai_chat = True
	doc = S.lock(S.get_session(session))
	return engine.Ctx(doc)


@frappe.whitelist(methods=["POST"])
def get_session(session=None):
	"""Full history of the user's current conversation (also advances finished extractions)."""
	ctx = _ctx(session)
	engine.greet_if_new(ctx)
	engine.poll(ctx)
	ctx.save()
	return ctx.payload(since=0)


@frappe.whitelist(methods=["POST"])
def send_message(session=None, text=None, file_url=None, action=None, client_id=None, context=None):
	"""context: {"route": frappe.get_route()} of the page the user is on, so “this Sales Order” can be
	resolved; it is only a hint and is re-checked against the user's permissions."""
	ctx = _ctx(session)
	engine.handle_message(ctx, text=text, file_url=file_url, action=action, client_id=client_id, context=context)
	ctx.save()
	return ctx.payload()


@frappe.whitelist(methods=["POST"])
def poll(session=None, since=0):
	ctx = _ctx(session)
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
