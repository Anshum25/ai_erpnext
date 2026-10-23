# Routing and pre-queries: every path before (and around) the AI router

The assistant has **one engine, with several nested decision layers**. They are tried in a fixed order, and the
first match handles the message. The model is consulted only at one point: `llm.route` in
[engine.py:557](../../ai_erpnext/chat/engine.py#L557). After that, a few deterministic rules can still override what
it proposed. When the layers "disagree", the earlier layer wins, because a later layer never sees a message an
earlier one handled.

The layers are:

1. Request-level: duplicate delivery, button presses, attachments ([engine.py:249](../../ai_erpnext/chat/engine.py#L249)).
2. Conversation commands and pending-question handling ([engine.py:419](../../ai_erpnext/chat/engine.py#L419) `_on_text`).
3. Page answers, shortcuts and creation commands ([engine.py:531](../../ai_erpnext/chat/engine.py#L531) `_general`, before the model).
4. The router model.
5. Post-model overrides (open record, creation wording, repeated clarification, claims guard).
6. AI-failure fallbacks.

Separately, every question the assistant asks has its own answer parser (options, ordinals, yes/no, quantities).

## Summary table

"Early return" means the handler produces the reply and nothing later runs. "DB" means it reads ERPNext or
conversation data. Writes are called out explicitly.

| # | Path | Trigger condition | Extracted parameters | AI call? | DB access? | Early return? | Fallback when it doesn't match |
|---|---|---|---|---|---|---|---|
| 1 | Duplicate delivery ([engine.py:253](../../ai_erpnext/chat/engine.py#L253)) | `client_id` is in `st.processed` (last 100) | – | no | session row | yes: replays the reply produced the first time | processed normally |
| 2 | Unreadable button ([engine.py:267](../../ai_erpnext/chat/engine.py#L267)) | `action` is not a dict or JSON object (≤ 4000 chars) | – | no | no | yes, and re-asks the pending question | – |
| 3 | Attachment ([engine.py:271](../../ai_erpnext/chat/engine.py#L271), `_attachment` L1079) | `file_url` present | the user's own `File` by URL; extension; size ≤ 10 MB; bytes decoded by PIL; PDF ≤ 10 MB, one page | no (the AI call happens later, in the job) | yes; **writes** a PNG `File` for a PDF and an `AI Document Entry` | yes | an error sentence ("couldn't access that attachment", "upload a JPG, PNG or WEBP …") |
| 3a | Payment caption ([engine.py:56](../../ai_erpnext/chat/engine.py#L56)) | caption matches `PAYMENT_CAPTION_RE` ("pay this", "payment entry") | – | no | no | no: adds "I never record or make payments" and continues | – |
| 3b | Busy with another document ([engine.py:393](../../ai_erpnext/chat/engine.py#L393)) | a form is active, or an image is in `extracting/collecting/failed` | `queued_file` | no | no | yes: asks `replace_operation` | starts the extraction |
| 4 | "Show more" button ([engine.py:286](../../ai_erpnext/chat/engine.py#L286)) | `action.command == "more"` | `last_query` offset + limit (≤ 500) | no | yes | yes | "There's no list to continue." |
| 5 | Suggestion button ([engine.py:289](../../ai_erpnext/chat/engine.py#L289)) | `action.command == "say"` | `value` as text | – | – | no: handled as typed text | – |
| 6 | Question button ([engine.py:409](../../ai_erpnext/chat/engine.py#L409)) | `action.question_id` | `value` (exact option value) | no | yes | yes | stale id → "That question is no longer active." + re-ask |
| 7 | Cancel ([engine.py:60](../../ai_erpnext/chat/engine.py#L60), `_cancel` L1178) | text **starts with** cancel, stop, abort, discard, never mind or forget it | – | no | no | yes: stops a lookup, a form (`forms.cancel`) or an image flow; otherwise "nothing in progress" | – |
| 8 | Start over ([engine.py:61](../../ai_erpnext/chat/engine.py#L61), `_restart` L1195) | starts with start over/again/from scratch, restart, begin again or reset | – | no | image: re-maps the entry | yes | – |
| 9 | Help ([engine.py:64](../../ai_erpnext/chat/engine.py#L64)) | the whole message is `help` or `?` | – | no | no | yes, and re-asks the pending question | – |
| 10 | Retry an image ([engine.py:427](../../ai_erpnext/chat/engine.py#L427)) | starts with retry, try again, recheck or refresh, **and** the conversation has an image entry | – | no (re-queues the job) | **writes** the entry → Pending | yes | goes on (a form's `form_confirm` accepts "retry" itself) |
| 11 | Why / status of an image ([engine.py:74](../../ai_erpnext/chat/engine.py#L74)) | starts with why, what's wrong, show errors or **status** while an image is in progress | – | no | yes | yes: lists validation errors and re-asks | – |
| 12 | Master correction, image flow ([engine.py:432](../../ai_erpnext/chat/engine.py#L432)) | a new customer/supplier/item draft is open and `FIELD_CORRECTION_RE` names a master field (customer name, group, item code, uom …) | field, value | no | yes (link checks) | yes | continues |
| 13 | Row and field correction, image flow ([engine.py:436](../../ai_erpnext/chat/engine.py#L436)) | `phase == collecting`: "row 2 item X", "change customer to X", "set posting date to …" | row number, field (mapped to the document's own fieldname by `_correction_key`), value | no | yes | yes | "This document has no X I can change." |
| 14 | Form switch ([forms.py:994](../../ai_erpnext/chat/forms.py#L994)) | form active and "I meant Purchase Receipt" / "Purchase Receipt, not Purchase Order" | new DocType; keeps party, company, warehouse, dates and item rows that still apply | no | yes | yes | – |
| 15 | Form correction ([forms.py:1142](../../ai_erpnext/chat/forms.py#L1142)) | form active: "remove row 2", "add item", "row 1 qty 5", "use Stores warehouse", "change quantity to 5", "change <label> to <value>"; generic rows (expense, time log, JE line) via `_row_correction` | row, field (by label or alias), value | no | yes | yes; a field the form doesn't have → "I can't change X here. You can change: …" | continues |
| 16 | Pending lookup question ([engine.py:460](../../ai_erpnext/chat/engine.py#L460)) | `pending.kind` starts with `query_` | reply = option label/value, ordinal ("2", "the second one"), "customer X", or a name/ID | no | yes | yes: `_answer_query` | if `_is_new_request` (has "?", > 8 words, a general/create/action cue, a shortcut or a capability question) the pending lookup is dropped and the message is routed fresh |
| 17 | Other pending question ([engine.py:466](../../ai_erpnext/chat/engine.py#L466)) | a form, image or master question is pending | reply as an answer | no | yes | yes: `_answer` | if it looks like a separate request (`GENERAL_CUE_RE`, `CREATE_DOC_RE`, `APPLY_RE`, `field_question`, or a page question), it is answered by `_general`, **then the pending question is asked again** |
| 18 | Page question ([page_context.py:508](../../ai_erpnext/chat/page_context.py#L508) `classify`) | a valid page context and a short message (≤ 14 words, ≤ 200 chars) such as "explain this invoice", "what did I change", "summarise the selected orders", "what filters are on this list", "explain this report", "what is this page about" | the open record, list filters/selection or report filters (re-validated on the server) | no | yes (permission-aware) | yes | continues to shortcuts |
| 19 | Greeting / thanks ([shortcuts.py:31](../../ai_erpnext/chat/shortcuts.py#L31)) | the whole message is a greeting or thanks | – | no | no | yes: fixed reply | – |
| 20 | More / open N ([shortcuts.py:39](../../ai_erpnext/chat/shortcuts.py#L39)) | "more", "next page"; "open the 2nd one", "open last", **only when** a previous result exists | index | no | yes | yes | goes to the model |
| 21 | HR read phrasings ([hr.py:702](../../ai_erpnext/chat/hr.py#L702)) | "(my) (<type>) leave balance (for X) (for <period>)", "how many (<type>) leaves do I have left", "X's leave balance", "(my) attendance (for X) (for <period>)", "my employee details", "find/who is employee X" | `self` or `search`, `leave_type` (1–2 words before "leave"), `date` (period presets, month names, "Month YYYY") | no | yes, at execution | yes | other HR wording goes to the model |
| 22 | Returns ([shortcuts.py:250](../../ai_erpnext/chat/shortcuts.py#L250)) | "show credit notes", "how many debit notes", "sales returns" | DocType, `is_return = 1`, count or list | no | yes | yes | – |
| 23 | Compare periods ([shortcuts.py:92](../../ai_erpnext/chat/shortcuts.py#L92)) | "compare sales this month with last month" (both periods from a fixed list) | DocType, two presets | no | yes | yes | – |
| 24 | Re-order ([shortcuts.py:86](../../ai_erpnext/chat/shortcuts.py#L86)) | "items below re-order level", "items needing reorder" (≤ 200 chars) | – | no | yes | yes | – |
| 25 | Count ([shortcuts.py:100](../../ai_erpnext/chat/shortcuts.py#L100)) | "how many (draft/submitted/cancelled) (unpaid/overdue) <doctype> (are there)" | DocType, docstatus, payment | no | yes | yes | – |
| 26 | Party summary ([shortcuts.py:204](../../ai_erpnext/chat/shortcuts.py#L204)) | "summary of customer X", "summarise supplier X" | kind, search | no | yes | yes | – |
| 27 | Field question ([shortcuts.py:333](../../ai_erpnext/chat/shortcuts.py#L333)) | "<field> of <target>", "<target>'s <field>", "which items are in <target>", "who is the supplier for <target>", "details of <target>" | concept (mobile, email, outstanding, items, taxes, status, dates …), target (a record name, an ID, or "this/it" → `ref`), an optional DocType prefix | no | yes | yes | "status of my orders" and similar plurals go to the model |
| 28 | Plain list ([shortcuts.py:76](../../ai_erpnext/chat/shortcuts.py#L76)) | must start with a verb or "last/latest…": "show (my) (last N) (draft/overdue) (open/…) <doctype>" | DocType, `mine`, sort, limit ≤ 99 (capped to 20 later), docstatus, status | no | yes | yes | a bare "sales invoices" goes to the model |
| 29 | Create command, nothing else ([engine.py:542](../../ai_erpnext/chat/engine.py#L542)) | **starts with** create/make/raise/add/new/prepare/draft/generate/start + a creatable name or alias (po, so, si, grn, je …), or "apply for leave", with nothing after it | DocType | no | yes (form start) | yes: guided form with no prefill | goes to the model |
| 30 | Create for the page ([engine.py:704](../../ai_erpnext/chat/engine.py#L704)) | the create command followed only by "for this customer/supplier" or "for these items" | `party = open_record`, `items = selected_records`, resolved from the validated page | no | yes | yes | goes to the model |
| 31 | **Router model** ([llm.py:242](../../ai_erpnext/chat/llm.py#L242)) | everything else | a plan: mode + query/values/items/reply | **yes** | no records are sent | – | see rows 37–41 |
| 32 | Open-record override ([engine.py:584](../../ai_erpnext/chat/engine.py#L584)) | the model answered `chat`, but the message asks what the open form contains (`asks_for_open_record`) | – | already made | yes | yes: the record card replaces the model reply | the model reply |
| 33 | Creation clarify override ([engine.py:595](../../ai_erpnext/chat/engine.py#L595)) | the model answered `clarify`, but `CREATE_ANY_RE` finds a creatable name anywhere in the message | DocType | already made | yes | yes: guided form | the model's question |
| 34 | Repeated clarify ([engine.py:607](../../ai_erpnext/chat/engine.py#L607)) | the model asks the same clarifying question again | – | already made | no | yes: fixed "I won't ask the same question again" | – |
| 35 | Claims guard ([claims.py:110](../../ai_erpnext/chat/claims.py#L110)) | model text claims an action, states counts or amounts the user didn't type, names a document ID, or claims a report result | – | – | no | replaces the text with a fixed sentence | text shown as written |
| 36 | Capability footer ([engine.py:591](../../ai_erpnext/chat/engine.py#L591)) | the `chat` answer is a "how do I / what is / what happens after" question about a known DocType | DocType | – | no | appends what the chat can do for it | – |
| 37 | AI down: page ([engine.py:562](../../ai_erpnext/chat/engine.py#L562)) | provider error, and a short message mentioning "page" or "screen" | – | failed | yes | yes | next fallback |
| 38 | AI down: create ([engine.py:566](../../ai_erpnext/chat/engine.py#L566)) | provider error, and the message **starts with** a create command (with anything after it) | DocType only; **everything else in the message is dropped** | failed | yes | yes: form with no prefill | next fallback |
| 39 | AI down: leave ([engine.py:568](../../ai_erpnext/chat/engine.py#L568)) | provider error, `LEAVE_REQUEST_RE` (apply/request/take/book … leave), no "?", not a capability question | – (dates, type and days are dropped) | failed | yes | yes: Leave Application form | next fallback |
| 40 | AI down: guidance ([engine.py:572](../../ai_erpnext/chat/engine.py#L572)) | provider error, and `capabilities.answer` recognises "what is a X", "how do I create a X", "what happens after I submit a X", "X vs Y", "what does the selling module do" | DocType or module | failed | no | yes: built-in text, labelled "Built-in guidance" | next fallback |
| 41 | AI down: error ([engine.py:824](../../ai_erpnext/chat/engine.py#L824)) | anything else | – | failed | no | yes: a category sentence ("AI request limit reached …") | – |

Two guards apply inside the paths above. The answer parsers are described in the next section.

| # | Path | Trigger | Effect |
|---|---|---|---|
| 42 | Busy guard ([engine.py:661](../../ai_erpnext/chat/engine.py#L661) `_start_form`) | a create request while a form or image is open | "Please finish or cancel the current X first, or say “I meant X”", then re-asks |
| 43 | Loop guard ([engine.py:203](../../ai_erpnext/chat/engine.py#L203) `Ctx.ask`) | the same question (kind, field, wording) asked 3 times in a row right after the user answered it | the question is replaced by a recovery text with a Cancel option; nothing is discarded |

## Answer parsers (when a question is pending)

| Parser | Where | Accepts | Notes |
|---|---|---|---|
| `_option_value` | [engine.py:1806](../../ai_erpnext/chat/engine.py#L1806) | an option's value or label (exact, case-insensitive), the name inside "use X" / "it's X", or an ordinal | used for both image and form questions |
| `ordinal_option` | [engine.py:963](../../ai_erpnext/chat/engine.py#L963) | "2", "the second one", "no, use the third customer", "last" | disabled where a number is a value (`LITERAL_ANSWERS`: qty, rate, item code, bill no, names) |
| `YES_RE` / `NO_RE` | [engine.py:78](../../ai_erpnext/chat/engine.py#L78), applied at L1846 | image-flow `confirm`, `data_errors`, `duplicate`, `master_confirm`, `use_existing`, `replace_operation` | **prefix match**: "ok, but …" counts as yes and "no problem, go ahead" counts as no. See [security-review.md](security-review.md#f2-medium-a-confirmation-with-a-correction-creates-the-record-unchanged) |
| `ROLE_RE` | [engine.py:1819](../../ai_erpnext/chat/engine.py#L1819) | "we are the seller/buyer/vendor/customer" | image-flow `role` question |
| `forms.answer` confirm | [forms.py:1128](../../ai_erpnext/chat/forms.py#L1128) | button `__create__`/`__retry__`, or text starting with yes, y, ok, okay, confirm, create, go ahead, do it, sure or retry | same prefix behaviour as `YES_RE` |
| `forms.answer` cancel | [forms.py:1085](../../ai_erpnext/chat/forms.py#L1085) | `__cancel__`, or exactly cancel/stop/abort/no at the confirm step | a nested master form pops back to its parent |
| more items | [forms.py:1116](../../ai_erpnext/chat/forms.py#L1116) | yes/add/another/more vs no/done/that's all/finish/nothing, **anywhere** in the text | "no more" contains "more" → treated as "add another" (`yes` is checked first) |
| `answer_term` | [forms.py:862](../../ai_erpnext/chat/forms.py#L862) | strips "use", "it's", "I meant", "the one called" and similar | link fields |
| `QTY_AND_RATE_RE` | [forms.py:855](../../ai_erpnext/chat/forms.py#L855) | "5 at 20", "5 x 20", "qty 5 rate 20" | quantity question only |
| `_set` | [forms.py:1596](../../ai_erpnext/chat/forms.py#L1596) | data, date (today/tomorrow/yesterday, "25 October" + current year), number, datetime, select, check, link | each type validated; links re-resolved |

## Probe: what the deterministic layer does with real phrases

Run on `ci-test.local` (HRMS installed) with
[the probe script](test-coverage.md#read-only-probes-run-for-this-audit). These are actual outputs of
`shortcuts.match()` and the creation regexes. **[probe]**

| Message | Deterministic result | Goes to the model? |
|---|---|---|
| Show my leave balance | `leave_balance`, `filters.self = true` | no |
| Show leave balance for Anshum | `leave_balance`, `search = "Anshum"` | no |
| Show casual leave balance for Anshum **for 2026** | `leave_balance`, `leave_type = "casual"`, **`search = "Anshum for 2026"`**, no date | no (**defect**) |
| Show casual leave balance for Anshum **as of 25 October** | `leave_type = "casual"`, **`search = "Anshum as of 25 October"`**, no date | no (**defect**) |
| show casual leave balance for Anshum for October | `search = "Anshum"`, `leave_type = "casual"`, date 2026-10-01..2026-10-31 | no |
| how many leaves do I have left | `leave_balance`, `self = true` | no |
| Create a Sales Order | create-only → guided Sales Order form | no |
| Create a Sales Order for Customer ABC (…with five units of Item XYZ …in Warehouse A) | `CREATE_DOC_RE` matches, but text follows it | **yes** |
| create sales ordr / create a slaes order for ABC | nothing matches (no fuzzy DocType matching) | **yes** (and with the AI down: an error, no form) |
| show the last 10 sales invoices and create a sales order | no shortcut (`LIST_RE` needs the DocType at the end); `CREATE_ANY_RE` finds "sales order" | **yes**; if the model answers `clarify`, row 33 turns it into a Sales Order form |
| how many draft purchase invoices | `count`, Purchase Invoice, `docstatus = draft` | no |
| show overdue sales invoices | operation `overdue`, sort newest | no |
| what is the mobile number of Amit Patel? | `details`, `doctype = auto`, `search = "Amit Patel"`, `fields = ["mobile"]` | no |
| compare sales this month with last month | `compare`, presets this_month / last_month | no |
| items below reorder level | `Bin.below_reorder` | no |
| hi / thanks | fixed replies | no |
| apply for leave | create-only → Leave Application form | no |
| apply for two days of casual leave tomorrow | nothing | **yes** (AI down → empty Leave Application form, row 39) |
| explain what a sales invoice is | nothing (not even the offline guidance: `WHAT_RE` expects "explain a sales invoice") | **yes**; with the AI down it gets the error sentence, although the HELP text uses this exact example |

### Why "for 2026" and "as of 25 October" break the HR shortcut

`LEAVE_BALANCE_RE` ([hr.py:555](../../ai_erpnext/chat/hr.py#L555)) captures `for (?P<target>.+?)` followed by an
optional `_TAIL` period ([hr.py:552](../../ai_erpnext/chat/hr.py#L552)). The period alternatives are relative presets
and month names with an optional year. A bare year ("2026") and "as of <date>" are not among them. The lazy target
therefore expands to the end of the message, and the regex still matches. `_split_period` only helps when the
*whole* target is a period. The plan goes straight to `queries.validate`, and the model never sees the message.
**[probe]**

Then `resolve_record("Employee", "Anshum for 2026")` ([queries.py:1225](../../ai_erpnext/chat/queries.py#L1225))
requires every typed word to prefix a word of the name, ID or `employee_name`. "for" and "2026" cannot match, so the
result is "I couldn't find an employee matching “Anshum for 2026” that you can access". The user is asked to retype
the name at most twice (`MAX_QUERY_ATTEMPTS`, [engine.py:179](../../ai_erpnext/chat/engine.py#L179)). **[code]**
(`ci-test.local` has no Employee records, so this last step was not executed.)

## Edge cases worth knowing

- `CANCEL_RE` is a prefix match. "Stop. Show me last month's invoices" cancels the pending work and nothing else
  happens. **[code]**
- `WHY_RE` includes a leading "status". While an image flow is in progress, "status of SO-0001" explains the image's
  validation problems instead of looking up the order. **[code]**
- The deterministic creation command is anchored at the start of the message. "Can you create a sales order?" goes to
  the model, and with the AI down it gets an error sentence, not a form. **[code]**
- `GENERAL_CUE_RE` ([engine.py:66](../../ai_erpnext/chat/engine.py#L66)) decides whether a reply to a pending
  question is an answer or a new request. A customer called "Show Stoppers Ltd" typed as an answer to "Which
  customer?" is treated as a new request (it starts with "show"). The question is then asked again, and the user can
  pick the name from the buttons. **[code]**
- Shortcuts never look at conversation state except `last_query` (for "more" and "open N"). An HR phrasing typed while
  a form is open is answered (row 17), and then the form question is asked again. **[code]**
