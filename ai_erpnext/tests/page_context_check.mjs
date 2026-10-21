// Node check of public/js/ai_page_context.js with stubbed Desk objects (run by tests/test_page_context.py).
import assert from "node:assert/strict";
import {
  collectPageContext,
  snapshotForm,
  forgetSnapshots,
  LIMITS,
} from "../public/js/ai_page_context.js";

const frappe = (route) => ({ get_route: () => route });
const field = (fieldtype, extra = {}) => ({ df: { fieldtype }, ...extra });
function form(doc, { dirty = false, fields = {}, selected = [] } = {}) {
  const fields_dict = {
    grand_total: field("Currency"),
    customer: field("Link"),
    status: field("Select"),
    notes: field("Text Editor"),
    api_pw: field("Password"),
    description: field("Data"),
    items: field("Table", { grid: { get_selected: () => selected } }),
    ...fields,
  };
  return {
    doctype: "Sales Invoice",
    docname: doc.name,
    doc,
    fields_dict,
    is_dirty: () => dirty,
    is_new: () => !!doc.__islocal,
  };
}
const doc = () => ({
  name: "SINV-1",
  modified: "2026-10-16 10:00:00.123456",
  customer: "C1",
  grand_total: 100,
  status: "Draft",
  notes: "long private notes",
  api_pw: "hunter2",
  description: "d",
  owner: "x@y",
  docstatus: 0,
  items: [{ name: "row1", idx: 1, item_code: "A", qty: 1, rate: 100 }],
  _liked_by: "[]",
});

// route only pages and unknown pages
assert.deepEqual(collectPageContext({ frappe: frappe(["Dashboard"]) }), {
  route: ["Dashboard"],
});
assert.equal(
  collectPageContext({
    frappe: {
      get_route: () => {
        throw new Error("x");
      },
    },
  }),
  null
);
assert.equal(collectPageContext({ frappe: frappe([]) }), null);
assert.equal(collectPageContext({}), null);

// saved, clean form
let frm = form(doc());
snapshotForm(frm);
let ctx = collectPageContext({
  frappe: frappe(["Form", "Sales Invoice", "SINV-1"]),
  cur_frm: frm,
});
assert.equal(ctx.page, "form");
assert.equal(ctx.v, 2);
assert.deepEqual(
  [ctx.doctype, ctx.name, ctx.modified, ctx.dirty],
  ["Sales Invoice", "SINV-1", "2026-10-16 10:00:00.123456", false]
);
assert.equal(ctx.changed, undefined);
const text = JSON.stringify(ctx);
for (const secret of ["hunter2", "long private notes", "x@y", "_liked_by"])
  assert.ok(!text.includes(secret), secret);

// a different form open than the route says: route only
assert.deepEqual(
  collectPageContext({
    frappe: frappe(["Form", "Sales Order", "SO-1"]),
    cur_frm: frm,
  }),
  { route: ["Form", "Sales Order", "SO-1"] }
);

// unsaved changes are named, sensitive fields never are
frm.doc.customer = "C2";
frm.doc.grand_total = 250;
frm.doc.api_pw = "newsecret";
frm.doc.notes = "changed private notes";
frm.is_dirty = () => true;
frm.doc.items[0].qty = 3;
ctx = collectPageContext({
  frappe: frappe(["Form", "Sales Invoice", "SINV-1"]),
  cur_frm: frm,
});
assert.equal(ctx.dirty, true);
assert.deepEqual(ctx.changed, { customer: "C2", grand_total: "250" });
assert.deepEqual(ctx.changed_tables, ["items"]);
assert.ok(
  !JSON.stringify(ctx).includes("newsecret") &&
    !JSON.stringify(ctx).includes("changed private notes")
);

// a form that was dirty before the assistant saw it: dirty, but no field names invented
forgetSnapshots();
ctx = collectPageContext({
  frappe: frappe(["Form", "Sales Invoice", "SINV-1"]),
  cur_frm: frm,
});
assert.equal(ctx.dirty, true);
assert.deepEqual(ctx.changed, {});

// values are bounded, and no more than LIMITS.changed fields are sent
forgetSnapshots();
const wide = doc();
const manyFields = {};
for (let i = 0; i < 30; i++) {
  wide["f" + i] = "a";
  manyFields["f" + i] = field("Data");
}
frm = form(wide, { fields: manyFields });
snapshotForm(frm);
for (let i = 0; i < 30; i++) wide["f" + i] = "b".repeat(500);
frm.is_dirty = () => true;
ctx = collectPageContext({
  frappe: frappe(["Form", "Sales Invoice", "SINV-1"]),
  cur_frm: frm,
});
assert.equal(Object.keys(ctx.changed).length, LIMITS.changed);
assert.ok(Object.values(ctx.changed).every((v) => v.length <= LIMITS.value));
assert.ok(JSON.stringify(ctx).length <= LIMITS.json);

