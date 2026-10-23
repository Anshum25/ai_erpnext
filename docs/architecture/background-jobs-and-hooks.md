# Background jobs, hooks and side effects

## Hooks registered by the app ([hooks.py](../../ai_erpnext/hooks.py))

| Hook | Target | What it does | When it runs |
|---|---|---|---|
| `app_include_js` | `ai_erpnext.bundle.js` | loads the chat widget, the page-context collector and the AI Document Entry review UI on every Desk page | every Desk page load |
| `boot_session` | [boot.py](../../ai_erpnext/boot.py) | sets `ai_chat_enabled` (can the user create `AI Chat Session`) | login / boot |
| `after_install`, `after_migrate` | [setup_doctypes.py](../../ai_erpnext/setup_doctypes.py) `ensure_schema` | creates or updates the custom DocTypes `AI Document Entry`, `AI Provider Settings` and `AI Chat Session`, adds fields, creates the **AI Assistant User** role and its permission rows once, and **disables the Client Script "AI Document Entry UI"** if present (L362) | `bench migrate`, install |
| `doc_events["AI Document Entry"].validate` | `pipeline.controller.validate_ai_document` | the attached file must be one the user can already read (before Frappe attaches it) | every save of an entry |
| `doc_events["AI Document Entry"].before_save` | `pipeline.controller.process_ai_document` | `status == Pending` and a file → **enqueue extraction** | every save of a Pending entry (chat or Desk) |
| `doc_events["AI Provider Settings"].validate` | `ai.providers.validate_settings` | local configuration check, no provider call | saving the settings |
| `has_permission` / `permission_query_conditions` | [permissions.py](../../ai_erpnext/permissions.py) | AI Document Entry rows are visible to their owner only (not to other System Managers); Administrator excepted | every read and list |
| `scheduler_events.daily` | `chat.session.cleanup_expired_sessions`, `pipeline.retention.purge_resolved_entries` | see below | daily scheduler |
| `before_tests` | `tests.bootstrap.before_tests` | test fixtures + the network guard | `bench run-tests` only |

There are no `override_whitelisted_methods`, no `doc_events` on ERPNext DocTypes, and no `*` handlers. **[code]**

## The background job

