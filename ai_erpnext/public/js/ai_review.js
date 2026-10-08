// Shared "Review & Create" dialog for AI Document Entry.
// Used by the AI Assistant bot (ai_bot_v2.js) and the AI Document Entry form.
// Everything that came from the AI is escaped before rendering (untrusted input).
(function () {
	window.ai_erpnext = window.ai_erpnext || {};
	const API = "ai_erpnext.api.";
	const esc = (v) => frappe.utils.escape_html(v === null || v === undefined ? "" : String(v));
	const RESOLVED = ["MATCHED", "DEFAULTED"];
	const EXACT_TYPES = ["exact", "exact_code", "manual", "date:iso"];

	const LINK_FILTERS = {
		Customer: () => ({ disabled: 0 }),
		Item: () => ({ disabled: 0, has_variants: 0, is_sales_item: 1 }),
		UOM: () => ({ enabled: 1 }),
		Currency: () => ({ enabled: 1 }),
		Company: () => ({}),
		Warehouse: (review) => {
			const f = { is_group: 0, disabled: 0 };
			if (review.mapped && review.mapped.company) f.company = review.mapped.company;
			return f;
		},
	};

	function statusBadge(res) {
		if (!res) return "";
		const s = res.status;
		let color = "gray", label = s, icon = "";
		if (s === "MATCHED" && EXACT_TYPES.includes(res.match_type)) {
			color = "green"; icon = "✓ ";
			label = res.match_type === "manual" ? "Selected" : (res.match_type || "").startsWith("date:") ? "Valid date" : "Exact match";
		} else if (s === "MATCHED") {
			color = "blue"; icon = "✓ "; label = (res.match_type || "").replace(/_/g, " ");
		} else if (s === "DEFAULTED") {
			color = "orange"; icon = "⚠ "; label = "Defaulted";
		} else if (s === "AMBIGUOUS") {
			color = "orange"; icon = "⚠ "; label = res.match_type === "fuzzy_suggestion" ? "Needs confirmation" : "Ambiguous";
		} else if (s === "NOT_FOUND" || s === "INVALID") {
			color = "red"; icon = "✗ "; label = s === "INVALID" ? "Invalid" : "Not found";
		} else if (s === "NOT_PROVIDED") {
			color = "gray"; icon = "⚠ "; label = "Not detected";
		}
		const conf = res.confidence ? ` ${Math.round(res.confidence * 100)}%` : "";
		return `<span class="indicator-pill ${color}" style="white-space:nowrap">${icon}${esc(label)}${esc(conf)}</span>`;
	}

	function needsAttention(res) {
		return !res || !RESOLVED.includes(res.status) || res.requires_review || res.status === "DEFAULTED" ||
			!EXACT_TYPES.includes(res.match_type);
	}

	function correctableKeys(review) {
		const keys = [];
		const results = review.results || {};
		["customer", "company", "currency", "warehouse"].forEach((k) => results[k] && keys.push(k));
		Object.keys(results).filter((k) => /^item_\d+$/.test(k)).sort((a, b) => parseInt(a.slice(5)) - parseInt(b.slice(5)))
			.forEach((k) => { keys.push(k); results[k + "_uom"] && keys.push(k + "_uom"); });
		return keys;
	}

	function labelFor(key) {
		const m = key.match(/^item_(\d+)(_uom)?$/);
		if (m) return `Row ${m[1]} ${m[2] ? "UOM" : "Item"}`;
		return frappe.model.unscrub(key);
	}

	function candidateLinks(key, res) {
		if (!res || !res.candidates || !res.candidates.length) return "";
		const links = res.candidates.map((c) =>
			`<a href="#" class="ai-cand" data-key="${esc(key)}" data-value="${esc(c.name)}">${esc(c.display_name)}${c.display_name !== c.name ? " (" + esc(c.name) + ")" : ""}${c.score ? " " + Math.round(c.score * 100) + "%" : ""}</a>`
		).join(", ");
		return `Candidates: ${links}`;
	}

	function issueList(title, issues, color) {
		if (!issues || !issues.length) return "";
		return `<div style="margin-top:10px"><b style="color:var(--${color}-600, ${color})">${esc(title)} (${issues.length})</b>
			<ul style="margin:4px 0 0 18px;padding:0">${issues.map((i) => `<li>${esc(i.message)}</li>`).join("")}</ul></div>`;
	}

	function summaryHtml(review) {
		const r = review.results || {}, v = review.validation || {}, m = review.mapped || {}, src = review.source || {};
		const dt = r.document_type || {};
		const status = review.status || v.document_status;
		const statusColor = { "Ready To Create": "green", "Ready For Review": "blue", "Needs Review": "orange", "Validation Failed": "red", "Created": "green" }[status] || "gray";

		let html = `<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:8px">
			<div><span class="text-muted">Document Type:</span> <b>${esc(review.target_doctype || dt.input || "Unknown")}</b>
				${dt.confidence ? `<span class="text-muted">(${Math.round(dt.confidence * 100)}% ${esc(dt.match_type || "")})</span>` : ""}</div>
			<span class="indicator-pill ${statusColor}">${esc(status ? status.toUpperCase() : "")}</span>
			${src.invoice_number ? `<div><span class="text-muted">Invoice No:</span> ${esc(src.invoice_number)}</div>` : ""}
		</div>`;

		const row = (label, key) => {
			const res = r[key];
			if (!res) return "";
			const input = typeof res.input === "object" && res.input !== null ? "" : res.input;
			return `<tr><td class="text-muted">${esc(label)}</td><td>${esc(input || "—")}</td>
				<td><b>${esc(res.display_name || res.name || "—")}</b>${res.name && res.display_name && res.name !== res.display_name ? ` <span class="text-muted">${esc(res.name)}</span>` : ""}</td>
				<td>${statusBadge(res)}${res.message && needsAttention(res) ? `<div class="text-muted small">${esc(res.message)}</div>` : ""}</td></tr>`;
		};
		html += `<div style="overflow-x:auto"><table class="table table-bordered table-sm" style="font-size:12.5px;margin-bottom:6px">
			<thead><tr><th>Field</th><th>On document</th><th>ERPNext</th><th>Status</th></tr></thead><tbody>
			${row("Customer", "customer")}${row("Company", "company")}${row("Currency", "currency")}
			${row("Posting Date", "posting_date")}${row("Due Date", "due_date")}${row("Warehouse", "warehouse")}
			</tbody></table></div>`;

		const items = m.items || [];
		if (items.length) {
			html += `<div style="overflow-x:auto"><table class="table table-bordered table-sm" style="font-size:12.5px">
				<thead><tr><th>#</th><th>Item on document</th><th>ERPNext Item</th><th>UOM</th><th class="text-right">Qty × Rate</th><th class="text-right">Amount</th></tr></thead><tbody>`;
			items.forEach((it, i) => {
				const ir = r[`item_${i + 1}`] || {}, ur = r[`item_${i + 1}_uom`] || {};
				html += `<tr><td>${i + 1}</td>
					<td>${esc(ir.input || "")}${it.description && it.description !== ir.input ? `<div class="text-muted small">${esc(it.description)}</div>` : ""}</td>
					<td><b>${esc(it.item_code || "—")}</b><div>${statusBadge(ir)}</div></td>
					<td>${esc(ur.input || "")}${ur.input ? " → " : ""}<b>${esc(it.uom || "—")}</b><div>${statusBadge(ur)}</div></td>
					<td class="text-right">${esc(it.qty)} × ${esc(it.rate)}</td><td class="text-right">${esc(it.amount)}</td></tr>`;
			});
			html += `</tbody></table></div>`;
		}

		if (src.subtotal !== undefined || src.tax_amount !== undefined || src.total_amount !== undefined) {
			html += `<div class="text-muted small">Document totals — Subtotal: ${esc(src.subtotal ?? "—")}, Tax: ${esc(src.tax_amount ?? "—")}${src.tax_rate !== undefined && src.tax_rate !== null ? " (" + esc(src.tax_rate) + ")" : ""}, Total: ${esc(src.total_amount ?? "—")}</div>`;
		}
		if (v.duplicates && v.duplicates.length) {
			html += `<div style="margin-top:10px;padding:8px;border:1px solid var(--red-300,#fca5a5);border-radius:6px"><b>⚠ Possible duplicate</b>
				<ul style="margin:4px 0 0 18px;padding:0">${v.duplicates.map((d) => `<li><a href="/app/sales-invoice/${encodeURIComponent(d.name)}" target="_blank">${esc(d.name)}</a> — ${esc(d.reason)}</li>`).join("")}</ul></div>`;
		}
		html += issueList(__("Errors"), (v.errors || []).filter((e) => e.category !== "duplicate"), "red");
		html += issueList(__("Warnings"), v.warnings, "orange");
		return html;
	}

	function buildFields(review) {
		const results = review.results || {};
		const fields = [{ fieldtype: "HTML", fieldname: "summary" }];
		const unsupported = !results || !Object.keys(results).some((k) => k !== "document_type");
		if (unsupported) return fields;

		fields.push({ fieldtype: "Section Break", label: __("Review / Correct Mappings"), collapsible: 0 });
		correctableKeys(review).forEach((key) => {
			const res = results[key];
			if (key !== "warehouse" && !needsAttention(res)) return;
			const getFilters = LINK_FILTERS[res.doctype] || (() => ({}));
			fields.push({
				fieldtype: "Link", fieldname: key, label: labelFor(key), options: res.doctype,
				default: RESOLVED.includes(res.status) ? res.name : "",
				get_query: () => ({ filters: getFilters(review) }),
				description: candidateLinks(key, res) || (res.message ? esc(res.message) : ""),
			});
		});
		const errorFields = ((review.validation || {}).errors || []).map((e) => e.field);
		["posting_date", "due_date"].forEach((key) => {
			const res = results[key];
			const dateError = errorFields.includes("posting_date") || errorFields.includes("due_date");
			if (res && (res.status === "INVALID" || res.status === "DEFAULTED" || dateError)) {
				fields.push({ fieldtype: "Date", fieldname: key, label: labelFor(key), default: res.name || "", description: esc(res.message || "") });
			}
		});

		const v = review.validation || {};
		if ((v.needs_acknowledgement || {}).duplicate) {
			fields.push({ fieldtype: "Check", fieldname: "acknowledge_duplicate", label: __("I confirm this is NOT a duplicate invoice") });
		}
		if ((v.needs_acknowledgement || {}).data_errors) {
			fields.push({ fieldtype: "Check", fieldname: "acknowledge_errors", label: __("I reviewed the validation errors; create a Draft that I will correct in ERPNext") });
		}
		return fields;
	}

	function collectCorrections(dialog, review) {
		const values = dialog.get_values(true) || {};
		const results = review.results || {};
		const corrections = {};
		Object.keys(values).forEach((key) => {
			if (key.startsWith("acknowledge_") || !results[key]) return;
			const current = RESOLVED.includes(results[key].status) ? results[key].name : "";
			if ((values[key] || "") !== (current || "")) corrections[key] = values[key] || "";
		});
		return corrections;
	}

	function openLegacyForm(review) {
		const data = review.mapped || {};
		if (!data.doctype) return frappe.msgprint(__("Select a Target DocType first."));
		frappe.route_options = data;
		frappe.set_route("Form", data.doctype, "new-" + data.doctype.toLowerCase().replace(/ /g, "-"));
	}

	function render(review, opts) {
		const results = review.results || {};
		const supported = Object.keys(results).some((k) => k !== "document_type");
		const dialog = new frappe.ui.Dialog({
			title: __("Review & Create {0}", [review.target_doctype || __("Document")]),
			size: "extra-large",
			fields: buildFields(review),
		});
		dialog.fields_dict.summary.$wrapper.html(summaryHtml(review));

		const done = (r) => { opts.on_change && opts.on_change(r); };

		if (review.created_document) {
			dialog.set_primary_action(__("Open {0}", [review.created_document]), () => {
				dialog.hide();
				frappe.set_route("Form", review.target_doctype, review.created_document);
			});
		} else if (supported) {
			dialog.set_primary_action(__("Confirm & Create Document"), () => {
				const values = dialog.get_values(true) || {};
				frappe.call({
					method: API + "creation.create_document",
					args: {
						entry: review.name,
						corrections: collectCorrections(dialog, review),
						acknowledge_errors: values.acknowledge_errors ? 1 : 0,
						acknowledge_duplicate: values.acknowledge_duplicate ? 1 : 0,
					},
					freeze: true,
					freeze_message: __("Validating and creating draft..."),
					callback: (r) => {
						const res = r.message || {};
						done(res);
						if (res.created) {
							dialog.hide();
							frappe.show_alert({ message: __("{0} {1} created as Draft", [res.doctype, res.name]), indicator: "green" });
							frappe.set_route("Form", res.doctype, res.name);
						} else {
							dialog.hide();
							frappe.show_alert({ message: res.message, indicator: "orange" }, 7);
							render(res.review, opts);
						}
					},
				});
			});
			dialog.set_secondary_action_label(__("Apply Corrections"));
			dialog.set_secondary_action(() => {
				frappe.call({
					method: API + "mapping.apply_corrections",
					args: { entry: review.name, corrections: collectCorrections(dialog, review) },
					freeze: true,
					callback: (r) => { dialog.hide(); done(r.message); render(r.message, opts); },
				});
			});
		} else {
			if ((review.validation || {}).legacy && review.mapped && review.mapped.doctype) {
				dialog.set_primary_action(__("Open Prefilled Form"), () => { dialog.hide(); openLegacyForm(review); });
			} else {
				dialog.set_primary_action(__("Close"), () => dialog.hide());
			}
		}

		dialog.$wrapper.on("click", "a.ai-cand", function (e) {
			e.preventDefault();
			dialog.set_value($(this).attr("data-key"), $(this).attr("data-value"));
		});
		dialog.show();
		return dialog;
	}

	ai_erpnext.review = {
		open(entry, opts = {}) {
			// Always re-map on open (unless already created) so the review reflects the
			// current ERPNext masters and invoices, not a stale snapshot.
			frappe.call({
				method: API + "mapping.remap",
				args: { entry },
				freeze: true,
				freeze_message: __("Mapping and validating..."),
				callback: (r) => { opts.on_change && opts.on_change(r.message); render(r.message, opts); },
			});
		},
		remap(entry, target_doctype) {
			return frappe.call({ method: API + "mapping.remap", args: { entry, target_doctype }, freeze: true });
		},
		status_badge: statusBadge,
		escape: esc,
	};
})();
