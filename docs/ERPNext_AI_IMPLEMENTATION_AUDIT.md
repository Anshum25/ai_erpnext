# ERPNext AI Chatbot: Implementation and Coverage Report

This document has two parts.

- **Part I (current)** is the state after the implementation phase of 2026-10-21 (branch `ai-implementation-phase`, based on
  `ab0a09f`). It holds the changelog, the current coverage per interaction mode, the test evidence and what remains.
- **Part II (baseline)** is the original audit at commit `085bf0f`, kept unchanged for reference. Its statuses and line
  numbers describe the code *before* this phase.

---

# Part I: Implementation phase 2026-10-21

## I.1 Summary

| | Baseline (085bf0f) | Now |
| --- | --- | --- |
| Tests (collected / passed / failed / skipped) | 471 / 471 / 0 / 0 | 573 / 573 / 0 / 0 |
| Audited features: implemented and data-tested | 23/72 | 34/72 |
| Image types with tested Draft creation | 1/11 | 11/11 |
| Guided creation types (all tested) | 12 | 14 |
| Model text checked for false claims | no | yes (`chat/claims.py`) |
| Suite-wide outbound-network guard in tests | no (README overstated) | yes (`tests/network_guard.py`) |

All P0 and P1 findings of the baseline audit are fixed and covered by tests (§I.3). Draft-only creation, System
Manager-only access, and no edit/submit/cancel/delete/payment remain enforced (§I.6).

## I.2 Changelog

| Area | Change | Files |
| --- | --- | --- |
| Document direction (new) | A deterministic classifier decides what an image becomes from the kind of document (perspective-free), the issuer/recipient and seller/buyer, and which side is the user's company (selected, or the validated default; a non-default company named on the document must be confirmed). Contradictions, unrelated parties, intercompany documents, unknown types and a debit note of unknown issuer are refused or asked about. The model's title never decides. | `detection/direction.py` (new), `mapping/mapper.py` |
| Unsafe override removed | The one-click "Yes, it's a Sales Invoice" question is gone. `remap(target_doctype=…)` and a Desk edit of `target_doctype` can no longer change the classification. The legacy "Open Prefilled Form" path (which used permission-bypassing lookups) is retired. | `chat/engine.py`, `api/mapping.py`, `pipeline/review_service.py`, `public/js/ai_review.js`; deleted `pipeline/mapper.py`, `detection/document_detector.py` |
| Image types | 11 registered definitions sharing one adapter, mapper, validator and builder: Sales/Purchase Invoice, Sales/Purchase Order, Quotation, Delivery Note, Purchase Receipt, Material Request, BOM, Credit Note (Sales Invoice return), Debit Note (Purchase Invoice return). Returns are linked to their original invoice when it is printed and found (same party and company, submitted, readable), or created unlinked only after an explicit choice. BOMs are created neither active nor default. | `registry/definitions.py`, `registry/document_registry.py`, `mapping/transaction.py`, `validation/transaction.py`, `creation/transaction.py` (new); `mapping/adapters.py`; old Sales Invoice modules are thin aliases |
| Prompt B3 | The extraction prompt asks for issuer, recipient, seller and buyer explicitly, perspective-free kinds, and the per-type fields (delivery/required-by and valid-till dates, references, original invoice, MR purpose, BOM header). All B2 keys and rules are kept; B2 is kept verbatim; both SHA-256s are pinned. **B3 is not benchmarked against a live model** (§I.8). | `pipeline/processor.py` |
| Discount (P1-1) | The document discount is applied (`discount_amount`, `apply_discount_on`). The base is read from the document's own numbers (tax computed after or before the discount); if it can't be decided and the document has tax, the user is asked. Discounts larger than the subtotal are data errors. When the Draft's grand total differs from the printed total (e.g. taxes left out), the success message says so. | `mapping/transaction.py`, `creation/transaction.py`, `api/creation.py` |
| Duplicates | Per-type checks: Sales Invoice (name, source tag, customer/date/total), Purchase Invoice/Receipt and Sales Order by the other party's reference (`bill_no`, `supplier_delivery_note`, `po_no`), others by party/date/total, BOM by item/company/quantity. | `validation/duplicate.py` |
| Image content | An upload must decode as the image format its extension claims (PNG/JPEG/WEBP, bounded size), checked in the chat and again in the background job before any provider call. | `pipeline/controller.py`, `chat/engine.py` |
| Model claims (P0-1) | The model's `chat`, `clarify` and `unsupported` text is checked: completed-action claims, counts/amounts/IDs the user didn't give, and report-result claims are replaced by a fixed truthful sentence (metadata-only log). The router prompt states the contract, and the widget labels model text "General answer, not from your data". | `chat/claims.py` (new), `chat/engine.py`, `chat/llm.py`, `public/js/ai_bot_v2.js` |
| Open record (P1-3) | Model plans may use the opaque `"ref":"open_record"` (queries) and `"party":"open_record"` / `"selected_records"` (creation). The server resolves them from the validated page: same DocType gives a name filter, otherwise the Link field pointing to the open record. A Supplier is never put into a Customer field. "Create X for this customer/supplier/these items" also works without the AI service. | `chat/engine.py`, `chat/llm.py` |
| Current list (P1-4) | "Show my current list (with its filters)", "what is in this filtered list", "count the items in my current list" and "the records currently visible" are answered from the list's validated filters, or refused with the reason; a model query on the open list's DocType says the list filters weren't applied. | `chat/page_context.py`, `chat/engine.py` |
| Reports | Questions about report results are answered with the report's description and validated filters plus a plain statement that it was not run. `REPORT_RESULTS_ENABLED` stays False. | `chat/page_context.py` |
| Supplier parity | New `outstanding_by_supplier`. Outstanding by customer/supplier and in record details is now **net** of standalone credit/debit notes (payments already reduce it), per currency, excluding cancelled documents. | `chat/queries.py`, `capability/policy.py`, `chat/llm.py` |
| Returns | `is_return` and `return_against` are queryable fields of Sales/Purchase Invoice. "Show credit notes" / "debit notes" is a deterministic route, and invoice totals state that returns count as negative amounts. | `capability/policy.py`, `chat/shortcuts.py`, `chat/queries.py` |
| Guided creation | New Draft flows: Material Request (purpose, required-by date, warehouse per stock row, no prices) and Stock Entry (Material Receipt / Issue / Transfer only, with the warehouses each needs). Conditional form fields (`when=`). Supplier creation inside the image flow. | `chat/forms.py`, `chat/masters.py`, `chat/requirements.py`, `capability/service.py` |
| Allow-list fix (found in this phase) | Link lookups merged `{**filters, "name": value}`, which replaced a `name in [...]` allow-list with the typed value (e.g. a "Manufacture" Stock Entry type was accepted). Filters are now combined as conditions. | `chat/forms.py` |
| Launcher (P3) | A server boot flag `ai_chat_enabled` hides the launcher from users who may not use the chat; the endpoints still check. | `boot.py` (new), `hooks.py`, `public/js/ai_bot_v2.js` |
| Retention (P3) | A daily job clears extracted content from resolved entries (status Created with a created document) older than "Keep Extracted Document Data For (Days)" (default 180; 0 disables). Unresolved and in-chat entries are never touched; a names-free classification summary and the link to the created document are kept. | `pipeline/retention.py` (new), `setup_doctypes.py`, `hooks.py` |
| Test safety (P2-1) | Every socket connection to a non-loopback address fails during tests; the README now says exactly that. | `tests/network_guard.py` (new), `tests/__init__.py`, `tests/bootstrap.py`, `README.md` |
| Inventory (P2-3) | `VERIFIED` corrected and extended only where a data-backed test now asserts returned values; image column added; `docs/coverage_inventory.*` regenerated. | `capability/inventory.py`, `docs/` |
| Maintenance | Removed `run_tests2.py` (broken imports, inserts with `ignore_permissions`), `check_provider.py`, `fix_client_script.py`; updated help text, engine docstring and README. | — |

## I.3 Baseline findings: status

| Finding | Status | Evidence (tests) |
| --- | --- | --- |
| P0-1 model text unchecked | **Fixed** | `test_claims_and_network` (11 claim tests, through the engine with a mocked router) |
| P1-1 discount dropped | **Fixed** | `test_document_direction.TestDiscounts` (persisted `discount_amount`/`apply_discount_on`/totals, base detection, question, data error) |
| P1-2 one-click Sales Invoice override | **Fixed (removed)** | `TestNoTypeOverride`, `TestImageChatFlows.test_no_generic_yes_override_exists_for_unsupported_documents`, `test_a_supplier_bill_becomes_a_purchase_invoice_never_a_sales_invoice` |
| P1-3 open record via the model | **Fixed** | `test_page_reference_and_parity.TestOpenRecordReference`, `TestContextAwareCreation` |
| P1-4 current-list phrasings | **Fixed** | `TestCurrentList` |
| P2-1 no network guard | **Fixed** | `TestNetworkGuard` |
| P2-2 smoke-only features | **Partly fixed**: Purchase Invoice, Payment Entry, Journal Entry, Stock Entry, Stock Ledger Entry, BOM, Work Order, Employee, Sales Order and Supplier summary are now data-backed; 24 features remain smoke-only (§I.5) | `test_coverage_phase_d.TestDataBackedQueries` |
| P2-3 inventory over-claims | **Fixed** (claims now backed; regenerated) | `test_inventory` |
| P2-4 browser tests | **Not possible here**: no browser test infrastructure in the repo; added a Node syntax check of every Desk script and guard-order checks | `test_page_context.TestFrontend` |
| P2-5 untested paths | **Fixed**: why/status, image master correction (customer group, supplier type), image start over, type override, discount, return links | `TestImageChatFlows` |
| P2-6 CI on newest v16 | **Not run** (local checkout only) | — |
| P3-1 supplier parity | **Fixed** | `TestSupplierOutstanding` |
| P3-2 return filtering | **Fixed** | `TestReturns` |
| P3-3 launcher for non-permitted users | **Fixed** | `TestLauncherFlag` |
| P3-4 documentation drift | **Fixed** | README, help text, engine docstring |
| P3-5 stray scripts | **Fixed** (removed after checking no runtime/CI reference) | `test_security_hardening` static checks |
| P3-6 legacy prefill mapper | **Fixed (retired)** | `TestFrontend.test_widget_hides_without_the_server_flag_and_labels_model_text` |
| P3-7 "explain this" on Bin/GL/SLE forms | Open | — |
| P3-8 caption false refusals | **Fixed** (captions no longer block reading; a payment request gets a "never pays" note) | No dedicated test (the image tests upload with captions such as "please process this") |
| P3-9 business duplicates in guided creation | Open (image flow has them; guided forms rely on confirmation) | — |
| P3-10 retention | **Fixed** | `test_coverage_phase_d.TestRetention` |

## I.4 Document-direction policy (implemented)

Kind of document (from the title, perspective-free) × the user's company's role (from the parties) → target.
Implemented in `detection/direction.py:TARGETS`; every row is a test case in
`test_document_direction.TestDirectionPolicy.test_the_perspective_table`.

