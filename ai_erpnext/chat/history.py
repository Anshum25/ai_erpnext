"""Replaying a stored conversation without leaking business data.

Query results are stored in the conversation (text and card) when they are produced. Showing them
again later must not bypass the permissions the reader has *now*: a role, User Permission or company
restriction may have changed since, and a record may have been restricted. So, when messages produced
by an earlier request are returned, each one that carries ERPNext data is checked again:

- the reader must still be allowed to read the DocType;
- every record it refers to (message "ref", or the card's DocType and names) must still be readable,
  with the same permission-aware, company-scoped query the lookups use;
- a table loses the rows that are no longer readable; any other message that refers to an unreadable
  record, or whose records can't be determined, is replaced by a neutral sentence.

Messages without ERPNext data (greetings, general answers, questions, the user's own messages) are
returned as stored. Nothing here writes to the conversation.
"""

import frappe

SAFE_TEXT = "This earlier result is no longer available to you."
PARTIAL_CARD_NOTE = "From your ERPNext data. Some of the earlier rows are no longer available to you."
PARTIAL_NOTE = " Some of the earlier rows are no longer available to you and were removed."
MAX_REFS = 50


def references(msg):
	"""(doctype, names) a stored message's ERPNext data comes from: (None, None) when it has none,
	(doctype, None) when it can't be determined (the message must then be withheld)."""
	card = msg.get("card") if isinstance(msg.get("card"), dict) else None
	ref = msg.get("ref") if isinstance(msg.get("ref"), dict) else None
	if ref and isinstance(ref.get("doctype"), str):
		names = ref.get("names")
		return ref["doctype"], [n for n in names if isinstance(n, str)][:MAX_REFS] if isinstance(names, list) else []
	if card:
		doctype = card.get("doctype") or card.get("source_doctype")
		if isinstance(doctype, str) and doctype:
			if isinstance(card.get("name"), str):
				return doctype, [card["name"]]
			rows = card.get("rows") if isinstance(card.get("rows"), list) else []
			return doctype, [r.get("_name") for r in rows if isinstance(r, dict) and isinstance(r.get("_name"), str)][:MAX_REFS]
	if msg.get("source") == "erpnext":
		return "", None  # an ERPNext result whose source can't be determined (stored before references were kept)
	return None, None


def _readable(doctype, names, cache):
	"""False: the DocType isn't readable. None: readable and no particular record. Otherwise the set of readable names."""
	key = (doctype, tuple(names))
	if key in cache:
		return cache[key]
	result = False
	try:
		if frappe.db.exists("DocType", doctype) and frappe.has_permission(doctype, "read"):
			if not names:
				result = None
			else:
				from ai_erpnext.capability.access import readable_names
				from ai_erpnext.chat import queries

				scope = queries._scope(doctype) if doctype in queries.REGISTRY else []
				result = set(readable_names(doctype, [["name", "in", names], *scope], limit=len(names)))
	except Exception:
		frappe.local.message_log = []
		result = False
	cache[key] = result
	return result


def _withheld(msg):
	return {k: msg[k] for k in ("id", "role", "ts") if k in msg} | {"text": SAFE_TEXT}


def recheck(messages, replay_upto):
	"""Messages as the current user may see them. Those with id <= replay_upto were stored by earlier requests."""
	out, cache = [], {}
	for msg in messages:
		if msg.get("id", 0) > replay_upto or msg.get("role") != "assistant":
			out.append({k: v for k, v in msg.items() if k != "ref"})
			continue
		doctype, names = references(msg)
		if doctype is None:  # no ERPNext data in this message
			out.append({k: v for k, v in msg.items() if k != "ref"})
			continue
		requires = (msg.get("ref") or {}).get("requires") or []
		msg = {k: v for k, v in msg.items() if k != "ref"}
		allowed = _readable(doctype, names, cache) if doctype and names is not None else False
		if allowed is not False and any(_readable(dt, [], cache) is False for dt in requires if isinstance(dt, str)):
			allowed = False  # e.g. totals computed from invoices the user can no longer read
		if allowed is None or (allowed is not False and set(names) <= allowed):
			out.append(msg)
			continue
		card = msg.get("card")
		if allowed and isinstance(card, dict) and card.get("type") == "table" and isinstance(card.get("rows"), list):
			rows = [r for r in card["rows"] if isinstance(r, dict) and r.get("_name") in allowed]
			if rows:
				# the card's note can quote the filters that selected the removed rows (their IDs, a customer): it is replaced
				out.append(dict(msg, card=dict(card, rows=rows, note=PARTIAL_CARD_NOTE), text=(msg.get("text") or "").rstrip() + PARTIAL_NOTE))
				continue
		out.append(_withheld(msg))
	return out
