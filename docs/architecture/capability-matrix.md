# Capability matrix

This table was generated on 2026-10-23 from the live policy objects (`chat.capabilities.catalog()`) on `ci-test.local`,
where HRMS is installed. It was produced by a read-only script, the same source the help text, the router prompt and
[coverage_inventory.md](../coverage_inventory.md) use. Without HRMS, the rows marked `(hrms)` are not queryable and
Leave Application / Expense Claim cannot be created. The chat says so.

## Legend

| Column | Meaning |
|---|---|
| Query operations | "standard" = list, count, details, aggregate. `child_rows` = list or filter rows of the allowlisted child tables. The other operations are explained in [queries-and-permissions.md](queries-and-permissions.md#execution-queriesexecute-queriespy656) |
| Company scope | `required`: every record has a company and the user's companies apply. `optional`: records without a company stay visible. `shared`: reviewed master data without a company field (role permissions and User Permissions apply) |
| Employee scope | users without HR Manager / HR User see only their own employees' records and those they approve |
| Create | `Draft` = created as a Draft (never submitted); `saved immediately` = no Draft state; both only after an explicit confirmation in a guided form |
| Image draft | can be created from an attached document image or a one-page PDF (server side; see [frontend-api-contract.md](frontend-api-contract.md#mismatches-between-the-frontend-and-the-backend) for the UI's PDF limitation) |

## Never available, for any DocType

Submit, cancel, amend, delete, pay, approve or reject, and edit existing records (`capabilities.NEVER`,
[capabilities.py:325](../../ai_erpnext/chat/capabilities.py#L325)). Standard reports are only *described* when open
(11 reports in `policy.REPORTS`). They are never run (`REPORT_RESULTS_ENABLED = False`).

## Summary

| | Count |
|---|---|
| Queryable DocTypes (policy) | 74 (9 need HRMS) |
| Creatable by guided form | 24: 15 as Draft, 9 saved immediately |
| Creatable from an image | 11 registry targets on 9 DocTypes (Sales Invoice and Purchase Invoice also as returns: credit and debit notes) |
| Masters creatable *inside* another flow | Customer, Supplier, Item |
| Reports described (not run) | 11 |

## Per DocType

| DocType | Query operations | Company scope | Employee scope | Create | Image draft |
|---|---|---|---|---|---|
| Sales Invoice | standard + child_rows, sales_summary, overdue, unpaid, outstanding_by_customer, compare | required |  | Draft | yes |
| Purchase Invoice | standard + child_rows, overdue, unpaid, outstanding_by_supplier, compare | required |  | Draft | yes |
| Sales Order | standard + child_rows | required |  | Draft | yes |
| Purchase Order | standard + child_rows | required |  | Draft | yes |
| Delivery Note | standard + child_rows | required |  | Draft | yes |
| Purchase Receipt | standard + child_rows | required |  | Draft | yes |
| Stock Entry | standard + child_rows | required |  | Draft |
| Payment Entry | standard + child_rows | required |  | – |
| Customer | standard, summary | shared |  | saved immediately |
| Supplier | standard, summary | shared |  | saved immediately |
| Contact | standard + child_rows | shared |  | – |
| Item | standard + child_rows | shared |  | saved immediately |
| Warehouse | standard + child_rows | required |  | – |
| Employee | standard + child_rows, leave_balance, attendance | required |  | saved immediately |
| Quotation | standard + child_rows | required |  | Draft | yes |
| Supplier Quotation | standard + child_rows | required |  | Draft |
| Material Request | standard + child_rows | required |  | Draft | yes |
| Lead | standard + child_rows | optional |  | saved immediately |
| Opportunity | standard + child_rows | required |  | saved immediately |
| Journal Entry | standard + child_rows | required |  | Draft |
| Bin | list, count, aggregate, below_reorder | optional |  | – |
| Project | standard + child_rows | required |  | saved immediately |
| Task | standard + child_rows | optional |  | saved immediately |
| Issue | standard + child_rows | optional |  | saved immediately |
| Account | standard + child_rows | required |  | – |
| Cost Center | standard + child_rows | required |  | – |
| GL Entry | list, count, aggregate | required |  | – |
| Payment Request | standard + child_rows | required |  | – |
| Dunning | standard + child_rows | required |  | – |
| Asset | standard + child_rows | required |  | – |
| Stock Ledger Entry | list, count, aggregate | required |  | – |
| Stock Reconciliation | standard + child_rows | required |  | – |
| Serial No | standard + child_rows | required |  | – |
| Batch | standard + child_rows | shared |  | – |
| Pick List | standard + child_rows | required |  | – |
| Item Group | standard + child_rows | shared |  | – |
| Brand | standard + child_rows | shared |  | – |
| Price List | standard + child_rows | shared |  | – |
| Item Price | standard + child_rows | shared |  | – |
| Customer Group | standard + child_rows | shared |  | – |
| Territory | standard + child_rows | shared |  | – |
| Supplier Group | standard + child_rows | shared |  | – |
| Prospect | standard + child_rows | required |  | – |
| Request for Quotation | standard + child_rows | required |  | – |
| BOM | standard + child_rows | required |  | – | yes |
| Work Order | standard + child_rows | required |  | Draft |
| Job Card | standard + child_rows | required |  | – |
| Production Plan | standard + child_rows | required |  | – |
| Quality Inspection | standard + child_rows | required |  | – |
| Asset Category | standard + child_rows | shared |  | – |
| Subcontracting Order | standard + child_rows | required |  | – |
| Subcontracting Receipt | standard + child_rows | required |  | – |
| Timesheet | standard + child_rows | required |  | Draft |
| Warranty Claim | standard + child_rows | required |  | – |
| Leave Application (hrms) | standard | required | yes | Draft |
| Leave Allocation (hrms) | standard | required | yes | – |
| Attendance (hrms) | standard | required | yes | – |
| Expense Claim (hrms) | standard + child_rows | required | yes | Draft |
| Employee Advance (hrms) | standard | required | yes | – |
| Shift Assignment (hrms) | standard | required | yes | – |
| Appraisal (hrms) | list, count, details | required | yes | – |
| Training Event (hrms) | list, count, details | optional |  | – |
| Holiday List | list, count, details, child_rows | shared |  | – |
| Address | list, count, details | shared |  | – |
| Campaign | list, count, details | shared |  | – |
| Activity Type | list, count, details | shared |  | – |
| Workstation | list, count, details | shared |  | – |
| Routing | list, count, details, child_rows | shared |  | – |
| Asset Movement | list, count, details, child_rows | required |  | – |
| Asset Repair | standard | optional |  | – |
| Asset Maintenance | list, count, details | required |  | – |
| Maintenance Schedule | list, count, details, child_rows | required |  | – |
| Quality Goal | list, count, details, child_rows | shared |  | – |
| Quality Procedure | list, count, details | shared |  | – |

## Gaps and limits worth knowing

- **Editing existing records** is not implemented, by decision. The chat explains this, and for a form with unsaved
  changes it says that nothing typed was saved.
- **Payment Entry** is queryable but never creatable ("I never record or make payments").
- **Reports** are described from their name and validated filters; their results are never read.
- **Image extraction** covers invoices, credit/debit notes, orders, quotations, delivery documents, goods receipts,
  material requests and BOMs. Other kinds ("Proforma", "Packing List") are refused, not guessed.
- **Guided Stock Entry** supports Material Receipt, Material Issue and Material Transfer only.
- **Journal Entry** is restricted: no bank, cash, party or stock lines, and the company currency only.
- The **PDF** upload path exists on the server, but the chat UI accepts only images (F16).

## Regenerating this table

Run `chat.capabilities.catalog()` on a test site and format `query.operations`, `query.company`,
`query.employee_scope`, `create.result` and `image` per DocType. The machine-readable inventory is
[coverage_inventory.json](../coverage_inventory.json). `tests/test_inventory.py` checks that it is complete.
