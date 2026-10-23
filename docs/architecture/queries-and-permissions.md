# Queries and permissions: read-only data access

Everything the assistant tells a user about their ERPNext data comes from
[chat/queries.py](../../ai_erpnext/chat/queries.py), [chat/hr.py](../../ai_erpnext/chat/hr.py) or the page answers in
[chat/page_context.py](../../ai_erpnext/chat/page_context.py). In all three, the data is read with Frappe's
permission-aware `frappe.get_list` / `get_doc(...).check_permission`. **No SQL is written or executed by this app.**
A grep of the application code (tests excluded) finds no `frappe.db.sql`. **[code]**

## From words to a plan

There are two producers of a query plan, and the same validator handles both:

| Producer | Example | Plan |
|---|---|---|
| Deterministic shortcut ([shortcuts.py](../../ai_erpnext/chat/shortcuts.py), [hr.py:702](../../ai_erpnext/chat/hr.py#L702)) | "how many draft purchase invoices" | `{doctype: "Purchase Invoice", operation: "count", filters: {docstatus: "draft"}}` **[probe]** |
| Router model ([llm.py:206](../../ai_erpnext/chat/llm.py#L206)) | "unpaid invoices of Acme over 10,000 this quarter" | `{doctype, operation, filters: {party, payment}, where: [{field: grand_total, op: ">", value: 10000}], date: {preset: this_quarter}}` **[unverified]**: the model's output |

The model gets a compact schema for up to 6 relevant DocTypes. `select_doctypes` picks them deterministically, from
the open page, the previous query, DocType names and keywords in the message ([llm.py:141](../../ai_erpnext/chat/llm.py#L141)).
The prompt also lists the names of all other queryable DocTypes. The schema lines list the allowed fields, the party
field, child tables and link fields **straight from the policy and the user's field permissions**, so the model is
only told about fields the user may read ([llm.py:175](../../ai_erpnext/chat/llm.py#L175)).

## Validation: `queries.validate` ([queries.py:163](../../ai_erpnext/chat/queries.py#L163))

The checks run in order, and each one fails closed with a user-safe sentence (`CapabilityError`, re-exported as
`QueryError`):

1. **Operation and DocType.** `_hr_operation` turns an Employee `details` plan that asks for leave or attendance into
   the HR operation. `_match_doctype` maps the name (case and trailing "s" ignored) to a `REGISTRY` key, or to `auto`
   for "details of a person/company name". Unknown → "I can't look up X yet. I can search: …".
2. `capability.is_queryable`: listed in [capability/policy.py](../../ai_erpnext/capability/policy.py) (74 DocTypes),
   installed, its app installed (9 HRMS DocTypes have `app="hrms"`), and either a `company` field or an explicit
   `shared=True` ([service.py:38](../../ai_erpnext/capability/service.py#L38)).
3. `frappe.has_permission(doctype, "read")`.
4. `refuse_lifecycle` (submit, cancel, delete, sql, run … are never operations), then `check_operation` (the
   operation must be in `Spec.operations`).
5. Filters:
   - `company`: must be one of the user's usable companies, by name, company name or abbreviation;
   - `party` / `search` / `name`: text ≤ 140;
   - `status`: an exact option, or "open"/"pending" → `OPEN_STATUSES`; unknown → rejected with the real statuses;
   - `docstatus`: draft/submitted/cancelled; `payment`: unpaid/overdue; `mine`; `self`.
   
   **Any other key in `filters` is ignored** (see the [finding](security-review.md#f4-medium-unknown-filter-keys-silently-widen-a-model-plan)).
6. `fields`: for `details`, each word is mapped to allowlisted fields through `FIELD_CONCEPTS` (mobile → `mobile_no`,
   outstanding → `outstanding_amount` or a computed party total …). Unknown → "I can't show X for Y records". For
   lists, the fields must be in the spec, and only permitted ones are kept.
7. `date`: preset or from/to; the DocType's document date, or `creation` if the user says "created"; at most 3 years.
8. `where` / `aggregate` / `child` / `link` → `capability.normalize_*`
   ([service.py:356](../../ai_erpnext/capability/service.py#L356)):
   - only policy fields that are filterable for their type, with typed and coerced values (Select values must be
     real options);
   - at most 8 conditions and 20 `in` values;
   - aggregates: count/sum/avg/min/max over numeric fields, grouped only by Link/Select/Check fields (never free text),
     by month/year buckets, and **refused** when the currency of the summed field isn't known;
   - one-hop links to another queryable DocType.

   A part that fails is never dropped: the whole plan is refused.
9. Questions instead of guesses (`QueryClarify`): a `details`/`summary` plan with no target asks "Which X do you
   mean?"; a `sales_summary` without a period asks for one; a monthly aggregate without dates asks for a period.

Plans that come back from conversation state ("show more", the answer to a lookup question) are validated again by
`_recheck` ([queries.py:620](../../ai_erpnext/chat/queries.py#L620)) before they run.

## Execution: `queries.execute` ([queries.py:656](../../ai_erpnext/chat/queries.py#L656))

| Operation | Function | How the numbers are computed |
|---|---|---|
| `list`, `overdue`, `unpaid` | `_list` L794 | `get_list`, `limit + 1` rows to know whether there is more |
| `count` | `_count` L1185 | `get_list(fields=[COUNT(*)])`, in the database |
| `aggregate` | `_aggregate` L1058 | `get_list` with SUM/AVG/MIN/MAX/COUNT and `group_by`. The currency (or company) is always part of the grouping, so currencies are never added together. Month and year buckets are one query per bucket (≤ 120 rows) |
| `details` | `_details` L1384 / `_field_answer` L1591 | `get_doc` + `check_permission("read")`; only permitted policy fields are shown; a party's outstanding is summed in the database per currency |
| `summary` (customer/supplier) | `_party_summary` L1446 | one grouped query per order/invoice DocType the user can read; the others are named as "not included" |
| `child_rows` | `_child_rows` L965 | the parents are filtered first (permissions, companies), then rows of ≤ 50 parents, ≤ 100 rows |
| `sales_summary` | `_sales_summary` L1924 | submitted Sales Invoices grouped by company and currency, in the database |
| `compare` | `_compare` L1763 | two grouped database totals; the difference and percentage in Python |
| `outstanding_by_customer/_supplier` | `_outstanding_by_party` L1995 | `SUM(outstanding_amount)` per party and currency, over submitted invoices |
| `below_reorder` | `_below_reorder` L1818 | Item re-order levels and Bin `projected_qty`, compared in Python (≤ 200 levels) |
| `leave_balance` | `hr.leave_balance` [hr.py:206](../../ai_erpnext/chat/hr.py#L206) | HRMS `get_leave_details`; nothing is recalculated |
| `attendance` | `hr.attendance` [hr.py:316](../../ai_erpnext/chat/hr.py#L316) | grouped counts of submitted Attendance; ≤ 62 rows listed |

**The model never calculates anything.** All arithmetic is either a database aggregate or plain Python on query
results (a difference, a percentage, a quantity comparison). **[code]**

### Filters every query gets (`_base_filters`, [queries.py:686](../../ai_erpnext/chat/queries.py#L686))

- **Document state.** Submittable DocTypes default to `docstatus < 2`, so cancelled documents are excluded and the
  card note says so. "Unpaid/overdue" means submitted, outstanding > 0, not a return, and (for overdue) a due date in
  the past. Page answers set `all_states` to match what the list shows.
- **Company isolation** (`_company_filter`, L507): the named company, or the user's Company User Permissions when they
  have any (`permitted_companies`, L452). DocTypes marked `optional_company` also include records without a company.
  Without a Company User Permission there is no company filter. Role permissions and `get_list`'s own User Permission
  checks still apply.
- **Employee scope** (`capability.employee_scope`, [service.py:75](../../ai_erpnext/capability/service.py#L75)): for
  specs with an `employee_field` (Leave Application, Attendance, Expense Claim …), a user without the HR Manager / HR
  User role only sees their own employees' records and those they approve. **Employee itself has no
  `employee_field`**, so Employee lookups are governed by the site's Employee read permission alone.
- `mine`: `owner = user`, `_assign` contains the user, or the user's own employees, depending on the spec's `mine_field`.
- `party` / `search`: `LIKE %value%` over the party and search fields. The value is a bound parameter; `%` and `_`
  are not escaped, so they act as wildcards (`_like_value` exists, but it is only used for `_assign`).
- `default_filters` per spec (for example, cancelled ledger entries are not data).

### Record resolution (`resolve_record`, [queries.py:1225](../../ai_erpnext/chat/queries.py#L1225))

The lookup order is:

1. exact ID;
2. exact title (case-insensitive);
3. title or ID equal after normalisation;
4. every typed word is the prefix of a word of the title, ID or a search field, within the 50 most recently
   modified records that contain the longest typed word.

ID-like terms (`ACC-SINV-2026-00001`) only match an ID or title exactly. A misspelling is never matched. One match
is used: if it wasn't exact, the answer says "I found one customer matching “…”: X". Several matches produce a pick
question, at most 6 options. For `doctype: auto`, Customer, Supplier and Contact are searched (plus document
DocTypes for an ID). A contact is dropped when it duplicates its own customer or supplier.

### Zero, one, many

| Result | Behaviour |
|---|---|
| A list with zero rows | "I found no X records matching that. Filters: …" (the filters are spelled out) |
| A list with rows | the first `limit` rows with a "Show more" button when there is more. Pages are capped at 20 rows; offsets beyond 500 → "open the list in ERPNext" |
| `details` not found | "I couldn't find a … matching “…” that you can access." The user is asked to retype at most 2 times. **A missing record and a hidden record give the same answer** |
| `details` with several matches | a pick question with buttons `DocType::name`; the choice is re-checked by `_resolve_target` with `exact_name=True` |

### Formatting

Results are returned as `{text, card, names, titles, result_doctype?, requires?}`. The engine posts them with
`source="erpnext"` and a `ref` (DocType, ≤ 50 names, required DocTypes). The `ref` is used later by
[history.py](../../ai_erpnext/chat/history.py) to withhold the stored result when the reader can no longer read those
records or DocTypes ([request-lifecycle.md](request-lifecycle.md), step 15).

**Query results are never sent to the model.** The router's history only contains user messages and the model's own
`chat` / `clarify` replies (`HISTORY_SOURCES`, [llm.py:35](../../ai_erpnext/chat/llm.py#L35)). Its context is the
previous **validated plan** and a **row count**. **[test]** `test_chat_routing.test_erpnext_results_are_never_sent_to_the_model`.

## Worked examples

| Message | Path | Notes |
|---|---|---|
| "Show my leave balance" | shortcut → `hr.leave_balance` | see [parameters-and-context.md](parameters-and-context.md#show-my-leave-balance) |
| "my attendance for October" | shortcut → `attendance`, date 2026-10-01..31 | requires Attendance read; draft attendance is counted separately and named; scope = the user's companies + employee scope |
| "how many draft purchase invoices" | shortcut → `_count` with `docstatus = 0`, plus the company filter | "There are N Purchase Invoice records you can access (draft). From your ERPNext data." |
| "show overdue sales invoices" | shortcut → `_list` operation `overdue` | `docstatus = 1`, `outstanding_amount > 0`, `is_return = 0`, `due_date < today`; columns: name, customer, dates, totals, outstanding, currency, status |
| "what is the mobile number of Amit Patel?" | shortcut → `details`, `auto` → Customer/Supplier/Contact → `_field_answer` | when the customer has no mobile, linked Contacts are checked (if the user can read Contact) |
| "compare sales this month with last month" | shortcut → `_compare` | per company and currency; returns count as negative |
| "explain this invoice" (form open) | `page_context._explain` → a `details` plan with `name` = the open record | plus the status meaning and the general description of the DocType (`source="guide"`) |

## Can sensitive data leak?

- **Fields.** Only fields listed in a `Spec` can ever be returned, and `allowed_fields` also removes fields the user's
  permlevels hide and prohibited fields: Password/Attach/Signature types, and names containing password, secret,
  token, otp, salary, iban, ctc, aadhaar, passport, credential, api_key, bank_account …
  ([policy.py:1745](../../ai_erpnext/capability/policy.py#L1745)). The Employee spec exposes only `employee_name`,
  department, designation, company, branch and status. **[code]** + **[test]** `test_capability`.
- **Existence.** Missing, hidden and forged records all give the same reply ("… that you can access"), across
  lookups, page context and prefill. **[test]** `test_security_hardening.test_existing_record_checks_do_not_reveal_hidden_records`.
- **Replay.** Stored answers are re-checked against current permissions before they are shown again
  ([history.py](../../ai_erpnext/chat/history.py)). **[test]** `test_security_hardening` (9 tests).
- **Residual risks.** `get_all` (permission-free) is used for some lookups that only produce filter values or labels:
  the user's own Employee names in `employee_scope`, company names already known to be usable, item titles for
  options, and UOM conversions. None of these returns values to the user beyond names they could already choose.
  `frappe.db.get_value("Employee", name, "employee_name")` in `employee_label` reads a name for a record that was
  just resolved with permissions. **[code]**
