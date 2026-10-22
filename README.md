## AI for ERPNext (`ai_erpnext`)

An AI assistant inside ERPNext Desk. It answers questions about ERPNext and about your own records, creates documents step by step from a conversation, and turns images of business documents into Drafts of the right ERPNext document. Everything it creates is a **Draft** (or, for masters, a saved record you confirmed): it never submits, cancels, deletes or pays.

Requires Frappe and ERPNext **version 16** (CI uses Python 3.14).

### What it can do

- **Look up your data (read-only).** For example: “What is the mobile number of Amit Patel?”, “Which items are in Sales Invoice ACC-SINV-2026-00001?”, “Show me the tax breakdown of this invoice”, “How many submitted Sales Invoices are outstanding?”, “Show the last 10 purchase orders”, “Which suppliers do we owe?”, “Show credit notes”, “Show my open tasks”, “Which items are below their re-order level?”, “Compare sales this month with last month”. “My …” is always a filter (assigned to you for tasks and issues, your own employee records for HR, otherwise created by you), and a status the DocType doesn't have is refused with its real statuses instead of being ignored. Answers come from ERPNext with the user's own permissions and company restrictions, and are labelled as coming from your data. Outstanding balances are net of credit/debit notes and payments, per currency.
- **Explain ERPNext.** How-to and concept questions (“How do I create a Purchase Order?”, “What does the Stock module do?”) are answered as general guidance, with a note on what the assistant itself can do. Built-in guidance covers common questions when the AI service is unavailable.
- **Create records by conversation.** As Drafts: Sales/Purchase Invoices, Sales/Purchase Orders, Quotations, Supplier Quotations, Delivery Notes, Purchase Receipts, Material Requests, Stock Entries (receipt, issue or transfer), Work Orders (for items with a default BOM; ERPNext takes the BOM's materials), Timesheets, restricted Journal Entries (existing ledger accounts of the company in its currency; no bank, cash, receivable, payable or stock accounts; debits must equal credits) and, with HRMS, Leave Applications and Expense Claims. Saved immediately when you confirm (they have no Draft state, and the confirmation says so): Customers, Suppliers, Items, Leads, Issues, Employees, Opportunities, Projects and Tasks. Before the preview ERPNext validates the new document on an unsaved copy that is always rolled back, so problems (an unbalanced entry, a date of birth in the future, an overlapping time log) are shown before you confirm, together with the figures ERPNext calculated (totals, the BOM used, the employee's full name). Line-based documents (expenses, time logs, journal lines) are collected line by line and can be corrected (“row 2 amount 300”, “remove row 1”, “add another line”). With a Customer or Supplier form open, “create a quotation for this customer” fills in the party; with Items selected in a list, “create a purchase receipt for these items” adds them. Short forms work: “create po”, “new so”. Existing records are offered as buttons, with a separate **Create new …** choice; nothing is created until you confirm a preview. You can correct anything on the way (“change quantity to 7”, “use Finished Goods warehouse”, “I meant Purchase Receipt”, “start over”, “cancel”).
- **Drafts from document images.** Attach a JPG, PNG or WEBP image (or a one-page PDF) of an invoice, credit or debit note, purchase or sales order, quotation, delivery document, goods receipt note, material request or bill of materials. What it becomes is decided from **who issued it and who it is addressed to**, never from its title: a customer's purchase order to you becomes a Sales Order, a supplier's invoice a Purchase Invoice, a supplier's delivery note a Purchase Receipt, a supplier's credit note a Debit Note (Purchase Invoice return), and so on (`detection/direction.py`). If neither party is your company, or the parties contradict each other, the assistant asks which side you are on and creates nothing until that is clear; a supplier's quotation (a Supplier Quotation) and unknown documents are refused. It then matches parties, items, units, warehouses, discounts and dates to your masters, asks only about what it can't resolve, and creates the Draft after you confirm. A one-page PDF is read as an image of its page; multi-page or encrypted PDFs are refused.

### Installation

