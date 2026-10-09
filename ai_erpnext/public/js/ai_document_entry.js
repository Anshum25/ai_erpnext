frappe.ui.form.on("AI Document Entry", {
	setup(frm) {
		frappe.call("ai_erpnext.api.mapping.get_supported_doctypes").then((r) => {
			frm._ai_supported = r.message || [];
		});
		frm.set_query("target_doctype", () => ({
			filters: { istable: 0, issingle: 0, name: ["in", frm._ai_supported || ["Sales Invoice"]] },
		}));
	},

	refresh(frm) {
		const doc = frm.doc;
		if (!doc.__islocal && doc.status === "Failed" && !doc.created_document) {
			frm.add_custom_button(__("Retry Extraction"), () =>
				frappe.call({
					method: "ai_erpnext.api.extraction.retry_extraction",
					args: { entry: doc.name },
					freeze: true,
				}).then(() => frm.reload_doc())
			).addClass("btn-primary");
			return;
		}
		if (doc.__islocal || !doc.extracted_data || ["Pending", "Failed"].includes(doc.status)) return;

		if (doc.created_document) {
			frm.add_custom_button(__("Open {0}", [doc.created_document]), () =>
				frappe.set_route("Form", doc.target_doctype, doc.created_document)
			).addClass("btn-primary");
			frm.dashboard.set_headline_alert(
				`${__("Draft created")}: ${frappe.utils.escape_html(doc.target_doctype)} ${frappe.utils.escape_html(doc.created_document)}`, "green");
			return;
		}

		frm.add_custom_button(__("Review & Create Document"), () =>
			ai_erpnext.review.open(doc.name, { on_change: () => frm.reload_doc() })
		).addClass("btn-primary");
		frm.add_custom_button(__("Re-run Mapping"), () =>
			ai_erpnext.review.remap(doc.name).then(() => frm.reload_doc()), __("Actions"));

		let v = {};
		try { v = JSON.parse(doc.validation_results || "{}"); } catch (e) { v = {}; }
		const errors = (v.errors || []).length, warnings = (v.warnings || []).length;
		if (v.document_status) {
			const color = { "Ready To Create": "green", "Ready For Review": "blue", "Needs Review": "orange", "Validation Failed": "red" }[v.document_status] || "gray";
			frm.dashboard.set_headline_alert(
				`${frappe.utils.escape_html(v.document_status.toUpperCase())} — ${errors} ${__("error(s)")}, ${warnings} ${__("warning(s)")}${v.duplicate ? " — " + __("possible duplicate") : ""}`, color);
		}
	},

	target_doctype(frm) {
		// Manual selection (e.g. when detection was not confident): re-map with the chosen type.
		if (frm.doc.__islocal || !frm.doc.extracted_data || frm.doc.created_document) return;
		ai_erpnext.review.remap(frm.doc.name, frm.doc.target_doctype || "").then(() => frm.reload_doc());
	},
});