`enqueue_extraction` ([pipeline/controller.py:8](../../ai_erpnext/pipeline/controller.py#L8)) calls
`frappe.enqueue("…run_ai_pipeline", doc_name=…, timeout=1500, enqueue_after_commit=True, job_id="ai-extract-<entry>",
deduplicate=True)`. It uses the default queue and runs as the user who queued it.

`run_ai_pipeline` ([controller.py:96](../../ai_erpnext/pipeline/controller.py#L96)):

1. **Skips** an entry that is no longer `Pending` or already has a created document. A re-delivered or stale job
   spends no quota.
2. `_readable_file`: some `File` with that URL must be readable by the user. `image_problem`: PIL decodes the bytes as
   the claimed format, with at most 50 megapixels.
3. Provider call with retries and the fallback ([providers-and-fallbacks.md](providers-and-fallbacks.md)).
4. `apply_mapping` (classification, mapping, validation). A mapping error keeps the extraction and sets
   `Needs Review`.
5. `doc.save(ignore_permissions=True)` (only the result fields; validation runs), then **`frappe.db.commit()`** so
   that the polling chat sees the result.

Recovery: `engine._resume_if_lost` ([engine.py:352](../../ai_erpnext/chat/engine.py#L352)), run from `poll`, checks
the RQ job status:

- a running job is left alone;
- a vanished or failed job, older than 180 s, is re-queued once;
- the second time (or when it sits queued without a worker), the entry is marked `Failed` with "the background job
  stopped. Please retry."

**[test]** `test_lifecycle_safety.test_lost_extraction_is_requeued_once_then_offered_for_retry`,
`test_job_stuck_in_queue_is_reported`.

**If no worker runs** (`bench worker` is in the Procfile), extractions stay Pending and are reported as Failed after
the requeue. Text chat, queries and forms don't need a worker.

## Scheduled jobs (independent of any chat request)

| Job | What it changes | Scope |
|---|---|---|
| `cleanup_expired_sessions` ([session.py:219](../../ai_erpnext/chat/session.py#L219)) | conversations idle for more than 7 days: `status = Closed`, `messages = []`, `state = {}`; **deletes** untitled conversations (no user message) older than 1 day with no active entry | `AI Chat Session` only |
| `purge_resolved_entries` ([retention.py:31](../../ai_erpnext/pipeline/retention.py#L31)) | for `Created` entries with a created document, older than `document_retention_days` (default 180; 0 = keep), not active in an open conversation: clears extracted, mapped and validation data, overrides and the error log, and reduces `mapping_results` to a names-free summary. The row and its link to the created document stay | `AI Document Entry` only |

Neither job touches ERPNext business documents. **[code]**

## Cache and Redis

| Use | Key | Notes |
|---|---|---|
| Fallback daily cap | `ai_erpnext:fallback_requests:<YYYY-MM-DD>` via `frappe.cache.incr` | expires after 2 days ([providers.py:210](../../ai_erpnext/ai/providers.py#L210)) |
| RQ queue | `ai-extract-<entry>` job id | deduplicated |
| `frappe.get_cached_value` | Company currency, Item flags | read-only |

**No conversation state lives in the cache.** It is all in the `AI Chat Session` row, so it survives a restart.

## Transactions and savepoints

| Name | Where | Purpose | Always rolled back? |
|---|---|---|---|
| (request) | Frappe | one transaction per POST; commit on success, rollback on exception | – |
| `FOR UPDATE` on `AI Chat Session` | [session.py:135](../../ai_erpnext/chat/session.py#L135) | serialise requests of one conversation | released at commit |
| `FOR UPDATE` on `AI Document Entry` | [api/creation.py:21](../../ai_erpnext/api/creation.py#L21) | serialise image-draft creation | released at commit |
| `ai_chat_form_check` | `forms._precheck` | `validate()` on a copy | **yes** |
| `ai_chat_leave_check` | `hr.precheck` | HRMS `validate()` on a copy | **yes** |
| `ai_chat_form` | `forms._create` | the insert | on error |
| `ai_chat_create` | `engine._create_document` | the image draft insert | on error |
| `ai_chat_master` | `engine._answer_master` | the master insert (image flow) | on error |
| explicit `commit()` | `run_ai_pipeline` only (plus the CLI setup commands) | | – |

A savepoint rollback only undoes **database** writes. See below for what it cannot undo.

## Side effects when the chat creates a document

Creating a record calls the standard `doc.insert()`. That runs, as for a record created by hand in Desk:

- **ERPNext/HRMS controller methods:** `before_insert`, `validate`, `before_save`, `after_insert`, `on_update`;
- **Hooks of every installed app** (here erpnext, hrms, estate_notice, proposals). On this bench, HRMS hooks
  `Employee` (validate, `after_insert` ×3, `on_update` ×2) and `Journal Entry` (validate).
  `ai-erpnext.local` has **no Server Scripts** **[site]**;
- **Frappe generic effects:** the naming series counter is consumed, a Version (if tracked), Notification rules ("New"
  events), Assignment Rules, document follow / energy points if configured, realtime `list_update`, and the "created"
  timeline entry.

Specific effects found in the code:

| Document | Effect | Evidence |
|---|---|---|
| Leave Application (Draft, Open) | PWA Notification to the approver (`after_insert`); **email to the leave approver** if `send_leave_notification` (**on** for `ai-erpnext.local` **[site]**); DocShare (submit) to the approver; realtime publish | HRMS `leave_application.py` L106–137, `mixins/pwa_notifications.py` L33 **[code]** |
| Expense Claim (Draft) | PWA Notification to the expense approver (same mixin) | **[code]** |
| Employee (saved) | HRMS `after_insert` / `on_update` handlers (approver roles, publish update …) | hrms `hooks.py` **[code]** |
| Work Order (Draft) | `get_items_and_operations_from_bom` before insert (reads only) | [forms.py:2687](../../ai_erpnext/chat/forms.py#L2687) |
| Image drafts | `remarks` names the AI Document Entry; `created_document` is written on the entry | [creation/transaction.py](../../ai_erpnext/creation/transaction.py) |
| PDF attachment | a new private PNG `File` (page 1) owned by the user | [engine.py:1141](../../ai_erpnext/chat/engine.py#L1141) |
| Any image | an `AI Document Entry` row (kept as the audit trail; the role can't delete it) | [engine.py:1160](../../ai_erpnext/chat/engine.py#L1160) |

### Prechecks run real `validate()` code

`_precheck` and `hr.precheck` run `before_validate` and `validate`, and with them the `validate` hooks of every app,
on an unsaved copy inside a savepoint. Database writes are rolled back. **These cannot be undone:** an email sent
immediately (rather than queued in the DB), a realtime event, an enqueued job (unless `enqueue_after_commit`), a call
to an external service, or a `frappe.db.commit()` inside some hook, which would end the savepoint.

ERPNext's and HRMS's `validate` methods for the precheck DocTypes (Employee, Work Order, Expense Claim, Timesheet,
Journal Entry, Leave Application) were not audited line by line for this. `requirements.inspect` runs
`set_missing_values` without a savepoint (it only reads). **[unverified]**: needs a review of the `validate` chains
of those DocTypes and of any custom app hooks a site adds.

## Can anything submit, modify or delete without an explicit user action?

- **Submit / cancel / amend / delete / pay:** no code path in the app. **[test]** (the source scan in
  `test_security_hardening.test_no_lifecycle_call_exists_in_application_code`).
- **Business documents are created** only after the confirmation step, by the user's own request.
- **Automatic writes:**
  - the user's own `AI Chat Session` (every request; daily cleanup);
  - `AI Document Entry` (job result, retention purge, requeue/Failed marking from `poll`);
  - the PNG `File` for a PDF;
  - the Client Script toggle in `after_migrate`.
- **Desk side:** saving any `AI Document Entry` in `Pending` with a file (for example from the Desk form) enqueues an
  extraction, which costs a provider call.