// new (unsaved) documents are not described
const fresh = form({ ...doc(), name: "new-sales-invoice-1", __islocal: 1 });
assert.deepEqual(
  collectPageContext({
    frappe: frappe(["Form", "Sales Invoice", "new-sales-invoice-1"]),
    cur_frm: fresh,
  }),
  { route: ["Form", "Sales Invoice", "new-sales-invoice-1"] }
);

// selected child rows (saved rows only)
forgetSnapshots();
frm = form(doc(), { selected: ["row1", "new-sales-invoice-item-abc", 7] });
ctx = collectPageContext({
  frappe: frappe(["Form", "Sales Invoice", "SINV-1"]),
  cur_frm: frm,
});
assert.deepEqual(ctx.selected_rows, { table: "items", rows: ["row1"] });

// list pages: filters, sort, paging, selection
const lv = (names, filters) => ({
  doctype: "Sales Invoice",
  get_filters_for_args: () => filters,
  sort_by: "modified",
  sort_order: "desc",
  start: 0,
  page_length: 20,
  get_checked_items: () => names,
});
ctx = collectPageContext({
  frappe: frappe(["List", "Sales Invoice", "List"]),
  cur_list: lv(
    ["A", "B"],
    [
      ["Sales Invoice", "grand_total", ">", 100],
      ["Sales Invoice", "docstatus", "=", 1],
    ]
  ),
});
assert.equal(ctx.page, "list");
assert.deepEqual(ctx.filters, [
  ["Sales Invoice", "grand_total", ">", 100],
  ["Sales Invoice", "docstatus", "=", 1],
]);
assert.deepEqual(
  [ctx.sort_by, ctx.sort_order, ctx.selected, ctx.selected_count],
  ["modified", "desc", ["A", "B"], 2]
);
const many = Array.from({ length: 50 }, (_, i) => "SINV-" + i);
ctx = collectPageContext({
  frappe: frappe(["List", "Sales Invoice", "List"]),
  cur_list: lv(many, []),
});
assert.equal(ctx.selected_count, 50);
assert.equal(ctx.selected.length, LIMITS.selected);
const filters = Array.from({ length: 30 }, (_, i) => [
  "Sales Invoice",
  "customer",
  "=",
  "C" + i,
]);
ctx = collectPageContext({
  frappe: frappe(["List", "Sales Invoice", "List"]),
  cur_list: lv([], filters),
});
assert.equal(ctx.filters.length, LIMITS.filters);
ctx = collectPageContext({
  frappe: frappe(["List", "Sales Invoice", "List"]),
  cur_list: { doctype: "Sales Invoice" },
});
assert.equal(ctx.filters, undefined); // filters unknown: the server says so rather than guessing
ctx = collectPageContext({
  frappe: frappe(["List", "Sales Invoice", "Report"]),
  cur_list: {
    ...lv([], []),
    get_filters_for_args: () => {
      throw new Error("x");
    },
  },
});
assert.equal(ctx.filters, undefined);
// another list is open than the route says
assert.deepEqual(
  collectPageContext({
    frappe: frappe(["List", "Customer", "List"]),
    cur_list: lv([], []),
  }),
  { route: ["List", "Customer", "List"] }
);

// reports: name and scalar filters only
const report = {
  get_filter_values: () => ({
    company: "Acme",
    report_date: "2026-10-01",
    nested: { a: 1 },
    empty: "",
  }),
  report_doc: { report_type: "Script Report" },
};
ctx = collectPageContext({
  frappe: {
    ...frappe(["query-report", "Accounts Receivable"]),
    query_report: report,
  },
});
assert.deepEqual(
  [ctx.page, ctx.report, ctx.report_type, ctx.filters],
  [
    "report",
    "Accounts Receivable",
    "Script Report",
    { company: "Acme", report_date: "2026-10-01" },
  ]
);
ctx = collectPageContext({
  frappe: {
    ...frappe(["query-report", "Stock Balance"]),
    query_report: {
      get_filter_values: () => {
        throw new Error("x");
      },
    },
  },
});
assert.deepEqual(
  [ctx.page, ctx.report, ctx.filters],
  ["report", "Stock Balance", undefined]
);

// oversized context falls back to the route
const huge = {
  doctype: "Sales Invoice",
  get_filters_for_args: () =>
    Array.from({ length: 9 }, () => [
      "Sales Invoice",
      "customer",
      "in",
      Array.from({ length: 9 }, () => "x".repeat(140)),
    ]),
  get_checked_items: () => Array.from({ length: 21 }, () => "N".repeat(140)),
};
ctx = collectPageContext({
  frappe: frappe(["List", "Sales Invoice", "List"]),
  cur_list: huge,
});
assert.deepEqual(ctx, { route: ["List", "Sales Invoice", "List"] });
console.log("ok");
