"""Coverage inventory: which ERPNext DocTypes the assistant supports, and how well that is verified.

The inventory is generated, not stored: it reads the installed apps and DocType metadata of the site it runs on and the
real policy objects (capability.policy / capability.service / chat.forms), so it can't claim a capability that has no
policy entry. Nothing is written to the database. Use it on a development or test site:

    bench --site <test-site> execute ai_erpnext.capability.inventory.write_inventory

which refreshes docs/coverage_inventory.md and docs/coverage_inventory.json.

A DocType in the metadata is never available because it exists. "Implemented" means the policy lists it AND an execution path
exists (queries.execute). "Tested" is a separate, human-maintained claim (VERIFIED below): it may only list what a data-backed test
in tests/ really exercises; tests/test_capability.py checks that every entry names a registered DocType and capability.
"""

import json
import os

import frappe

from ai_erpnext.capability import metadata as M
from ai_erpnext.capability import policy as P
from ai_erpnext.capability import service as C

IMPLEMENTED_TESTED = "Implemented and tested"
IMPLEMENTED = "Implemented but not sufficiently tested"
PARTIAL = "Partially implemented"
NOT_IMPLEMENTED = "Not implemented"
NOT_APPLICABLE = "Not applicable"
BLOCKED = "Blocked"

# Capabilities of the query side, in the order they are reported.
QUERY_CAPABILITIES = ("read", "details", "filter", "aggregate", "child", "link", "summary")

# Capabilities exercised against real data (records, users, companies, permissions) by tests in ai_erpnext/tests. Everything else
# registered is only smoke-tested (tests/test_coverage_batch.py runs list/count/aggregate/child_rows for every registered DocType,
# which proves the execution path works but not that the results are right).
VERIFIED = {
	"Sales Invoice": {"read", "details", "filter", "aggregate", "child", "link", "company"},
	"Purchase Invoice": {"read", "details", "filter", "company"},  # test_coverage_phase_d, test_page_reference_and_parity (returns, by supplier)
	"Customer": {"read", "details", "summary", "company"},
	"Item": {"read", "details"},
	"Supplier": {"read", "summary"},  # test_page_reference_and_parity.test_a_supplier_summary_with_values
	"Employee": {"read", "filter"},  # test_coverage_phase_d.test_employee_organisational_fields_only reads a real Employee
	"Quotation": {"read", "details", "child", "company"},
	"Sales Order": {"read", "filter", "summary"},
	"Payment Entry": {"read", "filter", "aggregate"},
	"Journal Entry": {"read", "filter", "child"},
	"Stock Entry": {"read", "filter", "child"},
	"BOM": {"details"},
	"Work Order": {"read", "filter", "child"},
	"Account": {"read", "company"},
	"Cost Center": {"read", "company"},
	"Warehouse": {"read", "company"},
	"GL Entry": {"read", "aggregate", "company"},
	"Stock Ledger Entry": {"read", "filter"},  # real movements from submitted Stock Entries (test_coverage_phase_d)
	"Territory": {"read", "company"},
	"Lead": {"read", "company"},
	"Project": {"read", "company"},
	"Issue": {"read", "company"},
	"Task": {"read", "aggregate", "company"},
	"Bin": {"read", "filter", "aggregate", "company"},
}
CREATE_TESTED = {"Sales Invoice", "Purchase Invoice", "Sales Order", "Purchase Order", "Quotation", "Delivery Note", "Purchase Receipt", "Customer",
	"Supplier", "Item", "Lead", "Issue", "Material Request", "Stock Entry"}
# Target DocTypes an image becomes, each with an end-to-end test of the persisted Draft (tests/test_document_direction.py).
IMAGE_TESTED = {"Sales Invoice", "Purchase Invoice", "Sales Order", "Purchase Order", "Quotation", "Delivery Note", "Purchase Receipt",
	"Material Request", "BOM"}

