# Parameters and context: where values come from, and what happens to them

## Where a parameter can come from

| Source | How it enters | Trusted? | Where it is checked |
|---|---|---|---|
| The user's words, matched by regex | `shortcuts.py`, `hr.match`, engine and form regexes | as a search term only | resolved against ERPNext with permission-aware queries |
| The router model's plan | `llm.route` → `plan["query"]`, `plan["values"]`, `plan["items"]` | **no**: shapes only ([llm.py:276](../../ai_erpnext/chat/llm.py#L276)) | `queries.validate` / `forms.start` → `_resolve_link`, `_set` |
| A button the user pressed | `action.value` = an exact record name or a `__token__` | as a name only | re-checked with `get_list` (+ the field's filters) before use ([forms.py:1768](../../ai_erpnext/chat/forms.py#L1768)) |
| Conversation state | `st.forms[-1].values`, `st.last_query.plan`, `st.pending.plan` | re-validated | `queries._recheck` ([queries.py:620](../../ai_erpnext/chat/queries.py#L620)); `forms._requirements` re-checks every link before preview and create ([forms.py:2012](../../ai_erpnext/chat/forms.py#L2012)) |
| The open Desk page | browser `context` | a hint | `page_context.parse` re-reads the record, filters and selection ([page_context.py:202](../../ai_erpnext/chat/page_context.py#L202)) |
| ERPNext defaults | `_default` ([forms.py:1439](../../ai_erpnext/chat/forms.py#L1439)), `requirements.inspect` (ERPNext's `set_missing_values` on a copy), HRMS `get_employee_leave_approver` | yes (the server computes them) | shown in the preview |
| An image | the vision model's JSON (`extracted_data`) | **no** | `detection/direction.py`, the mappers and the validators |

## Normalisation helpers ([mapping/normalize.py](../../ai_erpnext/mapping/normalize.py))

| Helper | Behaviour (probe results on `ci-test.local` **[probe]**) |
|---|---|
| `clean_text(v, n)` | strips and collapses whitespace, truncates to `n` characters; chat text to 2000, names to 140 |
| `parse_number` | `"5"` → 5, `"5 units"` → 5, `"1,200.50"` → 1200.5, **`"five"` → None**, **`"five units"` → None** (no number words) |
| `parse_date` | `"2026-10-25"` and `"25 October 2026"` → ok; **`"25 October"` → not ok** (no year); `"10/11/2026"` → ambiguous (refused); `"tomorrow"` → not ok (handled separately by `forms._set`) |
| `forms._set` date | adds today/tomorrow/yesterday, and appends the current year to a date without one ("25 October" → 2026-10-25) ([forms.py:1615](../../ai_erpnext/chat/forms.py#L1615)). The query engine does **not** do this |
| `normalize_name` | lower case, punctuation and spacing folded; used for names, leave types and DocType words |
| `hr._period_plan` | presets ("this month", "last quarter", "today" …), month names, "Month YYYY". **A bare year, "as of <date>" and date ranges return None** **[probe]** |

## The requested examples, traced

The mode the router model chooses cannot be established without calling it, and no paid calls were made for this
audit. Where a path depends on the model, the doc describes what the code does with each plausible plan and marks it
**[unverified]**. Confirming it needs a recorded model response, or a test with a scripted provider.

### "Show my leave balance"

1. `shortcuts.match` → `hr.match` → `LEAVE_BALANCE_RE`: `{doctype: Employee, operation: leave_balance, filters: {self: true}}`. No AI call. **[probe]**
2. `queries.validate` ([queries.py:163](../../ai_erpnext/chat/queries.py#L163)) needs DocType-level read permission on
   Employee (L185), else "You don't have permission to view Employee records." `hr.validate` (L125) sets `self = True`,
   `leave_type = None`, `date = None` (no default period for balances), and **clears** `where`, `aggregate`, `child`,
   `link` and `fields`.
3. `hr.leave_balance` ([hr.py:206](../../ai_erpnext/chat/hr.py#L206)) → `_target` → `self_employee`. That finds the
   Employees with `user_id = session user`, via `get_list` limited to the user's companies. 0 → "Your user account
   isn't linked to an Employee record …"; > 1 active → a pick question; exactly 1 → used.
4. `as_of` = today. HRMS `get_leave_details(employee, today)` re-checks access: the employee themself, their leave
   approver or Employee read permission.
5. Answer: one sentence for a single leave type, otherwise a table with allocated / taken / pending / expired /
   remaining per type. All figures come from HRMS.

Values: the employee from the session user, the date from the server clock, the figures from HRMS. **[code]**, and
**[test]** `test_chat_hr.test_leave_balance_follows_hrms_and_excludes_pending_and_rejected`.

### "Show leave balance for Anshum"

Shortcut → `filters.search = "Anshum"`. `_target` → `queries._resolve_target` → `resolve_record("Employee",
"Anshum", scope)` ([queries.py:1225](../../ai_erpnext/chat/queries.py#L1225)). It tries an exact ID, then an exact
`employee_name`, then a normalised equal name, then every typed word as a prefix of a word of the name. A misspelling
never matches.

- 0 matches → "I couldn't find an employee matching “Anshum” that you can access." The user is asked to retype it, at
  most 2 times (`query_target`).
- 1 match → used.
- Several → "I found N records matching “Anshum” …" with buttons (`Employee::<id>`), never a silent pick
  (**[test]** `test_an_ambiguous_employee_name_is_asked_never_picked`).

The scope is the user's permitted companies. There is **no** employee-level scope on Employee itself: what decides
access to another employee's balance is the Employee read permission in step 2 plus HRMS `validate_leave_access`.
See [security-review.md](security-review.md#f6-low-to-medium-employee-lookups-depend-on-the-sites-employee-read-permission).

### "Show casual leave balance for Anshum for 2026"

**Defect, [probe].** The shortcut returns `leave_type = "casual"` and `search = "Anshum for 2026"`, with no date
(see [routing-and-prequeries.md](routing-and-prequeries.md#why-for-2026-and-as-of-25-october-break-the-hr-shortcut)).
Resolution then fails, and the user is asked to retype the employee's name. The model is never consulted.

If the phrase reached the model, the prompt asks for `leave_type` and a `date` for "a period". A plan with
`date: {from: 2026-01-01, to: 2026-12-31}` would make `_as_of` ([hr.py:177](../../ai_erpnext/chat/hr.py#L177))
report the balance **as of today**, because today lies inside the period. The year selects nothing more. **[code]**

`leave_type` matching ([hr.py:194](../../ai_erpnext/chat/hr.py#L194)): exact name first, then every word except
"leave/day/holiday" must appear in the leave type's name ("casual" → "Casual Leave"). No match → QueryError listing the
leave types the user can see. Several → "matches several leave types … Please name one."

### "Show casual leave balance for Anshum as of 25 October"

**Same defect, [probe]**: `search = "Anshum as of 25 October"`. If the date reached the query engine through the model,
`hr.period` parses `from`/`to` with `parse_date`, which rejects "25 October" without a year. The model is told to
write YYYY-MM-DD, so it would have to add the year itself **[unverified]**. With `2026-10-25`, `_as_of` returns
2026-10-25: a future date, the start of the period. **[code]**

### "Create a Sales Order"

1. `CREATE_DOC_RE` matches with nothing after it → `{mode: create, doctype: "Sales Order"}`. No AI call. **[probe]**
2. `_start_form` refuses if another form or image is open. Otherwise `forms.start`
   ([forms.py:902](../../ai_erpnext/chat/forms.py#L902)) checks `has_permission("Sales Order", "create")` and fills
   the defaults:
   - `company`: the user default if usable, else the global default if usable, else the only usable company,
     otherwise asked;
   - `transaction_date`: today.
3. `next_step` asks the header fields in spec order ([forms.py:132](../../ai_erpnext/chat/forms.py#L132)):
   - **customer**: up to 10 existing customers (`modified desc`), "Show more", and "Create new customer" if the user may;
   - company, only if not defaulted;
   - **delivery date** (required).

   `set_warehouse` is never asked up front.
4. Items: item (sales items only) → quantity → rate. The rate question offers the newest `Item Price` with
   `selling = 1`, if the user can read Item Price. Then "Add another item?".
5. `_requirements`:
   - every link is re-checked;
   - ERPNext's own `set_missing_values` runs on a copy to find each stock row's default warehouse; a row without a
     usable default gets a warehouse question;
   - mandatory fields ERPNext still reports (including site custom fields) are asked.
6. Preview card, then "Create draft Sales Order" → `_create` → `insert()` as Draft. See
   [document-creation.md](document-creation.md).

### "Create a Sales Order for Customer ABC"

Text follows the command, so the model decides **[unverified]**. The prompt's create schema is
`{"mode":"create","doctype":"","values":{"party":"","posting_date":""},"items":[…]}`
([llm.py:222](../../ai_erpnext/chat/llm.py#L222)) and says "only values the user stated".

- With `values.party = "ABC"`: `forms.start` maps `party` to the spec's party field (`customer`) and resolves it with
  `CustomerMatcher` (L942).
  - matched → set silently and shown in the preview;
  - ambiguous → "Several customer records match “ABC”. Which one?";
  - not found → "I couldn't find a customer called “ABC”. Pick an existing one, type another name, or create “ABC” as
    a new customer." **A name that matches nothing is never created automatically** (**[test]**
    `test_chat_guided.test_unknown_name_is_never_created_automatically`).
- If the model copies "Customer ABC" as the party, the matcher searches for "Customer ABC" and probably asks.
  **[unverified]**
- **AI service down:** fallback row 38 starts the form **without any prefill**. "ABC" is dropped, and the form asks
  for the customer from the start. **[code]**

### "… with five units of Item XYZ"

The model is expected to send `items: [{item: "XYZ", qty: 5}]`. `forms.start` queues up to 20 items, reading only
`item`, `qty` and `rate` (L958). `parse_number` turns a numeric qty into a float. If the model passes the word "five",
it becomes `None`, and the quantity is asked later ("How many of XYZ?"). The item is resolved when the item step is
reached (`_items_step`, L1809):

- exactly one sales item → used;
- several → "Several items match “XYZ”";
- a non-sales item → "Item XYZ is not a sales item.", then asked again;
- none → "I couldn't find the item “XYZ”. Pick one, search again, or create it."

### "… in Warehouse A"

The create schema has no warehouse key **[code]**. What the code does with each plausible plan:

- `values.set_warehouse = "Warehouse A"`: it is a field of the Sales Order form, so it is resolved by name or
  `warehouse_name` within the form's company, and used for every stock row.
- `values.warehouse` or a per-item `warehouse`: **dropped silently**. `_field(doctype, "warehouse")` is `None`
  (L936–938), and item dicts keep only item/qty/rate.
- Once the form is open, "use Warehouse A warehouse" / "set warehouse to Warehouse A" is understood
  (`WAREHOUSE_RE`, L842).

### A request with an invalid item name

As above: a question with existing items and an explicit "Create new item" (when the user may create Items). The
item text is never stored as an item code. **[test]** `test_chat_forms.test_partial_name_is_only_a_suggestion`.

### A misspelt DocType ("create sales ordr")

No deterministic match: neither the regexes nor `forms.match_doctype` do fuzzy matching (**[probe]**:
`match_doctype("sales ordr") → None`). The model may normalise it to "Sales Order" and the form starts
**[unverified]**. If it returns "Sales Ordr", the reply is `forms.not_creatable` ("I can't create Sales Ordr records
from the chat …"). With the AI down, the message gets the generic error sentence. A misspelt DocType in a *query* plan
is refused by `queries._match_doctype` ("I can't look up “Sales Ordr” yet. I can search: …") **[probe]**.

### A message that mixes a read and a creation

"show the last 10 sales invoices and create a sales order" gets no shortcut and goes to the model **[probe]**. The
router returns **one** plan (one mode), so one half of the request is **dropped without a message**. The exception is
a `clarify` answer: `CREATE_ANY_RE` finds "sales order", and the creation form starts (row 33). **[code]**

### A follow-up that corrects one parameter

| Situation | What handles "change customer to ABC" / "use 7 instead" | Result |
|---|---|---|
| Guided form open | `forms.correction` generic rule ([forms.py:1220](../../ai_erpnext/chat/forms.py#L1220)) → `_field_by_label` → `_set` | the new value replaces the old one, after resolution. A company change clears the header and row warehouses (L1673). Then the current question or the preview is shown again |
| Guided form, item rows | "row 2 qty 5", "change quantity to 5" (applies to the item being added, or to the only row; otherwise asks which row) | row updated |
| Image flow | `FIELD_CORRECTION_RE` / `ROW_CORRECTION_RE` ([engine.py:122](../../ai_erpnext/chat/engine.py#L122)) → `_set_master` / `_set_date` → `set_overrides` → re-map | an override stored on the entry, the document re-validated |
| After a query ("only the unpaid ones", "for last month instead") | the model, which receives `previous_query` (the validated plan) and is told to "change only what was asked" ([llm.py:219](../../ai_erpnext/chat/llm.py#L219)) | **[unverified]**: depends on the model. The new plan is validated like any other |
| A field the form doesn't have ("change territory to North" on a Sales Order) | `forms.correction` (L1234) | "I can't change “territory” on a Sales Order here. You can change: …" |

## Conversation context that persists

All of it lives in `AI Chat Session.state` (JSON). See [session.py:151](../../ai_erpnext/chat/session.py#L151) for
the key list.

| Key | Written by | Read by | Notes |
|---|---|---|---|
| `pending` | `Ctx.ask` | `_on_text`, `_on_action`, the payload's `pending_id` | one open question: `{id, kind, key, options, input}`; lookups add `plan`, `attempts` |
| `forms` | `forms.start` | `forms.*` | a stack: the outer document first, then a nested master (create a customer inside an invoice) |
| `last_query` | `_run_query` | shortcuts ("more", "open N"), router context, `_resolve_ref` | `{plan, names, titles, doctype}`. The model only receives `plan` (names stripped from page-selection filters) and the row **count** ([llm.py:306](../../ai_erpnext/chat/llm.py#L306)) |
| `last_created`, `created` | `forms._create`, `_create_document` | `_resolve_ref` ("this purchase order") | |
| `entry`, `phase`, `acks`, `master`, `queued_file`, `requeued` | the image flow | the image flow | |
| `processed`, `replies` | `handle_message` | duplicate detection | the last 100 client ids |
| `ask_sig` | `Ctx.ask` | the loop guard | |
| `metrics` | `_count_route` | none (diagnostics) | `model_routes`, `shortcut_routes` |

**Conflicts between old context and new input.** New input always wins for the field it names. A new create request
while a form is open is refused ("finish or cancel first"), unless it is an explicit "I meant X", which switches and
keeps the compatible values. A new image while busy asks before replacing. A new *query* while a lookup question is
pending replaces the lookup. A new query while a form question is pending is answered, and then the form question is
asked again.

## Places where a default is chosen silently

"Silently" means without asking. Most of these values are visible in the preview card or in the result card's note.

| Default | Where | Visible? |
|---|---|---|
| Company: user default / global default / the only usable company | [forms.py:1443](../../ai_erpnext/chat/forms.py#L1443) | preview |
| Posting / transaction date = today; `set_posting_time = 1` when another date is chosen | `F(default="today")`, [forms.py:2520](../../ai_erpnext/chat/forms.py#L2520) | preview |
| Customer/Supplier type "Company"; Item `is_sales_item = is_purchase_item = 1`; `item_name = item_code`; `stock_uom` from Stock Settings | `SPECS` | preview |
| Employee = the user's own active employee (Leave Application, Expense Claim, Timesheet) when exactly one exists | [hr.py:74](../../ai_erpnext/chat/hr.py#L74) | preview |
| Leave approver, expense approver and Work Order BOM taken from the employee/item | [forms.py:2396](../../ai_erpnext/chat/forms.py#L2396) | preview |
| Row warehouse = ERPNext's default for the item/company | `requirements.inspect` | preview, marked "ERPNext default" |
| Quotation to Customer; Opportunity from Customer; Employee status Active; JE voucher type | `SPECS[…]["fixed"]` | preview (partly) |
| Query sort = newest; limit = 10 (max 20); cancelled documents excluded; all permitted companies | `queries.validate`, `_base_filters` | result note (the limit isn't stated) |
| Unknown `sort` → newest; `limit` > 20 → 20 | [queries.py:204](../../ai_erpnext/chat/queries.py#L204) **[probe]** | no |
| Attendance period = this month; leave balance as of today | [hr.py:136](../../ai_erpnext/chat/hr.py#L136), [hr.py:177](../../ai_erpnext/chat/hr.py#L177) | answer text |
| Image drafts: `ignore_pricing_rule = 1`, `update_stock = 0`, BOM inactive and not default, a remarks line | [creation/transaction.py](../../ai_erpnext/creation/transaction.py) | not in the preview |

## Places where user-provided information is dropped silently

| What is dropped | Where | Evidence |
|---|---|---|
| A period the HR shortcut doesn't know (a year, "as of …"), merged into the employee name | [hr.py:555](../../ai_erpnext/chat/hr.py#L555) | **[probe]** |
| **Unknown keys in a query plan's `filters`** (e.g. `filters.territory`): ignored, so the query is **wider** than asked. The same condition under `where` is refused | [queries.py:192–213](../../ai_erpnext/chat/queries.py#L192) | **[probe]** |
| `where`, `aggregate`, `child`, `link` and `fields` on a leave balance / attendance plan | [hr.py:137](../../ai_erpnext/chat/hr.py#L137) | **[probe]** |
| `party`, `search`, `status` and `date` on `outstanding_by_customer` / `outstanding_by_supplier`: validated and kept in the plan, then ignored by the executor | [queries.py:1995](../../ai_erpnext/chat/queries.py#L1995) | **[probe]** + **[code]** |
| `status` on `sales_summary` | [queries.py:1925](../../ai_erpnext/chat/queries.py#L1925) | **[code]** |
| Create-plan `values` whose key is not a field of the form; per-item keys other than item/qty/rate; items after the 20th | [forms.py:935–966](../../ai_erpnext/chat/forms.py#L935) | **[code]** |
| Every prefill when the AI service is down (only the DocType survives) | [engine.py:566](../../ai_erpnext/chat/engine.py#L566) | **[code]** |
| Half of a mixed read + create message | `llm.route` (one mode) | **[code]** |
| A correction typed together with a confirmation ("ok, but change the date") | [forms.py:1128](../../ai_erpnext/chat/forms.py#L1128), [engine.py:1856](../../ai_erpnext/chat/engine.py#L1856) | **[code]** |
| Text beyond 2000 characters; history beyond 8 turns × 200 characters for the model | [engine.py:34](../../ai_erpnext/chat/engine.py#L34), [llm.py:28](../../ai_erpnext/chat/llm.py#L28) | **[code]** |
| Model plan keys of the wrong type (`query` not a dict, `items` not a list …) → `None` | [llm.py:278](../../ai_erpnext/chat/llm.py#L278) | **[code]** |

## Unexpected input: ignored, rejected or passed on

| Input | Treatment |
|---|---|
| Unknown DocType (query) | rejected, with the list of queryable DocTypes |
| Unknown DocType (create) | rejected, with the list of creatable DocTypes |
| Field the user can't read, or a prohibited field (password, token, salary, IBAN …) | rejected, or reported as "isn't available to your account" |
| A `where` field outside the allowlist, a wrong operator, a badly typed value | rejected (`normalize_where`) |
| A status word that is not a status of the DocType | rejected, with the real statuses **[probe]** |
| A lifecycle verb as the operation (delete, submit, sql, run …) | rejected: "I can only read data …" **[probe]** |
| Unknown `filters` key | **ignored** (widens the query, see above) |
| A non-numeric quantity/rate in a form | rejected with a sentence; the question is asked again |
| An ambiguous date ("10/11/2026") | rejected: "Please use YYYY-MM-DD" |
| SQL-looking text in a value | passed as a bound parameter to `get_list`. In LIKE patterns, `%` is stripped for `where` values but not for `search`/`party`; it is never executable **[probe]** |
