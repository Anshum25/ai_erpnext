$(document).ready(function() {
    if (!window.frappe || !frappe.boot || !frappe.boot.user || frappe.boot.user.name === 'Guest') return;

    let bot_html = `
        <style>
            #ai-floating-bot {
                position: fixed;
                bottom: 30px;
                right: 30px;
                z-index: 1050;
                font-family: 'Inter', sans-serif;
            }
            #ai-bot-btn {
                border-radius: 50%;
                width: 60px;
                height: 60px;
                background: var(--gray-900, #000000ff);
                border: none;
                box-shadow: 0 10px 25px rgba(0, 0, 0, 0.2);
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                transition: all 0.3s ease;
                position: absolute;
                bottom: 0;
                right: 0;
            }
            #ai-bot-btn:hover {
                transform: scale(1.05);
            }
            
            #ai-chat-panel {
                position: absolute;
                bottom: 80px;
                right: 0;
                width: min(350px, calc(100vw - 40px));
                height: min(500px, calc(100vh - 140px));
                background: #ffffff;
                border-radius: 16px;
                box-shadow: 0 15px 40px rgba(0,0,0,0.15);
                display: none;
                flex-direction: column;
                overflow: hidden;
                border: 1px solid #e5e7eb;
            }
            
            .ai-chat-header {
                flex-shrink: 0;
                background: var(--gray-900, #000000ff);
                color: white;
                padding: 15px 20px;
                font-weight: 600;
                font-size: 16px;
                display: flex;
                align-items: center;
                justify-content: space-between;
            }
            
            .ai-chat-close {
                cursor: pointer;
                opacity: 0.8;
            }
            .ai-chat-close:hover { opacity: 1; }
            
            .ai-chat-body {
                flex: 1 1 auto;
                min-height: 0; /* lets the flex child scroll instead of pushing the footer out */
                overscroll-behavior: contain;
                padding: 15px;
                overflow-y: auto;
                background: #f9fafb;
                display: flex;
                flex-direction: column;
                gap: 12px;
            }
            
            .ai-msg {
                flex-shrink: 0;
                max-width: 85%;
                padding: 10px 14px;
                border-radius: 12px;
                font-size: 13.5px;
                line-height: 1.4;
            }
            .ai-msg-bot {
                background: white;
                color: #374151;
                align-self: flex-start;
                border: 1px solid #e5e7eb;
                border-bottom-left-radius: 4px;
            }
            .ai-msg-user {
                background: var(--gray-900, #171717);
                color: white;
                align-self: flex-end;
                border-bottom-right-radius: 4px;
            }
            
            .ai-chat-footer {
                flex-shrink: 0;
                padding: 12px 15px;
                background: white;
                border-top: 1px solid #e5e7eb;
                display: flex;
                align-items: center;
                gap: 10px;
            }
            
            .ai-upload-btn {
                background: #f3f4f6;
                border: 1px solid #e5e7eb;
                padding: 8px 12px;
                border-radius: 8px;
                color: #4b5563;
                cursor: pointer;
                font-size: 13px;
                font-weight: 500;
                display: flex;
                align-items: center;
                gap: 6px;
                flex: 1;
                justify-content: center;
                transition: background 0.2s;
            }
            .ai-upload-btn:hover {
                background: #e5e7eb;
            }
            
            .ai-typing {
                display: flex;
                gap: 4px;
                padding: 4px 2px;
            }
            .ai-typing-dot {
                width: 6px;
                height: 6px;
                background: #9ca3af;
                border-radius: 50%;
                animation: typing 1.4s infinite ease-in-out both;
            }
            .ai-typing-dot:nth-child(1) { animation-delay: -0.32s; }
            .ai-typing-dot:nth-child(2) { animation-delay: -0.16s; }
            @keyframes typing {
                0%, 80%, 100% { transform: scale(0); }
                40% { transform: scale(1); }
            }
            
            .ai-review-card {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 10px;
                margin-top: 8px;
                font-size: 12.5px;
            }
            .ai-review-row {
                display: flex;
                justify-content: space-between;
                padding: 4px 0;
                border-bottom: 1px solid #e2e8f0;
            }
            .ai-review-row:last-child { border-bottom: none; }
            .ai-review-label { color: #64748b; font-weight: 500; }
            .ai-review-value { color: #0f172a; font-weight: 600; text-align: right; }
            
            .ai-submit-btn {
                width: 100%;
                padding: 8px;
                margin-top: 10px;
                background: var(--gray-900, #171717);
                color: white;
                border: none;
                border-radius: 6px;
                cursor: pointer;
                font-weight: 600;
                font-size: 13px;
            }
            .ai-submit-btn:hover { background: #000000; }
        </style>
        
        <div id="ai-floating-bot">
            <div id="ai-chat-panel">
                <div class="ai-chat-header">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
                            <path d="M9 10h.01"></path>
                            <path d="M15 10h.01"></path>
                            <path d="M12 10h.01"></path>
                        </svg>
                        AI Assistant
                    </div>
                    <svg class="ai-chat-close" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                </div>
                <div class="ai-chat-body" id="ai-chat-body">
                    <div class="ai-msg ai-msg-bot">
                        Hi! Upload a document or photo, and I'll extract the details for you to review and verify.
                    </div>
                </div>
                <div class="ai-chat-footer">
                    <button class="ai-upload-btn" id="ai-upload-trigger">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.2 15c.7-1.2 1-2.5.7-3.9-.6-2-2.4-3.5-4.4-3.5h-1.2c-.7-3-3.2-5.2-6.2-5.6-3-.3-5.9 1.3-7.3 4-1.2 2.5-1 6.5.5 8.8m8.7-1.6V21"/><path d="M16 16l-4-4-4 4"/></svg>
                        Upload Document
                    </button>
                </div>
            </div>
            
            <button id="ai-bot-btn">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
                    <path d="M9 10h.01"></path>
                    <path d="M15 10h.01"></path>
                    <path d="M12 10h.01"></path>
                </svg>
            </button>
        </div>
    `;

    $('body').append(bot_html);
    
    const $panel = $('#ai-chat-panel');
    const $btn = $('#ai-bot-btn');
    const $close = $('.ai-chat-close');
    const $body = $('#ai-chat-body');
    const $uploadBtn = $('#ai-upload-trigger');
    
    function scrollToBottom() {
        $body.scrollTop($body[0].scrollHeight);
        requestAnimationFrame(() => $body.scrollTop($body[0].scrollHeight));
    }
    
    function appendMsg(text, type, html = false) {
        let content = html ? text : $('<div>').text(text).html();
        let msgHtml = `<div class="ai-msg ai-msg-${type}">${content}</div>`;
        $body.append(msgHtml);
        scrollToBottom();
    }
    
    function showTyping() {
        let typingHtml = `
            <div class="ai-msg ai-msg-bot" id="ai-typing-indicator">
                <div class="ai-typing">
                    <div class="ai-typing-dot"></div>
                    <div class="ai-typing-dot"></div>
                    <div class="ai-typing-dot"></div>
                </div>
            </div>`;
        $body.append(typingHtml);
        scrollToBottom();
    }
    
    function hideTyping() {
        $('#ai-typing-indicator').remove();
    }

    $btn.on('click', function() {
        // fadeToggle/fadeIn would show the panel as display:block, which breaks the
        // flex column layout (body stops scrolling, footer is pushed out). Keep it flex.
        if ($panel.is(':visible')) {
            $panel.fadeOut(200);
        } else {
            $panel.css({ display: 'flex', opacity: 0 }).animate({ opacity: 1 }, 200, scrollToBottom);
        }
    });
    
    $close.on('click', function() {
        $panel.fadeOut(200);
    });

    $uploadBtn.on('click', function() {
        new frappe.ui.FileUploader({
            allow_multiple: false,
            on_success: function(file_doc) {
                appendMsg("Uploaded " + file_doc.file_name, "user");
                showTyping();
                
                frappe.call({
                    method: 'frappe.client.insert',
                    args: {
                        doc: {
                            doctype: 'AI Document Entry',
                            file: file_doc.file_url
                        }
                    },
                    callback: function(r) {
                        if (r.message) {
                            pollDocument(r.message.name);
                        } else {
                            hideTyping();
                            appendMsg("Failed to create document entry.", "bot");
                        }
                    }
                });
            }
        });
    });
    
    function pollDocument(docname) {
        // AI extraction runs in a background job and usually takes 30-90s, but can
        // take longer when the provider or the queue is slow. Poll sequentially (no
        // overlapping requests), tell the user if it is slow, and never give up silently.
        const POLL_MS = 3000;
        const SLOW_NOTICE_MS = 90 * 1000;
        const GIVE_UP_MS = 10 * 60 * 1000;
        const started = Date.now();
        let slowNoticeShown = false;
        const entryLink = `<a href="/app/ai-document-entry/${encodeURIComponent(docname)}">${frappe.utils.escape_html(docname)}</a>`;

        function check() {
            frappe.call({
                method: 'frappe.client.get',
                args: { doctype: 'AI Document Entry', name: docname },
                callback: function(r) {
                    const doc = r.message;
                    if (doc && doc.status === 'Failed') {
                        hideTyping();
                        appendMsg("Processing failed: " + (doc.error_log || "Unknown error"), "bot");
                        return;
                    }
                    // Any status other than Pending means extraction finished
                    // (Processed for legacy entries, or a mapping/review status).
                    if (doc && doc.status && doc.status !== 'Pending') {
                        hideTyping();
                        renderReview(doc);
                        return;
                    }
                    next();
                },
                error: next,
            });
        }

        function next() {
            const elapsed = Date.now() - started;
            if (elapsed >= GIVE_UP_MS) {
                hideTyping();
                appendMsg(`Still not finished after 10 minutes. Open ${entryLink} to check it.`, "bot", true);
                return;
            }
            if (!slowNoticeShown && elapsed >= SLOW_NOTICE_MS) {
                slowNoticeShown = true;
                hideTyping();
                appendMsg(`This is taking longer than usual. I'm still working on ${entryLink} and will show the result here.`, "bot", true);
                showTyping();
            }
            setTimeout(check, POLL_MS);
        }

        setTimeout(check, POLL_MS);
    }
    
    function renderReview(doc) {
        if (!doc.mapped_data && !doc.extracted_data) {
            appendMsg("Processed, but no data was extracted.", "bot");
            return;
        }
        const esc = (v) => frappe.utils.escape_html(v === null || v === undefined ? "" : String(v));
        let results = {}, validation = {}, mapped = {};
        try { results = JSON.parse(doc.mapping_results || "{}") || {}; } catch (e) {}
        try { validation = JSON.parse(doc.validation_results || "{}") || {}; } catch (e) {}
        try { mapped = JSON.parse(doc.mapped_data || "{}") || {}; } catch (e) {}

        const doctype = doc.target_doctype || mapped.doctype || 'Document';
        const badge = (key) => (window.ai_erpnext && ai_erpnext.review) ? ai_erpnext.review.status_badge(results[key]) : "";
        const row = (label, value, key) => `
            <div class="ai-review-row">
                <span class="ai-review-label">${esc(label)}</span>
                <span class="ai-review-value">${esc(value || "—")} ${key ? badge(key) : ""}</span>
            </div>`;

        let html = `<div>I've extracted and mapped the data. Please review before creating the <b>${esc(doctype)}</b>.</div>`;
        html += `<div class="ai-review-card">`;
        html += row("Status", doc.status);
        if (results.customer) html += row("Customer", (results.customer.display_name || results.customer.input), "customer");
        if (results.company) html += row("Company", results.company.name, "company");
        const items = mapped.items || [];
        if (items.length) {
            const okItems = items.filter((it) => it.item_code).length;
            html += row("Items mapped", `${okItems} / ${items.length}`);
        }
        const errs = (validation.errors || []).length, warns = (validation.warnings || []).length;
        if (validation.document_status) html += row("Checks", `${errs} error(s), ${warns} warning(s)`);
        if (validation.duplicate) html += row("Duplicate", "Possible duplicate found");
        html += `</div>`;

        const btnId = 'ai-submit-' + frappe.utils.get_random(5);
        html += `<button class="ai-submit-btn" id="${btnId}">Review & Create ${esc(doctype)}</button>`;
        appendMsg(html, "bot", true);

        setTimeout(() => {
            $('#' + btnId).on('click', function() {
                $panel.fadeOut(200);
                ai_erpnext.review.open(doc.name);
            });
        }, 100);
    }
});