# Sensitive or consequential DocTypes that must not be exposed without a separate design.
BLOCKED_REASONS = {
	"Bank Account": "bank account numbers and IBANs", "Bank Transaction": "bank statement lines", "Bank Guarantee": "bank references",
	"Payment Gateway Account": "payment gateway credentials", "Payment Order": "payment execution", "Payment Reconciliation": "payment execution",
	"Bank Clearance": "bank reconciliation action", "Bank Reconciliation Tool": "bank reconciliation action",
	"Period Closing Voucher": "consequential accounting lifecycle", "Exchange Rate Revaluation": "posts journal entries",
	"Process Deferred Accounting": "posts journal entries", "Repost Item Valuation": "rewrites stock valuation",
	"Repost Accounting Ledger": "rewrites ledgers", "Transaction Deletion Record": "deletes data", "Process Statement Of Accounts": "sends statements",
	"Opening Invoice Creation Tool": "bulk document creation", "Bulk Transaction Log": "bulk mutation log", "Unreconcile Payment": "payment lifecycle",
	"Subscription": "recurring document creation", "Auto Repeat": "recurring document creation", "Salary Slip": "payroll data (HRMS)",
}

# DocTypes that are tools, logs, settings or configuration rather than business data.
NOT_APPLICABLE_WORDS = ("Settings", "Tool", "Log", "Import", "Wizard", "Setup", "Repost", "Unlink", "Preview", "Mapping", "Reference", "Template Task")

# Highest-value candidates for the next read-only batch (reviewed by hand; everything else not implemented is "Low").
PRIORITY = {
	"Payment Terms Template": "Medium", "Sales Taxes and Charges Template": "Medium", "Purchase Taxes and Charges Template": "Medium", "Delivery Trip": "Medium",
	"Landed Cost Voucher": "Medium", "Material Request Plan Item": "Low", "Sales Person": "Medium", "Sales Partner": "Medium", "Campaign": "Medium",
	"Contract": "Medium", "Maintenance Schedule": "Medium", "Maintenance Visit": "Medium", "Blanket Order": "Medium", "Packing Slip": "Medium",
	"Workstation": "Medium", "Operation": "Low", "BOM Creator": "Low", "Supplier Scorecard": "Low", "Quality Goal": "Medium", "Quality Review": "Low",
	"Non Conformance": "Medium", "Quality Procedure": "Low", "Asset Maintenance": "Medium", "Asset Movement": "Medium", "Asset Repair": "Medium",
	"Asset Depreciation Schedule": "Medium", "Pricing Rule": "High", "Coupon Code": "Low", "Mode of Payment": "Medium", "Payment Terms": "Low",
	"Finance Book": "Low", "Journal Entry Template": "Low", "Address": "High", "Supplier Quotation": "High", "Stock Closing Entry": "Low",
	"Serial and Batch Bundle": "Medium", "Inventory Dimension": "Low", "Item Attribute": "Low", "Product Bundle": "Medium", "Unit of Measure": "Low",
	"UOM": "Medium", "Currency Exchange": "High", "Sales Forecast": "Low", "Master Production Schedule": "Low", "Opportunity Type": "Low",
	"Lead Source": "Low", "Appointment": "Low", "Service Level Agreement": "Medium", "Issue Type": "Low", "Issue Priority": "Low", "Warranty Claim": "Low",
}

# Framework (Frappe core) DocTypes that matter for a business assistant. Everything else of the framework is system plumbing.
FRAMEWORK = {
	"Contact": ("Implemented", "shared business master data"), "Address": (NOT_IMPLEMENTED, "personal addresses: needs a field review"),
	"Currency": (NOT_IMPLEMENTED, "reference data; used through links only"), "User": (BLOCKED, "accounts and credentials"),
	"Role": (BLOCKED, "permission model"), "DocPerm": (BLOCKED, "permission model"), "Custom DocPerm": (BLOCKED, "permission model"),
	"User Permission": (BLOCKED, "permission model"), "Workflow": (BLOCKED, "workflow definitions: no workflow actions are supported"),
	"File": (BLOCKED, "private attachments"), "Communication": (BLOCKED, "emails and messages"), "Email Account": (BLOCKED, "mailbox credentials"),
	"Error Log": (BLOCKED, "may contain business data"), "Version": (BLOCKED, "document change history may hold restricted values"),
	"Activity Log": (BLOCKED, "user activity"), "ToDo": (NOT_IMPLEMENTED, "personal tasks: needs an owner-scoped design"),
	"Note": (NOT_IMPLEMENTED, "personal notes"), "Event": (NOT_IMPLEMENTED, "calendar: needs an owner-scoped design"),
	"Comment": (BLOCKED, "free text across documents"), "Notification Log": (NOT_APPLICABLE, "system notifications"),
	"Custom Field": (BLOCKED, "customisation, not data"), "Property Setter": (BLOCKED, "customisation, not data"), "Server Script": (BLOCKED, "code"),
	"Client Script": (BLOCKED, "code"), "System Settings": (BLOCKED, "site configuration"), "Print Format": (NOT_APPLICABLE, "layout"),
	"Report": (NOT_IMPLEMENTED, "report runner deferred, see the report blocker"),
}

