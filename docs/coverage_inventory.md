# ai_erpnext coverage inventory

Generated from the installed apps and DocType metadata of site `ci-test.local` and from the real capability policy. Do not edit by hand: regenerate with `bench --site <test-site> execute ai_erpnext.capability.inventory.write_inventory`.

Installed apps: frappe, erpnext, ai_erpnext, proposals, hrms. HRMS installed: **yes**.

A DocType that exists in the metadata is **not** available to the chatbot. Only DocTypes listed in `capability/policy.py` are, and only for users who may read them (role, User Permission, company, field permission level).

## Legend

| Code | Status |
| --- | --- |
| T | Implemented and tested (a data-backed test exercises it) |
| I | Implemented but not sufficiently tested: the execution path exists and is smoke-tested only |
| P | Partially implemented: guided creation of a deliberately small field set |
| - | Not implemented |
| n/a | Not applicable |
| X | Blocked: sensitive, consequential or unsafe without a separate design |

Editing, standard-report access and workflow actions are **not implemented for any DocType** (out of scope; reports are blocked, see below).

## Summary by module

| Module | Implemented and tested | Implemented, not sufficiently tested | Not implemented | Blocked | Not applicable | child tables / single |
| --- | --- | --- | --- | --- | --- | --- |
| AI | 0 | 0 | 2 | 0 | 0 | 1 |
| Accounting | 1 | 8 | 54 | 12 | 5 | 107 |
| Assets | 0 | 5 | 8 | 0 | 1 | 12 |
| Bulk Transaction | 0 | 0 | 0 | 0 | 1 | 0 |
| Buying | 0 | 4 | 5 | 0 | 0 | 11 |
| CRM | 0 | 4 | 9 | 0 | 0 | 15 |
| Communication | 0 | 0 | 1 | 0 | 0 | 1 |
| EDI | 0 | 0 | 2 | 0 | 0 | 0 |
| ERPNext Integrations | 0 | 0 | 0 | 0 | 0 | 1 |
| Frappe framework | 0 | 2 | 5 | 18 | 2 | 0 |
| HR (HRMS) | 0 | 8 | 82 | 7 | 1 | 59 |
| Maintenance | 0 | 1 | 1 | 0 | 0 | 3 |
| Manufacturing | 0 | 6 | 9 | 0 | 1 | 31 |
| Portal | 0 | 0 | 0 | 0 | 0 | 2 |
| Projects | 0 | 4 | 5 | 0 | 0 | 6 |
| Proposals | 0 | 0 | 1 | 0 | 0 | 0 |
| Quality Management | 0 | 2 | 6 | 0 | 0 | 8 |
| Regional / localization | 0 | 0 | 1 | 0 | 3 | 1 |
| Selling | 0 | 3 | 6 | 0 | 0 | 9 |
| Setup | 0 | 7 | 18 | 1 | 0 | 14 |
| Stock | 0 | 15 | 23 | 1 | 0 | 36 |
| Subcontracting | 0 | 2 | 2 | 0 | 0 | 9 |
| Support | 0 | 2 | 3 | 0 | 0 | 6 |
| Telephony | 0 | 0 | 1 | 0 | 3 | 1 |
| Utilities | 0 | 0 | 1 | 0 | 0 | 3 |

## Implemented DocTypes

