"""The multi-step workflow a conversation is in (a guided form or a document image), and cancelling it.

A conversation runs at most one workflow at a time. Its state lives in the conversation state like everything else
(chat/session.py): "workflow" = {"id", "kind", "created": [{"doctype", "name"}]}. The id goes to the browser with every
reply (payload "workflow") and the Cancel control sends it back, so a Cancel pressed for a workflow that already ended
(finished, cancelled or replaced by a new one) never cancels another one. Question buttons are bound to their question id
the same way (engine.Ctx.ask), and cancelling clears the pending question, so an old "Create" button, a retried request or a
reloaded page can't complete a cancelled workflow.

Cancelling discards what the workflow collected and nothing else. Records it already created on the way (a customer or
item added from a question, the Draft itself) stay in ERPNext and are named in the reply: nothing is deleted, submitted or
cancelled in ERPNext, and an email ERPNext already sent can't be recalled.
"""

import uuid

import frappe

IMAGE_PHASES = ("extracting", "collecting", "failed")
MAX_CREATED = 20


def kind(st):
	""" "form", "image" or None for a conversation state."""
	if st.get("forms"):
		return "form"
	if st.get("entry") and st.get("phase") in IMAGE_PHASES:
		return "image"
	return None


def begin(ctx, workflow_kind):
	"""A new workflow starts: a new id, so Cancel controls of earlier workflows no longer apply."""
	ctx.st["workflow"] = {"id": uuid.uuid4().hex[:12], "kind": workflow_kind, "created": []}
	ctx.st["finished"] = None
	return ctx.st["workflow"]


def current(ctx):
	"""The active workflow, or None. A workflow that started before workflows had ids gets one now."""
	active = kind(ctx.st)
	if not active:
		return None
	wf = ctx.st.get("workflow")
	if not (isinstance(wf, dict) and isinstance(wf.get("id"), str) and wf.get("kind") == active):
		wf = begin(ctx, active)
	return wf


def sync(ctx):
	"""Before the state is saved: a workflow that has ended is no longer the conversation's workflow."""
	if not kind(ctx.st):
		ctx.st["workflow"] = None


def note_created(ctx, doctype, name):
	"""A record was created on the way (a new customer for the invoice being made): it stays if the workflow is cancelled."""
	wf = current(ctx)
	if wf is not None and len(wf["created"]) < MAX_CREATED:
		wf["created"].append({"doctype": doctype, "name": name})


def finish(ctx, doctype, name):
	"""The workflow's own record was created; remembered so that a "cancel" right afterwards can say it exists."""
	wf = current(ctx) or {}
	ctx.st["finished"] = {
		"doctype": doctype,
		"name": name,
		"seq": int(ctx.st.get("seq") or 0),
		"created": list(wf.get("created") or []),
	}


def describe(ctx):
	"""What the browser shows while a workflow runs (its Cancel control), or None."""
	wf = current(ctx)
	if not wf:
		return None
	if wf["kind"] == "form":
		doctype = ctx.st["forms"][0]["doctype"]
		label = f"Creating {doctype}" + ("" if _is_master(doctype) else " (Draft)")
	elif ctx.st.get("phase") == "extracting":
		label = "Reading the attached document"
	else:
		label = "Creating a Draft from the attached document"
	return {"id": wf["id"], "kind": wf["kind"], "label": label}


def _is_master(doctype):
	from ai_erpnext.chat.forms import MASTERS

	return doctype in MASTERS


def _record(doctype, name):
	return f"{doctype} {name}"


def links(created):
	return [
		{"label": f"Open {r['doctype']} {r['name']}", "doctype": r["doctype"], "name": r["name"]}
		for r in created
	]


def cancelled_text(what, created):
	"""The reply to a cancelled workflow: what was not created, and what was created earlier and still exists."""
	text = f"Cancelled{' the ' + what if what else ''}. "
	if not created:
		return text + "Nothing was created by this workflow."
	return (
		text
		+ f"No {what or 'document'} was created. Already created while we worked on it, and kept in ERPNext "
		"(cancelling here never deletes records): "
		+ ", ".join(_record(r["doctype"], r["name"]) for r in created)
		+ "."
	)


def finished_text(ctx, previous_user_id):
	"""The reply to "cancel" when nothing is in progress: if the last workflow ended by creating its record in reply to the
	previous message, say that the record exists and what cancelling it would mean in ERPNext. None otherwise."""
	done = ctx.st.get("finished")
	if not (isinstance(done, dict) and done.get("name") and int(done.get("seq") or 0) >= previous_user_id):
		return None
	doctype, name = done.get("doctype"), done.get("name")
	try:
		if not (frappe.db.exists(doctype, name) and frappe.has_permission(doctype, "read", name)):
			return None
		meta = frappe.get_meta(doctype)
		docstatus = frappe.db.get_value(doctype, name, "docstatus") if meta.is_submittable else None
	except Exception:
		frappe.local.message_log = []
		return None
	if docstatus == 1:
		state = (
			"is already submitted. Cancelling a submitted document is a separate ERPNext action (Cancel on the document, "
			"under ERPNext's own rules); I never cancel documents."
		)
	elif docstatus == 2:
		state = "is already cancelled in ERPNext."
	elif meta.is_submittable:
		state = (
			"was already created as a Draft and stays in ERPNext: cancelling here doesn't delete it. Open it to review it; "
			"if you don't need it, you can delete the Draft there yourself."
		)
	else:
		state = "was already created and stays in ERPNext: cancelling here doesn't delete it."
	text = f"There's nothing in progress to cancel. {_record(doctype, name)} {state}"
	if doctype == "Leave Application" and _leave_notification_on():
		text += " HRMS may already have emailed the leave approver when it was saved; that can't be undone from here."
	return text


def _leave_notification_on():
	try:
		return bool(frappe.db.get_single_value("HR Settings", "send_leave_notification"))
	except Exception:
		frappe.local.message_log = []
		return False
