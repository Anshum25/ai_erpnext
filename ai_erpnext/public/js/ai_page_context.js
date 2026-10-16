// Page context for the AI Assistant: a small, bounded description of the Desk page the user is on.
// It is only a hint. The server (ai_erpnext/chat/page_context.py) validates every part again and answers only from data the
// user may read. Sent with a chat message, never polled; nothing is stored in the browser.
//
// Sent fields (see LIMITS for the bounds):
//   always:  route                      first three route segments
//   form:    v, page, doctype, name, modified, dirty, changed {field: value}, changed_tables [table], selected_rows {table, rows [row names]}
//   list:    v, page, doctype, filters [[doctype, field, op, value]], sort_by, sort_order, start, page_length, selected [names], selected_count
//   report:  v, page, report, report_type, filters {name: value}
// Never sent: the other fields of a document, child-table contents, password/attachment/long-text fields, report results.

export const LIMITS = { json: 4000, value: 140, changed: 10, tables: 5, filters: 9, selected: 21, rows: 20, reportFilters: 12 };
const SKIP_FIELDS = new Set(["name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "doctype", "parent", "parenttype", "parentfield",
	"amended_from", "naming_series"]);
const SKIP_TYPES = new Set(["Password", "Attach", "Attach Image", "Signature", "Table", "Table MultiSelect", "Text Editor", "HTML Editor",
	"Markdown Editor", "Code", "JSON", "Long Text", "HTML", "Image", "Section Break", "Column Break", "Tab Break", "Button"]);
const MAX_SNAPSHOTS = 5;
const snapshots = new Map();  // "DocType::name" -> {fields, tables}: the saved state the form had when it was last clean

const isScalar = (v) => v === null || ["string", "number", "boolean"].includes(typeof v);
const cut = (v, n = LIMITS.value) => (typeof v === "string" ? v.slice(0, n) : v);
const norm = (v) => (v === null || v === undefined ? "" : String(v));

function formKey(frm) { return `${frm.doctype}::${frm.docname}`; }

function fieldType(frm, fieldname) {
	const f = frm.fields_dict && frm.fields_dict[fieldname];
	return f && f.df ? f.df.fieldtype : null;
}

function scalarFields(frm) {
	const out = {};
	Object.keys(frm.doc || {}).forEach((k) => {
		if (SKIP_FIELDS.has(k) || k.startsWith("_")) return;
		const type = fieldType(frm, k);
		if (!type || SKIP_TYPES.has(type)) return;  // unknown or sensitive/long fields are neither compared nor sent
		if (isScalar(frm.doc[k])) out[k] = frm.doc[k];
	});
	return out;
}

function tableSignatures(frm) {
	const out = {};
	Object.keys(frm.fields_dict || {}).forEach((k) => {
		const f = frm.fields_dict[k];
		if (!f || !f.df || f.df.fieldtype !== "Table") return;
		out[k] = (frm.doc[k] || []).map((row) => Object.keys(row).filter((c) => !c.startsWith("_") && c !== "modified" && c !== "creation" && isScalar(row[c]))
			.map((c) => norm(row[c])).join("\u0001")).join("\u0002");
	});
	return out;
}

function isNew(frm) {
	return !frm || !frm.doc || frm.doc.__islocal || (frm.is_new && frm.is_new()) || String(frm.docname || "").startsWith("new-");
}

function isDirty(frm) { return !!(frm.is_dirty && frm.is_dirty()); }

// Called when a form is (re)drawn: if it is clean, remember its saved values so a later change can be named.
export function snapshotForm(frm) {
	try {
		if (!frm || isNew(frm) || isDirty(frm)) return;
		const key = formKey(frm);
		snapshots.delete(key);
		snapshots.set(key, { fields: scalarFields(frm), tables: tableSignatures(frm) });
		while (snapshots.size > MAX_SNAPSHOTS) snapshots.delete(snapshots.keys().next().value);
	} catch (e) { /* a page we don't understand: no snapshot */ }
}

export function forgetSnapshots() { snapshots.clear(); }