Columns: R read/list, D details, F filter+sort, A aggregate, C child tables, L linked records, S party summary, Cr creation (guided; Draft or saved-immediately), Img image to Draft (the document's direction decides the target, see detection/direction.py), Co company policy, W workflow (none), Ed edit, Rp reports.

| Module | DocType | R | D | F | A | C | L | S | Cr | Img | Ed | Rp | W | Company policy | Tests |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Accounting | Account | T | I | I | I | n/a | I | n/a | - | - | - | - | - | required | data-backed: company, read |
| Accounting | Cost Center | T | I | I | I | n/a | I | n/a | - | - | - | - | - | required | data-backed: company, read |
| Accounting | Dunning | I | I | I | I | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| Accounting | GL Entry | T | - | I | T | n/a | I | n/a | - | - | - | - | - | required | data-backed: aggregate, company, read |
| Accounting | Journal Entry | T | I | T | I | T | n/a | n/a | PT | - | - | - | - | required | data-backed: child, filter, read |
| Accounting | Payment Entry | T | I | T | T | n/a | n/a | n/a | - | - | - | - | - | required | data-backed: aggregate, filter, read |
| Accounting | Payment Request | I | I | I | I | n/a | n/a | n/a | - | - | - | - | - | required | smoke only |
| Accounting | Purchase Invoice | T | T | T | I | I | I | n/a | PT | T | - | - | - | required | data-backed: company, details, filter, read |
| Accounting | Sales Invoice | T | T | T | T | T | T | n/a | PT | T | - | - | - | required | data-backed: aggregate, child, company, details, filter, link, read |
| Assets | Asset | I | I | I | I | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| Assets | Asset Category | I | I | I | I | n/a | n/a | n/a | - | - | - | - | - | shared | smoke only |
| Assets | Asset Maintenance | I | I | I | - | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| Assets | Asset Movement | I | I | I | - | I | n/a | n/a | - | - | - | - | - | required | smoke only |
| Assets | Asset Repair | I | I | I | I | n/a | I | n/a | - | - | - | - | - | optional | smoke only |
| Buying | Purchase Order | I | I | I | I | I | I | n/a | PT | T | - | - | - | required | smoke only |
| Buying | Request for Quotation | I | I | I | I | I | I | n/a | - | - | - | - | - | required | smoke only |
| Buying | Supplier | T | I | I | I | n/a | I | T | PT | - | - | - | - | shared | data-backed: read, summary |
| Buying | Supplier Quotation | I | I | I | I | I | I | n/a | PT | - | - | - | - | required | smoke only |
| CRM | Campaign | I | I | I | - | n/a | n/a | n/a | - | - | - | - | - | shared | smoke only |
| CRM | Lead | T | I | I | I | n/a | I | n/a | PT | - | - | - | - | optional | data-backed: company, read |
| CRM | Opportunity | I | I | I | I | I | I | n/a | PT | - | - | - | - | required | smoke only |
| CRM | Prospect | I | I | I | I | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| Frappe framework | Address | I | I | I | - | n/a | n/a | n/a | - | - | - | - | - | shared | smoke only |
| Frappe framework | Contact | I | I | I | I | n/a | n/a | n/a | - | - | - | - | - | shared | smoke only |
| HR (HRMS) | Appraisal | I | I | I | - | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| HR (HRMS) | Attendance | I | I | I | I | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| HR (HRMS) | Employee Advance | I | I | I | I | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| HR (HRMS) | Expense Claim | T | T | I | T | I | I | n/a | PT | - | - | - | - | required | data-backed: aggregate, company, details, read |
| HR (HRMS) | Leave Allocation | I | I | I | I | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| HR (HRMS) | Leave Application | I | I | I | I | n/a | I | n/a | PT | - | - | - | - | required | smoke only |
| HR (HRMS) | Shift Assignment | I | I | I | I | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| HR (HRMS) | Training Event | I | I | I | - | n/a | n/a | n/a | - | - | - | - | - | optional | smoke only |
| Maintenance | Maintenance Schedule | I | I | I | - | I | I | n/a | - | - | - | - | - | required | smoke only |
| Manufacturing | BOM | I | T | I | I | I | I | n/a | - | T | - | - | - | required | data-backed: details |
| Manufacturing | Job Card | I | I | I | I | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| Manufacturing | Production Plan | I | I | I | I | I | n/a | n/a | - | - | - | - | - | required | smoke only |
| Manufacturing | Routing | I | I | I | - | I | n/a | n/a | - | - | - | - | - | shared | smoke only |
| Manufacturing | Work Order | T | I | T | I | T | I | n/a | PT | - | - | - | - | required | data-backed: child, filter, read |
| Manufacturing | Workstation | I | I | I | - | n/a | I | n/a | - | - | - | - | - | shared | smoke only |
| Projects | Activity Type | I | I | I | - | n/a | n/a | n/a | - | - | - | - | - | shared | smoke only |
| Projects | Project | T | I | I | I | n/a | I | n/a | PT | - | - | - | - | required | data-backed: company, read |
| Projects | Task | T | I | T | T | n/a | I | n/a | PT | - | - | - | - | optional | data-backed: aggregate, company, filter, read |
| Projects | Timesheet | I | I | I | I | I | I | n/a | PT | - | - | - | - | required | smoke only |
| Quality Management | Quality Goal | I | I | I | - | I | I | n/a | - | - | - | - | - | shared | smoke only |
| Quality Management | Quality Procedure | I | I | I | - | n/a | I | n/a | - | - | - | - | - | shared | smoke only |
| Selling | Customer | T | T | I | I | n/a | I | T | PT | - | - | - | - | shared | data-backed: company, details, read, summary |
| Selling | Quotation | T | T | I | I | T | n/a | n/a | PT | T | - | - | - | required | data-backed: child, company, details, read |
| Selling | Sales Order | T | I | T | I | I | I | n/a | PT | T | - | - | - | required | data-backed: filter, read, summary |
| Setup | Brand | I | I | I | I | n/a | n/a | n/a | - | - | - | - | - | shared | smoke only |
| Setup | Customer Group | I | I | I | I | n/a | I | n/a | - | - | - | - | - | shared | smoke only |
| Setup | Employee | T | T | T | I | n/a | n/a | n/a | PT | - | - | - | - | required | data-backed: company, details, filter, read |
| Setup | Holiday List | I | I | I | - | I | n/a | n/a | - | - | - | - | - | shared | smoke only |
| Setup | Item Group | I | I | I | I | n/a | I | n/a | - | - | - | - | - | shared | smoke only |
| Setup | Supplier Group | I | I | I | I | n/a | I | n/a | - | - | - | - | - | shared | smoke only |
| Setup | Territory | T | I | I | I | n/a | I | n/a | - | - | - | - | - | shared | data-backed: company, read |
| Stock | Batch | I | I | I | I | n/a | I | n/a | - | - | - | - | - | shared | smoke only |
| Stock | Bin | T | - | T | T | n/a | I | n/a | - | - | - | - | - | optional | data-backed: aggregate, company, filter, read, summary |
| Stock | Delivery Note | I | I | I | I | I | I | n/a | PT | T | - | - | - | required | smoke only |
| Stock | Item | T | T | I | I | n/a | I | n/a | PT | - | - | - | - | shared | data-backed: details, read |
| Stock | Item Price | I | I | I | I | n/a | I | n/a | - | - | - | - | - | shared | smoke only |
| Stock | Material Request | I | I | I | I | I | I | n/a | PT | T | - | - | - | required | smoke only |
| Stock | Pick List | I | I | I | I | I | I | n/a | - | - | - | - | - | required | smoke only |
| Stock | Price List | I | I | I | I | n/a | n/a | n/a | - | - | - | - | - | shared | smoke only |
| Stock | Purchase Receipt | I | I | I | I | I | I | n/a | PT | T | - | - | - | required | smoke only |
| Stock | Quality Inspection | I | I | I | I | I | I | n/a | - | - | - | - | - | required | smoke only |
| Stock | Serial No | I | I | I | I | n/a | I | n/a | - | - | - | - | - | required | smoke only |
| Stock | Stock Entry | T | I | T | I | T | I | n/a | PT | - | - | - | - | required | data-backed: child, filter, read |
| Stock | Stock Ledger Entry | T | - | T | I | n/a | I | n/a | - | - | - | - | - | required | data-backed: filter, read |
| Stock | Stock Reconciliation | I | I | I | I | I | I | n/a | - | - | - | - | - | required | smoke only |
| Stock | Warehouse | T | I | I | I | n/a | I | n/a | - | - | - | - | - | required | data-backed: company, read |
| Subcontracting | Subcontracting Order | I | I | I | I | I | I | n/a | - | - | - | - | - | required | smoke only |
| Subcontracting | Subcontracting Receipt | I | I | I | I | I | I | n/a | - | - | - | - | - | required | smoke only |
| Support | Issue | T | I | I | I | n/a | I | n/a | PT | - | - | - | - | optional | data-backed: company, read |
| Support | Warranty Claim | I | I | I | I | n/a | I | n/a | - | - | - | - | - | required | smoke only |

## Not implemented, blocked or not applicable

### AI

| DocType | Status | Reason / priority |
| --- | --- | --- |
| AI Chat Session | Not implemented | priority Low |
| AI Document Entry | Not implemented | priority Low |

### Accounting

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Bank Account | Blocked | bank account numbers and IBANs |
| Bank Guarantee | Blocked | bank references |
| Bank Transaction | Blocked | bank statement lines |
| Exchange Rate Revaluation | Blocked | posts journal entries |
| Payment Gateway Account | Blocked | payment gateway credentials |
| Payment Order | Blocked | payment execution |
| Period Closing Voucher | Blocked | consequential accounting lifecycle |
| Process Deferred Accounting | Blocked | posts journal entries |
| Process Statement Of Accounts | Blocked | sends statements |
| Repost Accounting Ledger | Blocked | rewrites ledgers |
| Subscription | Blocked | recurring document creation |
| Unreconcile Payment | Blocked | payment lifecycle |
| Bank Statement Import | Not applicable | tool, log, setting or configuration |
| Bank Statement Import Log | Not applicable | tool, log, setting or configuration |
| POS Invoice Merge Log | Not applicable | tool, log, setting or configuration |
| Process Payment Reconciliation Log | Not applicable | tool, log, setting or configuration |
| Repost Payment Ledger | Not applicable | tool, log, setting or configuration |
| Account Category | Not implemented | priority Low |
| Account Closing Balance | Not implemented | priority Low |
| Accounting Dimension | Not implemented | priority Low |
| Accounting Dimension Filter | Not implemented | priority Low |
| Accounting Period | Not implemented | priority Low |
| Advance Payment Ledger Entry | Not implemented | priority Low |
| Bank | Not implemented | priority Low |
| Bank Account Balance | Not implemented | priority Low |
| Bank Account Subtype | Not implemented | priority Low |
| Bank Account Type | Not implemented | priority Low |
| Bank Transaction Rule | Not implemented | priority Low |
| Bisect Nodes | Not implemented | priority Low |
| Budget | Not implemented | priority Low |
| Cashier Closing | Not implemented | priority Low |
| Cheque Print Template | Not implemented | priority Low |
| Cost Center Allocation | Not implemented | priority Low |
| Coupon Code | Not implemented | priority Low |
| Dunning Type | Not implemented | priority Low |
| Finance Book | Not implemented | priority Low |
| Financial Report Template | Not implemented | priority Low |
| Fiscal Year | Not implemented | priority Low |
| Invoice Discounting | Not implemented | priority Low |
| Item Tax Template | Not implemented | priority Low |
| Journal Entry Template | Not implemented | priority Low |
| Ledger Health | Not implemented | priority Low |
| Ledger Merge | Not implemented | priority Low |
| Loyalty Point Entry | Not implemented | priority Low |
| Loyalty Program | Not implemented | priority Low |
| Mode of Payment | Not implemented | priority Medium |
| Monthly Distribution | Not implemented | priority Low |
| POS Closing Entry | Not implemented | priority Low |
| POS Invoice | Not implemented | priority Low |
| POS Opening Entry | Not implemented | priority Low |
| POS Profile | Not implemented | priority Low |
| Party Link | Not implemented | priority Low |
| Payment Ledger Entry | Not implemented | priority Low |
| Payment Term | Not implemented | priority Low |
| Payment Terms Template | Not implemented | priority Medium |
| Pricing Rule | Not implemented | priority High |
| Process Payment Reconciliation | Not implemented | priority Low |
| Process Period Closing Voucher | Not implemented | priority Low |
| Process Subscription | Not implemented | priority Low |
| Promotional Scheme | Not implemented | priority Low |
| Purchase Taxes and Charges Template | Not implemented | priority Medium |
| Sales Taxes and Charges Template | Not implemented | priority Medium |
| Share Transfer | Not implemented | priority Low |
| Share Type | Not implemented | priority Low |
| Shareholder | Not implemented | priority Low |
| Shipping Rule | Not implemented | priority Low |
| Subscription Plan | Not implemented | priority Low |
| Tax Category | Not implemented | priority Low |
| Tax Rule | Not implemented | priority Low |
| Tax Withholding Category | Not implemented | priority Low |
| Tax Withholding Group | Not implemented | priority Low |

### Assets

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Asset Maintenance Log | Not applicable | tool, log, setting or configuration |
| Asset Activity | Not implemented | priority Low |
| Asset Capitalization | Not implemented | priority Low |
| Asset Depreciation Schedule | Not implemented | priority Medium |
| Asset Maintenance Team | Not implemented | priority Low |
| Asset Shift Allocation | Not implemented | priority Low |
| Asset Shift Factor | Not implemented | priority Low |
| Asset Value Adjustment | Not implemented | priority Low |
| Location | Not implemented | priority Low |

### Bulk Transaction

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Bulk Transaction Log Detail | Not applicable | tool, log, setting or configuration |

### Buying

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Supplier Scorecard | Not implemented | priority Low |
| Supplier Scorecard Criteria | Not implemented | priority Low |
| Supplier Scorecard Period | Not implemented | priority Low |
| Supplier Scorecard Standing | Not implemented | priority Low |
| Supplier Scorecard Variable | Not implemented | priority Low |

### CRM

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Appointment | Not implemented | priority Low |
| Competitor | Not implemented | priority Low |
| Contract | Not implemented | priority Medium |
| Contract Template | Not implemented | priority Low |
| Email Campaign | Not implemented | priority Low |
| Market Segment | Not implemented | priority Low |
| Opportunity Lost Reason | Not implemented | priority Low |
| Opportunity Type | Not implemented | priority Low |
| Sales Stage | Not implemented | priority Low |

### Communication

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Communication Medium | Not implemented | priority Low |

### EDI

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Code List | Not implemented | priority Low |
| Common Code | Not implemented | priority Low |

### Frappe framework

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Activity Log | Blocked | user activity |
| Client Script | Blocked | code |
| Comment | Blocked | free text across documents |
| Communication | Blocked | emails and messages |
| Custom DocPerm | Blocked | permission model |
| Custom Field | Blocked | customisation, not data |
| DocPerm | Blocked | permission model |
| Email Account | Blocked | mailbox credentials |
| Error Log | Blocked | may contain business data |
| File | Blocked | private attachments |
| Property Setter | Blocked | customisation, not data |
| Role | Blocked | permission model |
| Server Script | Blocked | code |
| System Settings | Blocked | site configuration |
| User | Blocked | accounts and credentials |
| User Permission | Blocked | permission model |
| Version | Blocked | document change history may hold restricted values |
| Workflow | Blocked | workflow definitions: no workflow actions are supported |
| Notification Log | Not applicable | system notifications |
| Print Format | Not applicable | layout |
| Currency | Not implemented | reference data; used through links only |
| Event | Not implemented | calendar: needs an owner-scoped design |
| Note | Not implemented | personal notes |
| Report | Not implemented | report runner deferred, see the report blocker |
| ToDo | Not implemented | personal tasks: needs an owner-scoped design |

### HR (HRMS)

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Employee Checkin | Blocked | employees' geolocation; no company field to isolate companies (HRMS) |
| Interview | Blocked | candidates' assessments; no company field (HRMS) |
| Job Applicant | Blocked | candidates' personal data; no company field (HRMS) |
| Payroll Entry | Blocked | payroll execution (HRMS) |
| Salary Slip | Blocked | payroll data (HRMS) |
| Salary Structure | Blocked | payroll data (HRMS) |
| Salary Structure Assignment | Blocked | payroll data (HRMS) |
| Vehicle Log | Not applicable | tool, log, setting or configuration |
| Additional Salary | Not implemented | priority Low |
| Appointment Letter | Not implemented | priority Low |
| Appointment Letter Template | Not implemented | priority Low |
| Appraisal Cycle | Not implemented | priority Low |
| Appraisal Template | Not implemented | priority Low |
| Arrear | Not implemented | priority Low |
| Attendance Request | Not implemented | priority Low |
| Compensatory Leave Request | Not implemented | priority Low |
| Employee Benefit Application | Not implemented | priority Low |
| Employee Benefit Claim | Not implemented | priority Low |
| Employee Benefit Ledger | Not implemented | priority Low |
| Employee Feedback Criteria | Not implemented | priority Low |
| Employee Grade | Not implemented | priority Low |
| Employee Grievance | Not implemented | priority Low |
| Employee Health Insurance | Not implemented | priority Low |
| Employee Incentive | Not implemented | priority Low |
| Employee Onboarding | Not implemented | priority Low |
| Employee Onboarding Template | Not implemented | priority Low |
| Employee Other Income | Not implemented | priority Low |
| Employee Performance Feedback | Not implemented | priority Low |
| Employee Promotion | Not implemented | priority Low |
| Employee Referral | Not implemented | priority Low |
| Employee Separation | Not implemented | priority Low |
| Employee Separation Template | Not implemented | priority Low |
| Employee Skill Map | Not implemented | priority Low |
| Employee Tax Exemption Category | Not implemented | priority Low |
| Employee Tax Exemption Declaration | Not implemented | priority Low |
| Employee Tax Exemption Proof Submission | Not implemented | priority Low |
| Employee Tax Exemption Sub Category | Not implemented | priority Low |
| Employee Transfer | Not implemented | priority Low |
| Employment Type | Not implemented | priority Low |
| Exit Interview | Not implemented | priority Low |
| Expense Claim Type | Not implemented | priority Low |
| Full and Final Statement | Not implemented | priority Low |
| Goal | Not implemented | priority Low |
| Gratuity | Not implemented | priority Low |
| Gratuity Rule | Not implemented | priority Low |
| Grievance Type | Not implemented | priority Low |
| HR Telemetry Milestone | Not implemented | priority Low |
| Holiday List Assignment | Not implemented | priority Low |
| Identification Document Type | Not implemented | priority Low |
| Income Tax Slab | Not implemented | priority Low |
| Interest | Not implemented | priority Low |
| Interview Feedback | Not implemented | priority Low |
| Interview Type | Not implemented | priority Low |
| Job Applicant Source | Not implemented | priority Low |
| Job Offer | Not implemented | priority Low |
| Job Offer Term Template | Not implemented | priority Low |
| Job Opening | Not implemented | priority Low |
| Job Opening Template | Not implemented | priority Low |
| Job Requisition | Not implemented | priority Low |
| KRA | Not implemented | priority Low |
| Leave Adjustment | Not implemented | priority Low |
| Leave Block List | Not implemented | priority Low |
| Leave Encashment | Not implemented | priority Low |
| Leave Ledger Entry | Not implemented | priority Low |
| Leave Period | Not implemented | priority Low |
| Leave Policy | Not implemented | priority Low |
| Leave Policy Assignment | Not implemented | priority Low |
| Leave Type | Not implemented | priority Low |
| Offer Term | Not implemented | priority Low |
| Overtime Slip | Not implemented | priority Low |
| Overtime Type | Not implemented | priority Low |
| PWA Notification | Not implemented | priority Low |
| Payroll Correction | Not implemented | priority Low |
| Payroll Period | Not implemented | priority Low |
| Purpose of Travel | Not implemented | priority Low |
| Retention Bonus | Not implemented | priority Low |
| Salary Component | Not implemented | priority Low |
| Salary Withholding | Not implemented | priority Low |
| Shift Location | Not implemented | priority Low |
| Shift Request | Not implemented | priority Low |
| Shift Schedule | Not implemented | priority Low |
| Shift Schedule Assignment | Not implemented | priority Low |
| Shift Type | Not implemented | priority Low |
| Skill | Not implemented | priority Low |
| Staffing Plan | Not implemented | priority Low |
| Training Feedback | Not implemented | priority Low |
| Training Program | Not implemented | priority Low |
| Training Result | Not implemented | priority Low |
| Travel Request | Not implemented | priority Low |
| Vehicle Service Item | Not implemented | priority Low |

### Maintenance

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Maintenance Visit | Not implemented | priority Medium |

### Manufacturing

| DocType | Status | Reason / priority |
| --- | --- | --- |
| BOM Update Log | Not applicable | tool, log, setting or configuration |
| BOM Creator | Not implemented | priority Low |
| Blanket Order | Not implemented | priority Medium |
| Downtime Entry | Not implemented | priority Low |
| Master Production Schedule | Not implemented | priority Low |
| Operation | Not implemented | priority Low |
| Plant Floor | Not implemented | priority Low |
| Sales Forecast | Not implemented | priority Low |
| Workstation Operating Component | Not implemented | priority Low |
| Workstation Type | Not implemented | priority Low |

### Projects

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Activity Cost | Not implemented | priority Low |
| Project Template | Not implemented | priority Low |
| Project Type | Not implemented | priority Low |
| Project Update | Not implemented | priority Low |
| Task Type | Not implemented | priority Low |

### Proposals

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Proposal | Not implemented | priority Low |

### Quality Management

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Non Conformance | Not implemented | priority Medium |
| Quality Action | Not implemented | priority Low |
| Quality Feedback | Not implemented | priority Low |
| Quality Feedback Template | Not implemented | priority Low |
| Quality Meeting | Not implemented | priority Low |
| Quality Review | Not implemented | priority Low |

### Regional / localization

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Import Supplier Invoice | Not applicable | tool, log, setting or configuration |
| South Africa VAT Settings | Not applicable | tool, log, setting or configuration |
| UAE VAT Settings | Not applicable | tool, log, setting or configuration |
| Lower Deduction Certificate | Not implemented | priority Low |

### Selling

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Delivery Schedule Item | Not implemented | priority Low |
| Industry Type | Not implemented | priority Low |
| Installation Note | Not implemented | priority Low |
| Party Specific Item | Not implemented | priority Low |
| Product Bundle | Not implemented | priority Medium |
| Sales Partner Type | Not implemented | priority Low |

### Setup

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Transaction Deletion Record | Blocked | deletes data |
| Authorization Rule | Not implemented | priority Low |
| Branch | Not implemented | priority Low |
| Company | Not implemented | priority Low |
| Currency Exchange | Not implemented | priority High |
| Department | Not implemented | priority Low |
| Designation | Not implemented | priority Low |
| Driver | Not implemented | priority Low |
| Email Digest | Not implemented | priority Low |
| Employee Group | Not implemented | priority Low |
| Incoterm | Not implemented | priority Low |
| Party Type | Not implemented | priority Low |
| Quotation Lost Reason | Not implemented | priority Low |
| Sales Partner | Not implemented | priority Medium |
| Sales Person | Not implemented | priority Medium |
| Terms and Conditions | Not implemented | priority Low |
| UOM | Not implemented | priority Medium |
| UOM Conversion Factor | Not implemented | priority Low |
| Vehicle | Not implemented | priority Low |

### Stock

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Repost Item Valuation | Blocked | rewrites stock valuation |
| Customs Tariff Number | Not implemented | priority Low |
| Delivery Trip | Not implemented | priority Medium |
| Inventory Dimension | Not implemented | priority Low |
| Item Alternative | Not implemented | priority Low |
| Item Attribute | Not implemented | priority Low |
| Item Lead Time | Not implemented | priority Low |
| Item Manufacturer | Not implemented | priority Low |
| Landed Cost Voucher | Not implemented | priority Medium |
| Manufacturer | Not implemented | priority Low |
| Packing Slip | Not implemented | priority Medium |
| Putaway Rule | Not implemented | priority Low |
| Quality Inspection Parameter | Not implemented | priority Low |
| Quality Inspection Parameter Group | Not implemented | priority Low |
| Quality Inspection Template | Not implemented | priority Low |
| Serial and Batch Bundle | Not implemented | priority Medium |
| Shipment | Not implemented | priority Low |
| Shipment Parcel Template | Not implemented | priority Low |
| Stock Closing Balance | Not implemented | priority Low |
| Stock Closing Entry | Not implemented | priority Low |
| Stock Entry Type | Not implemented | priority Low |
| Stock Reservation Entry | Not implemented | priority Low |
| UOM Category | Not implemented | priority Low |
| Warehouse Type | Not implemented | priority Low |

### Subcontracting

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Subcontracting BOM | Not implemented | priority Low |
| Subcontracting Inward Order | Not implemented | priority Low |

### Support

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Issue Priority | Not implemented | priority Low |
| Issue Type | Not implemented | priority Low |
| Service Level Agreement | Not implemented | priority Medium |

### Telephony

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Call Log | Not applicable | tool, log, setting or configuration |
| Incoming Call Settings | Not applicable | tool, log, setting or configuration |
| Voice Call Settings | Not applicable | tool, log, setting or configuration |
| Telephony Call Type | Not implemented | priority Low |

### Utilities

| DocType | Status | Reason / priority |
| --- | --- | --- |
| Video | Not implemented | priority Low |

## Standard reports

Standard reports are not exposed. A report runner needs proof, per report, that its underlying query applies the user's role, User Permission and company restrictions; none was proven. Next phase, one report at a time.

## Page context

- **Current form and record:** Supported (v2 context): DocType, record, the saved record's modified timestamp, whether the form is dirty, up to 10 changed fields and the selected rows of one child table. The server re-fetches the record with permission, company and field checks; a forged or inaccessible record is ignored.
- **List filters and selected rows:** Supported for allowlisted DocTypes: up to 8 filters (translated by the capability service; an untranslatable filter is named, never dropped), sort order, page, and up to 20 selected records (each re-authorised on the server).
- **Reports and report filters:** Supported for the reports in capability/policy.py REPORTS: the report is described by name and validated filters only. Reports are never executed and their results are never read (REPORT_RESULTS_ENABLED is False).
- **Unsaved form changes:** Supported for explanation only: labelled as unsaved, shown once, not stored in the conversation, never written. An edit request is told the form must first be saved or discarded.
- **Stale-record detection:** Supported: the browser's modified timestamp is compared with the saved record; a mismatch is announced and the answer comes from the latest saved version.

Frappe framework DocTypes not listed individually (system plumbing): 240.