ERPNext must be installed on the site first (`required_apps`).

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench --site <site> install-app ai_erpnext
bench build --app ai_erpnext
```

After updating the app, run `bench --site <site> migrate` (it creates or upgrades the app's DocTypes; repeated runs change nothing) and `bench build --app ai_erpnext` for the chat widget.

The assistant appears as a chat button in Desk for users with the **System Manager** role or the opt-in **AI Assistant User** role (created on install/migrate; an administrator assigns it per user). The role only opens the chat (the user's own conversations and uploaded documents): every look-up and creation still runs with that user's own ERPNext roles, User Permissions and companies, so an employee can, for example, check their leave and draft their own expense claim, but nothing their roles don't allow. *AI Provider Settings* stays System Manager only. Removing the role closes the chat for that user, including their history.

### AI provider configuration

Open **AI Provider Settings** (System Manager only). Keys are stored encrypted in Password fields and are never shown again, logged or sent to the browser.

- **Primary AI Provider** / **Primary API Key**: Groq, OpenAI, OpenRouter and Nvidia NIM work for chat and invoice images; Gemini for invoice images only. Leave **Base URL** and **Model Name** empty to use the provider defaults (OpenRouter and Nvidia NIM need a model name).
- **Enable Fallback Provider** (off by default): when Groq reports that its *daily* quota is exhausted, one request goes to the fallback (OpenAI, e.g. `gpt-4.1-mini`). Per-minute limits, outages, invalid keys, missing credits and other errors never switch providers, and a failing fallback is never retried. **Fallback Daily Request Limit** caps paid requests per site per day.
- The fallback is a paid service: enable it only after its account has credits. Saving the settings checks them locally and never calls a provider.

Changing the provider or a key takes effect immediately; no code change or deployment is needed. Without a working provider, data look-ups and guided creation still work for messages the assistant understands on its own (for example “create purchase order” or “mobile number of Amit Patel”).

### Conversations

- Each browser page has its own conversation. Opening the assistant on a new page (another browser, an incognito window, after a reload) starts a fresh one; **New chat (+)** starts another.
- Earlier conversations stay in **History** (clock icon) for 7 days and can be reopened there, including any unfinished work such as an open supplier question or an invoice being reviewed.
- Conversations belong to the user who started them. Every request is checked on the server; another user's conversation can't be read, listed or reopened.
- Conversations nobody wrote in are removed after a day; expired ones are closed and their messages cleared by a daily scheduler job.

### Page-aware answers

The chat widget describes the Desk page you are on (`public/js/ai_page_context.js`): the saved form (DocType, record, `modified`, whether it has unsaved changes and which fields changed), a list (filters, sort, selected records) or a report (name and filters). With it, “Explain this invoice”, “Explain these invoice items”, “What do these fields mean?”, “What did I change?”, “Compare the selected orders”, “Why are these orders pending?”, “Show my current list”, “Count the records in my current list”, “Summarize the current filtered list”, “Explain this report”, “What is the outstanding amount for this customer?” and “Show stock for this item” need no record name. The AI model never sees the record: it can only refer to “the open record”, which the server resolves from the validated page. If a list filter can't be applied, the assistant says so instead of showing an unfiltered list. It is only a hint: the server (`chat/page_context.py`) re-fetches everything with your permissions, ignores forged or inaccessible records, names list filters it cannot apply instead of dropping them, announces when the saved record changed since the page loaded, and shows unsaved values only as such (never written, not kept in the conversation). Reports are described by name and filters only; their results are never read. The browser bundle must be rebuilt (`bench build --app ai_erpnext`) for the widget change to reach users.

### Safety rules

- Documents are only ever saved as Drafts through ERPNext's normal `insert()`, with its permissions and validation. Masters are created only after their own preview and confirmation.
- Look-ups use permission-aware queries against an allowlist of DocTypes and fields; the AI model only proposes a plan and never writes SQL. Record data is never sent back to the model.
- What the chat may query or create is one explicit policy (`ai_erpnext/capability/`). ERPNext metadata only describes what exists; a DocType or field is usable only if the policy lists it, it exists, and the current user may read it (role, User Permission, company, field permission level). Queryable and creatable are separate lists; passwords, secrets, tokens and attachments are never exposed, and submit/cancel/delete/pay/edit are not capabilities.
- Structured questions (filters, count/sum/average/min/max with grouping, rows inside item/tax tables, one related record per row) are validated server-side against that policy; amounts are never added across currencies. 74 DocTypes are queryable (66 on sites without HRMS) (accounting ledgers and charts, stock ledger and balances, manufacturing, quality, assets, subcontracting, CRM, projects, support and shared masters such as price lists and item groups); the exact list, with what is tested and what is not, is generated into `docs/coverage_inventory.md`. DocTypes without a company field are queryable only when explicitly reviewed as shared master data. Monthly/yearly breakdowns, "overdue by more than N days" and customer/supplier summaries are built in. Standard reports, editing existing records and workflow actions are not supported.
- Stored conversation results are checked again against the reader's current permissions whenever a conversation is reopened; results that are no longer readable are removed or replaced by a neutral sentence. An AI Document Entry (extracted invoice data) can only be opened by its owner.
- Each company's data stays separate: queries are limited to the companies the user may access, and warehouses must belong to the document's company.
- **HR records** (leave, attendance, expense claims, advances, shifts, appraisals): users without the HR User or HR Manager role only ever see their own employee's records, plus those of employees whose approver they are, and HR forms (leave, expenses, timesheets) can only be filed for their own employee, even where ERPNext's role permissions would show more (HRMS gives the Employee role read access to every employee's records unless User Permissions are set). No salary, bank, score, geolocation or candidate data is queryable; payroll, Employee Checkin, Job Applicant and Interview are deliberately excluded.
- The AI model's own words are checked before they are shown (`chat/claims.py`): a reply that claims something was created, submitted or paid, or that states counts, amounts, record IDs or report results the server never fetched, is replaced by a plain statement. Model answers are labelled “General answer, not from your data”.
- Uploaded files must really decode as the image type their name claims, before anything is read or sent to a provider.
- Logs contain metadata only (provider, status, timings), never keys, document content or model output.
- **Data retention:** the data read from an image is cleared from an AI Document Entry once a document was created from it and **Keep Extracted Document Data For (Days)** (AI Provider Settings, default 180; 0 keeps it) has passed. Unresolved entries are never cleared, and the link to the created document stays.

### Code layout

| Path | Purpose |
| --- | --- |
| `ai_erpnext/capability/` | The shared capability policy (`policy.py`), metadata reader (`metadata.py`), answers/validation (`service.py`), permission-aware lookups (`access.py`) and the coverage inventory generator (`inventory.py`) |
| `docs/` | Generated coverage inventory (`coverage_inventory.md` / `.json`): regenerate on a test site with `bench --site <test-site> execute ai_erpnext.capability.inventory.write_inventory`. Deployment, release checklist, rollback and recovery: `OPERATIONS_AND_RELEASE.md`; implementation history: `ERPNext_AI_IMPLEMENTATION_AUDIT.md` |
| `ai_erpnext/ai/` | Provider adapters and settings (`providers.py`), retry policy, safe error handling |
| `ai_erpnext/detection/direction.py` | Which ERPNext document an image becomes: kind of document × your company's role (seller, buyer, internal) |
| `ai_erpnext/registry/` | The image document types (one definition per target) and their per-type rules |
| `ai_erpnext/chat/` | Conversation engine, sessions, data look-ups (`queries.py`), guided forms (`forms.py`), capability guide |
| `ai_erpnext/pipeline/`, `mapping/`, `validation/`, `creation/` | Image extraction (Prompt B3), master matching, validation, Draft building, retention |
| `ai_erpnext/api/` | Whitelisted endpoints (chat, extraction, review, creation) |
| `ai_erpnext/public/js/` | Chat widget (`ai_bot_v2.js`) and AI Document Entry form |
| `ai_erpnext/setup_doctypes.py` | Creates and upgrades the app's DocTypes on install and migrate |

### Tests

Integration tests run against a real site; AI providers are always mocked, so no keys or credits are needed. While the tests run, `tests/network_guard.py` refuses every socket connection to a non-loopback address, so an unmocked provider call fails immediately instead of reaching a paid API.

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app ai_erpnext
bench --site <test-site> run-tests --module ai_erpnext.tests.test_chat_sessions   # one module
```

On a fresh site the `before_tests` hook completes the ERPNext setup wizard with a throw-away test company. Use a dedicated test site, not a production one.

### CI

GitHub Actions workflows in `.github/workflows/`:

- **CI** (`ci.yml`): on every push to `version-16` and on pull requests, installs the latest Frappe and ERPNext `version-16`, installs this app on a new site, runs `bench migrate` again, then runs the full test suite.
- **Linters** (`linter.yml`): on pull requests, runs pre-commit, [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/).

CI always uses the newest ERPNext v16, so it can catch schema changes a local bench doesn't have yet.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/ai_erpnext
pre-commit install
```

Pre-commit is configured to use ruff, eslint, prettier and pyupgrade.

### License

MIT