function formContext(frm, route) {
	if (isNew(frm)) return null;  // an unsaved new document has no saved record to talk about
	const ctx = { v: 2, page: "form", route, doctype: frm.doctype, name: frm.docname, modified: norm(frm.doc.modified), dirty: isDirty(frm) };
	if (ctx.dirty) {
		const snap = snapshots.get(formKey(frm));
		const changed = {};
		let count = 0;
		if (snap) {
			const now = scalarFields(frm);
			Object.keys(now).forEach((k) => {
				if (norm(now[k]) !== norm(snap.fields[k]) && count++ < LIMITS.changed) changed[k] = cut(norm(now[k]));
			});
			const sigs = tableSignatures(frm);
			ctx.changed_tables = Object.keys(sigs).filter((t) => sigs[t] !== snap.tables[t]).slice(0, LIMITS.tables);
		}
		ctx.changed = changed;  // empty when the form was already dirty before the assistant saw it: the server says so
	}
	Object.keys(frm.fields_dict || {}).some((k) => {
		const f = frm.fields_dict[k];
		if (!f || !f.df || f.df.fieldtype !== "Table" || !f.grid || !f.grid.get_selected) return false;
		const rows = (f.grid.get_selected() || []).filter((n) => typeof n === "string" && !n.startsWith("new-")).slice(0, LIMITS.rows);
		if (!rows.length) return false;
		ctx.selected_rows = { table: k, rows };
		return true;
	});
	return ctx;
}

function listFilter(f) {
	if (!Array.isArray(f)) return null;
	return f.slice(0, 4).map((v) => (Array.isArray(v) ? v.slice(0, LIMITS.filters).map((x) => cut(isScalar(x) ? x : String(x))) : cut(isScalar(v) ? v : String(v))));
}

function listContext(lv, route) {
	const ctx = { v: 2, page: "list", route, doctype: lv.doctype };
	let filters = null;
	try { filters = lv.get_filters_for_args ? lv.get_filters_for_args() : (lv.filter_area ? lv.filter_area.get() : null); } catch (e) { filters = null; }
	if (Array.isArray(filters)) ctx.filters = filters.slice(0, LIMITS.filters).map(listFilter).filter(Boolean);
	if (lv.sort_by) { ctx.sort_by = String(lv.sort_by); ctx.sort_order = String(lv.sort_order || "desc"); }
	if (Number.isInteger(lv.start)) ctx.start = lv.start;
	if (Number.isInteger(lv.page_length)) ctx.page_length = lv.page_length;
	try {
		const names = lv.get_checked_items ? lv.get_checked_items(true) : [];
		if (Array.isArray(names) && names.length) {
			ctx.selected_count = names.length;
			ctx.selected = names.filter((n) => typeof n === "string").slice(0, LIMITS.selected);
		}
	} catch (e) { /* no selection API on this view */ }
	return ctx;
}

function reportContext(frappe_, route) {
	const qr = frappe_ && frappe_.query_report;
	const ctx = { v: 2, page: "report", route, report: route[1] };
	if (!qr) return ctx;
	try {
		const values = qr.get_filter_values ? qr.get_filter_values() : {};
		const filters = {};
		Object.keys(values || {}).slice(0, LIMITS.reportFilters).forEach((k) => { if (isScalar(values[k]) && norm(values[k]) !== "") filters[k] = cut(norm(values[k]), 60); });
		ctx.filters = filters;
		if (qr.report_doc && qr.report_doc.report_type) ctx.report_type = String(qr.report_doc.report_type).slice(0, 40);
	} catch (e) { /* filters not readable: only the report name is sent */ }
	return ctx;
}

// env: {frappe, cur_frm, cur_list}. Returns the context object, or null when the page can't be described.
export function collectPageContext(env) {
	const frappe_ = env && env.frappe;
	let route = null;
	try { route = frappe_ && frappe_.get_route ? frappe_.get_route() : null; } catch (e) { route = null; }
	if (!Array.isArray(route) || !route.length) return null;
	const short = route.slice(0, 3).map(String);
	const fallback = { route: short };  // the original, route-only context
	let ctx = null;
	try {
		if (short[0] === "Form" && env.cur_frm && env.cur_frm.doctype === short[1]) ctx = formContext(env.cur_frm, short);
		else if (short[0] === "List" && env.cur_list && env.cur_list.doctype === short[1]) ctx = listContext(env.cur_list, short);
		else if (short[0] === "query-report") ctx = reportContext(frappe_, short);
	} catch (e) { ctx = null; }  // a page that doesn't look as expected: route only
	if (!ctx || JSON.stringify(ctx).length > LIMITS.json) return fallback;
	return ctx;
}
