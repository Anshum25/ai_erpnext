# ai_erpnext: operations, deployment readiness and release

For the people who deploy and run the assistant. Prepared on 2026-10-22 from the development bench. No production
system was inspected or changed (none is reachable from that bench), so every production step below is a
**procedure to run**, not a record of something done.

## 1. Readiness summary

| Area | State | Evidence or blocker |
| --- | --- | --- |
| Code revision | Branch `ai-implementation-phase`, based on `794da7d`, with this phase's changes **not yet committed** | `git status` in `apps/ai_erpnext` |
| Automated tests | Full suite on `ci-test.local` (HRMS installed), plus a simulated site without HRMS | §6 |
| CI (newest ERPNext v16, no HRMS) | **Not run** for this phase | Push the branch and open a PR. CI also runs the HRMS-less suite for real. |
| Lint | ruff 0.14.10 (the pinned pre-commit version) clean. Semgrep and the rest of pre-commit **not run** locally. | CI Linters job on the PR |
| Real-site check | Read-only smoke on `ai-erpnext.local` (queries, help, catalog). The site was **not migrated**, so the new role isn't there yet. | §6 |
| Production environment | **Unknown / not reachable.** The bench used here is a development bench (`bench start`, no nginx or supervisor). | Owner of the production server |
| HRMS version | The development site runs HRMS `17.0.0-dev` (`develop`) on Frappe/ERPNext `version-16`. | Production should run an HRMS release branch that matches v16; test there before enabling HR features. |
| Live AI behaviour | Not verified (no paid calls were allowed). Router plans for the new phrasings are only mock-tested; the deterministic phrasings work without a model. | §7 |

The assistant is **not yet ready to be called production-ready**. The blockers before release are: commit, a green CI
on the newest v16, a staging run on a copy of production, and an explicit go-ahead.

## 2. What this release changes on a site

| Change | Effect on `bench migrate` | Reversible |
| --- | --- | --- |
| New role **AI Assistant User**, with owner-only rights on *AI Chat Session* (read/write/create/delete) and *AI Document Entry* (read/write/create, no delete) | Created once by `after_migrate` (`setup_doctypes.ensure_assistant_role`). The rows are added only together with a new role, so later admin edits are never undone. | Yes: unassign the role, or delete the role. Nothing else depends on it. |
| New creatable types, new query DocTypes, employee scope, "my"/status/compare/reorder queries, single-page PDFs | Code only | Yes (code rollback) |
| Dependency `pypdfium2` declared | Already installed with ERPNext (pdfplumber); `bench setup requirements` installs nothing new | Yes |

No table, column or existing record is changed. The JavaScript is unchanged in this phase. The earlier phase's note
still applies: run `bench build --app ai_erpnext` once if the deployed bundle predates it.

## 3. Release checklist

Run on **staging first** (a restored copy of production), then on production in a maintenance window.

1. **Code**
   - [ ] Commit this phase and open a PR into `version-16`. CI must be green: tests on the newest v16 without HRMS, and the Linters job (pre-commit, Semgrep, pip-audit).
   - [ ] Tag the release commit. Note the previous production commit for rollback.
2. **Backups** (before anything else changes)
   - [ ] `bench --site <site> backup --with-files`, then copy the four files (database, site config, public and private files) off the server.
   - [ ] Restore that backup on a scratch site and open it (`bench --site <scratch> restore <db.sql.gz> --with-public-files … --with-private-files …`). A backup that has never been restored isn't verified.
3. **Configuration**
   - [ ] `bench --site <site> list-apps`: frappe, erpnext and (if HR features are wanted) hrms on matching v16 release branches.
   - [ ] AI Provider Settings: primary provider and key set. Fallback off unless approved, with a daily limit. Keys are never printed; check only that the fields are set.
   - [ ] HTTPS in front of the site (the chat sends business data over the session).
4. **Deploy**
   ```bash
   cd <bench>
   bench --site <site> set-maintenance-mode on      # optional for this release (no schema change)
   git -C apps/ai_erpnext fetch && git -C apps/ai_erpnext checkout <release-tag>
   bench setup requirements --python                 # installs nothing new; confirms dependencies
   bench --site <site> migrate                       # creates the AI Assistant User role (once)
   bench build --app ai_erpnext
   bench restart                                     # production: supervisor/systemd restarts web, workers, scheduler
   bench --site <site> set-maintenance-mode off
   ```
5. **Smoke tests after deployment** (§5) as a System Manager, then as one user who has only *AI Assistant User* plus *Employee*.
6. **Enable for users**: assign *AI Assistant User* to a pilot group first. The role is the only switch; no restart is needed.

## 4. Rollback

1. `git -C apps/ai_erpnext checkout <previous-tag>`, then `bench build --app ai_erpnext` and `bench restart`.
2. The role created by the migrate can stay: on the old code it only grants the chat DocTypes to users who have it. To
   remove access completely, unassign it or delete the *AI Assistant User* role (Role list, as a System Manager).
3. Drafts created in the meantime are ordinary ERPNext Drafts. Nothing was submitted, so nothing needs reversing.
   Records saved immediately (Employee, Opportunity, Project, Task, Customer, Supplier, Item, Lead, Issue) stay; review
   them in their lists by **Created By** if needed.
4. A full restore from the backup taken in step 2 is only needed if something unrelated went wrong. It loses everything
   entered after the backup.

## 5. Smoke tests (each takes seconds; nothing is submitted)