REQUESTED_MODULES = {"Selling": "Selling", "Buying": "Buying", "Stock": "Stock", "Accounts": "Accounting", "CRM": "CRM", "Projects": "Projects",
	"Support": "Support", "Manufacturing": "Manufacturing", "Quality Management": "Quality Management", "Assets": "Assets",
	"Subcontracting": "Subcontracting", "Regional": "Regional / localization"}

REPORT_BLOCKER = ("Standard reports are not exposed. A report runner needs proof, per report, that its underlying query applies the "
	"user's role, User Permission and company restrictions; none was proven. Next phase, one report at a time.")
PAGE_CONTEXT = {
	"Current form and record": "Supported (v2 context): DocType, record, the saved record's modified timestamp, whether the form is dirty, up to 10 changed fields and the "
		"selected rows of one child table. The server re-fetches the record with permission, company and field checks; a forged or inaccessible record is ignored.",
	"List filters and selected rows": "Supported for allowlisted DocTypes: up to 8 filters (translated by the capability service; an untranslatable filter is named, never "
		"dropped), sort order, page, and up to 20 selected records (each re-authorised on the server).",
	"Reports and report filters": "Supported for the reports in capability/policy.py REPORTS: the report is described by name and validated filters only. Reports are never "
		"executed and their results are never read (REPORT_RESULTS_ENABLED is False).",
	"Unsaved form changes": "Supported for explanation only: labelled as unsaved, shown once, not stored in the conversation, never written. An edit request is told the "
		"form must first be saved or discarded.",
	"Stale-record detection": "Supported: the browser's modified timestamp is compared with the saved record; a mismatch is announced and the answer comes from the latest saved version.",
}


def installed_apps():
	return frappe.get_installed_apps()


def hrms_installed():
	return "hrms" in installed_apps()


def _app_of(module):
	return frappe.local.module_app.get(frappe.scrub(module))


def _group(module):
	return REQUESTED_MODULES.get(module, module)


def _classify_unregistered(doctype, row):
	if row.istable:
		return NOT_APPLICABLE, "child table, reached through its parent"
	if doctype in BLOCKED_REASONS:  # a blocked tool stays blocked even when it is a single DocType
		return BLOCKED, BLOCKED_REASONS[doctype]
	if row.issingle:
		return NOT_APPLICABLE, "single settings DocType"
	if any(word in doctype for word in NOT_APPLICABLE_WORDS):
		return NOT_APPLICABLE, "tool, log, setting or configuration"
	return NOT_IMPLEMENTED, f"priority {PRIORITY.get(doctype, 'Low')}"


def _status(implemented, tested):
	if not implemented:
		return NOT_IMPLEMENTED
	return IMPLEMENTED_TESTED if tested else IMPLEMENTED


def capabilities_of(doctype):
	"""Per-capability status of a registered DocType, from the real policy objects."""
	spec = P.REGISTRY[doctype]
	verified = VERIFIED.get(doctype, set())
	tables = C.child_tables(doctype) if C.is_queryable(doctype) else {}
	links = C.link_fields(doctype) if C.is_queryable(doctype) else {}
	measures = C.measure_fields(doctype, date_ok=True) if "aggregate" in spec.operations else []
	creatable = C.is_creatable(doctype)
	return {
		"read": _status(C.is_queryable(doctype), "read" in verified),
		"details": _status("details" in spec.operations, "details" in verified),
		"filter": _status(bool(C.filterable_fields(doctype)), "filter" in verified),
		"aggregate": _status(bool(measures) or "aggregate" in spec.operations, "aggregate" in verified),
		"child": _status(bool(tables), "child" in verified) if tables else NOT_APPLICABLE,
		"link": _status(bool(links), "link" in verified) if links else NOT_APPLICABLE,
		"summary": (IMPLEMENTED_TESTED if "summary" in verified else IMPLEMENTED) if "summary" in spec.operations else NOT_APPLICABLE,
		"create": (PARTIAL if creatable else NOT_IMPLEMENTED),
		"edit": NOT_IMPLEMENTED,
		"reports": NOT_IMPLEMENTED,
		"workflow": NOT_IMPLEMENTED,
		"company": C.company_mode(doctype),
		"tests": ("data-backed: " + ", ".join(sorted(verified))) if verified else "smoke only",
		"create_tested": creatable and doctype in CREATE_TESTED,
		"image": _image_status(doctype),
		"tables": sorted(tables), "links": sorted(links), "filters": len(C.filterable_fields(doctype)), "measures": len(measures),
	}