| Kind | Issuer's role | We are the seller | We are the buyer |
| --- | --- | --- | --- |
| Invoice (any bill, whatever its heading) | seller | Sales Invoice | Purchase Invoice |
| Credit Note | seller | Sales Invoice return | Purchase Invoice return |
| Debit Note (issuer must be known) | buyer | Sales Invoice return | Purchase Invoice return |
| Purchase Order | buyer | Sales Order (the customer's PO number goes to `po_no`) | Purchase Order |
| Sales Order / order confirmation | seller | Sales Order | Purchase Order |
| Quotation | seller | Quotation | **refused** (a Supplier Quotation) |
| Delivery Note / challan | seller | Delivery Note | Purchase Receipt (`supplier_delivery_note`) |
| Goods Receipt Note | buyer | **refused** (the customer's record) | Purchase Receipt |
| Material Request | internal | Material Request | — |
| Bill of Materials | internal | BOM (Draft, not active, not default) | — |

Rules applied before the table:

- **Our company:** the user's explicit choice, else the validated default company. A non-default company named on the
  document, or a document between two of the user's companies, needs the user to choose. Names match only the same
  organisation: exact, without legal-form words, or the exact abbreviation; never fuzzy.
- **Roles:** explicit seller/buyer are checked against the issuer's role. If they contradict each other, nothing is
  created until the user says which side they are on, and then only the reading that puts the company on that side is used.
- **Neither party is the company:** the user is asked. A role the user selects can never override a side the document
  proves.
- **Unknown title, low confidence, a debit note without an issuer:** refused.
- **Revalidation:** classification runs again on every mapping and again inside `create_document`.

## I.5 Current coverage

### I.5.1 Features (denominator: the same 72 features as Part II §3)

| Status | Baseline | Now |
| --- | --- | --- |
| Implemented (path works and important values are asserted by a data-backed test) | 23 | **34** |
| Partial | 43 | **32** |
| …results only smoke-tested | 34 | 24 |
| …sub-capability missing or excluded (report results, ageing, consumption) | 9 | 8 |
| Not implemented | 2 | 2 (manufacturing reports; custom `Proposal`, deferred: §I.7) |
| Blocked (HRMS not installed) | 3 | 3 |
| Intentionally excluded (payroll) | 1 | 1 |

Newly implemented:
- Purchase/Sales Invoice returns (credit and debit notes): image, filter and shortcut.
- Payment Entry and Journal Entry: read-only, data-backed. Creation stays excluded or deferred.
- Supplier balances and history.
- Material Request: image and guided creation.
- Stock Entry: data-backed, plus guided creation.
- Stock Ledger: real movements asserted.
- BOM: image and details.
- Work Order.
- Employee: organisational fields only.

Still partial:
- **Reports (results deliberately not read):** Accounts Receivable, Accounts Payable, Trial Balance, P&L, Balance Sheet and the other accounting reports.
- **Smoke-tested only:** Payment Request, Dunning, Opportunity, Contact, Customer Group, Prospect, RFQ, Supplier Quotation, Supplier Group, Item Group, Stock Reconciliation, Serial No, Batch, Pick List, Brand, Job Card, Production Plan, Timesheet, Asset, Asset Category, Subcontracting Order/Receipt, Warranty Claim and Quality Inspection.
- **Material consumption:** consumed quantities aren't asserted.
- **Inventory reports:** described, not run.

### I.5.2 By interaction mode

| Mode | Count | Evidence |
| --- | --- | --- |
| Image extraction → Draft | 11/11 authorized types implemented, each with a persisted-Draft test (provider mocked; real classification, mapping, validation, `insert()`); duplicate re-confirmation refused for every type | `test_document_direction.TestDocumentTypes` (and `test_sales_invoice_mapping`, `test_chat_agent`) |
| Image types refused by policy | Supplier Quotation, a customer's goods receipt note, unknown titles (e.g. Proforma Invoice), debit note of unknown issuer; PDF not supported | `TestDirectionPolicy`, `TestImageChatFlows` |
| Data Q&A | 54 allowlisted DocTypes; 24 with data-backed manifest entries (`capability/inventory.py:VERIFIED`) | `docs/coverage_inventory.md` |
| Intents | 40 (37 + `outstanding_by_supplier`, page `show_list`, page `list_count`), all with handlers and tests; model-written text checked in 3 | §I.3 |
| Guided creation | 14 types: 9 Drafts (SI, PI, SO, PO, Quotation, DN, PR, Material Request, Stock Entry) + 5 saved-immediately (Customer, Supplier, Item, Lead, Issue), all with persisted-record tests | `test_chat_forms`, `test_chat_guided`, `test_chat_creation_requirements`, `test_coverage_batch`, `test_coverage_phase_d.TestGuidedStockDocuments` |
| Page-aware | form, list (show/count/summary/filters/selected), report (description + "not run"), workspace numbers; opaque open-record references for model plans and creation | `test_page_context`, `test_page_reference_and_parity` |
| Security | §I.6 | |

## I.6 Invariants re-verified

| Invariant | How it is enforced | Test |
| --- | --- | --- |
| System Manager only | DocType permissions + `session._owned` on every request; launcher hidden by the server flag | `test_page_context.test_only_system_managers_can_use_the_chat_with_any_context`, `TestLauncherFlag` |
| Draft only; no submit/cancel/delete/amend/pay | Builders set `docstatus = 0` and re-check after insert; static check finds exactly one `ignore_permissions` (the job's own entry save) and no lifecycle call | `test_security_hardening.TestDraftOnly`, `test_lifecycle_safety`, every creation test asserts `docstatus == 0` |
| No editing of existing records | No edit path; `unsupported` mode | `test_page_context.test_edit_requests_do_not_use_unsaved_values…` |
| Company isolation and permissions | Unchanged query layer; the classifier only uses the user's usable companies; return/original-invoice and duplicate lookups use permission-aware `get_list` | `test_company_isolation`, `TestSupplierOutstanding.test_outstanding_by_supplier_follows_company_permissions`, `TestOpenRecordReference.test_a_forged_or_inaccessible_open_record_is_not_used` |
| Client context untrusted | Page context still validated server-side; opaque references only | `test_page_context`, `test_page_reference_and_parity` |
| Model output can't act | Plans validated; text checked | `test_claims_and_network` |
| No partial transactions / duplicates | Savepoints, row locks, `created_document` check, client_id dedupe | `TestDocumentTypes` (second confirmation raises), `test_lifecycle_safety` |
| No network in tests | Socket guard | `TestNetworkGuard` |
| No secrets/content in logs | Static log check (also caught a comment in this phase) | `test_security_hardening.TestLogging` |

## I.7 Remaining, deferred and excluded

| Item | Status | Reason / next step |
| --- | --- | --- |
| Report results | Excluded by policy | Needs a per-report permission and company-isolation proof before `REPORT_RESULTS_ENABLED` changes |
| Journal Entry creation | Deferred | Party accounts, cost centres, multi-currency and balancing need a separate design |
| Payment Entry creation | Excluded | Payments are out of scope |
| Custom `Proposal` DocType | Deferred | No company field (no company isolation), holds personal recipient data, app not installed in CI (a static policy entry would break CI). Proposed design: let the `proposals` app register a reviewed query spec through a hook |
| HR (Attendance, Leave, Expense Claim) | Blocked | HRMS is not installed; nothing was installed or changed |
| Payroll | Excluded | Sensitive compensation data |
| 24 smoke-only query features | Partial | Fixture-backed tests per DocType (§I.5.1) |
| Manufacturing reports, ageing buckets | Not implemented | Depend on report results |
| "Explain this" on Bin/GL Entry/Stock Ledger Entry forms | Open | These DocTypes have no `details` operation; return a page answer instead of a capability error |
| Business-duplicate check in guided creation | Open | Warn before confirming (e.g. the same supplier `bill_no`) |
| Browser end-to-end tests | Not possible without new infrastructure | Node syntax and guard checks only |
| CI on the newest ERPNext v16 | Not run | Run CI (or the scratch-clone procedure) before release |

## I.8 Manual steps and verification that was not performed

1. **Migrate** each site after deploying: `bench --site <site> migrate`. This adds the "Data Retention" setting to AI
   Provider Settings and stores its default of 180 days once. No other schema change was made.
2. **Rebuild the Desk bundle:** `bench build --app ai_erpnext`. The widget and review dialog changed; the committed
   `public/dist` bundle was not rebuilt in this phase.
3. **Prompt B3 with a live model was not verified** (no paid calls were allowed). Suggested manual check on a non-production
   site:
   - extract 2–3 real samples of each kind (our invoice, a supplier invoice, a customer PO, a supplier delivery challan, a
     credit note);
   - confirm that `issuer_name`, `recipient_name`, `supplier_name` and `customer_name` are filled as defined;
   - confirm that the classification shown on the confirmation card is right.
   To roll back to B2, set `EXTRACTION_PROMPT = PROMPT_B2`: B2 extractions still classify through `supplier_name` /
   `customer_name`.
4. **Router behaviour** for the new `ref`/`open_record` and `outstanding_by_supplier` plans is tested with mocked plans;
   whether the live model emits them for natural phrasings is unverified. The deterministic routes ("…for this
   customer", "show my current list", "credit notes") work without the model.
5. No paid provider call was made. The test site has no provider key, and the suite ran with the socket guard and a
   dead HTTP proxy.

## I.9 Test execution (this phase)

Command (isolated `ci-test.local`, Docker MariaDB, no provider key, dead HTTP proxy, socket guard active):
`bench --site ci-test.local run-tests --app ai_erpnext`

| Run | Collected | Passed | Failed | Skipped | Notes |
| --- | --- | --- | --- | --- | --- |
| Baseline, first attempt | 283 | — | 9 failures, 49 errors | 0 | Redis was down (the bench was restarted during the run): environment, not code |
| Baseline (HEAD = audited code) | 471 | 471 | 0 | 0 | |
| After Phase A | 528 | 527 | 1 | 0 | a log-content static check caught a comment on a log line: fixed |
| After Phase B | 553 | 552 | 1 | 0 | a line-number pin in a static check: made content-based |
| After Phases C/D | 573 | 571 | 2 | 0 | two tests pinned the old creation scope (Stock Entry/Material Request query-only; bare "create lead" via the model): updated |
| **Final** | **573** | **573** | **0** | **0** | 196 s, 29 modules |

Kinds of tests in the final run: all 573 are Frappe integration tests against the local test database. Provider calls are
always mocked or faked; the HTTP transport is scripted where the SDK is exercised. The new modules are:

- `test_document_direction` (42): the direction policy, plus persisted Drafts for every image type;
- `test_claims_and_network` (15);
- `test_page_reference_and_parity` (25);
- `test_coverage_phase_d` (17): data-backed queries, guided stock documents, image content check, retention.

Frontend:

- a Node check of the page-context collector;
- a Node syntax check of every Desk script;
- source checks of the widget guard and labels.

Not run:

- browser end-to-end tests: none exist;
- CI against the newest ERPNext v16;
- live provider calls: not allowed.

Test-data hygiene: a `setUpClass` failure during development left fixtures committed on the test site, and so did one
direct call of the extraction job, which commits. Both were found, the data was removed, and the fixture classes now roll
back when their setup fails. After the final run no fixture of the new modules remains on the site.

---

# Part II: Baseline audit (commit 085bf0f, unchanged)

All statuses, counts, line numbers and recommendations below describe the code at `085bf0f`, before the implementation phase. Part I supersedes them.


| | |
| --- | --- |
| Audit date | 2026-10-21 |
| Code audited | `apps/ai_erpnext` at commit `085bf0f` ("on page question answers"), clean working tree |
| Frappe / ERPNext | Local bench checkout (version-16 line, Sep-2026). CI's "latest v16" was **not** re-run for this audit |
| Test site | `ci-test.local` (isolated Docker MariaDB on :3317; apps: frappe, erpnext, ai_erpnext, proposals; 1 throw-away company, no customer or invoice data, **no AI provider key configured**) |
| Scope | Audit only. No runtime code, prompt, mapping, schema, provider, permission or data was changed. The only file written is this report |

---

## 1. Executive summary

The chatbot is a **deterministic, server-validated assistant**. A language model is used for exactly two things: (a) proposing a JSON *plan* for free-text messages that no deterministic rule recognised (`chat/llm.py`), and (b) reading invoice images (`pipeline/processor.py`). Every plan is re-validated against one explicit allowlist policy (`capability/policy.py`) and executed with Frappe's permission-aware `get_list` / `get_doc`. No model output reaches SQL, and no record data is sent to the model.

**What works and is verified by passing data-backed tests**

- **Read-only data Q&A** over 54 allowlisted DocTypes (list, count, details, filters, aggregates with currency separation, child-table rows, one-hop links, party summaries, overdue/unpaid, sales summary, outstanding by customer), with company isolation and field permissions.
- **Guided conversational creation** of 12 types: 7 transactions created as **Draft** (Sales/Purchase Invoice, Sales/Purchase Order, Quotation, Delivery Note, Purchase Receipt) and 5 non-submittable records saved immediately (Customer, Supplier, Item, Lead, Issue).
- **Image → Draft Sales Invoice**, end to end (extraction mocked; mapping, validation and `insert()` real), with duplicate detection and idempotent confirmation.
- **Page-aware answers** for saved forms, list filters and selections, reports (name and filters only) and workspaces, all re-checked on the server.
- **Security posture**: System Manager only, owner-only conversations and extraction entries, history re-checked against current permissions, Draft-only creation, no lifecycle calls in application code, metadata-only logging.

**What is not there, or only partly there**

- **Image extraction supports one type only: Sales Invoice.** The extraction prompt also *recognises* Purchase Invoice, Purchase Order, Sales Order, Quotation and Delivery Note, but no mapping, validation or creation exists for them. PDF is refused.
- **Standard reports are never executed.** Report questions get a description of the report and its filters only. This is a deliberate decision (`REPORT_RESULTS_ENABLED = False`).
- **HR** (Attendance, Leave, Expense Claim) is blocked because HRMS is not installed. Payroll is deliberately excluded.
- **34 of 72 audited features are "path complete, result correctness not verified"**: a smoke test runs them, but no test asserts their returned values.
- **Page context is not available to the model-routed path.** "*this* customer" resolves only through a few deterministic phrasings.

**Most important findings** (details in §11)

| # | Priority | Finding |
| --- | --- | --- |
| 1 | P0 | Requirement "generic AI responses cannot falsely claim a transaction or a query succeeded" is enforced **only by the prompt**. The model's `chat`/`clarify`/`unsupported` text is shown as written, with no server-side check and no test |
| 2 | P1 | Image flow: the extracted `discount_amount` is used to validate totals but is **never applied to the draft**. A discounted invoice becomes a draft with a higher grand total, and the user gets no warning |
| 3 | P1 | Image flow: a document detected as another type (e.g. a supplier's Purchase Invoice) can be turned into a **Sales Invoice with one "Yes"**. This path is untested |
| 4 | P1 | The model-routed path cannot use the open record. List-summary phrasings outside the regex (e.g. "show my current list with its applied filters") go to the model, which cannot apply the page's filters |
| 5 | P2 | The README claims a test-wide "fake network". None exists. Test safety relies on per-test mocks plus the test site having no key |

**Test execution (observed):** 471 tests collected, **471 passed, 0 failed, 0 skipped** (238 s). All are Frappe integration tests against the local test database, with AI providers mocked. No live provider call was made.

---

## 2. Architecture and execution paths

```
Desk (any logged-in user loads the bundle)
 └─ public/js/ai_erpnext.bundle.js
     ├─ ai_bot_v2.js ......... chat widget; sends text / file_url / button action / client_id / page context
     ├─ ai_page_context.js ... bounded page description (form | list | report | route only)
     ├─ ai_document_entry.js . AI Document Entry form buttons (retry, review, re-map)
     └─ ai_review.js ......... review dialog → api.creation.create_document / legacy "Open Prefilled Form"

api/chat.py (POST only) ─ get_session · send_message · poll · new_conversation · list_conversations · open_conversation
 └─ chat/session.py ..... owner + DocType-permission check on every request, row lock, 7-day expiry
 └─ chat/engine.py:handle_message
     ├─ client_id de-duplication ──────────────────────────────── (engine.py:156-168)
     ├─ page_context.parse(context) ── validated PageContext ──── (page_context.py:129)
     ├─ file_url → _attachment → AI Document Entry (Pending) → background job
     │      pipeline/controller.run_ai_pipeline → providers.run("extract") → DocumentProcessor (Prompt B2)
     │      → review_service.apply_mapping → detector → registry → adapter → mapper → validator
     │      engine.poll/advance → questions → confirm → api.creation.create_document → builder.create_draft (Draft)
     ├─ button action → pending question (_answer) / "more" / "say"
     └─ text → control commands (cancel, start over, help, retry, why, corrections, switch)
              → pending question?  → answer it, or treat as a new request
              → _general:
                  1. page_context.answer (deterministic page intents)            no AI
                  2. shortcuts.match (greetings, list/count, field questions)     no AI
                  3. bare "create <type>"                                         no AI
                  4. llm.route → plan {chat|query|open|more|clarify|create|unsupported}
                       query  → queries.validate → queries.execute (get_list / get_doc, company-scoped)
                       create → forms.start → guided questions → confirm → insert() (Draft)
                       chat / clarify / unsupported → model text shown (labelled "General answer")
 └─ history.recheck: every stored ERPNext result re-checked against the reader's current permissions on replay

api/mapping.py, api/creation.py, api/validation.py, api/extraction.py (POST): AI Document Entry review (owner only)
hooks.py: doc_events (AI Document Entry validate/before_save, AI Provider Settings validate), has_permission +
          permission_query_conditions (AI Document Entry owner-only), daily scheduler cleanup_expired_sessions,
          before_tests bootstrap, after_install/after_migrate schema setup
```

**Whitelisted endpoints:** 13 in total (6 chat, 7 review/extraction). All declare `methods`; 12 are POST-only. `get_supported_doctypes` also allows GET and is read-only.

---

## 3. Master module and feature coverage matrix

### 3.1 Counting rules

- **Category**: a business area from the audit brief. ERPNext modules are grouped into 12 categories: Accounting & Finance; Selling & CRM; Buying; Stock; Manufacturing; Projects; HR; Assets; Subcontracting; Support; Quality; Custom apps.
- **Feature**: one user-facing capability on one DocType or report. The list is every item in the audit brief, de-duplicated (Sales Invoice and Purchase Invoice are listed once), plus every additional DocType found in the capability policy, plus the custom app's DocTypes. Total: **72 features**.
- **Overall status** (one per feature):
  - **Implemented**: at least one interaction mode has a working path AND a passing test asserts real returned or created values.
  - **Partial**: the path exists but its results are only smoke-tested, or a material sub-capability is missing (e.g. a report described but not computed).
  - **Not implemented**: no path in any mode.
  - **Blocked**: the path cannot work because of a missing dependency.
  - **Intentionally excluded**: refused by an explicit policy or safety decision.
- **Per-mode codes**: **S** supported · **P** partial · **N** not implemented · **X** intentionally excluded · **B** blocked · **–** not applicable.
- **Tests column**: **D** data-backed passing test · **Sm** smoke-only (`test_coverage_batch.test_every_registered_doctype_lists_counts_and_aggregates_without_error`) · **M** missing.
- **Columns**: Img = image extraction · Img→Draft = image Draft creation · Q&A = data Q&A · Conv = general conversation (G = built-in guidance in `chat/capabilities.py`, works without AI; A = AI model only, accuracy not verifiable) · Create = conversational creation · Page = page-aware.
- **Permissions**: every policy DocType goes through the same permission-aware, company-scoped code path. "Verified" means a test exercises permissions or company isolation **for that DocType**. "Shared" means the same path, verified generically (`test_coverage_batch.test_a_user_without_access_gets_a_clear_refusal_for_every_doctype`, `test_company_scoping_of_new_company_doctypes`).

### 3.2 Matrix

Evidence key: `pol:N` = `capability/policy.py` line N (the DocType's `Spec`); `forms` = `chat/forms.py` `SPECS`; tests are named by module and method.

#### Accounting & Finance (18)

| Feature | DocType / Report | Img | Img→Draft | Q&A | Conv | Create | Page | Perms | Tests | Overall | Evidence | Gaps | Recommendation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Sales Invoice | Sales Invoice | S | S | S | G | S | S | Verified | D | **Implemented** | pol:48; registry/definitions.py:5; forms; test_chat_agent.test_image_with_text_creates_draft_after_confirmation; test_company_isolation.*; test_page_context.test_explain_this_invoice_uses_saved_data_only | Discount not applied in image draft (§11 P1-1) | Fix discount; add test |
| Purchase Invoice | Purchase Invoice | P (recognised only; Desk legacy prefill) | N | P | G | S | S | Shared | D (create) / Sm (Q&A) | **Implemented** | pol:53; forms; test_chat_forms.test_purchase_invoice_step_by_step | No image mapping; Q&A values untested | Add Q&A data test; image type is P4 |
| Payment Entry | Payment Entry | N | N | P | G | X (payments) | S | Shared | Sm | Partial | pol:78 | Results unverified | Data-backed test |
| Journal Entry | Journal Entry | N | N | P | A | N | S | Shared | Sm | Partial | pol:129 (`accounts` child) | Results unverified | Data-backed test |
| Credit Note / Sales Invoice Return | Sales Invoice (`is_return`) | N | N | P | A | N | S | Shared | M | Partial | `status` filter "Return" works (status options include Return); `_sales_summary` nets returns (queries.py:1074); overdue/unpaid exclude returns (queries.py:446) | `is_return` is not an allowlisted field; returns can't be filtered except by status | Add `is_return` to policy fields + test (P3) |
| Debit Note / Purchase Invoice Return | Purchase Invoice (`is_return`) | N | N | P | A | N | S | Shared | M | Partial | as above | as above | as above |
| Accounts Receivable | Report + SI ops | – | – | P | A | – | P (report described only) | Verified (SI) | D (outstanding ops) | Partial | policy.py:283 `REPORTS`; queries.py:1097 `_outstanding_by_customer`; test_chat_queries.test_outstanding_by_customer | No ageing buckets; report results not read (deliberate) | Keep exclusion until report permission model is proven |
| Accounts Payable | Report + PI ops | – | – | P | A | – | P | Shared | Sm | Partial | policy.py:285; PI `overdue`/`unpaid` | No outstanding-by-supplier op; no ageing | Add `outstanding_by_supplier` (P3) |
| Outstanding invoices | SI / PI | – | – | S | A | – | S | Verified | D | **Implemented** | queries.py:441 `_base_filters`; test_chat_queries.test_overdue_uses_accounting_criteria; test_chat_lookup.test_count_of_outstanding_invoices; test_coverage_batch.test_overdue_by_more_than_n_days | PI variant untested | – |
| General Ledger | GL Entry + report | – | – | S | A | – | P (report described) | Verified | D | **Implemented** | pol:160; test_coverage_batch.test_gl_entry_totals_are_company_scoped_and_use_company_currency, test_gl_entries_balance_and_cancelled_entries_are_not_counted | Report itself not run (deliberate) | – |
| Trial Balance | Report | – | – | P (GL sums by account possible) | A | – | P | Shared | M | Partial | policy.py:289 | No opening/closing balance computation | P4 (needs report results decision) |
| Profit and Loss | Report | – | – | N | A | – | P | – | M | Partial | policy.py:293 | Description only | P4 |
| Balance Sheet | Report | – | – | N | A | – | P | – | M | Partial | policy.py:291 | Description only | P4 |
| Other accounting reports | Sales Register, Purchase Register, Gross Profit | – | – | N | A | – | P | – | test_page_context.TestReports (description) | Partial | policy.py:299-304 | Description only | P4 |
| Account (chart of accounts) | Account | – | – | S | A | N | S | Verified | D | **Implemented** | pol:154; test_coverage_batch.test_company_scoping_of_new_company_doctypes | – | – |
| Cost Center | Cost Center | – | – | S | A | N | S | Verified | D | **Implemented** | pol:157; same test | – | – |
| Payment Request | Payment Request | – | – | P | A | N | S | Shared | Sm | Partial | pol:165 | Results unverified | Data-backed test |
| Dunning | Dunning | – | – | P | A | N | S | Shared | Sm | Partial | pol:169 | Results unverified | Data-backed test |

#### Selling & CRM (12)

| Feature | DocType | Img | Img→Draft | Q&A | Conv | Create | Page | Perms | Tests | Overall | Evidence | Gaps | Recommendation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Customer | Customer | – (matched) | S (nested master creation) | S | G | S (saved immediately) | S | Verified | D | **Implemented** | pol:83; test_chat_lookup.test_mobile_number_of_a_named_customer; test_chat_agent.test_missing_customer_is_created_only_after_explicit_approval; test_chat_guided.test_explicit_create_new_customer_for_a_sales_order | – | – |
| Lead | Lead | – | – | S | A | S (saved immediately) | S | Verified | D | **Implemented** | pol:120; test_coverage_batch.test_lead_is_created_after_confirmation_and_is_described_as_saved_immediately | – | – |
| Opportunity | Opportunity | – | – | P | A | N | S | Shared | Sm | Partial | pol:124 | Results unverified | Data-backed test |
| Quotation | Quotation | P (recognised only) | N | S | G | S | S | Verified | D | **Implemented** | pol:105; test_generic_queries.test_quotation_is_queryable_and_still_creatable; test_chat_creation_requirements.test_each_supported_doctype_follows_its_own_warehouse_rule | No image mapping | P4 |
| Sales Order | Sales Order | P (recognised only; Desk legacy prefill) | N | S (summary D; list Sm) | G | S | S | Shared | D (create, summary) | **Implemented** | pol:57; test_chat_forms.test_prefilled_sales_order_asks_only_missing_fields | List results unverified | Add list test |
| Delivery Note | Delivery Note | P (recognised only) | N | P | G | S | S | Shared | D (create) / Sm | **Implemented** | pol:65; warehouse-rule test above | Q&A values untested | Add test |
| Pricing and item lookup | Item, Item Price, Price List | – | – | S (Item) / P (prices) | G | – | S | Verified (Item, price suggestions) | D (Item, `_price`) / Sm (Item Price) | **Implemented** | pol:94, 199, 201; forms.py:819 `_price`; test_security_hardening.test_price_suggestions_respect_item_permissions; test_chat_queries.test_item_search | Price-list queries untested | Add Item Price test |
| Customer balances and transaction history | Customer `summary`, outstanding | – | – | S | A | – | S | Verified | D | **Implemented** | queries.py:897 `_party_summary`, :953 `_party_outstanding`; test_coverage_batch.test_customer_summary_is_company_scoped_and_currency_aware; test_chat_lookup.test_outstanding_of_a_customer | – | – |
| Contact | Contact | – | – | P | G | N | S | Shared | Sm (+ used by "auto" lookups) | Partial | pol:91; queries.py:1030 `_missing` (contact fallback) | Shared master: visible across companies (by design) | Data-backed test |
| Customer Group | Customer Group | – | – | P | A | N | S | Shared | Sm | Partial | pol:206 | – | – |
| Territory | Territory | – | – | S | A | N | S | Verified | D | **Implemented** | pol:208; test_coverage_batch.test_shared_masters_follow_user_permissions | – | – |
| Prospect | Prospect | – | – | P | A | N | S | Shared | Sm | Partial | pol:212 | – | – |

#### Buying (8)

| Feature | DocType | Img | Img→Draft | Q&A | Conv | Create | Page | Perms | Tests | Overall | Evidence | Gaps | Recommendation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Supplier | Supplier | – | – | S | G | S (saved immediately) | S | Shared | D | **Implemented** | pol:87; test_chat_lookup.test_supplier_lookup_by_exact_name; test_chat_guided.test_explicit_create_new_supplier_needs_its_own_confirmation | – | – |
| Material Request | Material Request | N | N | P | A | N | S | Shared | Sm | Partial | pol:115 | No creation; results unverified | P4 creation |
| Request for Quotation | RFQ | N | N | P | A | N | S | Shared | Sm | Partial | pol:215 | – | – |
| Supplier Quotation | Supplier Quotation | N | N | P | A | N | S | Shared | Sm | Partial | pol:110 | – | – |
| Purchase Order | Purchase Order | P (recognised only; Desk legacy prefill) | N | P | G | S | S | Verified (warehouse company scope) | D (create) / Sm (Q&A) | **Implemented** | pol:61; test_chat_guided.test_purchase_order_from_existing_records_with_corrections; test_chat_creation_requirements.test_restricted_user_only_sees_and_uses_their_companys_warehouses | "Awaiting receipt" questions rely on the model (status / `per_received`) | Add test |
| Purchase Receipt | Purchase Receipt | N | N | P | G | S | S | Shared | D (create) / Sm | **Implemented** | pol:69; warehouse-rule test | – | – |
| Supplier balances and purchase history | Supplier `summary` | – | – | P | A | – | S | Shared | M | Partial | queries.py:897 (Supplier branch) | No data-backed test | Add test |
| Supplier Group | Supplier Group | – | – | P | A | N | S | Shared | Sm | Partial | pol:210 | – | – |

#### Stock (12)

| Feature | DocType / Report | Img | Img→Draft | Q&A | Conv | Create | Page | Perms | Tests | Overall | Evidence | Gaps | Recommendation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Item | Item | – (matched) | S (nested master creation) | S | G | S (saved immediately) | S | Verified | D | **Implemented** | pol:94; test_chat_agent.test_missing_item_is_created_only_after_explicit_approval; test_chat_forms.test_item_master_creation | – | – |
| Item Group | Item Group | – | – | P | A | N | S | Shared | Sm | Partial | pol:196 | – | – |
| Warehouse | Warehouse | – (matched) | – | S | G | N | S | Verified | D | **Implemented** | pol:97; test_coverage_batch.test_company_scoping_of_new_company_doctypes | – | – |
| Bin / stock balance | Bin | – | – | S | A | – | P (no `details`: "explain this" fails) | Verified | D | **Implemented** | pol:136; test_generic_queries.test_bin_respects_company_and_warehouse_permissions | "Available stock for *this item*" via the model can't use the open Item | §11 P1-3 |
| Stock Entry | Stock Entry | N | N | P | G | N | S | Shared | Sm | Partial | pol:73 | – | – |
| Stock Reconciliation | Stock Reconciliation | N | N | P | A | N | S | Shared | Sm | Partial | pol:183 | – | – |
| Serial No | Serial No | – | – | P | A | N | S | Shared | Sm | Partial | pol:186 | – | – |
| Batch | Batch | – | – | P | A | N | S | Shared | Sm | Partial | pol:189 | – | – |
| Stock Ledger | Stock Ledger Entry + report | – | – | P | A | – | P (no `details`) | Shared | Sm (see §9.4) | Partial | pol:179; test_coverage_batch.test_stock_ledger_rows_never_expose_valuation | The inventory claims "data-backed read", but the fixture has no stock movements | Add stock-moving fixture |
| Inventory reports | Stock Balance, Stock Ledger | – | – | N | A | – | P (described only) | – | test_page_context.TestReports | Partial | policy.py:295-298 | Description only | P4 |
| Pick List | Pick List | – | – | P | A | N | S | Shared | Sm | Partial | pol:192 | – | – |
| Brand | Brand | – | – | P | A | N | S | Shared | Sm | Partial | pol:198 | – | – |

#### Manufacturing (6)

| Feature | DocType | Img | Img→Draft | Q&A | Conv | Create | Page | Perms | Tests | Overall | Evidence | Gaps | Recommendation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BOM | BOM | N | N | P | A | N | S | Shared | Sm | Partial | pol:219 | No image or creation path | P4 |
| Work Order | Work Order | – | – | P | A | N | S | Shared | Sm | Partial | pol:223 | – | – |
| Job Card | Job Card | – | – | P | A | N | S | Shared | Sm | Partial | pol:228 | – | – |
| Production Plan | Production Plan | – | – | P | A | N | S | Shared | Sm | Partial | pol:232 | – | – |
| Material consumption | Work Order `required_items.consumed_qty` | – | – | P | A | – | S | Shared | Sm | Partial | pol:227 | – | – |
| Manufacturing reports | – | – | – | N | A | – | N | – | M | Not implemented | No entry in `REPORTS` | – | P4 |

#### Projects (3), HR (5), Assets (2), Subcontracting (2), Support (2), Quality (1), Custom (1)

| Feature | DocType | Img | Img→Draft | Q&A | Conv | Create | Page | Perms | Tests | Overall | Evidence | Gaps | Recommendation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Project | Project | – | – | S | A | N | S | Verified | D | **Implemented** | pol:140; test_generic_queries.test_new_doctypes_are_queryable_with_company_isolation | – | – |
| Task | Task | – | – | S | A | N | S | Verified | D | **Implemented** | pol:145; test_generic_queries.test_records_without_a_company_stay_visible_only_where_the_doctype_allows_it, test_new_doctype_aggregates_are_company_scoped | – | – |
| Timesheet | Timesheet | – | – | P | A | N | S | Shared | Sm | Partial | pol:250 (rates/costing excluded) | – | – |
| Employee | Employee (organisational fields only) | – | – | P | G | N | S | Shared | Sm (see §9.4) | Partial | pol:101; test_chat_queries.test_disallowed_doctypes_fields_and_limits (refuses `ctc`, `bank_ac_no`) | The inventory claims "data-backed read", but no test reads an Employee record | Add test |
| Attendance | (HRMS) | – | – | B | A | B | – | – | – | **Blocked** | DocType absent: HRMS not installed | – | Decide whether HRMS is in scope |
| Leave Application | (HRMS) | – | – | B | A | B | – | – | – | **Blocked** | as above | – | as above |
| Expense Claim | (HRMS) | – | – | B | A | B | – | – | – | **Blocked** | as above | – | as above |
| Payroll | Salary Slip etc. | – | – | X | A | X | – | – | test_capability.test_no_listed_field_is_a_secret | **Intentionally excluded** | policy.py:263 (`salary`, `ctc`, `iban` prohibited); inventory `BLOCKED_REASONS["Salary Slip"]` | – | Keep excluded |
| Asset | Asset | – | – | P | A | N | S | Shared | Sm | Partial | pol:173 | – | – |
| Asset Category | Asset Category | – | – | P | A | N | S | Shared | Sm | Partial | pol:239 | – | – |
| Subcontracting Order | Subcontracting Order | – | – | P | A | N | S | Shared | Sm | Partial | pol:241 | – | – |
| Subcontracting Receipt | Subcontracting Receipt | – | – | P | A | N | S | Shared | Sm | Partial | pol:245 | – | – |
| Issue | Issue | – | – | S | A | S (saved immediately) | S | Verified | D | **Implemented** | pol:149; test_coverage_batch.test_issue_is_created_with_an_optional_customer | – | – |
| Warranty Claim | Warranty Claim | – | – | P | A | N | S | Shared | Sm | Partial | pol:254 | – | – |
| Quality Inspection | Quality Inspection | – | – | P | A | N | S | Shared | Sm | Partial | pol:235 | – | – |
| Proposal (custom app `proposals`) | Proposal | – | – | N | A | N | N (named "not readable") | – | M | Not implemented | Not in `REGISTRY`; `page_context._unavailable` names it only | – | P4, if wanted |

The custom app `estate_notice` contains no DocTypes in source, so it contributes no features.

---

## 4. Image extraction coverage

### 4.1 Pipeline (verified by reading the code)

1. **Upload** (`engine._attachment`, engine.py:697): the File must be owned by the user; extension must be png/jpg/jpeg/webp; size ≤ 10 MB. **PDF is refused** ("PDF files aren't supported yet"). The type check uses the file extension, not its content.
2. **Entry**: an `AI Document Entry` is inserted as Pending. The `validate` hook (controller.py:20) requires that the user can already read the file, which blocks adopting another user's private file.
3. **Job**: `enqueue_extraction` (controller.py:8) uses a deduplicated job id. `run_ai_pipeline` (controller.py:56) skips entries that are not Pending or already created. It calls `providers.run("extract")` with `policy.call_with_retries` (3 attempts, 60 s budget; the paid fallback gets 1 attempt, and only for an exhausted Groq daily quota).
4. **Prompt**: "Prompt B2" (processor.py:11), pinned by SHA-256 in tests. Its `document_type` enum lists **6** types.
5. **Mapping** (`review_service.apply_mapping`): detector (document_detector.py:12) → registry (`document_registry.supported_doctypes()` → `["Sales Invoice"]`) → `SalesInvoiceAdapter` → `SalesInvoiceMapper` → `SalesInvoiceValidator`.
6. **Questions**: engine.py:852 `_plan` asks, in order: company, customer, duplicate, item rows, UOM, posting/due date, currency, warehouse, other blocking issues, tax template, data errors. Missing customers and items can be created after their own confirmation.
7. **Create**: `api.creation.create_document` (creation.py:11) row-locks the entry, re-maps and re-validates (stored results are never trusted), then calls `SalesInvoiceBuilder.create_draft` → `insert()` with `docstatus = 0` and a post-insert check.
8. **Confirmation**: a "created" card with an Open button (engine.py:1379 `_created_card`).

### 4.2 Coverage table

| Document type | Detection | Header extraction | Child rows | Validation | Draft creation | Link returned | Integration tests | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Sales Invoice** | Registry key + 4 aliases; confidence ≥ 0.80; minimum structure | customer, company (issuer → own company), invoice no., invoice/due date, currency, warehouse, subtotal, discount, taxes, total | item_code, item_name, description, qty, uom, rate, amount, warehouse (≤ 500 rows) | Master matching (fuzzy matches are never auto-accepted), sales-item flag, UOM conversion, qty/rate/amount, subtotal/tax/total, due ≥ posting, fiscal year, exchange rate, party-account currency, tax-template note, duplicates | **Yes**, Draft via `insert()` | Yes (card + Open button; Desk dialog routes to the form) | **Passing** (provider mocked): test_chat_agent.test_image_with_text_creates_draft_after_confirmation, test_lifecycle_safety.test_image_chat_flow_creates_a_draft, test_review_api_creates_a_draft, test_second_create_never_duplicates_the_draft, test_sales_invoice_mapping.test_20_erpnext_draft_creation (+28 mapping/validation tests) | **Implemented** (gaps: discount, §11 P1-1) |
| Purchase Invoice | Prompt enum only; detector reports "unregistered" | Same prompt keys are returned | Same | None | No. Chat: "I can only create Sales Invoices… is this a sales invoice?" Desk: "Open Prefilled Form" (legacy mapper: supplier/date/items by exact name) | No | Only `get_definition("Purchase Invoice") is None` | Not implemented (recognition only) |
| Purchase Order | Prompt enum only | Same | Same | None | No (Desk legacy prefill: supplier, transaction_date, items) | No | None | Not implemented (recognition only) |
| Sales Order | Prompt enum only | Same | Same | None | No (Desk legacy prefill: customer, transaction_date, items) | No | None | Not implemented (recognition only) |
| Quotation | Prompt enum only | Same | Same | None | No (legacy mapper fills items only) | No | None | Not implemented (recognition only) |
| Delivery Note | Prompt enum only | Same | Same | None | No (legacy mapper fills items only) | No | None | Not implemented (recognition only) |
| Purchase Receipt | Not in prompt enum | – | – | – | No | – | None | Not implemented |
| Material Request | Not in prompt enum | – | – | – | No | – | None | Not implemented |
| BOM | Not in prompt enum | – | – | – | No | – | None | Not implemented |
| Credit Note (SI return) | Not in enum; the prompt classifies billing documents as "Sales Invoice" | – | – | No return handling | No | – | None | Not implemented |
| Debit Note (PI return) | Not in enum; the prompt says a DEBIT MEMO heading is still "Sales Invoice" (pinned by test_extraction_safety.test_debit_memo_heading_is_never_auto_classified) | – | – | – | No | – | None | Not implemented |
| PDF (any type) | Refused at upload | – | – | – | – | – | test_chat_agent.test_attachment_validation | Not implemented (counted separately from image types) |

### 4.3 Mismatches between registry, prompt, mapping, validation, creation and UI

1. **Prompt vs registry**: the prompt asks the model to classify 6 types; the registry supports 1. In chat, any other type gets a one-click "Yes, it's a Sales Invoice" override (engine.py:837-842 and 1095-1101). No test covers it (§11 P1-2).
2. **Discount**: `discount_amount` is extracted (adapters.py:59) and used in the grand-total check (validation/totals.py:62), but `supported_fields` (definitions.py:31) has no discount field and the builder never sets one (§11 P1-1).
3. **Taxes**: per-component taxes are extracted, but only their sum is used. Tax rows are created only from a user-chosen Sales Taxes and Charges Template, or skipped with a warning. This is documented behaviour.
4. **Declared vs extracted fields**: `extraction_schema` (definitions.py:14) lists `customer_address`, `company_name`, `terms`, `warehouse` and item `warehouse`, but Prompt B2 asks for none of them. The issuer is read from `supplier_name`. `review_payload` still shows `terms`, which is always empty with the current prompt.
5. **Caption filter**: `engine.UNSUPPORTED` (engine.py:50) refuses an image whose caption mentions purchase documents, sales orders, quotations, delivery notes or "payment(s)". This also catches harmless captions such as "payment due next week" (false refusal; §11 P3-9).
6. **Desk legacy path**: `pipeline/mapper.py:4` `ERPNextMapper` uses `frappe.db.get_value` (no permission check) to match supplier, customer and item by exact name for the "Open Prefilled Form" button (ai_review.js:183, 245). It reveals only names, and only to System Managers (§11 P3-8).

---

## 5. Normal chat and data retrieval coverage

### 5.1 Counting rule

An **intent** is a distinct server-side handler that a user message or button can reach. Deterministic shortcuts are *routes* into those same handlers and are listed separately. Total: **37 intents**:

- 6 router modes other than `query`;
- 10 query operations;
- 10 page intents;
- 9 control commands;
- image upload;
- built-in guidance fallback.

### 5.2 Intent inventory

| # | Intent | Example | Routing | Handler | Live data | Permission enforcement | Response | Page-aware | Tests | Gaps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `chat` | "What does the Stock module do?" | Model, or shortcut (greeting/thanks) | engine.py:424 | **No** (model text) | n/a | Model text + capabilities footer, labelled "General answer" | – | test_chat_queries.test_greeting_and_education_are_general_answers | Unfiltered model text (§11 P0-1) |
| 2 | `clarify` | "Which period?" | Model | engine.py:434 | No | n/a | Model question; repeat guard | – | test_chat_lookup.test_model_repeating_the_same_clarification_is_not_repeated | Model text unfiltered |
| 3 | `unsupported` | "Delete all invoices" | Model | engine.py:451 | No | n/a | Model text + fixed safety sentence; unsaved-form note | Uses `dirty` | test_chat_queries.test_unsupported_action_is_not_executed; test_page_context.test_edit_requests_do_not_use_unsaved_values… | Model text unfiltered |
| 4 | `create` | "create po" | Shortcut (bare) or model | engine.py:444 → `forms.start` | – | `has_permission(create)` | Guided form | **No** ("for this customer" is not prefilled) | test_chat_forms.*, test_chat_guided.* | Context prefill missing |
| 5 | `open` | "open the 2nd one" | Shortcut / model | engine.py:665 `_open_result` | Yes (name only) | `has_permission(read, name)` | Card with Open button | – | test_chat_queries.test_follow_up_open_and_paging | – |
| 6 | `more` | "show more" | Shortcut / model / button | engine.py:653 `_more` | Yes | Stored plan re-validated (`queries._recheck`) | Next page (offset ≤ 500) | – | test_follow_up_open_and_paging; test_generic_queries.test_pagination_is_bounded | – |
| 7 | query `list` | "show the last 10 sales invoices" | Shortcut / model | queries.py:519 `_list` | Yes | `get_list` + company filter + permitted fields | Table card, "From your… data" | Via shortcut `ref` only | test_chat_queries.test_invoice_list_with_date_filter_and_empty_result; test_generic_queries.* | – |
| 8 | query `count` | "how many unpaid sales invoices" | Shortcut / model | queries.py:727 | Yes | same | Sentence | – | test_chat_queries.test_count | – |
| 9 | query `details` | "mobile number of Amit Patel" | Shortcut / model | queries.py:862 `_details`, :984 `_field_answer` | Yes | `get_doc` + `check_permission` + permitted fields | Field-first answer or record card | **Yes** (shortcut `ref` → open form, last result, last created) | test_chat_lookup.* (24) | Model plans cannot reference the open record |
| 10 | query `aggregate` | "total sales by month this year" | Model | queries.py:654 | Yes | same; never sums across currencies | Grouped table | – | test_generic_queries.test_sum_is_grouped_by_currency_never_mixed and 4 more | – |
| 11 | query `child_rows` | "items of invoices containing X" | Model | queries.py:613 | Yes | Parents permission-filtered first | Table | – | test_generic_queries.test_child_rows_only_come_from_accessible_parents | – |
| 12 | query `summary` | "summarize customer ABC" | Shortcut / model | queries.py:897 | Yes | Per-source permission; skipped sources named | Table | – | test_coverage_batch.test_customer_summary_is_company_scoped_and_currency_aware | Supplier side untested |
| 13 | query `overdue` | "which invoices are overdue?" | Model / shortcut ("list overdue sales invoices") | queries.py:441 (`docstatus = 1`, outstanding > 0, not return, due < today) | Yes | same | Table | – | test_chat_queries.test_overdue_uses_accounting_criteria | – |
| 14 | query `unpaid` | "show outstanding sales invoices" | Model / shortcut ("unpaid") | same | Yes | same | Table | – | test_chat_lookup.test_count_of_outstanding_invoices | "outstanding" wording needs the model |
| 15 | query `sales_summary` | "summarize sales for this month" | Model | queries.py:1074 | Yes | same | Per company/currency table | – | test_chat_queries.test_sales_summary_respects_status_currency_and_period | – |
| 16 | query `outstanding_by_customer` | "who owes us the most?" | Model | queries.py:1097 | Yes | same | Table | – | test_chat_queries.test_outstanding_by_customer | No supplier counterpart |
| 17 | page `about` | "what is this page about?" | page_context.classify | page_context.py:722 | Form/list/workspace: yes | Server re-fetch | Delegates to explain/report/list | **Yes** | TestAnyPage.*, TestWorkspaces.* | – |
| 18 | page `card` | "total stock value?" (workspace) | classify (workspace) | page_context.py:927 | Yes | Number Card permission + validated aggregate; custom-method cards never run | Value sentence | Yes | TestWorkspaces.test_a_number_can_be_asked_for_directly | – |
| 19 | page `explain` | "explain this invoice" | classify (form) | page_context.py:481 | Yes | details path + status help | Record card + guidance | Yes | TestFormAnswers.test_explain_this_invoice_uses_saved_data_only | Bin, GL Entry and SLE have no `details` operation |
| 20 | page `rows` | "explain these invoice items" | classify (form) | page_context.py:503 | Yes | Child fields permitted; selected rows are saved rows only | Table + column meanings | Yes | test_explain_these_invoice_items, test_selected_child_rows_only… | – |
| 21 | page `fields` | "what do these fields mean?" | classify (form) | page_context.py:552 | Metadata only | Allowed fields only | Guidance table | Yes | test_what_do_these_fields_mean | – |
| 22 | page `unsaved` | "what did I change?" | classify (form) | page_context.py:572 | Saved values + browser values (labelled) | Allowed fields; ephemeral (not stored) | Table | Yes | test_unsaved_changes_are_labelled_shown_once_and_never_stored_or_written | – |
| 23 | page `selected` | "compare the selected orders" | classify (list) | page_context.py:594 | Yes | Selected names re-checked (≤ 20) | Table + reasons/totals | Yes | test_selected_records_are_validated_on_the_server, test_compare_selected_orders_and_why_pending | – |
| 24 | page `list` | "summarize the current filtered list" | classify (list) | page_context.py:688 | Yes | Filters translated via `normalize_where`; untranslatable ones named, never dropped | Count + totals | Yes | TestListAnswers.test_filtered_list_is_summarized_with_translated_filters | Narrow regex (§11 P1-4) |
| 25 | page `filters` | "what filters are applied?" | classify (list) | page_context.py:675 | No (describes validated filters) | – | Sentence | Yes | test_filters_describe_themselves | – |
| 26 | page `report` | "explain this report" | classify (report) | page_context.py:709 | **No** (report never run; deliberate) | Report permitted + ref DocType readable; filter values validated | Description | Yes | TestReports.* | "Summarize this report using the filters…" goes to the model |
| 27 | cancel | "cancel" | Regex | engine.py:723 / forms.cancel | – | – | – | – | test_chat_agent.test_user_can_correct_fields_and_cancel; test_chat_forms.test_cancel_top_level_and_nested | – |
| 28 | start over | "start over" | Regex | engine.py:740 | – | – | – | – | test_chat_guided.test_start_over_and_cancel (forms) | Image-flow start over untested |
| 29 | help | "help" | Regex | engine.py:315 | – | – | Static text | – | test_chat_agent.test_text_only_messages_and_unsupported_requests | HELP omits Lead/Issue |
| 30 | retry | "retry" | Regex / button | engine.py:759 | – | Owner-only entry | Re-queues a Failed entry only | – | test_chat_agent.test_provider_failure_offers_retry; test_extraction_safety.test_retry_requeues_failed_entries_only_once | – |
| 31 | why / status | "why?" | Regex | engine.py:777 `_explain` | Entry's own validation | Owner-only | Problem list | – | **Missing** | Add test |
| 32 | invoice field/row correction | "change customer to …", "row 2 item SKU1" | Regex | engine.py:327-337 | Matchers (permission-aware) | `get_list` | Re-plans | – | test_chat_agent.test_user_can_correct_fields_and_cancel | – |
| 33 | master field correction (image flow) | "set customer group to …" | Regex | engine.py:1284 | Matchers | `get_list` | Re-asks | – | **Missing** | Add test |
| 34 | form switch | "I meant Purchase Receipt" | Regex | forms.py:206 | – | `has_permission(create)` | Restarts, keeping values | – | test_chat_guided.test_switching_document_type_keeps_the_values | – |
| 35 | form correction | "row 1 qty 5", "remove row 1", "use Stores warehouse" | Regex | forms.py:322 | Matchers | `get_list` | Updates form | – | test_chat_forms.test_corrections_and_remove_row; test_chat_creation_requirements.* | – |
| 36 | image upload | paperclip | file_url | engine.py:287 | – | File owner; readable-file hook | Extraction flow | – | §4.2 | – |
| 37 | built-in guidance fallback | "how do I create a PO?" (AI down) | `capabilities.answer` on AI error | engine.py:416 | No | – | Built-in text, labelled | – | test_chat_lookup.test_known_questions_get_built_in_guidance_when_the_ai_is_down | 15 DocTypes covered |

### 5.3 How the audit-brief example questions are routed

The routing column below was **observed** by running the real `shortcuts.match`, `page_context.classify` and `CREATE_DOC_RE` against each phrase, with synthetic page objects and no AI call. "Model" means the answer depends on the live router model, whose behaviour was **not verified** (no live calls were allowed).

| Question | Page | Observed route | Data-backed handler if planned correctly | Assessment |
| --- | --- | --- | --- | --- |
| Show outstanding sales invoices | – | Model | `unpaid` | Supported if the model plans it; untested wording |
| What is the total outstanding amount for this customer? | Customer form | Model | `details`/`outstanding` | **Partial**: the model never sees the record name, so it asks "which customer?" |
| What is the outstanding amount of this customer? | Customer form | Shortcut with `ref` | `details`/`@outstanding` on the open record | Supported |
| Find purchase orders awaiting receipt | – | Model | `list` + status / `per_received` | Model-dependent; untested |
| What is the available stock for this item? | Item form | Model | Bin `aggregate` | **Partial**: "this item" can't be resolved by the model |
| Show the latest sales orders for this customer | Customer form | Model | `list` with party | **Partial**: same reason |
| Summarize sales for this month | – | Model | `sales_summary` | Supported (handler tested) |
| Show my current list with its applied filters | SI list | **Model** | – | **Gap**: the model can't apply the page filters and may return an unfiltered list (§11 P1-4) |
| What filters are applied? | SI list | Page `filters` | – | Supported |
| Summarize the current filtered list | SI list | Page `list` | count + totals | Supported |
| Explain the selected record | SI list | Page `selected` | list of selected | Supported |
| Summarize this report using the filters currently applied | Report | **Model** | – | Not supported. "Explain this report" works (description only); report results are excluded by design |
| Which invoices are overdue? | – | Model | `overdue` | Supported if planned; handler tested |
| Compare purchases and sales for a date range | – | Model | – | **Not implemented**: one plan = one DocType; no cross-DocType comparison |

### 5.4 Answers to the specific questions in the brief

- **Intents in the prompt without handlers**: none. All 7 modes and all 10 operations named in `llm.SYSTEM` (llm.py:141) have handlers in `engine._general` / `queries.execute`.
- **Handlers unreachable from the router**: `plan["query"]["ref"]` resolution (engine.py:482 `_resolve_ref`) is reachable only from shortcuts. The router prompt never documents `ref`, so model plans cannot use it.
- **Routes that fall back to generic AI**: every message not matched by a page intent, shortcut or bare create goes to the model. If the model chooses `chat`, the answer is generic text with no data. This is labelled "General answer" in the widget (ai_bot_v2.js:361).
- **Frontend-only features**: none found. Every button maps to a server-checked pending question (`question_id` must equal the pending id, engine.py:300).
- **Overlapping intents**: `CREATE_DOC_RE` (engine) and the router's `create` mode lead to the same `forms.start`. Shortcut list/count and model list/count produce identical plans (by design; pinned by test_chat_routing.test_shortcut_plans). No conflicting overlap was found.
- **Unsupported requests not safely rejected**: none found. Lifecycle verbs fail closed in `capability.refuse_lifecycle`, and plans are validated (test_capability.test_lifecycle_and_raw_operations_fail_closed). Gap: the *wording* of the model's refusal is not filtered (§11 P0-1).

---

## 6. Conversational document creation coverage

| Type | Draft / saved | Required-field collection | Record matching | Validation | Permissions | Prefill from text | Context prefill ("this customer") | Duplicate guard | Link returned | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Sales Invoice | Draft | Spec header + `requirements.inspect` (ERPNext defaulting) + mandatory custom fields | Existing records as buttons; "Create new" separate | Re-check before preview and before create; `insert()` final | `has_permission(create)`; company/warehouse scoped | Model `values`/`items` (validated) | **N** | Session client_id + row lock; no business-duplicate check | Card + Open | D (test_each_supported_doctype_follows_its_own_warehouse_rule asserts docstatus 0) | **Implemented** |
| Purchase Invoice | Draft | + `bill_no` (optional) | same | same | same | same | N | same (ERPNext's own supplier-invoice-uniqueness setting may apply) | same | D (test_purchase_invoice_step_by_step) | **Implemented** |
| Sales Order | Draft | + delivery date | same | + warehouse rule | same | same | N | same | same | D | **Implemented** |
| Purchase Order | Draft | + schedule date | same | + warehouse rule | same | same | N | same | same | D | **Implemented** |
| Quotation | Draft (`quotation_to` fixed to Customer) | – | same | same | same | same | N | same | same | D | **Implemented** |
| Delivery Note | Draft | – | same | + warehouse rule | same | same | N | same | same | D | **Implemented** |
| Purchase Receipt | Draft | – | same | + warehouse rule | same | same | N | same | same | D | **Implemented** |
| Customer | Saved immediately (not submittable; confirmation says so) | name, type, group, territory | Duplicate-name refusal (exact + normalised) | `insert()` | create permission | – | – | Name check | Card | D | **Implemented** |
| Supplier | Saved immediately | name, type, group | Duplicate-name refusal | same | same | – | – | Name check | Card | D | **Implemented** |
| Item | Saved immediately | code, name, group, UOM, stock flag | Existing code refused | same | same | – | – | Code check | Card | D | **Implemented** |
| Lead | Saved immediately | name + optional org/email/mobile | – | same | same | – | – | None | Card | D | **Implemented** |
| Issue | Saved immediately | subject + optional customer/description | – | same | same | – | – | None | Card | D | **Implemented** |
| Payment Entry | – | – | – | – | – | – | – | – | – | – | **Intentionally excluded** (payments) |
| Journal Entry, Stock Entry, Material Request, Credit/Debit Note, BOM, Work Order, other types | – | – | – | – | – | – | – | – | – | – | Not implemented ("I can't create that from the chat") |
| Editing existing records | – | – | – | – | – | – | – | – | – | test_page_context.test_edit_requests_do_not_use_unsaved_values… | **Intentionally excluded** |

**Safety mechanics (verified):**

- `capability.assert_creation_safe` (service.py:410) rejects any field outside the form's allowlist and every lifecycle or ownership field.
- `forms._create` (forms.py:1046) inserts inside a savepoint and rolls back on any error.
- `doc.submit` is patched to fail throughout test_lifecycle_safety.
- test_security_hardening.test_model_supplied_lifecycle_fields_never_reach_insert proves the guard.
- Text "create X" works without an AI provider (test_chat_forms.test_works_without_ai_service_and_refuses_unsupported).

**Decision to confirm**: the five non-submittable types are saved immediately rather than as Drafts. The README and the confirmation text state this clearly. It is not a violation of the "transactions are Draft-only" rule, but Lead and Issue are outside the invoice workflow, so the product owner should explicitly confirm they are in scope.

---

## 7. Page-aware context audit

The browser sends a hint (`public/js/ai_page_context.js`). The server (`chat/page_context.py`) treats it as untrusted, and every record and field is re-fetched with the user's permissions.

| # | Capability | Frontend source | Backend handling and validation | Server-verified? | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Current form identification | `formContext` (js:77); route must match `cur_frm` | `_parse` / `_form` (py:141, 182) | Yes | TestParsing.test_saved_form_context_is_validated_and_fresh | Supported |
| 2 | DocType and record name | same | `_queryable` (policy + read) + `_readable` (company-scoped `get_list`) + `has_permission(name)`; `new-…` ignored | Yes | test_inaccessible_missing_and_forged_records_are_indistinguishable; test_every_supported_doctype_may_be_the_page_but_nothing_else | Supported |
| 3 | Document field context | Only *changed* scalar fields when dirty; sensitive types skipped (js:18-21) | Saved values re-fetched (`_saved`, py:460); browser values limited to `allowed_fields`, ≤ 10, ≤ 140 chars, labelled unsaved and never stored | Saved: yes. Unsaved: shown as browser values only | test_unsaved_values_are_limited_to_readable_fields_and_bounded; test_restricted_fields_follow_the_users_field_permissions | Supported |
| 4 | List view identification | `listContext` (js:110) | `_list` (py:219) | Yes | TestListAnswers.* | Supported |
| 5 | Selected list rows | `get_checked_items` | `_readable` re-check; unreadable dropped and counted | Yes | test_selected_records_are_validated_on_the_server | Supported |
| 6 | Selected-row limits | ≤ 21 sent (js:17) | > 20 → "please narrow the selection" (py:258); child-table rows ≤ 20 | Yes | test_selection_limits | Supported |
| 7 | Active list filters | `get_filters_for_args` (≤ 9) | `_translate_filter` → `normalize_where`; > 8 or untranslatable → named, list summary refused (never dropped) | Yes | test_unsupported_filters_are_named_never_dropped; test_too_many_filters_and_unknown_filters | Supported |
| 8 | Report identification | `reportContext` (js:128) | Report exists, not disabled, `is_permitted()`, ref DocType readable | Yes | test_unlisted_unknown_and_unpermitted_reports | Supported (11 allowlisted reports described) |
| 9 | Active report filters | `get_filter_values` (≤ 12, ≤ 60 chars) | Only filters named in `REPORTS`; company must be usable; dates parsed; others regex-checked. **Results never read** | Yes | test_report_filter_on_a_company_the_user_cannot_access_is_not_described; test_report_dates_and_values_are_validated | Supported (description only, by design) |
| 10 | Context retention in follow-ups | Context re-sent with every message | Not stored (test_context_is_not_stored_in_the_conversation). Follow-ups use `last_query` / `last_created`; the router gets only `summary()` counts | Partly | test_the_same_question_means_the_page_that_is_open; test_follow_up_context_is_passed_to_router | **Partial**: model-routed follow-ups can't reference the open record (§11 P1-3) |
| 11 | Stale or missing context | `modified` sent | `stale` when the saved `modified` differs → warning, latest saved version used; missing/malformed → route-only or ignored | Yes | test_stale_saved_record_is_detected; test_stale_form_is_answered_from_the_latest_saved_version_with_a_warning; test_without_context_the_chat_behaves_as_before | Supported |
| 12 | Context size limits | JSON ≤ 4000, value ≤ 140 | JSON ≤ 6000 (route-only ≤ 1000), all collections bounded | Yes | test_malformed_and_oversized_context_is_ignored | Supported |
| 13 | Permissions on referenced records | – | Company-scoped `get_list`, `has_permission`, field permlevels, child fields | Yes | test_company_without_access_never_reaches_the_answer; test_child_rows_of_another_company_are_never_shown; test_restricted_child_columns_are_not_shown | Supported |
| 14 | Client context not trusted | – | All of the above; `parse` swallows any exception, so a hint can't break the chat | Yes | test_coverage_batch.test_page_context_is_only_a_hint_and_rechecked; TestSafety.* | Supported |

**Do query handlers actually use the page context?** Only in two ways: (a) the 10 page intents; (b) the shortcut `ref` mechanism ("…of this customer"), which uses `ctx.view`. The model-routed path receives `{page, doctype, selected count, unsaved flag, filter count}` and never a record name or filter value (`PageContext.summary`, py:85). This is good for privacy, but it means "this X" questions phrased outside the shortcut regexes cannot use the open record.

---

## 8. Security and permission audit

| Requirement | Evidence | Tests | Status | Gaps |
| --- | --- | --- | --- | --- |
| Chat restricted to System Managers | DocType permissions: AI Chat Session (setup_doctypes.py:208, `if_owner`), AI Document Entry (:26). `session._owned` re-checks `has_permission` on every request (session.py:48) | test_page_context.test_only_system_managers_can_use_the_chat_with_any_context; test_security_hardening.test_a_user_who_lost_chat_access_gets_nothing_from_stored_conversations; test_chat_queries.test_permission_denied_for_user_without_access | **Verified** | The widget is loaded and shown to **every** logged-in user (ai_bot_v2.js:14 checks only "not Guest"); non-System-Managers get errors (UX only). Access follows DocType permission configuration, so adding a role there widens access without a code change |
| Reads respect permissions and companies | `frappe.get_list` / `get_doc` + `check_permission` everywhere; `_company_filter` (queries.py:313) from Company User Permissions (:270) | test_company_isolation (6), test_generic_queries (company/permission cases), test_coverage_batch.test_company_scoping_of_new_company_doctypes | **Verified** | A user with no Company User Permission sees all companies (ERPNext's own model). Minor `get_all`/`db.get_value` reads on already-authorised names (item titles, UOM conversion rows); the legacy mapper (§4.3 item 6) |
| DocType and field access restricted | `REGISTRY` (policy.py:47), `allowed_fields`, prohibited types/tokens (policy.py:262-273), `is_queryable` requires a company field or explicit `shared` | test_capability (26) | **Verified** | – |
| Client-supplied DocType/name/field/filter validated | `queries.validate` / `_recheck`, `normalize_where` (fails closed, never drops), `page_context`, `set_overrides` (only mapped keys), `remap` (registered types only), `_parse_action` | test_generic_queries.test_invalid_filters_never_widen_the_query; test_security_hardening.test_remap_only_accepts_registered_document_types; test_lifecycle_safety.test_malformed_action_payload_is_answered_not_raised | **Verified** | – |
| No arbitrary SQL | No `frappe.db.sql` in runtime code (grep). Aggregates use `get_list` field dicts with fixed functions; `order_by` / `group_by` built from policy field names | test_capability.test_lifecycle_and_raw_operations_fail_closed | **Verified** (code + tests) | – |
| Images and extracted values untrusted | `_attachment` (owner, extension, size); readable-file hook; `clean_text`, `parse_number` / `parse_date`; matchers never auto-accept fuzzy matches; builder copies only `supported_fields` | test_lifecycle_safety.test_entry_cannot_use_another_users_private_file, test_job_cannot_read_another_users_private_file; test_sales_invoice_mapping.test_untrusted_input_sanitising; test_chat_agent.test_document_text_cannot_issue_instructions | **Verified** | File type is checked by extension, not by content |
| Transactions Draft-only | builder `docstatus = 0` + post-insert check (document_builder.py:29-34); `forms._create` | test_lifecycle_safety (submit patched to fail), test_security_hardening.TestDraftOnly | **Verified** | 5 non-submittable records saved immediately (documented; §6) |
| No submit/cancel/delete/amend/pay | No such call in runtime code; `PROHIBITED_QUERY_OPERATIONS` | test_security_hardening.test_no_lifecycle_call_exists_in_application_code | **Verified** | – |
| Editing and autonomous multi-step writes excluded | No edit path; `unsupported` mode; nested master creation needs its own confirmation | test_page_context.test_edit_requests_do_not_use_unsaved_values…; test_chat_guided.test_unknown_name_is_never_created_automatically | **Verified** | – |
| ERPNext validation not bypassed | All business inserts use plain `insert()`; the only `ignore_permissions` is the background job saving its own AI Document Entry result (controller.py:129) | test_lifecycle_safety.test_job_result_save_runs_standard_validation | **Verified** | – |
| No secrets or content in errors/logs | `ai/errors.py` (`get_ai_logger`, `log_safe_error`); router logs metadata only (llm.py:296-305) | test_provider_config.test_errors_and_logs_never_contain_keys_or_provider_text; test_security_hardening.TestLogging; test_extraction_safety.test_provider_never_logs_response_content | **Verified** | `_safe_error` shows ERPNext's own validation message (≤ 300 chars) to the same user (acceptable) |
| Retries cannot duplicate transactions | client_id de-duplication (engine.py:160); session row lock (session.py:116); entry row lock + `created_document` (creation.py:16-19); deduplicated job; retry only Failed entries | test_lifecycle_safety.test_second_create_never_duplicates_the_draft; test_chat_agent.test_image_with_text_creates_draft_after_confirmation (re-delivery); test_rate_limit_regression.test_duplicate_client_id_not_processed_twice | **Verified** | Only image Sales Invoices check for *existing business duplicates*; guided creation relies on confirmation |
| No cross-user/company leakage via history | `_owned`; `history.recheck` (history.py:268) re-checks DocType, records and `requires` on replay; AI Document Entry owner-only hooks (permissions.py) | test_security_hardening.TestConversationHistory (10), TestAIDocumentEntryOwnership (5); test_chat_sessions (14) | **Verified** | Extracted invoice data in AI Document Entry is never purged (data-retention note, §11 P3-10) |
| Generic AI can't falsely claim success or data | Real results always arrive as cards with `source: erpnext`; the model never receives data (llm.py:226 `router_history`); the prompt forbids stating data | test_chat_routing.test_erpnext_results_are_never_sent_to_the_model | **Not enforced in code** | The model's `chat`/`clarify`/`unsupported` text is displayed as written; there is no check for invented numbers or "I created…" claims, and no test (§11 P0-1) |

**Critical security issues:** none found that allow a permission bypass, a cross-user or cross-company leak, an unsafe write or a submission. The one P0 item is a safety requirement enforced only by the prompt (misleading-message risk), not a data exposure. Smallest fix: in `engine._general` (engine.py:424-456), post-process model-authored text before `ctx.say`. Neutralise success or data claims (patterns such as created / submitted / saved, document-ID patterns, currency amounts) by replacing them with a fixed sentence and logging a metadata-only event. Add tests that mock `llm.route` to return such replies.

**Previously known item (outside this audit's code scope):** provider keys committed in git history (commit `92f7125`) were removed from the tree on 2026-10-15. This audit did not verify whether they were revoked.

---

## 9. Test execution results

### 9.1 Command and environment

```
docker start ai-erpnext-ci-db            # isolated test DB (was stopped)
HTTPS_PROXY/HTTP_PROXY/ALL_PROXY=http://127.0.0.1:9  NO_PROXY=localhost,127.0.0.1,::1 \
bench --site ci-test.local run-tests --app ai_erpnext
```

The dead proxy was added as extra protection so that any accidental outbound HTTP request would fail. The test site also has no provider key, so `providers.primary()` raises `config` before any network use.

### 9.2 Results (observed)

| Collected | Passed | Failed | Errors | Skipped | Duration | Exit |
| --- | --- | --- | --- | --- | --- | --- |
| 471 | **471** | 0 | 0 | 0 | 238 s | 0 |

All 25 test modules ran. Defined test methods per module equal executed tests per module (reconciled). The Node-based frontend check (`tests/page_context_check.mjs`) ran, because `node` is installed. Two Frappe `V17FrappeDeprecationWarning` messages (`limit_page_length`) were printed; no test failed.

### 9.3 Classification of the 471 tests

| Category | Modules | Tests | Kind |
| --- | --- | --- | --- |
| Policy / unit / schema | test_capability, test_inventory, test_install_schema, test_warehouse_defaults | 54 | DB-backed integration |
| Router and intent | test_chat_routing, test_router_prompt, test_chat_router_retry, test_rate_limit_regression | 66 | Provider mocked (fake client / mocked transport) |
| Query handlers | test_chat_queries, test_chat_lookup, test_generic_queries, test_coverage_batch | 91 | DB-backed; router mocked |
| Image extraction and validation | test_sales_invoice_mapping, test_extraction_safety | 45 | DB-backed; provider mocked |
| Draft creation (image + guided) | test_chat_agent, test_chat_forms, test_chat_guided, test_chat_creation_requirements, test_lifecycle_safety | 72 | DB-backed `insert()`; extraction simulated |
| Permission, security, sessions | test_security_hardening, test_company_isolation, test_chat_sessions | 49 | DB-backed with restricted users |
| Page-aware context (incl. 2 frontend) | test_page_context | 65 | DB-backed + Node unit + source check |
| Provider config, fallback, retry | test_provider_config, test_provider_fallback | 29 | Scripted fake transport |
| **Total** | 25 modules | **471** | |

| Kind | Present? |
| --- | --- |
| Mocked-provider tests | Yes: every test touching a provider |
| Local DB-backed Frappe integration tests | Yes: all 471 (`IntegrationTestCase`) |
| Browser / end-to-end (real Desk + widget) | **No** |
| Frontend tests | 2: Node check of the context collector; source-string assertions on the widget |
| Live provider verification | **Not performed** (not authorised; no key on the test site) |

### 9.4 Depth of verification

- **Data-backed assertions (good)**: the image→Draft test asserts the created Sales Invoice's customer, company and docstatus and the record count; company-isolation tests assert totals and outstanding per user; history tests assert withheld or partial replays; creation tests assert docstatus 0 and row warehouses.
- **Smoke-only**: `test_every_registered_doctype_lists_counts_and_aggregates_without_error` proves that list/count/aggregate/child_rows *run* for every DocType, mostly on empty data. It does not prove the answers are correct. 34 features rely on it alone.
- **Inventory over-claims**: `capability/inventory.py` `VERIFIED` marks two entries as data-backed when they are not.
  - `Employee: read`: the only test refuses sensitive fields; no Employee record is read.
  - `Stock Ledger Entry: read`: the fixture item is non-stock, so no ledger rows exist.
  The generated `docs/coverage_inventory.md` repeats both claims.
- **README inaccuracy**: "a fake network fails any unexpected request" holds only inside the provider modules (`FakeNet` in test_provider_config / test_provider_fallback). There is no suite-wide network guard.
- **Untested paths**: why/status (`engine._explain`); image-flow master field correction (`_master_correction`); image-flow "start over"; the "Yes, it's a Sales Invoice" override; non-null discounts; model replies containing fabricated claims; Supplier summary; Purchase Invoice query values.

### 9.5 Not run in this audit

| Item | Reason |
| --- | --- |
| CI against the newest ERPNext v16 | Requires a scratch clone and site install; the local checkout was used instead |
| Linters (ruff, eslint, semgrep, pip-audit) | Not part of the audit brief's test scope |
| Live provider calls | Prohibited (paid; no authorisation) |
| Browser end-to-end | No such tests exist |
| Tests on `ai-erpnext.local` | Working site; not to be used for tests |

---

## 10. Quantitative coverage statistics

### 10.1 Features (denominator: 72 audited features, §3.1)

| Status | Count | Share |
| --- | --- | --- |
| Implemented | 23 | 23/72 = 31.9 % |
| Partial | 43 | 43/72 = 59.7 % |
| …of which path complete, results only smoke-tested | 34 | 34/72 = 47.2 % |
| …of which a sub-capability is missing (returns, reports) | 9 | 9/72 = 12.5 % |
| Not implemented | 2 | 2/72 = 2.8 % |
| Blocked (HRMS not installed) | 3 | 3/72 = 4.2 % |
| Intentionally excluded (payroll) | 1 | 1/72 = 1.4 % |
| Not verified (overall) | 0 | Every feature has code evidence; *result correctness* is unverified for the 34 smoke-only features above |

### 10.2 Categories (denominator: 12)

| Category | Features | Implemented | Partial | Not impl. | Blocked | Excluded |
| --- | --- | --- | --- | --- | --- | --- |
| Accounting & Finance | 18 | 6 | 12 | 0 | 0 | 0 |
| Selling & CRM | 12 | 8 | 4 | 0 | 0 | 0 |
| Buying | 8 | 3 | 5 | 0 | 0 | 0 |
| Stock | 12 | 3 | 9 | 0 | 0 | 0 |
| Manufacturing | 6 | 0 | 5 | 1 | 0 | 0 |
| Projects | 3 | 2 | 1 | 0 | 0 | 0 |
| HR | 5 | 0 | 1 | 0 | 3 | 1 |
| Assets | 2 | 0 | 2 | 0 | 0 | 0 |
| Subcontracting | 2 | 0 | 2 | 0 | 0 | 0 |
| Support | 2 | 1 | 1 | 0 | 0 | 0 |
| Quality | 1 | 0 | 1 | 0 | 0 | 0 |
| Custom apps | 1 | 0 | 0 | 1 | 0 | 0 |
| **Total** | **72** | **23** | **43** | **2** | **3** | **1** |

- Categories with at least one implemented feature: **6/12** (Accounting, Selling & CRM, Buying, Stock, Projects, Support).
- Categories fully covered within their declared scope: **0/12**. Every category has at least one partial feature.
- Partially covered categories (some path, not all implemented): **11/12**.
- Categories with no implemented feature: **6/12** (Manufacturing, HR, Assets, Subcontracting, Quality, Custom). Of these, only Custom has no path at all.
- Categories wholly excluded or blocked: **0/12**. HR is 4/5 blocked or excluded.

### 10.3 Image extraction (denominator: 11 candidate document types; PDF counted separately)

| Measure | Count |
| --- | --- |
| Candidate types audited | 11 |
| Recognised by the prompt's enum | 6/11 (SI, PI, PO, SO, Quotation, DN) |
| Working extraction (mapped to a registered schema) | **1/11** (Sales Invoice) |
| Extraction + validation | 1/11 |
| Verified Draft creation | 1/11 |
| Passing integration tests (provider mocked) | 1/11 |
| Recognition only (no mapping, validation or creation) | 5/11 |
| Not implemented at all | 5/11 (PR, MR, BOM, Credit Note, Debit Note) |
| Intentionally excluded / blocked | 0/11 |
| PDF | Not implemented (refused at upload; tested) |

### 10.4 Normal chat (denominator: 37 intents, §5.1)

| Measure | Count |
| --- | --- |
| Intents with working handlers | 37/37 |
| Intents that return live ERPNext data | 19/37 (10 query ops, open, more, page: about, card, explain, rows, unsaved, selected, list) |
| General conversation / description only | 8/37 (chat, clarify, unsupported, help, guidance fallback, page fields, filters, report) |
| Workflow / control (no data answer) | 10/37 (create, image upload, cancel, start over, retry, why/status, invoice corrections, master correction, form switch, form correction) |
| Context-aware | 11/37 (10 page intents + `details` via shortcut `ref`) |
| Intents without a test | 2/37 (why/status, image-flow master correction) |
| Intents whose user-visible text is model-authored | 3/37 (chat, clarify, unsupported) |
| Intents in the prompt without a handler | 0 |

### 10.5 Conversational creation (denominator: 12 creatable types)

| Measure | Count |
| --- | --- |
| Creatable types | 12 (7 Draft transactions + 5 saved-immediately records) |
| Working text-to-Draft flows (transactions) | 7/7 |
| Working flows for saved-immediately records | 5/5 |
| Partial flows | 12/12 lack context prefill ("for this customer"); otherwise complete |
| Verified by integration tests | 12/12 |
| Explicitly excluded types | Payment Entry; editing of any record |

### 10.6 Tests

| Measure | Count |
| --- | --- |
| Collected / passed / failed / skipped / not run | 471 / 471 / 0 / 0 / 0 |
| Features with data-backed integration tests | 23/72 |
| Features with smoke-only tests | 34/72 |
| Features with no test of their behaviour | 15/72 (9 partial reports/returns, 2 not implemented, 3 blocked, 1 excluded; the excluded and blocked ones need no behaviour test) |
| Browser end-to-end tests | 0 |

---

## 11. Prioritised gaps

"Violates constraints?" means: would doing the recommended work conflict with the current scope rules (System Manager only, Draft-only, no editing, no arbitrary report execution, and so on)?

### P0: Critical security and correctness

**P0-1. Model-authored text is not checked for false claims**

| | |
| --- | --- |
| Module / feature | Chat, all categories |
| Status | Requirement not enforced in code |
| Evidence | engine.py:424-456 shows `plan["reply"]` / `plan["question"]` as written; llm.py:141 (prompt rule only); no test |
| Impact | The model may say "I've created the invoice", or quote counts and totals it has never seen. The "General answer" label and the absence of a card are the only signals |
| Missing work | Post-filter model text: block or rewrite success/lifecycle claims and data-like content (IDs, amounts); log a metadata-only event |
| Recommended tests | Mock `llm.route` to return chat replies containing "created SINV-…", "you have 5 overdue invoices", "submitted"; assert neutral output and no stored data reference |
| Dependencies / risks | Over-filtering could hide legitimate how-to text; keep patterns narrow |
| Violates constraints? | No |

### P1: Existing feature reliability

**P1-1. Image draft ignores the document's discount**

| | |
| --- | --- |
| Module / feature | Accounting / image → Sales Invoice |
| Status | Partial |
| Evidence | adapters.py:59 extracts the discount; totals.py:62 validates with it; definitions.py:31 `supported_fields` has no discount field; the builder never sets one |
| Impact | A discounted invoice passes validation and becomes a draft whose grand total exceeds the document's, with no warning |
| Missing work | Either map the discount to `discount_amount` (with `apply_discount_on`) or raise a data warning/question when the discount is non-zero |
| Recommended tests | Extraction with discount 10 → assert the draft grand total equals the document total, or that the question is asked |
| Dependencies / risks | Discount base (net or grand total) must match the source document |
| Violates constraints? | No |

**P1-2. One-click override turns any document into a Sales Invoice**

| | |
| --- | --- |
| Module / feature | Image flow, document type |
| Status | Implemented but risky; untested |
| Evidence | engine.py:837-842 (question) and 1095-1101 (`__yes__` sets the target to Sales Invoice) |
| Impact | A supplier's Purchase Invoice could become a Sales Invoice draft (party direction reversed). This requires several further confirmations, and the result is only a Draft |
| Missing work | When the detected type is a registered-elsewhere ERPNext type (e.g. Purchase Invoice), refuse instead of offering the override; or require the issuer to match one of the user's companies |
| Recommended tests | Purchase Invoice extraction → assert no Sales Invoice path, or an explicit company-issuer check |
| Dependencies / risks | – |
| Violates constraints? | No |

**P1-3. The model-routed path cannot use the open record**

| | |
| --- | --- |
| Module / feature | Page-aware Q&A and creation |
| Status | Partial |
| Evidence | page_context.py:85 (`summary` has no name); engine.py:462-465 (`ref` resolved only when the plan contains it); llm.py:141 (prompt does not define `ref`); §5.3 observations |
| Impact | "Total outstanding for this customer", "stock for this item", "create a quotation for this customer" → the model can't name the record, so the chat asks "which one?" or starts an empty form |
| Missing work | Let the prompt request `{"ref": "open_record"}` (an opaque token, no name sent) and resolve it with `ctx.view` on the server; prefill the party in `forms.start` from a validated open Customer/Supplier form |
| Recommended tests | Router mocked to return `ref` plans with a Customer/Item form open; forged and unreadable forms ignored |
| Dependencies / risks | Keep record names out of the prompt |
| Violates constraints? | No |

**P1-4. List-summary phrasings outside the regex lose the page filters**

| | |
| --- | --- |
| Module / feature | Page-aware list |
| Status | Partial |
| Evidence | page_context.py:356 `LIST_RE` (narrow verb list); "Show my current list with its applied filters" routes to the model (§5.3) |
| Impact | The model only knows a filter *count*. A list plan returns records without the page's filters, and the reply does not say the page filters were not applied |
| Missing work | Broaden the deterministic list intents ("show/what's in my current list"); or, when a list page is open and the model plans a query on the same DocType, apply the validated page filters or add an explicit "your list's filters were not applied" note |
| Recommended tests | Phrase variants with a filtered list open |
| Dependencies / risks | Regex breadth vs false positives |
| Violates constraints? | No |

### P2: Missing test coverage

**P2-1. No suite-wide network guard** (README claims one)

- Evidence: README "Tests" section; no global fixture found.
- Impact: on a site *with* a key, an unmocked path could make a paid call.
- Missing work: patch the provider factory (`providers.build`) in `before_tests`, or add a socket guard; fix the README.
- Risks: none. Violates constraints? No.

**P2-2. 34 features are smoke-only**

- Evidence: §3; `test_every_registered_doctype_lists_counts_and_aggregates_without_error`.
- Impact: wrong field mappings or filters would pass CI.
- Missing work: fixture records and value assertions, starting with Purchase Invoice, Payment Entry, Journal Entry, Stock Entry and BOM/Work Order.
- Risks: fixtures need stock and accounting setup. Violates constraints? No.

**P2-3. Coverage inventory over-claims**

- Evidence: inventory.py `VERIFIED["Employee"]`, `["Stock Ledger Entry"]`; §9.4.
- Impact: misleading coverage reporting.
- Missing work: add real data-backed tests, or downgrade the claims and regenerate `docs/coverage_inventory.*`.
- Violates constraints? No.

**P2-4. No browser end-to-end tests**

- Evidence: §9.3.
- Impact: widget rendering, escaping, the label (`ai_bot_v2.js:360`) and poll/retry behaviour are unverified in a real Desk.
- Missing work: one Cypress/Playwright smoke test (open widget, send, render card).
- Risks: CI time. Violates constraints? No.

**P2-5. Untested control and image paths**

- Evidence: §9.4 (why/status, image master correction, image start over, type override, discount, Supplier summary).
- Missing work: one test per path.
- Violates constraints? No.

**P2-6. CI against the newest v16 not re-run for this audit**

- Evidence: §9.5.
- Missing work: re-run CI or the documented scratch-clone procedure before release.
- Violates constraints? No.

### P3: Partial feature completion

| # | Item | Evidence | Work |
| --- | --- | --- | --- |
| P3-1 | Supplier-side parity: `outstanding_by_supplier`, Supplier summary test | queries.py:1097 (customer only) | Add operation + test |
| P3-2 | Credit/debit notes not filterable except by status | `is_return` absent from policy fields | Add `is_return` to SI/PI list/detail fields + test |
| P3-3 | Widget shown to users without chat permission | ai_bot_v2.js:14 | Hide the launcher unless a boot flag says the user may read AI Chat Session |
| P3-4 | Documentation drift | engine.py:1-8 (still says the engine "never asks a language model"); HELP (engine.py:36) omits Lead/Issue; README says "about 55" DocTypes (actual 54), "From your ERPNext data" (widget label now differs) and "fake network" | Update text |
| P3-5 | Stray dev scripts in the package | run_tests2.py (imports non-existent `GlobalMapper`; inserts with `ignore_permissions`), check_provider.py, fix_client_script.py | Remove or move out of the importable package |
| P3-6 | Legacy prefill mapper bypasses permissions | pipeline/mapper.py:45-63 `frappe.db.get_value` | Use `get_list`, or retire the legacy button |
| P3-7 | `details` missing for Bin / GL Entry / SLE, so "explain this" fails on those forms | policy.py:136, 160, 179 `operations` | Return a friendly page answer instead of a capability error |
| P3-8 | Image caption filter false refusals | engine.py:50 `UNSUPPORTED` (`payments?`) | Match only creation intent ("create a purchase order"), not any mention |
| P3-9 | Business-duplicate check for guided creation (e.g. same supplier `bill_no`) | forms.py (none) | Warn before confirm |
| P3-10 | AI Document Entry retention (extracted invoice content kept indefinitely) | no cleanup job | Decide a retention period |

### P4: New feature expansion (each needs an explicit scope decision)

| # | Item | Violates current constraints? |
| --- | --- | --- |
| P4-1 | Image extraction for Purchase Invoice (the prompt already detects it), then PO/SO/Quotation/DN. Needs a registry definition + adapter/mapper/validator/builder each; the prompt must not change without re-benchmarking (processor.py:7-10) | No (new extraction types need authorisation) |
| P4-2 | PDF ingestion | No |
| P4-3 | Report results (AR ageing, TB, P&L, BS, Stock Balance) | **Yes** until each report's permission/company behaviour is proven (`REPORT_RESULTS_ENABLED`) |
| P4-4 | Cross-DocType comparisons ("purchases vs sales") | No (two validated plans) |
| P4-5 | Creation of Stock Entry / Material Request / Journal Entry | No for Draft-only; Payment Entry remains excluded |
| P4-6 | HR features | Requires installing HRMS; payroll stays excluded |
| P4-7 | Custom `Proposal` DocType in the policy | No |
| P4-8 | Editing existing records, autonomous multi-step writes | **Yes**, excluded |

---

## 12. Recommended next-phase roadmap

1. **Hardening sprint (small, contained):** P0-1 (filter model text) and P1-1 (discount), each with tests; P2-1 (global network guard); P3-4 (documentation drift).
2. **Image flow correctness:** P1-2 (type override), plus P2-5 tests for the untested image paths.
3. **Context everywhere:** P1-3 (opaque `ref` for model plans, party prefill), P1-4 (list phrasing / filter carry-over).
4. **Verification depth:** P2-2 (data-backed tests for the 34 smoke-only features, highest-traffic first), P2-3 (fix inventory claims), P2-4 (one browser smoke test), P2-6 (CI on latest v16).
5. **Parity and polish:** P3-1, P3-2, P3-3, P3-5 to P3-10.
6. **Expansion (after decisions):** P4-1 Purchase Invoice extraction first; the report-results decision; HRMS scope.

---

## 13. Evidence index

| Area | Files and functions |
| --- | --- |
| Endpoints | `api/chat.py` (get_session :29, send_message :40, poll :53, new_conversation :66, list_conversations :76, open_conversation :82); `api/mapping.py` (get_entry :9, get_review :27, remap :33, apply_corrections :52, get_supported_doctypes :63); `api/creation.py:11` create_document; `api/validation.py` validate_entry; `api/extraction.py:8` retry_extraction |
| Sessions and history | `chat/session.py` (_owned :45, get_session :60, active_session :71, history :83, reopen :92, lock :116, save :170 ephemeral handling :178, cleanup_expired_sessions :189); `chat/history.py:268` recheck |
| Engine | `chat/engine.py` (handle_message :156, _on_text :310, _general :392, _resolve_ref :482, _run_query :533, _attachment :697, _start_extraction :714, advance :824, _plan :852, _answer :1066, _master_correction :1284, _create_invoice :1338, UNSUPPORTED :50) |
| Routing | `chat/llm.py` (SYSTEM :141, route :171, router_context :212, router_history :226, complete_with_policy :242); `chat/shortcuts.py` (match :96, field_question :128, REF_RE :88) |
| Queries | `chat/queries.py` (validate :102, _recheck :395, execute :421, _base_filters :441, _company_filter :313, permitted_companies :270, _list :519, _child_rows :613, _aggregate :654, _count :727, resolve_record :758, _details :862, _party_summary :897, _field_answer :984, _sales_summary :1074, _outstanding_by_customer :1097) |
| Capability policy | `capability/policy.py` (REGISTRY :47, prohibited sets :262-273, REPORTS :281-305); `capability/service.py` (is_queryable :37, allowed_fields :83, normalize_where :242, normalize_aggregate :305, normalize_child :336, normalize_link :350, assert_creation_safe :410); `capability/access.py`; `capability/inventory.py` (VERIFIED, CREATE_TESTED, BLOCKED_REASONS) |
| Guided creation | `chat/forms.py` (SPECS :50, MASTERS :97, start :147, switch :206, next_step :252, correction :322, _requirements :844, _create :1046); `chat/requirements.py`; `chat/masters.py` |
| Page context | `public/js/ai_page_context.js` (LIMITS :17, formContext :77, listContext :110, reportContext :128, collectPageContext :143); `chat/page_context.py` (summary :85, parse :129, _form :182, _list :219, _translate_filter :267, _report :299, classify :377, answer :418, handlers :481-937) |
| Image pipeline | `pipeline/controller.py` (enqueue_extraction :8, validate_ai_document :20, process_ai_document :34, run_ai_pipeline :56, save :129, _readable_file :133); `pipeline/processor.py:11` EXTRACTION_PROMPT; `pipeline/review_service.py` (evaluate :38, apply_mapping :49, _legacy_mapping :84, set_overrides :100); `pipeline/mapper.py:4` ERPNextMapper (legacy) |
| Registry, mapping, validation, creation | `registry/definitions.py:5`; `registry/document_registry.py:73`; `detection/document_detector.py:12`; `mapping/adapters.py:29`; `mapping/sales_invoice.py:96`; `mapping/base.py` (BaseMatcher); `validation/sales_invoice.py:19`; `validation/totals.py`; `validation/duplicate.py:23`; `creation/document_builder.py:29`; `creation/sales_invoice.py:6` |
| Providers | `ai/providers.py` (PROVIDERS :27, primary :64, fallback_for :78, run :97, _take_daily_slot :172); `ai/policy.py` (retry_delay :19, call_with_retries :28); `ai/errors.py` (classify :60, get_ai_logger :217, log_safe_error :251) |
| Permissions and hooks | `permissions.py` (:16, :23); `hooks.py` (doc_events, has_permission, permission_query_conditions, scheduler_events daily cleanup, before_tests); `setup_doctypes.py` (permissions :26, :41, :208) |
| Frontend | `public/js/ai_bot_v2.js` (init :14, send/context :274-286, source labels :360-362); `public/js/ai_review.js` (create_document :210, legacy form :183/:245); `public/js/ai_document_entry.js` |
| Tests | `ai_erpnext/tests/` (25 modules, 471 tests; §9.3); `tests/page_context_check.mjs` |

---

## 14. Limitations and items requiring manual verification

1. **Live model behaviour is unverified.** Every "Model" route in §5.3 depends on the router model producing the right plan. Tests mock `llm.route`, so they prove the handler, not the model's planning accuracy. A small, authorised live evaluation set (in a sandbox, with spending caps) is needed to measure it.
2. **Extraction accuracy on real images is unverified.** The prompt comment cites a benchmark (processor.py:7-10) that was not reproduced here.
3. **Real Desk behaviour was not observed** (no browser run): widget rendering, the page-context collector on real forms and lists, and polling.
4. **CI against the newest ERPNext v16 was not run.** Results are for the local Sep-2026 checkout.
5. **The working site `ai-erpnext.local` was not inspected.** Its permissions, roles, provider settings and the revocation status of the previously leaked keys need manual confirmation.
6. **Report permission semantics** (needed before P4-3) need a separate analysis per report.
7. **Feature counts depend on the counting rules in §3.1 and §5.1.** Different groupings change the denominators but not the per-feature statuses.