| # | As | Do | Expect |
| --- | --- | --- | --- |
| 1 | System Manager | Open the chat, type `help` | The creatable list includes Employee, Journal Entry, Work Order and Expense Claim (HRMS sites) |
| 2 | System Manager | `how many customers` | A count "you can access", from your data |
| 3 | System Manager | `compare sales this month with last month` | Totals per company and currency, with the change |
| 4 | System Manager | `create journal entry`, then add two lines of different amounts | A refusal stating the difference; nothing is created. Cancel. |
| 5 | Employee + AI Assistant User | `show expense claims` | Only their own employee's claims |
| 6 | Employee + AI Assistant User | `create expense claim`, then one line, then Cancel at the preview | The preview shows HRMS's totals; nothing is created |
| 7 | User without the role | Reload Desk | No chat button; the API answers "Conversation not found" |
| 8 | Any | Attach a two-page PDF | Refused ("only single-page PDFs"); nothing is queued |

## 6. Verification performed for this phase

All tests ran on the isolated `ci-test.local` (docker MariaDB on port 3317), with every outbound network connection
blocked and AI providers mocked:

```bash
cd ~/frappe-bench
HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 NO_PROXY=localhost,127.0.0.1,::1 \
  bench --site ci-test.local run-tests --app ai_erpnext
```

- The baseline before this phase was 575 tests, all passing (one class skipped because HRMS was absent).
- HRMS was then installed on `ci-test.local`, approved and kept, and the final result is recorded in
  `ERPNext_AI_IMPLEMENTATION_AUDIT.md` (Part 0).
- A site without HRMS is covered by `test_capability.TestWithoutOptionalApps`, which simulates it on any site. The real
  HRMS-less run is CI.
- Read-only smoke on `ai-erpnext.local` (as Administrator, nothing written): queries, help, catalog, statuses, the
  comparison and the re-order check.

**Not verified:** live AI providers; browser end-to-end behaviour (no browser test infrastructure); load and
concurrency; Semgrep; the newest ERPNext v16.

## 7. Services and how to check them

| Dependency | Used for | Check |
| --- | --- | --- |
| MariaDB | Everything | `bench --site <site> mariadb -e "select 1"` |
| Redis queue + background workers | Image and PDF extraction (`frappe.enqueue`, 25-minute job timeout, deduplicated per document) | `bench doctor` (queues, workers); RQ Job list in Desk; `logs/worker.error.log` |
| Scheduler | Daily: expired chat sessions closed, resolved extraction data cleared after the retention period | `bench doctor` ("Scheduler enabled"); Scheduled Job Log for the two `ai_erpnext` jobs |
| Redis cache | Frappe caches (metadata, permissions) | `redis-cli -p <cache port> ping` |
| AI provider | Free-text routing and image extraction only | AI Provider Settings; `logs/ai.log` (metadata only). Look-ups and guided creation that the chat understands on its own keep working without it. |

Logs contain metadata only (ids, statuses, error types, timings): never keys, extracted content, prompts, model output
or exception text from documents.

## 8. Limits (enforced in code)

| Limit | Value | Where |
| --- | --- | --- |
| Rows per answer / maximum offset | 10 (max 20) / 500 | `chat/queries.py` |
| Date range of a query | 3 years | `chat/queries.py` |
| Re-order levels checked per question | 200 (the answer says when more exist) | `chat/queries.py` |
| Lines per Expense Claim / Timesheet / Journal Entry from the chat | 20 | `chat/forms.py` |
| Image upload | 10 MB, 50 megapixels, real PNG/JPEG/WEBP | `chat/engine.py`, `pipeline/controller.py` |
| PDF upload | 10 MB, one page, unencrypted, page ≤ 3000 pt, rendered to 2000 px | `pipeline/pdf.py` |
| Router prompt | Estimated ≤ 2,500 tokens per message; at most a few DocTypes described in full | `chat/llm.py`, `tests/test_router_prompt.py` |
| Conversation | 200 messages; history 7 days | `chat/session.py` |
| AI retries | Bounded attempts and wait budget; quota, auth and invalid-request errors are never retried; the paid fallback makes one request, only if enabled | `ai/policy.py`, `ai/providers.py` |

No load test was run. Before a wide rollout, measure chat latency with the expected number of concurrent users on
staging. Each message is one request plus at most one AI call, and the extraction jobs run on the long queue.

## 9. Recovery procedures

- **Failed migrate:** the only schema step of this release is creating the role. Re-run `bench --site <site> migrate`;
  it is idempotent. If it fails on something unrelated, fix that and re-run. Don't restore a backup for a role.
- **AI provider outage or quota:** the chat says the AI service is unavailable and keeps answering what it understands
  on its own (look-ups, "create …", the phrasings in the README). Extractions stay *Pending* and can be retried from the
  chat ("retry") once the provider is back. Don't switch providers without the data-handling approval for that provider.
- **Interrupted upload or extraction:** the conversation keeps the document. "retry" re-queues it; the job id is
  deduplicated, so it never runs twice at the same time. A document that already produced a Draft is never extracted
  again.
- **Failed Draft creation:** nothing partial remains (savepoint and rollback), and the form keeps every answer. The chat
  shows ERPNext's reason and offers Retry, a correction, or Cancel.
- **A user should lose access immediately:** remove *AI Assistant User* (or System Manager). Their conversations stop
  opening on the next request.