def _image_status(doctype):
	from ai_erpnext.registry.document_registry import supported_doctypes

	if doctype not in supported_doctypes():
		return NOT_IMPLEMENTED
	return IMPLEMENTED_TESTED if doctype in IMAGE_TESTED else IMPLEMENTED


def overall(caps):
	implemented = [caps[k] for k in QUERY_CAPABILITIES if caps[k] not in (NOT_APPLICABLE, NOT_IMPLEMENTED)]
	if not implemented:
		return NOT_IMPLEMENTED
	return IMPLEMENTED_TESTED if all(s == IMPLEMENTED_TESTED for s in implemented) else IMPLEMENTED


def build():
	"""The inventory of the current site as plain data."""
	rows = frappe.get_all("DocType", filters={"is_virtual": 0}, fields=["name", "module", "istable", "issingle", "custom", "is_submittable"], limit=0)
	apps = installed_apps()
	workflows = set(frappe.get_all("Workflow", filters={"is_active": 1}, pluck="document_type")) if frappe.db.exists("DocType", "Workflow") else set()
	entries, framework_other = [], 0
	for r in sorted(rows, key=lambda x: (x.module, x.name)):
		app = _app_of(r.module)
		if app == "frappe":
			if r.name in FRAMEWORK:
				status, reason = FRAMEWORK[r.name]
				if r.name in P.REGISTRY:
					caps = capabilities_of(r.name)
					entries.append({"doctype": r.name, "module": r.module, "app": app, "group": "Frappe framework", "status": overall(caps), "caps": caps, "reason": reason,
						"submittable": bool(r.is_submittable), "has_company": M.has_company_field(r.name), "workflow": r.name in workflows})
				else:
					entries.append({"doctype": r.name, "module": r.module, "app": app, "group": "Frappe framework", "status": status, "reason": reason})
			else:
				framework_other += 1
			continue
		if r.name in P.REGISTRY:
			caps = capabilities_of(r.name)
			entries.append({"doctype": r.name, "module": r.module, "app": app, "group": _group(r.module), "status": overall(caps), "caps": caps,
				"submittable": bool(r.is_submittable), "has_company": M.has_company_field(r.name), "workflow": r.name in workflows})
		else:
			status, reason = _classify_unregistered(r.name, r)
			entries.append({"doctype": r.name, "module": r.module, "app": app, "group": _group(r.module), "status": status, "reason": reason,
				"is_child": bool(r.istable), "is_single": bool(r.issingle), "submittable": bool(r.is_submittable)})
	return {"site": frappe.local.site, "apps": apps, "hrms_installed": hrms_installed(), "framework_doctypes_not_listed": framework_other,
		"entries": entries, "report_blocker": REPORT_BLOCKER, "page_context": PAGE_CONTEXT}


def summary(data):
	"""{group: {status: count}} over the DocTypes of each module (child tables and single settings are counted apart)."""
	table = {}
	for e in data["entries"]:
		if e.get("is_child") or e.get("is_single"):
			status = "child table / single"
		else:
			status = e["status"]
		table.setdefault(e["group"], {}).setdefault(status, 0)
		table[e["group"]][status] += 1
	return table


SYMBOL = {IMPLEMENTED_TESTED: "T", IMPLEMENTED: "I", PARTIAL: "P", NOT_IMPLEMENTED: "-", NOT_APPLICABLE: "n/a", BLOCKED: "X"}


