# Test coverage and verification status

## How to run the tests

The suite runs against the isolated test site `ci-test.local`, whose database is the docker container
`ai-erpnext-ci-db` on 127.0.0.1:3317. Never run it against `ai-erpnext.local`.

```bash
docker start ai-erpnext-ci-db                     # after a reboot
cd ~/frappe-bench
# optional belt-and-braces: no outbound HTTP even if a test forgot a mock
export HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 NO_PROXY=localhost,127.0.0.1,::1
bench --site ci-test.local run-tests --app ai_erpnext                                    # everything
bench --site ci-test.local run-tests --app ai_erpnext --module ai_erpnext.tests.test_chat_hr   # one module
```

CI ([.github/workflows/ci.yml](../../.github/workflows/ci.yml)) installs frappe + erpnext `version-16` + this app,
**without HRMS**, and runs `bench --site test_site run-tests --app ai_erpnext`. The HRMS tests then exercise the
"HRMS not installed" behaviour. The Linters workflow runs pre-commit (ruff, prettier, eslint), Semgrep and
pip-audit.

All provider calls in tests are mocked: fake provider objects or a scripted HTTP transport.
[tests/network_guard.py](../../ai_erpnext/tests/network_guard.py) blocks every non-loopback socket while the test
package is loaded, so an unmocked call fails instead of reaching a paid API.

## Result of the run made for this audit

| Item | Value |
|---|---|
| Date | 2026-10-23 |
| Commit | `90dc039` (`ai-implementation-phase` = `version-16`) |
| Site | `ci-test.local` (frappe, erpnext, ai_erpnext, **hrms** installed) |
| Command | `bench --site ci-test.local run-tests --app ai_erpnext` (with the proxy variables above) |
| Result | **Ran 643 tests in 361.8 s: OK**, 0 failures, 0 errors, 0 skipped |

## Read-only probes run for this audit

Three small scripts were run with `env/bin/python` from `sites/` against `ci-test.local`, as Administrator. Each one
ends with `frappe.db.rollback()`. They make no AI call (the proxy variables were set) and no write. They were kept in
the session scratchpad, not in the repository. What they did:

