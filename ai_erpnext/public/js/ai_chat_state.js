// Pure helpers of the AI Assistant widget (ai_bot_v2.js): no DOM, no frappe, so they can be checked with Node
// (tests/chat_state_check.mjs). The server decides everything; these only shape what the widget shows and sends.

// The Cancel control of the workflow in progress (payload "workflow" from the server), or null when nothing can be cancelled.
// The action carries the workflow's id: the server ignores a Cancel for a workflow that has already ended.
export function cancelControl(workflow) {
	if (!workflow || typeof workflow.id !== "string" || !workflow.id) return null;
	return {
		text: String(workflow.label || "Task in progress"),
		label: "Cancel",
		ariaLabel: `Cancel: ${workflow.label || "the task in progress"}`,
		action: { command: "cancel", workflow_id: workflow.id },
	};
}

// The buttons of a question, in order: its answers, then its Cancel (question.cancel), in the same choice area.
export function questionChoices(question) {
	const options = Array.isArray(question && question.options) ? question.options : [];
	const choices = options.map((o) => ({
		label: o.label,
		value: o.value,
		secondary: o.style === "secondary",
		cancel: false,
	}));
	const cancel = question && question.cancel;
	if (cancel && cancel.value && !options.some((o) => o.value === cancel.value))
		choices.push({
			label: cancel.label || "Cancel",
			value: cancel.value,
			secondary: true,
			cancel: true,
		});
	return choices;
}

// Responses can arrive out of order (a Cancel sent while another request was still running). State (the active question,
// the workflow, busy) is only taken from the newest request answered so far; an older response may add messages, never state.
export function isStale(seq, appliedSeq) {
	return seq < appliedSeq;
}

// Where a message goes among the ones shown (ids ascending): the id of the first shown message with a larger id, or null
// to append. Late messages of an earlier request are inserted in place instead of being dropped.
export function insertBeforeId(shownIds, id) {
	for (const shown of shownIds) if (shown > id) return shown;
	return null;
}