def render_markdown(data):
	out = ["# ai_erpnext coverage inventory", "",
		f"Generated from the installed apps and DocType metadata of site `{data['site']}` and from the real capability policy. "
		"Do not edit by hand: regenerate with `bench --site <test-site> execute ai_erpnext.capability.inventory.write_inventory`.", "",
		f"Installed apps: {', '.join(data['apps'])}. HRMS installed: **{'yes' if data['hrms_installed'] else 'no'}**"
		+ ("" if data["hrms_installed"] else " (no HR DocTypes beyond ERPNext's Employee exist; Employee exposes organisational fields only)") + ".", "",
		"A DocType that exists in the metadata is **not** available to the chatbot. Only DocTypes listed in `capability/policy.py` are, and only for users who may "
		"read them (role, User Permission, company, field permission level).", "",
		"## Legend", "",
		"| Code | Status |", "| --- | --- |", f"| T | {IMPLEMENTED_TESTED} (a data-backed test exercises it) |",
		f"| I | {IMPLEMENTED}: the execution path exists and is smoke-tested only |", f"| P | {PARTIAL}: guided creation of a deliberately small field set |",
		f"| - | {NOT_IMPLEMENTED} |", "| n/a | Not applicable |", f"| X | {BLOCKED}: sensitive, consequential or unsafe without a separate design |", "",
		"Editing, standard-report access and workflow actions are **not implemented for any DocType** (out of scope; reports are blocked, see below).", "",
		"## Summary by module", "", "| Module | " + " | ".join(["Implemented and tested", "Implemented, not sufficiently tested", "Not implemented", "Blocked", "Not applicable", "child tables / single"]) + " |",
		"| --- | " + " | ".join(["---"] * 6) + " |"]
	for group, counts in sorted(summary(data).items()):
		out.append(f"| {group} | " + " | ".join(str(counts.get(s, 0)) for s in (IMPLEMENTED_TESTED, IMPLEMENTED, NOT_IMPLEMENTED, BLOCKED, NOT_APPLICABLE, "child table / single")) + " |")
	out += ["", "## Implemented DocTypes", "",
		"Columns: R read/list, D details, F filter+sort, A aggregate, C child tables, L linked records, S party summary, Cr creation (guided; Draft or saved-immediately), "
		"Img image to Draft (the document's direction decides the target, see detection/direction.py), Co company policy, W workflow (none), Ed edit, Rp reports.", "",
		"| Module | DocType | R | D | F | A | C | L | S | Cr | Img | Ed | Rp | W | Company policy | Tests |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
	for e in sorted((e for e in data["entries"] if "caps" in e), key=lambda e: (e["group"], e["doctype"])):
		c = e["caps"]
		create = SYMBOL[c["create"]] + ("T" if c["create_tested"] else "") if c["create"] == PARTIAL else "-"
		out.append(f"| {e['group']} | {e['doctype']} | " + " | ".join(SYMBOL[c[k]] for k in ("read", "details", "filter", "aggregate", "child", "link", "summary"))
			+ f" | {create} | {SYMBOL[c['image']]} | - | - | {'configured, not driven' if e['workflow'] else '-'} | {c['company']} | {c['tests']} |")
	out += ["", "## Not implemented, blocked or not applicable", ""]
	groups = {}
	for e in data["entries"]:
		if "caps" not in e and not (e.get("is_child") or e.get("is_single")):
			groups.setdefault(e["group"], []).append(e)
	for group in sorted(groups):
		out += [f"### {group}", "", "| DocType | Status | Reason / priority |", "| --- | --- | --- |"]
		out += [f"| {e['doctype']} | {e['status']} | {e['reason']} |" for e in sorted(groups[group], key=lambda e: (e["status"], e["doctype"]))]
		out.append("")
	out += ["## Standard reports", "", data["report_blocker"], "", "## Page context", ""]
	out += [f"- **{k}:** {v}" for k, v in data["page_context"].items()]
	out += ["", f"Frappe framework DocTypes not listed individually (system plumbing): {data['framework_doctypes_not_listed']}.", ""]
	return "\n".join(out)


def write_inventory(path=None):
	"""Regenerate docs/coverage_inventory.md and .json (run on a development or test site only)."""
	if frappe.conf.get("developer_mode") is None and frappe.conf.get("allow_tests") is None:
		print("Refusing to run: this does not look like a development/test site (set allow_tests or developer_mode).")
		return None
	data = build()
	base = path or os.path.join(frappe.get_app_path("ai_erpnext"), "..", "docs")
	os.makedirs(base, exist_ok=True)
	with open(os.path.join(base, "coverage_inventory.md"), "w") as f:
		f.write(render_markdown(data))
	with open(os.path.join(base, "coverage_inventory.json"), "w") as f:
		json.dump(data, f, indent=1, sort_keys=True, default=str)
	return os.path.abspath(base)