1. `shortcuts.match()`, `CREATE_DOC_RE`, `CREATE_ANY_RE`, `CANCEL_RE`, `RESTART_RE`, `HELP_RE`,
   `forms.match_doctype()`, `parse_number()`, `parse_date()` and `hr._period_plan()` on the example phrases of this
   audit. Results: [routing-and-prequeries.md](routing-and-prequeries.md#probe-what-the-deterministic-layer-does-with-real-phrases).
2. `queries.validate()` on HR plans, an unknown field, an unknown status, a misspelt DocType, a lifecycle operation,
   an injection-like value, and `outstanding_by_customer` with a party.
3. `queries.validate()` on plans with unknown `filters` keys, an out-of-range `limit` / `sort`, `where` with a
   non-allowlisted field, and a date on a DocType without one.

A metadata-only read of `ai-erpnext.local` (installed apps, number of Server Scripts, the provider name and whether a
key is set, fallback and retention settings, the role's existence, HR Settings `send_leave_notification`) supports the
**[site]** statements. No record content and no key value was read.

## Coverage matrix

"Integration" means a test that runs the real engine against a real database on the test site. Almost every test in
this suite does that. "Unit" means a test of a pure function or of a mocked client. "Mocked" means the provider (or
another external service) is a fake. "Live site" means a manual or scripted check on `ai-erpnext.local`: **none was
performed for this audit**, and none is recorded in the repository except the earlier phases' notes in
[ERPNext_AI_IMPLEMENTATION_AUDIT.md](../ERPNext_AI_IMPLEMENTATION_AUDIT.md) Part I.8 / 0.5.

| Flow | Source | Unit tests | Integration tests | Live site | Known gaps |
|---|---|---|---|---|---|
| Pre-query: cancel / restart / help / retry / why | engine `_on_text` | – | `test_chat_guided.test_start_over_and_cancel`, `test_chat_agent.test_user_can_correct_fields_and_cancel`, `test_chat_forms.test_cancel_top_level_and_nested` | not run | the prefix pitfalls (F15) are untested |
| Shortcuts | `shortcuts.py`, `hr.match` | `test_chat_routing.test_shortcut_plans`, `test_chat_hr.test_hr_phrasings_route_to_read_only_operations` | `test_chat_routing.test_shortcut_messages_make_no_model_call` | not run | **"for 2026", "as of <date>" (F3)** |
| Page-context routing | `page_context.py` | `test_page_context` (70), including a node check of the JS collector (`page_context_check.mjs`) | `test_page_reference_and_parity` (25) | not run | – |
| AI routing (prompt, plan shapes) | `llm.py` | `test_router_prompt` (28, **mocked**) | `test_chat_routing.test_other_messages_go_to_the_model_and_are_counted` (**mocked**) | not run | no recorded real-model responses for the example phrases; the model's handling of corrections and follow-ups is unverified |
| Router retries and parsing | `llm.complete_with_policy`, `salvage` | `test_chat_router_retry` (14, **mocked**), `test_rate_limit_regression` (18) | – | not run | timing under a real web server (F1) |
| Parameter extraction (forms) | `forms._set`, `_resolve_link`, `_resolve_item` | – | `test_chat_forms` (11), `test_chat_guided` (17), `test_chat_new_forms` (18) | not run | the confirmation + correction in one reply (F2); a model plan with a warehouse |
| General conversation | `engine._general` chat mode, `claims.py` | `test_claims_and_network` (15) | `test_chat_agent.test_text_only_messages_and_unsupported_requests` (**mocked**) | not run | the quality of real model answers |
| Query execution | `queries.py` | `test_capability` (29) | `test_chat_queries` (19), `test_chat_lookup` (24), `test_generic_queries` (26), `test_chat_query_extras` (8), `test_coverage_batch` (22), `test_coverage_phase_d` (17) | not run | **unknown `filters` keys (F4)**; outstanding-by-party filters (F5) |
| Company isolation | `_company_filter` | – | `test_company_isolation` (6), `test_warehouse_defaults` (11) | not run | – |
| Document creation (guided) | `forms.py`, `requirements.py` | – | `test_chat_forms`, `test_chat_guided`, `test_chat_creation_requirements` (17), `test_chat_new_forms`, `test_chat_hr_forms` (8) | not run | duplicate guided Drafts (F13) |
| Document creation (image) | engine image section, `api/creation.py` | `test_document_direction` (42), `test_sales_invoice_mapping` (29) | `test_chat_agent` (13), `test_lifecycle_safety` (14), `test_pdf_documents` (5) | not run | prompt B3 not benchmarked against a live model |
| HRMS | `hr.py` | – | `test_chat_hr` (18), `test_chat_hr_forms` (8); in CI without HRMS: "not installed" paths | not run | approver notifications (F7) not asserted; Employee lookups per site permissions (F6) |
| Permissions and roles | `session._owned`, `boot`, `capability.service` | – | `test_assistant_role` (8), `test_security_hardening` (29) | not run | – |
| Provider errors and fallback | `ai/providers.py`, `ai/errors.py` | `test_provider_fallback` (15), `test_provider_config` (14) (**mocked**) | – | not run | the Redis failure path (F17) |
| Retries and fallbacks (chat) | layers 2–6 | – | `test_chat_guided.test_provider_failure_keeps_the_pending_document`, `test_chat_agent.test_provider_failure_offers_retry` (**mocked**) | not run | – |
| Image extraction pipeline | `pipeline/controller.py` | `test_extraction_safety` (16) | `test_lifecycle_safety` (job paths) | not run | real worker timing |
| Conversation state | `session.py`, `history.py` | – | `test_chat_sessions` (14), `test_security_hardening` (replay) | not run | **a browser reload starts a new conversation**: the "survives reload" test passes the id itself |
| Frontend API contract | `api/chat.py`, `ai_bot_v2.js` | JS: only the page-context collector and the launcher flag (`test_page_reference_and_parity.TestLauncherFlag`) | endpoint behaviour via Python calls | not run | **no browser test of rendering, upload or polling**; the PDF mismatch (F16) |
| Lifecycle and background jobs | `hooks.py`, `controller.py`, `retention.py`, `session.cleanup_expired_sessions` | `test_install_schema` (6) | `test_lifecycle_safety`, `test_chat_sessions.test_cleanup_removes_only_conversations_nobody_used`, `test_coverage_phase_d` (retention) | not run | non-DB side effects of prechecks (F8) |
| Inventory / docs | `capabilities.catalog`, `capability/inventory.py` | `test_inventory` (11) | – | – | – |

## What is not verified anywhere

- The real router model's plans for any of the example phrases in [parameters-and-context.md](parameters-and-context.md)
  (requires paid calls, or a recorded transcript replayed through a scripted provider).
- The browser UI end to end: rendering, file upload, polling, Retry. Requires a browser test (Playwright or Cypress)
  against a dev site.
- Behaviour under gunicorn with a slow provider (F1). Requires `bench setup production` or a gunicorn run with a mock
  provider that sleeps.
- Email and notification side effects of Leave Application drafts on a site with mail configured (F7).
- The B3 extraction prompt on real documents.
