# Security and failure review

This review is based on the code (2026-10-23, `90dc039`), read-only probes on `ci-test.local`, and the full test
suite (643/643 passing). **No fixes were made in this task.** Severities assume an internal ERPNext deployment with
authenticated users.

Reproducibility labels:

- **Confirmed (probe):** executed and observed.
- **Confirmed (code):** a deterministic code path; not executed end to end.
- **Risk:** depends on runtime conditions or site configuration that were not tested.

## Controls that hold

These are the controls that most matter, with their evidence:

| Area | Control | Evidence |
|---|---|---|
| Authentication | every endpoint is whitelisted, POST-only, with no `allow_guest`; the conversation id is untrusted, so a missing and a foreign id get the same "Conversation not found." | [api/chat.py](../../ai_erpnext/api/chat.py), [session.py:45–57](../../ai_erpnext/chat/session.py#L45); **[test]** `test_chat_sessions.test_another_user_can_neither_read_nor_change_a_conversation`, `test_security_hardening.test_every_whitelisted_endpoint_of_the_app_declares_its_methods` |
| Chat access | the opt-in role or System Manager, rechecked on every load; a user who lost access gets nothing from stored conversations | [boot.py](../../ai_erpnext/boot.py), `_owned`; **[test]** `test_assistant_role` (8), `test_security_hardening.test_a_user_who_lost_chat_access_gets_nothing_from_stored_conversations` |
| Authorization of data | `get_list` / `check_permission` everywhere; a policy allowlist (74 DocTypes) ∩ metadata ∩ field permlevels; prohibited field types and names | [capability/service.py](../../ai_erpnext/capability/service.py); **[test]** `test_capability` (29) |
| Company isolation | the explicit company filter from Company User Permissions on every query with a company field | [queries.py:507](../../ai_erpnext/chat/queries.py#L507); **[test]** `test_company_isolation` (6) |
| HR records | an employee scope for non-HR users on 7 HR DocTypes; self-service forms only for one's own Employee | [service.py:75](../../ai_erpnext/capability/service.py#L75), [forms.py:1476](../../ai_erpnext/chat/forms.py#L1476); **[test]** `test_chat_hr_forms` (8), `test_chat_hr.test_another_employee_cannot_see_leave_or_attendance` |
| SQL injection | no raw SQL; field names only from the allowlist; values bound by `get_list`; `order_by` and `group_by` built from spec constants or validated group fields | grep (no `frappe.db.sql` in app code); [queries.py:761](../../ai_erpnext/chat/queries.py#L761); **[probe]** an injection-like value is accepted as a plain LIKE value |
| Model output | plans validated; creation fields allowlisted; lifecycle fields refused; the model never sees ERPNext data; claims guard on model text | [llm.py:276](../../ai_erpnext/chat/llm.py#L276), [service.py:555](../../ai_erpnext/capability/service.py#L555), [claims.py](../../ai_erpnext/chat/claims.py); **[test]** `test_claims_and_network` (15), `test_router_prompt` (28) |
| Lifecycle | no submit/cancel/delete/pay path; `docstatus` checked after insert | **[test]** `test_security_hardening.test_no_lifecycle_call_exists_in_application_code`, `test_lifecycle_safety` (14) |
| Files | an attachment must be the user's own `File`; the job re-checks read access; bytes verified with PIL; 10 MB and 50 MP limits | [engine.py:1079](../../ai_erpnext/chat/engine.py#L1079), [controller.py:184](../../ai_erpnext/pipeline/controller.py#L184); **[test]** `test_lifecycle_safety.test_entry_cannot_use_another_users_private_file` |
| Replay | stored results re-checked against current permissions | [history.py](../../ai_erpnext/chat/history.py); **[test]** `test_security_hardening` (9 replay tests) |
| Logging | metadata only, no prompts, replies or document content | [errors.py](../../ai_erpnext/ai/errors.py); **[test]** `test_security_hardening.test_source_does_not_log_prompts_responses_or_results` |
| Tests can't spend money | a network guard blocks every non-loopback socket during tests | [tests/network_guard.py](../../ai_erpnext/tests/network_guard.py); **[test]** `test_claims_and_network.test_an_unmocked_provider_request_cannot_leave_the_machine` |

## Findings

### F1 (Medium): long AI call inside a locked request

- **Where:** `api.chat.send_message` → `session.lock` (`SELECT … FOR UPDATE`, [session.py:135](../../ai_erpnext/chat/session.py#L135))
  → `engine._general` → `llm.route` → `complete_with_policy` (3 attempts, [llm.py:38](../../ai_erpnext/chat/llm.py#L38))
  → `OpenAIProvider` with a 90 s per-request timeout ([openai_provider.py:20](../../ai_erpnext/ai/openai_provider.py#L20)).
  The 12 s budget counts only the sleeps.
- **Evidence:** a worst case of ≈ 3 × 90 s + backoff ≈ 272 s with the row lock and the DB transaction held. Polls and
  other tabs of the same conversation wait on the lock, up to `innodb_lock_wait_timeout` (MariaDB default 50 s), and
  then fail. Under gunicorn (production `bench setup production`, default `http_timeout` 120 s), the worker is killed
  mid-request: everything rolls back and the user sees "The request didn't go through". This dev bench runs
  `bench serve` without a timeout. **[site]** (Procfile)
- **Reproducibility:** Risk. Confirmed (code) for the timing chain. Needs a load test with a slow mocked provider
  under gunicorn.
- **Impact:** blocked conversations, killed workers and worker-pool exhaustion when the provider is slow; the same
  request retried by the user.
- **Remediation:**
  - a router-specific timeout (e.g. 15–20 s) and an overall deadline;
  - do not hold the session lock during the provider call: snapshot the state, call, then lock, re-read and apply only
    if nothing changed; or move routing to a short background job with polling, as extraction does;
  - align the attempt count with the gunicorn timeout.

### F2 (Medium): a confirmation with a correction creates the record unchanged

- **Where:** `forms.answer` `form_confirm` ([forms.py:1128](../../ai_erpnext/chat/forms.py#L1128)) and the engine's
  `YES_RE` / `NO_RE` for the image flow ([engine.py:78](../../ai_erpnext/chat/engine.py#L78), applied at L1846–1865).
- **Evidence:** both use prefix regexes (`^\s*(yes|y|ok|okay|confirm|create|go ahead|do it|sure|retry)\b`). The
  corrections parser only matches messages that *start* with change/set/update … So "ok, but change the posting
  date to 2026-10-30" confirms and inserts without the date change. Also, "no problem, create it" matches `NO_RE` at
  an image `confirm` and cancels.
- **Reproducibility:** Confirmed (code). Not executed end to end.
- **Impact:** a Draft with values the user just asked to change. It is recoverable (a Draft, not submitted) but
  silently wrong. For masters (saved immediately), it is a saved wrong record.
- **Remediation:**
  - at confirmation steps, accept only short affirmative replies (an exact match after trimming punctuation);
  - run the correction parser before the yes/no parser;
  - if a confirmation contains anything else, re-show the preview.

### F3 (Medium, correctness): the HR shortcut merges the period into the employee name

- **Where:** `hr.LEAVE_BALANCE_RE` / `_TAIL` / `_PERIOD` ([hr.py:547–569](../../ai_erpnext/chat/hr.py#L547)).
  The same pattern is used for attendance.
- **Evidence:** **[probe]** "Show casual leave balance for Anshum for 2026" → `search = "Anshum for 2026"`; "… as of
  25 October" → `search = "Anshum as of 25 October"`. Because the shortcut matched, the model is never asked.
  `resolve_record` then requires every word to match, so the user gets "couldn't find an employee …" and is asked to
  retype the name, twice.
- **Reproducibility:** Confirmed (probe) for the plan; the final reply follows from the code (no Employee fixture on
  `ci-test.local`).
- **Impact:** valid HR questions fail; the period is lost.
- **Remediation:**
  - stop the target at a date-like tail ("for <year>", "as of/on <date>", "from … to …") and parse it with
    `parse_date`;
  - or return `None` from `hr.match` when the target contains digits or date words, so the model handles it;
  - add tests for these phrasings.

### F4 (Medium): unknown filter keys silently widen a model plan

- **Where:** `queries.validate` ([queries.py:192–213](../../ai_erpnext/chat/queries.py#L192)) reads a fixed set of
  `filters` keys and ignores the rest.
- **Evidence:** **[probe]** `{"doctype": "Sales Invoice", "operation": "list", "filters": {"territory": "North"}}`
  validates and lists **all** invoices. The same condition in `where` is refused ("I can't filter Sales Invoice
  records by “territory”"). The module's own rule (stated in `_generic_parts`) is that a part that can't be validated
  is never dropped, because dropping it widens the query.
- **Reproducibility:** Confirmed (probe).
- **Impact:** wrong answers that look authoritative ("From your ERPNext data", with the filters note missing the
  condition). It is not a permission bypass: user permissions still apply.
- **Remediation:** reject unknown `filters` keys, or translate them through `normalize_where`, so that they fail the
  same way.

### F5 (Low): validated parameters that the executor ignores

- **Where:** `_outstanding_by_party` ignores `party`, `search`, `status`, `date` and `docstatus`
  ([queries.py:1995](../../ai_erpnext/chat/queries.py#L1995)); `_sales_summary` drops `status` (L1925); `hr.validate`
  clears `where`/`aggregate`/`child`/`link`/`fields` ([hr.py:137](../../ai_erpnext/chat/hr.py#L137)).
- **Evidence:** **[probe]** an `outstanding_by_customer` plan keeps `party = "ABC"` and a date after validation; the
  executor's filters don't use them.
- **Impact:** "outstanding of customer ABC this month" through this operation answers for every customer and every
  date.
- **Remediation:** apply the filters, or reject plans that carry them.

### F6 (Low to Medium): Employee lookups depend on the site's Employee read permission

- **Where:** the `Employee` spec has no `employee_field` ([policy.py:409](../../ai_erpnext/capability/policy.py#L409)),
  so `employee_scope` doesn't apply to Employee lookups. Leave balances rely on HRMS `validate_leave_access`, which
  allows any user with Employee read permission on that employee.
- **Evidence:** Confirmed (code). Behaviour then depends on the site's role permissions and User Permissions: whether
  the "Employee" role can read all Employee records.
- **Reproducibility:** Risk. Test on the target site with a plain employee user who has no User Permission:
  "leave balance for <colleague>" and "find employee <colleague>".
- **Impact:** in the worst case, any employee can see colleagues' name, department, designation, branch, status and
  leave balances. That is the same as Desk would show with those permissions, but easier to discover through chat.
- **Remediation:** for users without HR roles, limit Employee lookups and `leave_balance` / `attendance` targets to
  their own employee, their reports and those they approve, as the HR forms already do.

### F7 (Low): a "Draft" Leave Application notifies and shares with the approver

- **Where:** HRMS `LeaveApplication.after_insert` / `on_update`, triggered by `forms._create`.
- **Evidence:** Confirmed (code + site): `send_leave_notification = 1` on `ai-erpnext.local`; DocShare and a PWA
  Notification. The chat says "It will not be submitted or approved" and does not mention the notification.
- **Impact:** a user exploring the chat sends a real request email to their manager.
- **Remediation:** say so in the preview ("Your leave approver X will be notified"). This is a business decision; see
  the questions at the end.

### F8 (Low): prechecks run real `validate()` hooks on rolled-back copies

- **Where:** `forms._precheck` ([forms.py:2694](../../ai_erpnext/chat/forms.py#L2694)), `hr.precheck`
  ([hr.py:450](../../ai_erpnext/chat/hr.py#L450)).
- **Evidence:** the savepoint undoes DB writes only. Emails sent immediately, realtime events, jobs enqueued without
  `after_commit`, external calls, or a `commit()` inside a hook are not undone.
- **Reproducibility:** Risk. Needs a review of the `validate` chains of Employee, Work Order, Expense Claim,
  Timesheet, Journal Entry and Leave Application, and of any site-specific hooks or Server Scripts (none on
  `ai-erpnext.local` today).
- **Remediation:** document the expectation for custom apps; optionally set a flag (`frappe.flags.ai_chat_precheck`)
  that custom hooks can check.

### F9 (Info): keys previously committed to git history

- **Where:** `set_api_key.py` and `update_provider.py` state that they "used to contain an API key in source".
- **Remediation:** confirm that those provider keys were revoked or rotated. A history rewrite is optional once they
  are revoked. The key values are deliberately not reproduced here.

### F10 (Low, privacy): what leaves the server

- **Router calls:** the current message (≤ 2000 characters), up to 8 earlier user and chat turns, the validated
  previous plan (which can contain the names the user typed, as search terms) and page counts.
- **Extraction calls:** the **whole document image** (invoices with customer, address and tax data).
- Nothing is redacted, and no per-user or per-company consent setting exists.
- **Remediation:** this is a policy decision (provider region, data processing agreement). Optionally redact, or
  offer a self-hosted provider.

### F11 (Low): prompt injection

A message or a document can steer the router's plan. A plan can only:

- read what the user may read anyway;
- start a form that still needs the user's confirmation;
- produce chat text that passes the claims guard.

The extraction model's output is treated as data and mapped through permission-aware matchers. No escalation path
was found. **Confirmed (code)** and **[test]** `test_chat_agent.test_document_text_cannot_issue_instructions`.

The residual risk is misleading general text that the narrow claims guard doesn't catch, for example a fabricated
customer *name* with no number or ID. Such answers are labelled "General answer, not from your data".

### F12 (Low): LIKE wildcards in search terms

- **Where:** `_base_filters` `party` / `search` ([queries.py:732–739](../../ai_erpnext/chat/queries.py#L732)),
  `_GenericMatcher` ([forms.py:1743](../../ai_erpnext/chat/forms.py#L1743)), `resolve_record` (L1265).
- **Evidence:** `%` and `_` in user text are not escaped (`_like_value` exists, but only `_mine` uses it). The values
  are bound parameters, so there is no injection; the effect is broader matches.
- **Remediation:** use `_like_value` consistently.

### F13 (Low): no duplicate protection for guided forms

Typing the same request twice creates two Drafts. The image flow has duplicate detection; guided forms don't (beyond
the `client_id` and the popped form). **Confirmed (code).** Remediation: an optional "a similar Draft X was created
N minutes ago" warning.

### F14 (Low, UX and correctness): silent losses

- one half of a mixed read + create request;
- every prefill when the AI is down;
- per-item warehouses and unknown create keys.

See [parameters-and-context.md](parameters-and-context.md#places-where-user-provided-information-is-dropped-silently).
Remediation: tell the user what was not used.

### F15 (Info): prefix-matched commands

- `CANCEL_RE` treats "stop, show me …" as cancel.
- `WHY_RE` treats "status of …" during an image flow as "why".
- "can you create a sales order" is not deterministic (anchored regex), so with the AI down it gets an error, not a
  form.

**Confirmed (code).**

### F16 (Info): the frontend refuses PDFs although the server supports them

See [frontend-api-contract.md](frontend-api-contract.md#mismatches-between-the-frontend-and-the-backend). It is not a
security issue, but the HELP text advertises a feature the UI blocks.

### F17 (Info): Redis failure while the fallback is used

`_take_daily_slot` calls `frappe.cache.incr`. A Redis error there is not an `AIProviderError`, so `_general` does
not catch it and the request fails with HTTP 500. It only happens when the fallback is enabled and Groq's quota is
exhausted. **Risk.**

## Failure scenarios

| Scenario | Behaviour | Evidence |
|---|---|---|
| Provider outage or quota exhausted | layer-2 fallbacks; pending work kept and asked again | **[test]** `test_chat_router_retry`, `test_chat_guided.test_provider_failure_keeps_the_pending_document` |
| No API key | the `config` category: "isn't configured correctly"; forms, shortcuts and page answers still work | **[test]** `test_chat_forms.test_works_without_ai_service_and_refuses_unsupported` |
| No worker | extraction re-queued once, then Failed with Retry | **[test]** `test_lifecycle_safety` |
| Permission failure during a query | "You don't have permission to view that." (message log cleared) | [engine.py:853](../../ai_erpnext/chat/engine.py#L853) |
| Malformed stored state | parts are dropped by `session.state()`, a stored plan is re-validated | [session.py:151](../../ai_erpnext/chat/session.py#L151), **[test]** `test_lifecycle_safety.test_malformed_router_plan_is_contained` |
| Two tabs or a double submit | the row lock + `client_id` | **[test]** `test_chat_sessions` |
| Unhandled exception in a form step | HTTP error, full rollback; Retry repeats the same step (and probably the same error) | **[code]** |

## Remediation priorities

1. F2: strict confirmation parsing (it touches data written to ERPNext).
2. F1: router timeout and lock scope (availability).
3. F4 and F5: never drop or ignore plan parts (correctness of "From your ERPNext data").
4. F3: HR period phrasing, plus tests.
5. F6: employee scope for Employee lookups (after checking the target site's permissions).
6. F7, F10: decisions on notifications and data sharing; then the wording.
7. F12–F16: hygiene.
