// AI Assistant: conversational ERP agent (chat UI).
// All decisions and validation happen on the server (ai_erpnext.api.chat); this file only
// renders server messages and sends user text, uploaded image URLs and button choices.
// Every server/AI-provided string is escaped before rendering.
$(document).ready(function () {
	if (!window.frappe || !frappe.boot || !frappe.boot.user || frappe.boot.user.name === "Guest") return;
	if (document.getElementById("ai-agent")) return;

	const API = "ai_erpnext.api.chat.";
	const MAX_BYTES = 10 * 1024 * 1024;
	const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"];
	const POLL_MS = 2500;
	const POLL_LIMIT_MS = 10 * 60 * 1000;
	const esc = (v) => frappe.utils.escape_html(v === null || v === undefined ? "" : String(v));
	const lines = (v) => esc(v).replace(/\n/g, "<br>");
	const uid = () => Math.random().toString(36).slice(2) + Date.now().toString(36);
	const store = {
		get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
		set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
	};

	const ICON = {
		chat: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
		clip: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/></svg>',
		send: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>',
		plus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
		expand: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>',
		close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg>',
	};

	const css = `
	#ai-agent { --ai-accent: var(--primary, #171717); position: fixed; z-index: 1050; right: 24px; bottom: 24px; font-family: var(--font-stack, inherit); }
	#ai-agent svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
	#ai-agent button { font: inherit; }
	#ai-agent :focus-visible { outline: 2px solid var(--ai-accent); outline-offset: 2px; }
	.ai-launcher { width: 56px; height: 56px; border-radius: 50%; border: none; background: var(--gray-900, #171717); color: #fff; box-shadow: 0 10px 25px rgba(0,0,0,.2); display: grid; place-items: center; cursor: pointer; transition: transform .15s; }
	.ai-launcher:hover { transform: scale(1.05); }
	.ai-launcher svg { width: 24px; height: 24px; }
	.ai-panel { position: absolute; right: 0; bottom: 72px; width: min(400px, calc(100vw - 32px)); height: min(640px, calc(100vh - 120px)); background: var(--card-bg, #fff); color: var(--text-color, #1f272e); border: 1px solid var(--border-color, #e5e7eb); border-radius: 16px; box-shadow: 0 20px 50px rgba(0,0,0,.18); display: flex; flex-direction: column; overflow: hidden; }
	.ai-panel[hidden] { display: none; }
	.ai-panel.ai-expanded { width: min(780px, calc(100vw - 48px)); height: calc(100vh - 120px); }
	.ai-head { flex-shrink: 0; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 14px; border-bottom: 1px solid var(--border-color, #e5e7eb); background: var(--card-bg, #fff); }
	.ai-head-id { display: flex; align-items: center; gap: 10px; min-width: 0; }
	.ai-avatar { width: 32px; height: 32px; border-radius: 10px; background: var(--gray-900, #171717); color: #fff; display: grid; place-items: center; font-weight: 600; font-size: 12px; flex-shrink: 0; }
	.ai-title { font-weight: 600; font-size: 14px; line-height: 1.2; }
	.ai-sub { font-size: 12px; color: var(--text-muted, #6b7280); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.ai-head-actions { display: flex; gap: 2px; }
	.ai-icon-btn { width: 32px; height: 32px; border: none; background: transparent; color: var(--text-muted, #6b7280); border-radius: 8px; display: grid; place-items: center; cursor: pointer; }
	.ai-icon-btn:hover { background: var(--fg-hover-color, #f3f4f6); color: var(--text-color, #111); }
	.ai-icon-btn:disabled { opacity: .45; cursor: not-allowed; }
	.ai-log { flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 16px 14px; display: flex; flex-direction: column; gap: 12px; background: var(--subtle-fg, #f9fafb); }
	.ai-row { display: flex; flex-direction: column; gap: 6px; max-width: 88%; flex-shrink: 0; }
	.ai-row.ai-user { align-self: flex-end; align-items: flex-end; }
	.ai-row.ai-assistant { align-self: flex-start; }
	.ai-bubble { padding: 9px 12px; border-radius: 14px; font-size: 13.5px; line-height: 1.45; word-wrap: break-word; overflow-wrap: anywhere; }
	.ai-user .ai-bubble { background: var(--gray-900, #171717); color: #fff; border-bottom-right-radius: 4px; }
	.ai-assistant .ai-bubble { background: var(--card-bg, #fff); border: 1px solid var(--border-color, #e5e7eb); border-bottom-left-radius: 4px; }
	.ai-thumb { max-width: 180px; max-height: 140px; border-radius: 10px; border: 1px solid var(--border-color, #e5e7eb); display: block; object-fit: cover; }
	.ai-card { background: var(--card-bg, #fff); border: 1px solid var(--border-color, #e5e7eb); border-radius: 12px; padding: 10px 12px; font-size: 12.5px; width: 100%; }
	.ai-card-title { font-weight: 600; font-size: 13px; margin-bottom: 6px; }
	.ai-kv { display: grid; grid-template-columns: minmax(90px, 38%) 1fr; gap: 4px 10px; }
	.ai-kv dt { color: var(--text-muted, #6b7280); font-weight: 500; }
	.ai-kv dd { margin: 0; overflow-wrap: anywhere; }
	.ai-note { display: block; color: var(--text-muted, #6b7280); font-size: 11.5px; }
	.ai-pill { display: inline-block; font-size: 11px; font-weight: 600; border-radius: 999px; padding: 1px 7px; margin-left: 4px; }
	.ai-pill.ok { background: var(--green-100, #dcfce7); color: var(--green-700, #15803d); }
	.ai-pill.warn { background: var(--orange-100, #ffedd5); color: var(--orange-700, #c2410c); }
	.ai-pill.error { background: var(--red-100, #fee2e2); color: var(--red-700, #b91c1c); }
	.ai-items { margin-top: 8px; overflow-x: auto; }
	.ai-items table { width: 100%; border-collapse: collapse; font-size: 12px; }
	.ai-items th, .ai-items td { text-align: left; padding: 4px 6px; border-top: 1px solid var(--border-color, #e5e7eb); vertical-align: top; }
	.ai-items th { color: var(--text-muted, #6b7280); font-weight: 500; }
	.ai-items td.num, .ai-items th.num { text-align: right; white-space: nowrap; }
	.ai-notes { margin: 8px 0 0; padding-left: 16px; color: var(--text-muted, #6b7280); }
	.ai-options { display: flex; flex-wrap: wrap; gap: 6px; }
	.ai-opt { border: 1px solid var(--gray-900, #171717); background: var(--gray-900, #171717); color: #fff; border-radius: 999px; padding: 5px 12px; font-size: 12.5px; cursor: pointer; text-align: left; }
	.ai-opt.secondary { background: var(--card-bg, #fff); color: var(--text-color, #111); border-color: var(--border-color, #d1d5db); }
	.ai-opt:disabled { opacity: .45; cursor: not-allowed; }
	.ai-date { display: flex; gap: 6px; align-items: center; }
	.ai-date input { border: 1px solid var(--border-color, #d1d5db); border-radius: 8px; padding: 4px 8px; background: var(--control-bg, #fff); color: inherit; }
	.ai-primary { border: none; background: var(--gray-900, #171717); color: #fff; border-radius: 8px; padding: 7px 12px; font-weight: 600; font-size: 12.5px; cursor: pointer; margin-top: 8px; }
	.ai-system { align-self: center; font-size: 12px; color: var(--text-muted, #6b7280); display: flex; gap: 8px; align-items: center; }
	.ai-system.error { color: var(--red-700, #b91c1c); }
	.ai-typing { display: inline-flex; gap: 6px; align-items: center; font-size: 12.5px; color: var(--text-muted, #6b7280); }
	.ai-dots { display: inline-flex; gap: 3px; }
	.ai-dots i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: ai-blink 1.4s infinite both; }
	.ai-dots i:nth-child(2) { animation-delay: .2s; } .ai-dots i:nth-child(3) { animation-delay: .4s; }
	@keyframes ai-blink { 0%, 80%, 100% { opacity: .2; } 40% { opacity: 1; } }
	@media (prefers-reduced-motion: reduce) { .ai-dots i { animation: none; opacity: .6; } .ai-launcher { transition: none; } }
	.ai-composer { flex-shrink: 0; border-top: 1px solid var(--border-color, #e5e7eb); padding: 10px 12px 8px; background: var(--card-bg, #fff); }
	.ai-attach { display: flex; align-items: center; gap: 10px; border: 1px solid var(--border-color, #e5e7eb); border-radius: 10px; padding: 6px 8px; margin-bottom: 8px; font-size: 12px; }
	.ai-attach[hidden] { display: none; }
	.ai-attach img { width: 40px; height: 40px; border-radius: 6px; object-fit: cover; }
	.ai-attach-info { flex: 1; min-width: 0; }
	.ai-attach-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 500; }
	.ai-progress { height: 4px; border-radius: 2px; background: var(--border-color, #e5e7eb); margin-top: 4px; overflow: hidden; }
	.ai-progress > div { height: 100%; width: 0; background: var(--gray-900, #171717); transition: width .2s; }
	.ai-attach-err { color: var(--red-700, #b91c1c); }
	.ai-input-row { display: flex; align-items: flex-end; gap: 6px; }
	.ai-input-row textarea { flex: 1; resize: none; border: 1px solid var(--border-color, #d1d5db); border-radius: 12px; padding: 8px 10px; font-size: 13.5px; line-height: 1.4; max-height: 140px; min-height: 38px; background: var(--control-bg, #fff); color: inherit; }
	.ai-send { width: 38px; height: 38px; border-radius: 10px; border: none; background: var(--gray-900, #171717); color: #fff; display: grid; place-items: center; cursor: pointer; flex-shrink: 0; }
	.ai-send:disabled { opacity: .4; cursor: not-allowed; }
	.ai-hint { font-size: 11px; color: var(--text-muted, #6b7280); margin-top: 4px; }
	.ai-source { font-size: 10.5px; font-weight: 600; letter-spacing: .02em; text-transform: uppercase; color: var(--text-muted, #6b7280); }
	.ai-source.erpnext { color: var(--blue-600, #2563eb); }
	.ai-link { color: var(--blue-600, #2563eb); background: none; border: none; padding: 0; cursor: pointer; text-decoration: underline; font: inherit; text-align: left; }
	.ai-table-note { margin-top: 6px; }
	.ai-card-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
	.ai-card-actions .ai-primary { margin-top: 0; }
	.ai-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
	@media (max-width: 640px) {
		#ai-agent { right: 16px; bottom: 16px; }
		.ai-panel, .ai-panel.ai-expanded { position: fixed; inset: 0; width: 100vw; height: 100dvh; border-radius: 0; border: none;
			padding-top: env(safe-area-inset-top, 0px); padding-bottom: env(safe-area-inset-bottom, 0px); }
		.ai-row { max-width: 94%; }
		.ai-expand-btn { display: none !important; }
	}`;

	$("head").append(`<style id="ai-agent-style">${css}</style>`);
	$("body").append(`
	<div id="ai-agent">
		<section id="ai-chat-panel" class="ai-panel" role="dialog" aria-label="AI Assistant" hidden>
			<header class="ai-head">
				<div class="ai-head-id"><span class="ai-avatar" aria-hidden="true">AI</span>
					<div style="min-width:0"><div class="ai-title">AI Assistant</div><div class="ai-sub">Chat · ERPNext lookups · draft invoices</div></div></div>
				<div class="ai-head-actions">
					<button type="button" class="ai-icon-btn" data-act="new" aria-label="Start a new conversation" title="New conversation">${ICON.plus}</button>
					<button type="button" class="ai-icon-btn ai-expand-btn" data-act="expand" aria-label="Expand chat" aria-pressed="false" title="Expand">${ICON.expand}</button>
					<button type="button" class="ai-icon-btn" data-act="close" aria-label="Close AI Assistant" title="Close">${ICON.close}</button>
				</div>
			</header>
			<div class="ai-log" id="ai-chat-body" role="log" aria-live="polite" aria-relevant="additions" aria-label="Conversation" tabindex="0"></div>
			<form class="ai-composer" novalidate>
				<div class="ai-attach" hidden>
					<img alt="" />
					<div class="ai-attach-info"><div class="ai-attach-name"></div><div class="ai-attach-state ai-note"></div>
						<div class="ai-progress" aria-hidden="true"><div></div></div></div>
					<button type="button" class="ai-icon-btn" data-act="retry-upload" aria-label="Retry upload" hidden>↻</button>
					<button type="button" class="ai-icon-btn" data-act="remove" aria-label="Remove attachment">${ICON.close}</button>
				</div>
				<div class="ai-input-row">
					<button type="button" class="ai-icon-btn" data-act="attach" aria-label="Attach invoice image (JPG, PNG or WEBP)" title="Attach image">${ICON.clip}</button>
					<input type="file" class="ai-file" accept="image/png,image/jpeg,image/webp" hidden />
					<label class="ai-sr" for="ai-input">Message</label>
					<textarea id="ai-input" rows="1" placeholder="Ask about your ERPNext data, or attach an invoice image…"></textarea>
					<button type="submit" class="ai-send" aria-label="Send message">${ICON.send}</button>
				</div>
				<div class="ai-hint">Enter to send · Shift+Enter for a new line · Drafts are never submitted automatically</div>
			</form>
		</section>
		<button type="button" id="ai-bot-btn" class="ai-launcher" aria-label="Open AI Assistant" aria-expanded="false" aria-controls="ai-chat-panel">${ICON.chat}</button>
	</div>`);

	const $root = $("#ai-agent"), $panel = $("#ai-chat-panel"), $log = $("#ai-chat-body"), $btn = $("#ai-bot-btn");
	const $form = $root.find(".ai-composer"), $input = $("#ai-input"), $send = $root.find(".ai-send"), $file = $root.find(".ai-file");
	const $attach = $root.find(".ai-attach");

	const S = { session: null, lastId: 0, pendingId: null, busy: false, sending: false, loaded: false, pollTimer: null,
		pollStarted: 0, attachment: null, lastPayload: null };

	// ---------------------------------------------------------------- panel
	function openPanel() {
		$panel.prop("hidden", false);
		$btn.attr("aria-expanded", "true");
		if (store.get("ai_agent_expanded") === "1") setExpanded(true);
		if (!S.loaded) loadSession(); else scrollToEnd();
		setTimeout(() => $input.trigger("focus"), 50);
	}
	function closePanel() {
		$panel.prop("hidden", true);
		$btn.attr("aria-expanded", "false").trigger("focus");
	}
	function setExpanded(on) {
		$panel.toggleClass("ai-expanded", on);
		$root.find('[data-act="expand"]').attr("aria-pressed", on ? "true" : "false").attr("aria-label", on ? "Shrink chat" : "Expand chat");
		store.set("ai_agent_expanded", on ? "1" : "0");
	}
	$btn.on("click", () => ($panel.prop("hidden") ? openPanel() : closePanel()));
	$panel.on("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); closePanel(); } });
	$root.on("click", '[data-act="close"]', closePanel);
	$root.on("click", '[data-act="expand"]', () => setExpanded(!$panel.hasClass("ai-expanded")));
	$root.on("click", '[data-act="new"]', () => {
		if (S.sending) return;
		call("new_conversation", {}, (r) => { resetLog(); apply(r); $input.trigger("focus"); }, null);
	});

	// ---------------------------------------------------------------- server calls
	function call(method, args, ok, retryPayload) {
		S.sending = true; updateComposer(); showTyping(true);
		frappe.call({
			method: API + method, args, type: "POST",
			callback: (r) => { S.sending = false; showTyping(false); updateComposer(); if (r.message) ok(r.message); },
			error: () => {
				S.sending = false; showTyping(false); updateComposer();
				systemNote("The request didn't go through.", retryPayload ? () => send(retryPayload) : null);
			},
		});
	}
	function loadSession() {
		call("get_session", { session: S.session }, (r) => { S.loaded = true; resetLog(); apply(r); }, null);
	}
	function send(payload) {
		S.lastPayload = payload;
		call("send_message", Object.assign({ session: S.session }, payload), apply, payload);
	}
	function apply(r) {
		if (S.session && r.session !== S.session) resetLog();
		S.session = r.session;
		(r.messages || []).forEach(renderMessage);
		S.pendingId = r.pending_id;
		refreshQuestions();
		setBusy(r.busy);
		scrollToEnd();
	}
	function setBusy(busy) {
		S.busy = !!busy;
		if (S.busy && !S.pollTimer) { S.pollStarted = Date.now(); schedulePoll(); }
		showTyping(S.sending || S.busy);
	}
	function schedulePoll() {
		S.pollTimer = setTimeout(() => {
			S.pollTimer = null;
			if (!S.busy) return;
			if (Date.now() - S.pollStarted > POLL_LIMIT_MS) {
				systemNote("Still working on it. Reopen the assistant later to see the result.");
				return;
			}
			frappe.call({ method: API + "poll", type: "POST", args: { session: S.session, since: S.lastId },
				callback: (r) => { if (r.message) apply(r.message); if (S.busy && !S.pollTimer) schedulePoll(); },
				error: () => schedulePoll() });
		}, POLL_MS);
	}

	// ---------------------------------------------------------------- rendering
	function resetLog() { $log.empty(); S.lastId = 0; S.pendingId = null; }
	function scrollToEnd() {
		const el = $log[0];
		el.scrollTop = el.scrollHeight;
		requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
	}
	function showTyping(on) {
		$log.find(".ai-typing-row").remove();
		if (!on) return;
		const label = S.busy ? "Reading the invoice" : "Working";
		$log.append(`<div class="ai-row ai-assistant ai-typing-row"><div class="ai-bubble ai-typing" role="status">
			<span class="ai-dots" aria-hidden="true"><i></i><i></i><i></i></span><span>${label}…</span></div></div>`);
		scrollToEnd();
	}
	function systemNote(text, retry) {
		const $n = $(`<div class="ai-system error" role="alert"><span>${esc(text)}</span></div>`);
		if (retry) $n.append($('<button type="button" class="ai-opt secondary">Retry</button>').on("click", function () { $n.remove(); retry(); }));
		$log.append($n); scrollToEnd();
	}
	function renderMessage(m) {
		if (m.id <= S.lastId) return;
		S.lastId = m.id;
		const $row = $(`<div class="ai-row ai-${m.role === "user" ? "user" : "assistant"}" data-mid="${m.id}"></div>`);
		if (m.attachment && m.attachment.file_url) {
			$row.append(`<img class="ai-thumb" src="${esc(m.attachment.file_url)}" alt="Attached invoice image ${esc(m.attachment.file_name)}" loading="lazy">`);
		}
		if (m.source === "erpnext") $row.append('<span class="ai-source erpnext">From your ERPNext data</span>');
		if (m.source === "general") $row.append('<span class="ai-source">General answer · not from your ERPNext data</span>');
		if (m.text) $row.append(`<div class="ai-bubble">${lines(m.text)}</div>`);
		if (m.card) $row.append(renderCard(m.card, m.id));
		if (m.suggestions && m.suggestions.length) $row.append(renderSuggestions(m.suggestions));
		if (m.question) $row.append(renderQuestion(m.question, m.links));
		else if (m.links) $row.append(renderLinks(m.links));
		$log.append($row);
	}
	function pill(status) {
		const map = { ok: ["✓", "Matched"], warn: ["!", "Check"], error: ["✗", "Needs input"] };
		const [icon, label] = map[status] || ["", ""];
		return status ? `<span class="ai-pill ${status}"><span aria-hidden="true">${icon}</span> ${label}</span>` : "";
	}
	function renderTable(card, mid) {
		const $c = $(`<div class="ai-card" role="group" aria-label="${esc(card.title || "Results")}"></div>`);
		if (card.title) $c.append(`<div class="ai-card-title">${esc(card.title)}</div>`);
		const cols = card.columns || [];
		const $t = $('<table></table>');
		$t.append(`<thead><tr><th scope="col">#</th>${cols.map((c) => `<th scope="col" class="${c.type === "currency" ? "num" : ""}">${esc(c.label)}</th>`).join("")}</tr></thead>`);
		const $b = $("<tbody></tbody>");
		(card.rows || []).forEach((r, i) => {
			const $tr = $(`<tr><td>${esc(r._n || i + 1)}</td></tr>`);
			cols.forEach((c) => {
				const v = r[c.key];
				const $td = $(`<td class="${c.type === "currency" ? "num" : ""}"></td>`);
				if (c.type === "link" && card.doctype && r._name) {
					$td.append($(`<button type="button" class="ai-link">${esc(v)}</button>`).attr("aria-label", `Open ${card.doctype} ${r._name}`)
						.on("click", () => openDoc(card.doctype, r._name)));
				} else $td.text(v === null || v === undefined ? "" : String(v));
				$tr.append($td);
			});
			$b.append($tr);
		});
		$t.append($b);
		$c.append($('<div class="ai-items"></div>').append($t));
		if (card.note) $c.append(`<div class="ai-note ai-table-note">${esc(card.note)}</div>`);
		const $actions = $('<div class="ai-card-actions"></div>');
		if (card.has_more) {
			$actions.append($('<button type="button" class="ai-opt secondary ai-more">Show more</button>').on("click", function () {
				if (S.sending) return;
				$(this).prop("disabled", true);
				send({ action: JSON.stringify({ command: "more" }), client_id: uid() });
			}));
		}
		if (card.list_route && card.list_route.doctype) {
			$actions.append($('<button type="button" class="ai-opt secondary">Open in ERPNext</button>').on("click", () => {
				frappe.route_options = card.list_route.filters || {};
				frappe.set_route("List", card.list_route.doctype);
			}));
		}
		if ($actions.children().length) $c.append($actions);
		return $c;
	}
	function renderSuggestions(list) {
		const $s = $('<div class="ai-options" role="group" aria-label="Suggestions"></div>');
		list.forEach((t) => $s.append($(`<button type="button" class="ai-opt secondary">${esc(t)}</button>`).on("click", function () {
			if (S.sending) return;
			send({ action: JSON.stringify({ command: "say", value: t }), client_id: uid() });
		})));
		return $s;
	}
	function renderCard(card, mid) {
		if (card.type === "table") return renderTable(card, mid);
		const $c = $(`<div class="ai-card" role="group" aria-label="${esc(card.title || "Details")}"></div>`);
		if (card.title) $c.append(`<div class="ai-card-title">${esc(card.title)}</div>`);
		if (card.rows && card.rows.length) {
			const kv = card.rows.map((r) => `<dt>${esc(r.label)}</dt><dd>${esc(r.value)}${pill(r.status)}${r.note ? `<span class="ai-note">${esc(r.note)}</span>` : ""}</dd>`).join("");
			$c.append(`<dl class="ai-kv">${kv}</dl>`);
		}
		if (card.items && card.items.columns) {
			const ic = card.items.columns;
			const body = (card.items.rows || []).map((r) => `<tr>${ic.map((c) => `<td>${esc(r[c.key])}</td>`).join("")}</tr>`).join("");
			$c.append(`<div class="ai-items"><table><thead><tr>${ic.map((c) => `<th scope="col">${esc(c.label)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`);
		} else if (card.items && card.items.length) {
			const body = card.items.map((it, i) => `<tr><td>${i + 1}</td><td>${esc(it.name)}${it.matched || it.code ? `<span class="ai-note">${esc(it.matched || it.code)}</span>` : ""}${it.status ? pill(it.status) : ""}</td>
				<td class="num">${esc(it.qty)}</td><td>${esc(it.uom)}</td><td class="num">${esc(it.rate)}</td><td class="num">${esc(it.amount)}</td></tr>`).join("");
			$c.append(`<div class="ai-items"><table><thead><tr><th scope="col">#</th><th scope="col">Item</th><th scope="col" class="num">Qty</th>
				<th scope="col">UOM</th><th scope="col" class="num">Rate</th><th scope="col" class="num">Amount</th></tr></thead><tbody>${body}</tbody></table></div>`);
		}
		if (card.totals) {
			const t = card.totals, parts = [];
			if (t.subtotal !== null && t.subtotal !== undefined) parts.push(`Subtotal ${esc(t.subtotal)}`);
			if (t.tax !== null && t.tax !== undefined) parts.push(`Tax ${esc(t.tax)}`);
			if (t.total !== null && t.total !== undefined) parts.push(`Total ${esc(t.total)}`);
			if (parts.length) $c.append(`<div class="ai-note" style="margin-top:6px">On the invoice: ${parts.join(" · ")}</div>`);
		}
		if (card.notes && card.notes.length) $c.append(`<ul class="ai-notes">${card.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`);
		if (card.note) $c.append(`<div class="ai-note ai-table-note">${esc(card.note)}</div>`);
		if ((card.type === "created" || card.type === "record") && card.doctype && card.name) {
			$c.append($(`<button type="button" class="ai-primary">${esc(card.action_label || "Open")}</button>`)
				.on("click", () => openDoc(card.doctype, card.name)));
		}
		return $c;
	}
	function renderLinks(links) {
		const $l = $('<div class="ai-options"></div>');
		links.forEach((l) => $l.append($(`<button type="button" class="ai-opt secondary">${esc(l.label)}</button>`).on("click", () => openDoc(l.doctype, l.name))));
		return $l;
	}
	function renderQuestion(q, links) {
		const $q = $(`<div class="ai-question" data-qid="${esc(q.id)}"></div>`);
		if (links) $q.append(renderLinks(links));
		if (q.options && q.options.length) {
			const $opts = $('<div class="ai-options" role="group" aria-label="Choose an answer"></div>');
			q.options.forEach((o) => {
				$opts.append($(`<button type="button" class="ai-opt ${o.style === "secondary" ? "secondary" : ""}">${esc(o.label)}</button>`)
					.on("click", function () { answer(q.id, o.value, o.label); }));
			});
			$q.append($opts);
		}
		if (q.input === "date") {
			const $d = $(`<div class="ai-date" style="margin-top:6px"><label class="ai-sr" for="d-${esc(q.id)}">Date</label>
				<input type="date" id="d-${esc(q.id)}"><button type="button" class="ai-opt">Use this date</button></div>`);
			$d.find("button").on("click", () => { const v = $d.find("input").val(); if (v) answer(q.id, v, v); });
			$q.append($d);
		} else if (q.input === "text") {
			$q.append('<div class="ai-note">…or type your answer below.</div>');
		}
		return $q;
	}
	function refreshQuestions() {
		const $more = $log.find(".ai-more");
		$more.slice(0, -1).prop("disabled", true);
		$log.find(".ai-question").each(function () {
			const active = $(this).attr("data-qid") === S.pendingId && !S.sending;
			$(this).find("button, input").prop("disabled", !active);
		});
	}
	function answer(qid, value, label) {
		if (S.sending || qid !== S.pendingId) return;
		send({ action: JSON.stringify({ question_id: qid, value: value, label: label }), client_id: uid() });
	}
	function openDoc(doctype, name) {
		frappe.set_route("Form", doctype, name);
		if (window.matchMedia("(max-width: 640px)").matches) closePanel();
	}

	// ---------------------------------------------------------------- composer
	function updateComposer() {
		const uploading = S.attachment && S.attachment.uploading;
		const hasContent = $input.val().trim() || (S.attachment && S.attachment.file_url);
		$send.prop("disabled", S.sending || uploading || !hasContent);
		$root.find('[data-act="attach"]').prop("disabled", S.sending);
		$root.find('[data-act="new"]').prop("disabled", S.sending);
		refreshQuestions();
	}
	function autosize() { $input.css("height", "auto").css("height", Math.min($input[0].scrollHeight, 140) + "px"); }
	$input.on("input", () => { autosize(); updateComposer(); });
	$input.on("keydown", (e) => {
		if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $form.trigger("submit"); }
	});
	$form.on("submit", (e) => {
		e.preventDefault();
		if ($send.prop("disabled")) return;
		const text = $input.val().trim();
		const payload = { text: text || null, client_id: uid() };
		if (S.attachment && S.attachment.file_url) payload.file_url = S.attachment.file_url;
		$input.val(""); autosize(); clearAttachment();
		send(payload);
	});

	$root.on("click", '[data-act="attach"]', () => $file.trigger("click"));
	$file.on("change", function () { const f = this.files && this.files[0]; this.value = ""; if (f) setAttachment(f); });
	$root.on("click", '[data-act="remove"]', clearAttachment);
	$root.on("click", '[data-act="retry-upload"]', () => { if (S.attachment) upload(S.attachment); });

	function setAttachment(file) {
		clearAttachment();
		if (!IMAGE_TYPES.includes(file.type)) { systemNote("Please attach a JPG, PNG or WEBP image. PDF files aren't supported yet."); return; }
		if (file.size > MAX_BYTES) { systemNote("That image is larger than 10 MB. Please choose a smaller image."); return; }
		S.attachment = { file, preview: URL.createObjectURL(file), uploading: false, file_url: null, xhr: null };
		$attach.prop("hidden", false).find("img").attr("src", S.attachment.preview).attr("alt", `Preview of ${file.name}`);
		$attach.find(".ai-attach-name").text(file.name);
		upload(S.attachment);
	}
	function clearAttachment() {
		if (S.attachment) {
			if (S.attachment.xhr) S.attachment.xhr.abort();
			if (S.attachment.preview) URL.revokeObjectURL(S.attachment.preview);
		}
		S.attachment = null;
		$attach.prop("hidden", true);
		updateComposer();
	}
	function upload(att) {
		const fd = new FormData();
		fd.append("file", att.file, att.file.name);
		fd.append("is_private", "1");
		fd.append("folder", "Home");
		const xhr = new XMLHttpRequest();
		att.xhr = xhr; att.uploading = true; att.file_url = null;
		const $bar = $attach.find(".ai-progress > div"), $state = $attach.find(".ai-attach-state");
		$attach.find('[data-act="retry-upload"]').prop("hidden", true);
		$state.removeClass("ai-attach-err").text("Uploading… 0%"); $bar.css("width", "0%");
		xhr.upload.onprogress = (e) => {
			if (!e.lengthComputable) return;
			const pct = Math.round((e.loaded / e.total) * 100);
			$bar.css("width", pct + "%"); $state.text(`Uploading… ${pct}%`);
		};
		xhr.onload = () => {
			att.uploading = false; att.xhr = null;
			let url = null;
			try { url = JSON.parse(xhr.responseText).message.file_url; } catch (e) { url = null; }
			if (xhr.status === 200 && url) { att.file_url = url; $bar.css("width", "100%"); $state.text("Ready to send"); }
			else uploadFailed();
			updateComposer();
		};
		xhr.onerror = () => { att.uploading = false; att.xhr = null; uploadFailed(); updateComposer(); };
		xhr.open("POST", "/api/method/upload_file");
		xhr.setRequestHeader("X-Frappe-CSRF-Token", frappe.csrf_token);
		xhr.setRequestHeader("Accept", "application/json");
		xhr.send(fd);
		updateComposer();
	}
	function uploadFailed() {
		$attach.find(".ai-attach-state").addClass("ai-attach-err").text("Upload failed.");
		$attach.find('[data-act="retry-upload"]').prop("hidden", false);
	}
});
