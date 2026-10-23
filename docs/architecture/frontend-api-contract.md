# Frontend ↔ backend contract

The chat UI is one jQuery module, [public/js/ai_bot_v2.js](../../ai_erpnext/public/js/ai_bot_v2.js), bundled with
[ai_page_context.js](../../ai_erpnext/public/js/ai_page_context.js) into `ai_erpnext.bundle.js`, which `hooks.py` puts
on every Desk page (`app_include_js`). The built bundle in `public/dist/js/` was rebuilt on 2026-10-23, after the
last source change. It contains the current source strings (checked by grep). **[code]**

## Who gets the widget

The launcher appears only when `frappe.boot.ai_chat_enabled` is true. [boot.py](../../ai_erpnext/boot.py) sets it
when the user may create `AI Chat Session`: System Manager, or the opt-in role **AI Assistant User**. This is only a
UI decision: every endpoint re-checks `AI Chat Session` read permission and ownership
([session.py:45](../../ai_erpnext/chat/session.py#L45)).

## Endpoints

All of them are `@frappe.whitelist(methods=["POST"])`. They need a logged-in session (cookie) and the CSRF token,
which `frappe.call` sends as `X-Frappe-CSRF-Token`. Responses are non-streaming JSON:
`{"message": <payload>}`. **[code]**, and **[test]** `test_security_hardening.test_state_changing_endpoints_accept_post_only`.

| Endpoint (`ai_erpnext.api.chat.`) | Arguments | Returns | Side effects |
|---|---|---|---|
| `get_session` | `session?` | payload, all messages | creates a conversation when `session` is empty, unusable or expired; greets; advances a finished extraction |
| `send_message` | `session`, `text?`, `file_url?`, `action?` (JSON string), `client_id?`, `context?` (JSON string) | payload: messages since the request started | the whole engine run (see [request-lifecycle.md](request-lifecycle.md)) |
| `poll` | `session`, `since` | payload: messages with `id > since` | advances an extraction; never creates a conversation |
| `new_conversation` | – | payload | creates a conversation; **old ones stay open** |
| `list_conversations` | – | `[{session, title, last_activity}]`, at most 20 | none (only the user's own conversations, with a title, from the last 7 days) |
| `open_conversation` | `session` | payload, all messages | reopens one of the user's own conversations (`Closed` → `Open` within the 7-day lifetime) |

Other endpoints of the app serve the Desk review form of an AI Document Entry, not the chat UI:
`api.mapping.get_review/remap/apply_corrections/get_supported_doctypes`, `api.creation.create_document`,
`api.extraction.retry_extraction` and `api.validation.validate_entry`. The chat calls
`create_document` and `retry_extraction` **in-process** ([engine.py:2257](../../ai_erpnext/chat/engine.py#L2257),
L1223).

### The payload

```json
{
  "session": "a1b2c3…",          // AI Chat Session name (hash)
  "messages": [ /* message objects, rechecked against current permissions */ ],
  "busy": false,                  // true while an image is being read (phase == "extracting")
  "pending_id": "9f3c…" ,         // id of the one active question, or null
  "closed": true                  // only on the closed/expired answer, with messages: []
}
```

### Message object

| Field | Type | Meaning | Rendered by |
|---|---|---|---|
| `id` | int | per-conversation sequence | `renderMessage` skips ids ≤ `S.lastId` |
| `role` | `user` / `assistant` | | bubble alignment |
| `ts` | string | server time | not shown |
| `text` | string | plain text; `\n` becomes `<br>` | escaped (`esc`/`lines`) |
| `source` | `erpnext` / `general` / `guide` / `clarify` | `erpnext`: "From your data" label; `general`: "General answer, not from your data"; `guide`: "General ERPNext guidance"; `clarify`: no label | L517–522 |
| `card` | object | see below | `renderCard` / `renderTable` |
| `question` | `{id, kind, options: [{label, value, style?}], input: "text"/"date"/null}` | the active question. Only the one whose `id == pending_id` is enabled | `renderQuestion`, `refreshQuestions` |
| `links` | `[{label, doctype, name}]` | "Open X" buttons (possible duplicates) | `renderLinks` |
| `suggestions` | `[string]` | buttons that send their text as `{command: "say"}` | `renderSuggestions` |
| `attachment` | `{file_name, file_url}` | thumbnail on the user's message | `<img>` |

Server-only fields (`ref`, `ephemeral`) are removed before sending ([history.py:80](../../ai_erpnext/chat/history.py#L80)).

### Card types

| `card.type` | Producer | Fields | UI |
|---|---|---|---|
| `table` | queries, hr, page_context | `title, doctype?, source_doctype?, columns[{key,label,type}], rows[{…,_name?,_n?}], note?, has_more?, list_route?` | a table; `type: "link"` columns become "Open DocType X" buttons when `card.doctype` and `row._name` exist; "Show more" (`{command: "more"}`); "Open in ERPNext" (`list_route`) |
| `record` | queries `_details` / `_field_answer` | `title, doctype, name, rows[{label,value}], items?{columns,rows}, note, action_label` | a key–value list + an "Open X" button |
| `created` | forms `_create`, engine `_created_card`, masters | `title, doctype, name, rows, action_label` | key–value list + an "Open X" button |
| `form_preview` | forms `_ask_confirm` | `title, rows, items?[{name,code,qty,uom,rate?,amount?,warehouse?,warehouse_note?}], note` | key–value list + an items table |
| `draft_preview` | engine `_ask_confirm` (image) | `title, rows, items` | same |
| `master_preview` | engine `_master_next` | `title, rows` | key–value list |
| `summary` | engine `_announce` (image) | `title, rows[{label,value,status:ok/warn/error,note}], items[{name,matched,status,qty,uom,rate,amount}], totals{subtotal,discount,tax,total}, notes[]` | key–value list with Matched/Check/Needs-input pills, items, "On the invoice: Subtotal · Tax · Total" |

### Button actions sent back

| UI element | `action` sent | Server handling |
|---|---|---|
| A question option | `{"question_id": q.id, "value": o.value, "label": o.label}` | `_on_action`: `question_id` must equal the pending id; `label` is only shown as the user's message |
| The date picker | same, with `value = YYYY-MM-DD` | same |
| Show more | `{"command": "more"}` | `_more` |
| A suggestion | `{"command": "say", "value": text}` | handled as typed text |

Every request carries a fresh `client_id`, except a **Retry**, which re-sends the identical payload. The server
returns the original reply when it already processed that id.

## Errors

There is no structured error object:

- Business problems (not found, no permission, invalid value, an ERPNext rejection) come back as **ordinary assistant
  messages** with HTTP 200.
- An exception that escapes the engine produces Frappe's HTTP error response (403 for `PermissionError`, 417/500
  otherwise) and a rollback. The UI shows only "The request didn't go through." with Retry (`call`, L341). It
  doesn't distinguish "Conversation not found." from a server bug. **[code]**

## Conversation handling in the browser

- The conversation id is kept **in memory only**, per page. The first time the panel opens, `get_session` is called
  without an id, which creates a new conversation (`loadSession`, L354).
- Frappe Desk is a single-page app, so moving between Desk routes keeps the same conversation. **A full reload (F5) or
  a new tab starts a new conversation.** Earlier conversations, including pending forms, continue only when the user
  picks them in the history list (`open_conversation`).
- The test `test_chat_guided.test_pending_document_survives_reload` passes the id to `get_session` explicitly, which
  models reopening the panel, not a browser reload. **[test] vs [code]**
- An uploaded but unsent image is kept in `localStorage` under `ai_agent_pending_upload:<user>` and restored after a
  reload (L884, L900). The `expanded` preference is also kept there.
- `S.gen` increases on every conversation switch, and late responses for the old one are dropped (L338, L455).

## Loading, polling, retry

- While a request is in flight: a typing indicator ("Working…"), and the composer, attach, new chat and history
  buttons are disabled.
- When `busy` is true: polling every 2.5 s with "Reading the invoice…" (the label is used for every document kind).
  After 10 minutes: "Still working on it. Check again in a moment." with Retry. Poll errors are retried silently.
- An expired conversation (`closed`): a new conversation is started and the typed text is put back into the input
  (L397–419).

## Attachments

1. The user picks a file. Only `image/png|jpeg|webp` ≤ 10 MB is accepted (`IMAGE_TYPES`, L27; `accept` attribute,
   L181). Anything else gets "Please attach a JPG, PNG or WEBP image. **PDF files aren't supported yet.**"
2. `XMLHttpRequest` POST `/api/method/upload_file` with `is_private=1`, `folder=Home` and the CSRF header, with a
   progress bar → `file_url`.
3. Send `{text?, file_url, client_id}`. The server accepts only a `File` **owned by the user** with that URL
   (`_attachment`, [engine.py:1079](../../ai_erpnext/chat/engine.py#L1079)). It checks the extension, the size and
   the real image bytes (PIL). A one-page PDF is rendered to a PNG `File`.

## Traces from the browser's point of view

**A successful text query ("how many draft purchase invoices").** `submit` → `send({text, client_id, context})` →
`send_message` → one assistant message `{text: "There are 3 Purchase Invoice records you can access (draft). From
your ERPNext data.", source: "erpnext"}` → `renderMessage` with the "From your data" label. No card for a count.

**A failed query ("show territories of invoices").** HTTP 200 as usual. Either the model's plan is refused (an
assistant message such as "I can't filter Sales Invoice records by “territory”."), or an AI failure produces the
`CHAT_ERRORS` sentence. Only a server exception shows the red "The request didn't go through." note with Retry.

**A Draft creation ("create purchase order").**

1. The reply has `question {kind: "form_field", options: [suppliers…, Show more, Create new supplier]}` and
   `pending_id` = its id.
2. Each click sends `{question_id, value, label}`, and the reply contains the next question.
3. The final reply has `card.type = "form_preview"` and a question with "Create draft Purchase Order" / "Cancel".
4. The click returns `{text: "Your Purchase Order draft has been created successfully …", card: {type: "created",
   doctype, name, action_label: "Open Purchase Order"}}`. The button calls `frappe.set_route("Form", doctype, name)`,
   and the panel closes on phones.

**An image upload.** Upload progress → "Ready to send" → send with `file_url` → user message with a thumbnail +
"Thanks. I'm reading the invoice now …", `busy: true` → polling → `card.type = "summary"` + the first question (or
`draft_preview` + confirm) → the same button loop → `created` card.

## Mismatches between the frontend and the backend

| # | Mismatch | Evidence | Impact |
|---|---|---|---|
| 1 | The server accepts one-page PDFs and the HELP text says "attach a document image or a one-page PDF", but the UI refuses PDFs ("PDF files aren't supported yet") | [ai_bot_v2.js:27](../../ai_erpnext/public/js/ai_bot_v2.js#L27), L181, L817 vs [engine.py:43](../../ai_erpnext/chat/engine.py#L43), L1101 | the PDF path ([pipeline/pdf.py](../../ai_erpnext/pipeline/pdf.py)) can't be reached from the chat UI |
| 2 | The source label contains a tab character: `"From your\t data"` | [ai_bot_v2.js:518](../../ai_erpnext/public/js/ai_bot_v2.js#L518) | cosmetic: renders as "From your  data" |
| 3 | `summary.totals.discount` is sent but not rendered | [engine.py:2422](../../ai_erpnext/chat/engine.py#L2422) vs [ai_bot_v2.js:668](../../ai_erpnext/public/js/ai_bot_v2.js#L668) | the printed discount is not shown in the summary card |
| 4 | The busy indicator always says "Reading the invoice" | L482 | wording for orders, quotations, BOMs … |
| 5 | Server errors are unstructured; the UI shows one generic sentence | `call` L341 | "Conversation not found." (a foreign or deleted id) looks the same as an outage |
| 6 | Table columns with `type: "date"` are rendered as raw strings | `renderTable` L566 | none (ISO dates) |
