const ChatPage = {
    conversation: [],
    currentAgentId: null,
    _agentFromUrl: false,
    sending: false,
    abortController: null,
    attachments: [],
    agents: [],
    viewingArchived: false,
    _archivedId: null,
    _history: [],
    _historyModal: null,
    _pasteSeq: 0,

    async render(params) {
        // Leaving a previous chat view: stop consuming its stream client-side
        // (the server keeps generating and we re-attach below via loadCurrent).
        this._abortClient();
        this.sending = false;
        // #/chat/session/<id> (dashboard's recent-chats list) opens an archived
        // session read-only; #/chat/<agent_id> picks an agent. The two never
        // collide: "session" is not a valid agent id ("session" the literal
        // would fall back to the first agent anyway).
        const openSession = params[0] === 'session' ? params[1] : null;
        this.currentAgentId = openSession ? null : (params[0] || null);
        this.viewingArchived = false;

        let agents = [];
        try { agents = await App.api('GET', '/agents?selectable=true'); } catch (e) { /* empty */ }
        this.agents = agents;
        // For the per-chat model selector. A failed fetch degrades to no
        // selector at all — the chat itself must not depend on this list.
        let models = [];
        try { models = await App.api('GET', '/models'); } catch (e) { /* empty */ }
        this.models = models;

        if (agents.length === 0) {
            App.container.innerHTML = `
                <div class="text-center mt-5">
                    <h4>${i18n('chat.noAgents')}</h4>
                    <p class="text-secondary">${i18n('chat.createFirst')}</p>
                    <a href="#/agents/new" class="btn btn-primary">${i18n('chat.createAgent')}</a>
                </div>`;
            return;
        }

        // An agent id in the URL (the "Chat" button on an agent card) is an
        // explicit pick, so loadCurrent() below must not switch back to the
        // agent the current session belongs to. One-shot: from then on the
        // session wins again, e.g. resuming an archived chat restores its agent.
        // An unknown or non-selectable id falls back to the first agent.
        this._agentFromUrl = agents.some(a => a.id === this.currentAgentId);
        if (!this._agentFromUrl) this.currentAgentId = agents[0].id;

        App.container.innerHTML = `
            <div class="d-flex flex-column mx-auto w-100 chat-wrap">
                <div class="d-flex gap-2 mb-2 flex-wrap align-items-center">
                    <select id="agent-select" class="form-select" style="max-width:200px">
                        <option value="auto" ${this.currentAgentId === 'auto' ? 'selected' : ''}>${i18n('chat.agentAuto')}</option>
                        ${agents.map(a => `
                            <option value="${App.escAttr(a.id)}" ${a.id === this.currentAgentId ? 'selected' : ''}>${App.esc(a.name)}</option>
                        `).join('')}
                    </select>
                    ${models.length ? `
                    <select id="model-select" class="form-select" style="max-width:200px"
                            title="${i18n('chat.modelTitle')}">
                        <option value="">${i18n('chat.modelDefault')}</option>
                        ${models.map(m => `
                            <option value="${App.escAttr(m.id)}">${App.esc(m.name || m.id)}</option>
                        `).join('')}
                    </select>` : ''}
                    <button class="btn btn-outline-primary" id="btn-new" title="${i18n('chat.newChatTitle')}">
                        <i class="bi bi-plus-lg"></i> ${i18n('chat.newChat')}
                    </button>
                    <button class="btn btn-outline-secondary" id="btn-history" title="${i18n('chat.history')}">
                        <i class="bi bi-clock-history"></i> ${i18n('chat.history')}
                    </button>
                    <button class="btn btn-outline-danger" id="btn-del" title="${i18n('chat.deleteArchivedTitle')}" style="display:none">
                        <i class="bi bi-trash"></i>
                    </button>
                </div>
                <div id="archived-banner" class="alert alert-warning py-1 px-2 mb-2" style="display:none">
                    <i class="bi bi-archive"></i> <span id="archived-text">${i18n('chat.archivedBanner')}</span>
                    <a href="#" id="resume-current">${i18n('chat.resume')}</a><span id="resume-sep"> · </span>
                    <a href="#" id="back-current">${i18n('chat.backToCurrent')}</a>
                </div>
                <div id="chat-messages" class="flex-grow-1 overflow-auto border rounded p-3 mb-2"></div>
                <div id="attach-chips" class="d-flex flex-wrap gap-2 mb-2"></div>
                <div class="input-group">
                    <button class="btn btn-outline-secondary" id="btn-attach" type="button" title="${i18n('chat.attachTitle')}">
                        <i class="bi bi-paperclip"></i>
                    </button>
                    <textarea id="chat-input" class="form-control" rows="2" placeholder="${i18n('chat.inputPlaceholder')}"
                              style="resize:none"></textarea>
                    <button id="chat-send" class="btn btn-primary">
                        <i class="bi bi-send"></i>
                        <span class="spinner-border spinner-border-sm spinner-chat" role="status"></span>
                    </button>
                </div>
                <input type="file" id="attach-input" multiple hidden
                       accept="image/*,text/*,.txt,.md,.csv,.json,.log,.py,.js,.html,.css,.xml,.yaml,.yml">
            </div>

            <div class="modal fade" id="history-modal" tabindex="-1" aria-hidden="true">
                <div class="modal-dialog modal-dialog-scrollable modal-lg">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title"><i class="bi bi-clock-history"></i> ${i18n('chat.historyTitle')}</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                        </div>
                        <div class="modal-body">
                            <div class="input-group mb-3">
                                <span class="input-group-text"><i class="bi bi-search"></i></span>
                                <input type="text" id="history-filter" class="form-control"
                                       placeholder="${i18n('chat.searchPlaceholder')}" autocomplete="off">
                            </div>
                            <div id="history-list" class="history-list"></div>
                        </div>
                    </div>
                </div>
            </div>`;

        this.bindEvents();
        await this.refreshHistory();
        await this.loadCurrent();
        // A session that no longer exists (deleted, or resumed and reloaded)
        // fails silently inside viewArchived, leaving the current chat shown.
        if (openSession) await this.viewArchived(openSession);
    },

    // ---- Server-backed sessions (one file per chat on disk) ---------------
    // Fetch the archived-chat list (newest first) and keep it in memory. If the
    // history window is open, re-render it in place so it stays in sync.
    async refreshHistory() {
        try { this._history = await App.api('GET', '/sessions'); } catch (e) { this._history = []; }
        const modal = document.getElementById('history-modal');
        if (modal && modal.classList.contains('show')) {
            const f = document.getElementById('history-filter');
            this.renderHistoryList(f ? f.value : '');
        }
    },

    // Open the filterable history window (chats grouped by date, newest first).
    openHistory() {
        const el = document.getElementById('history-modal');
        if (!el) return;
        this._historyModal = bootstrap.Modal.getOrCreateInstance(el);
        const filter = document.getElementById('history-filter');
        if (filter) filter.value = '';
        this.renderHistoryList('');
        el.addEventListener('shown.bs.modal', () => { if (filter) filter.focus(); }, { once: true });
        this._historyModal.show();
    },

    agentName(id) {
        return (this.agents.find(a => a.id === id) || {}).name || id || '';
    },

    // A human day bucket for grouping ("Oggi", "Ieri", or a full date).
    dayLabel(iso) {
        const d = iso ? new Date(iso) : null;
        if (!d || isNaN(d.getTime())) return i18n('chat.unknownDate');
        const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
        const days = Math.round((startOfDay(new Date()) - startOfDay(d)) / 86400000);
        if (days <= 0) return i18n('chat.today');
        if (days === 1) return i18n('chat.yesterday');
        return d.toLocaleDateString(I18n.getDateLocale(), { day: '2-digit', month: 'long', year: 'numeric' });
    },

    // Render the (filtered) session list into the modal, with date-group headers.
    renderHistoryList(query) {
        const box = document.getElementById('history-list');
        if (!box) return;
        const q = (query || '').trim().toLowerCase();
        // Provenance label shown on the badge of connector chats: the source
        // ("telegram") or a localized generic fallback when only the channel
        // key is known. The filter matches it too, so typing what the badge
        // displays keeps the row visible.
        const sourceLabel = (s) => s.source || (s.channel ? i18n('chat.channelSource') : '');
        const items = (this._history || []).filter(s =>
            !q || `${s.title || ''} ${this.agentName(s.agent_id)} ${sourceLabel(s)} ${s.channel || ''}`
                .toLowerCase().includes(q));

        if (!items.length) {
            box.innerHTML = `<div class="text-secondary text-center py-4">${
                (this._history || []).length ? i18n('chat.noMatch') : i18n('chat.noSessions')}</div>`;
            return;
        }

        let html = '', lastGroup = null;
        for (const s of items) {
            const group = this.dayLabel(s.updated_at);
            if (group !== lastGroup) {
                html += `<div class="history-group">${App.esc(group)}</div>`;
                lastGroup = group;
            }
            const active = s.id === this._archivedId ? ' active' : '';
            // Chats archived from an external connector carry their provenance:
            // source = connector type ("telegram"), channel = the external chat key.
            const src = sourceLabel(s);
            const srcIcon = s.source === 'telegram' ? 'bi-telegram'
                : s.source === 'autonomous' ? 'bi-robot' : 'bi-broadcast-pin';
            const srcBadge = src ? `
                            · <span class="hi-source" title="${App.escAttr(s.channel || '')}">
                                <i class="bi ${srcIcon}"></i> ${App.esc(src)}</span>` : '';
            html += `
                <div class="history-item${active}" data-id="${App.escAttr(s.id)}" role="button" tabindex="0">
                    <div class="hi-main">
                        <div class="hi-title">${App.esc(s.title || i18n('chat.untitled'))}</div>
                        <div class="hi-meta">
                            <i class="bi bi-cpu"></i> ${App.esc(this.agentName(s.agent_id))}
                            · ${i18n('chat.messagesCount', { n: s.message_count || 0 })}${srcBadge}
                        </div>
                    </div>
                    <div class="hi-when">${App.esc(this.fmtTime(s.updated_at))}</div>
                    <button class="btn btn-sm btn-link text-danger hi-del" data-id="${App.escAttr(s.id)}"
                            title="${i18n('chat.deleteArchivedTitle')}"><i class="bi bi-trash"></i></button>
                </div>`;
        }
        box.innerHTML = html;

        box.querySelectorAll('.history-item').forEach(row => {
            const open = () => { if (this._historyModal) this._historyModal.hide(); this.viewArchived(row.dataset.id); };
            row.onclick = (e) => { if (!e.target.closest('.hi-del')) open(); };
            row.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
        });
        box.querySelectorAll('.hi-del').forEach(btn => {
            btn.onclick = async (e) => {
                e.stopPropagation();
                if (!confirm(i18n('chat.deleteConfirm'))) return;
                const id = btn.dataset.id;
                try { await App.api('DELETE', '/sessions/' + id); } catch (err) { /* ignore */ }
                if (this._archivedId === id) await this.loadCurrent();
                await this.refreshHistory();  // re-renders the list in place
            };
        });
    },

    async loadCurrent() {
        this.viewingArchived = false;
        this._archivedId = null;
        document.getElementById('archived-banner').style.display = 'none';
        document.getElementById('btn-del').style.display = 'none';
        this.setInputEnabled(true);
        let session = null;
        try { session = await App.api('GET', '/sessions/current'); } catch (e) { /* ignore */ }
        const urlPick = this._agentFromUrl;
        this._agentFromUrl = false;
        // Auto mode has its own session flag: record_user_turn overwrites
        // session.agent_id with the RESOLVED agent on every turn, so "auto"
        // can never be read back from there.
        if (!urlPick && session && session.agent_auto) {
            this.currentAgentId = 'auto';
            const as = document.getElementById('agent-select'); if (as) as.value = 'auto';
        } else if (!urlPick && session && session.agent_id && this.agents.some(a => a.id === session.agent_id)) {
            this.currentAgentId = session.agent_id;
            const as = document.getElementById('agent-select'); if (as) as.value = session.agent_id;
        }
        // The chat's model pick lives on the session (a new chat has none).
        // Only restore an id that still exists: a deleted model must degrade
        // to the default, not 404 every send from a stale selector.
        const override = session && session.model_override;
        this.modelOverride = (override && this.models.some(m => m.id === override))
            ? override : null;
        const msel = document.getElementById('model-select');
        if (msel) msel.value = this.modelOverride || '';
        this.renderSessionMessages(session || { messages: [] });
        // If a response is still being generated for this chat, reconnect to
        // its live stream (replay so far + follow the tail).
        try {
            const live = await App.api('GET', '/chat/live');
            if (live && live.active) this._attachToLive();
        } catch (e) { /* ignore */ }
    },

    async viewArchived(id) {
        let session = null;
        try { session = await App.api('GET', '/sessions/' + id); } catch (e) { /* ignore */ }
        if (!session) return;
        this.viewingArchived = true;
        this._archivedId = id;
        // A LIVE channel session (Telegram, satellite, an autonomous agent) is
        // read-only for a different reason than an archived chat: its connector
        // is still writing to it, so "resume" has nothing to reopen and the
        // endpoint 404s. Offering the link made it a silent no-op — say why
        // instead. `live` comes from the history listing (loaded before any
        // viewArchived call, deep links included).
        const row = (this._history || []).find(s => s.id === id);
        const live = this._archivedLive = !!(row && row.live);
        document.getElementById('archived-banner').style.display = '';
        const banner = document.getElementById('archived-text');
        if (banner) banner.textContent = i18n(live ? 'chat.liveBanner' : 'chat.archivedBanner');
        const resume = document.getElementById('resume-current');
        if (resume) resume.style.display = live ? 'none' : '';
        const sep = document.getElementById('resume-sep');
        if (sep) sep.style.display = live ? 'none' : '';
        document.getElementById('btn-del').style.display = '';
        this.setInputEnabled(false);
        this.renderSessionMessages(session);
    },

    async newChat() {
        try { await App.api('POST', '/sessions/new', { agent_id: this.currentAgentId }); } catch (e) { /* ignore */ }
        this.attachments = [];
        this.renderAttachChips();
        await this.refreshHistory();
        await this.loadCurrent();
    },

    async resumeArchived() {
        if (!this._archivedId || this._archivedLive) return;
        // Makes the archived chat the current one (archiving whatever is active).
        try { await App.api('POST', '/sessions/' + this._archivedId + '/resume'); } catch (e) { /* ignore */ }
        this.attachments = [];
        this.renderAttachChips();
        await this.refreshHistory();
        await this.loadCurrent();  // restores the session's original agent_id
    },

    setInputEnabled(on) {
        ['chat-input', 'chat-send', 'btn-attach'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.disabled = !on;
        });
        const inp = document.getElementById('chat-input');
        if (inp) inp.placeholder = on ? i18n('chat.inputPlaceholder') : i18n('chat.archivedPlaceholder');
    },

    renderSessionMessages(session) {
        const box = document.getElementById('chat-messages');
        if (box) box.innerHTML = '';
        let pendingTools = [];
        // For the Auto-mode "via <agent>" label: user messages persist the
        // resolved agent_id of the turn they opened (record_user_turn).
        let lastAgentId = null;
        const flushAssistant = (text, ts, reasoning, context) => {
            const msgDiv = this.createMessageDiv('assistant');
            msgDiv._md = text || '';  // raw markdown, for the copy / source view
            this.setReasoning(msgDiv, reasoning || '', false);
            // Same .msg-flow anatomy as the live bubble (one tool group + one
            // text segment: the stored turn has no finer chronology).
            const flow = document.createElement('div');
            flow.className = 'msg-flow';
            msgDiv.appendChild(flow);
            if (pendingTools.length) {
                const tc = document.createElement('div');
                tc.className = 'tool-calls';
                // Expandable tool calls, with nested sub-agent flows (recursive).
                for (const t of pendingTools) this.renderToolCall(tc, t);
                flow.appendChild(tc);
            }
            const c = document.createElement('div');
            c.className = 'msg-content';
            c.innerHTML = this.renderMarkdown(text || '');
            flow.appendChild(c);
            // Persisted tool messages carry the same resources/sub_trace shape
            // as trace steps, so a reloaded session shows the same strip.
            this.renderResources(msgDiv, this.collectResources(pendingTools));
            this.setContext(msgDiv, context);
            msgDiv.appendChild(this._timeEl(ts));
            // Known cosmetic limit: a chat where Auto was toggled midway labels
            // every turn — each label still names the agent that really answered.
            if (session.agent_auto && lastAgentId) {
                this._tagAgent(msgDiv, lastAgentId);
            }
            pendingTools = [];
        };
        for (const m of (session.messages || [])) {
            if (m.role === 'user') {
                pendingTools = [];
                if (m.agent_id) lastAgentId = m.agent_id;
                this.appendUserMessage(m.text || '', m.attachments || [], m.ts);
            } else if (m.role === 'tool') {
                pendingTools.push(m);
            } else if (m.role === 'assistant') {
                flushAssistant(m.text || '', m.ts, m.reasoning, m.context);
            } else if (m.role === 'error' || m.role === 'notice') {
                this.appendMessage(m.role, m.text || '', m.ts);
            }
        }
        this._decorateMessages();
        if (box) box.scrollTop = box.scrollHeight;
    },

    bindEvents() {
        document.getElementById('chat-send').onclick = () =>
            this.sending ? this.stopGeneration() : this.sendMessage();
        document.getElementById('chat-input').onkeydown = (e) => {
            // On touch keyboards there is no Shift+Enter: let Enter insert a
            // newline and keep the send button as the only submit path.
            const isTouch = window.matchMedia('(pointer: coarse)').matches;
            if (e.key === 'Enter' && !e.shiftKey && !isTouch) {
                e.preventDefault();
                this.sendMessage();
            }
        };
        // Paste an image straight from the clipboard (screenshots, copied images)
        // — routed through the same pipeline as attached files.
        document.getElementById('chat-input').addEventListener('paste', (e) => this.handlePaste(e));
        document.getElementById('btn-attach').onclick = () =>
            document.getElementById('attach-input').click();
        document.getElementById('attach-input').onchange = (e) => {
            this.handleFiles(e.target.files);
            e.target.value = '';  // allow re-selecting the same file
        };
        document.getElementById('btn-new').onclick = () => this.newChat();
        document.getElementById('btn-history').onclick = () => this.openHistory();
        const hf = document.getElementById('history-filter');
        if (hf) hf.oninput = () => this.renderHistoryList(hf.value);
        document.getElementById('btn-del').onclick = async () => {
            if (!this._archivedId) return;
            try { await App.api('DELETE', '/sessions/' + this._archivedId); } catch (e) { /* ignore */ }
            await this.refreshHistory();
            await this.loadCurrent();
        };
        document.getElementById('resume-current').onclick = (e) => { e.preventDefault(); this.resumeArchived(); };
        document.getElementById('back-current').onclick = (e) => { e.preventDefault(); this.loadCurrent(); };
        document.getElementById('agent-select').onchange = (e) => {
            this.currentAgentId = e.target.value;
            this.attachments = [];
            this.renderAttachChips();
            if (this.viewingArchived) this.loadCurrent();
        };
        // Per-chat model pick: state only — it rides every send request, and
        // the server keeps it on the current session so a reload restores it.
        const ms = document.getElementById('model-select');
        if (ms) ms.onchange = (e) => { this.modelOverride = e.target.value || null; };
    },

    readFile(file, as) {
        return new Promise((resolve, reject) => {
            const r = new FileReader();
            r.onload = () => resolve(r.result);
            r.onerror = () => reject(r.error || new Error('read error'));
            if (as === 'dataURL') r.readAsDataURL(file);
            else r.readAsText(file);
        });
    },

    // Pull image files out of a paste event and feed them to handleFiles. Text
    // paste is left untouched (we only preventDefault when we grab an image, so
    // pasting words into the textarea still works normally).
    handlePaste(e) {
        if (this.viewingArchived) return;
        const cd = e.clipboardData || window.clipboardData;
        if (!cd || !cd.items) return;
        const files = [];
        for (const item of cd.items) {
            if (item.kind !== 'file' || !item.type.startsWith('image/')) continue;
            const blob = item.getAsFile();
            if (!blob) continue;
            // Clipboard images often have no filename (or a generic one): give
            // them a stable, unique name so the chip label isn't blank.
            if (blob.name) {
                files.push(blob);
            } else {
                const ext = (item.type.split('/')[1] || 'png').split(';')[0];
                const name = `${i18n('chat.pastedImage')}-${++this._pasteSeq}.${ext}`;
                files.push(new File([blob], name, { type: item.type }));
            }
        }
        if (files.length) {
            e.preventDefault();  // keep the raw image/path out of the textarea
            this.handleFiles(files);
        }
    },

    async handleFiles(fileList) {
        const MAX = 20 * 1024 * 1024;  // 20 MB per file (images are downscaled)
        for (const file of fileList) {
            if (file.size > MAX) {
                this.appendMessage('error', i18n('chat.fileTooLarge', { name: file.name }));
                continue;
            }
            try {
                if (file.type.startsWith('image/')) {
                    // Downscale before sending: local vision models are slow on
                    // large images and big payloads risk request timeouts.
                    const dataUrl = await this.downscaleImage(file, 1280);
                    this.attachments.push({ name: file.name, kind: 'image', data: dataUrl, mime: 'image/jpeg' });
                } else {
                    const text = await this.readFile(file, 'text');
                    this.attachments.push({ name: file.name, kind: 'text', data: text, mime: file.type || 'text/plain' });
                }
            } catch (err) {
                this.appendMessage('error', i18n('chat.readError', { name: file.name, error: err.message }));
            }
        }
        this.renderAttachChips();
    },

    downscaleImage(file, maxDim) {
        return new Promise((resolve, reject) => {
            const url = URL.createObjectURL(file);
            const img = new Image();
            img.onload = () => {
                URL.revokeObjectURL(url);
                let { width, height } = img;
                if (Math.max(width, height) > maxDim) {
                    const scale = maxDim / Math.max(width, height);
                    width = Math.round(width * scale);
                    height = Math.round(height * scale);
                }
                const canvas = document.createElement('canvas');
                canvas.width = width;
                canvas.height = height;
                canvas.getContext('2d').drawImage(img, 0, 0, width, height);
                resolve(canvas.toDataURL('image/jpeg', 0.85));
            };
            img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('image decode failed')); };
            img.src = url;
        });
    },

    renderAttachChips() {
        const el = document.getElementById('attach-chips');
        if (!el) return;
        el.innerHTML = this.attachments.map((a, i) => {
            const inner = a.kind === 'image'
                ? `<img src="${a.data}" style="height:32px;width:32px;object-fit:cover;border-radius:4px" class="me-1">`
                : `<i class="bi bi-file-earmark-text me-1"></i>`;
            return `<span class="badge text-bg-light border d-inline-flex align-items-center p-1">
                ${inner}<span class="small">${App.esc(a.name)}</span>
                <button type="button" class="btn-close ms-1 attach-remove" style="font-size:.6rem" data-idx="${i}"></button>
            </span>`;
        }).join('');
        el.querySelectorAll('.btn-close').forEach(b => {
            b.onclick = () => {
                this.attachments.splice(parseInt(b.dataset.idx, 10), 1);
                this.renderAttachChips();
            };
        });
    },

    async sendMessage() {
        if (this.sending || this.viewingArchived) return;
        const input = document.getElementById('chat-input');
        const message = input.value.trim();
        const attachments = this.attachments.slice();
        if (!message && attachments.length === 0) return;
        input.value = '';
        this.attachments = [];
        this.renderAttachChips();
        await this._send(message, attachments);
    },

    // Post one turn and stream the answer into a fresh bubble. Shared by the
    // composer, Regenerate and Edit-prompt — the latter two supply the message
    // themselves (after rewinding the chat), so everything downstream (live run,
    // persistence, title) behaves exactly like a normal send.
    async _send(message, attachments) {
        if (this.sending || this.viewingArchived) return;
        attachments = attachments || [];
        if (!message && !attachments.length) return;  // nothing to ask
        this.appendUserMessage(message, attachments);
        this.setSending(true);
        const ui = this._newAssistantBubble();

        try {
            this.abortController = new AbortController();
            const resp = await fetch(App.apiUrl('/chat/stream'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...App.authHeaders() },
                body: JSON.stringify({
                    agent_id: this.currentAgentId,
                    message,
                    attachments: attachments.map(a => this._attachForSend(a)),
                    model_override: this.modelOverride || null,
                }),
                signal: this.abortController.signal,
            });
            // App.errorText unwraps FastAPI's {"detail": ...}; without it a
            // "no default model" 404 reaches the bubble as a raw JSON blob.
            if (!resp.ok) {
                throw new Error(App.errorText(await resp.text()) || `HTTP ${resp.status}`);
            }
            await this._consumeStream(resp, ui);
        } catch (err) {
            if (ui.thinking && ui.thinking.parentNode) ui.thinking.remove();
            if (err.name !== 'AbortError') {
                this.appendMessage('error', i18n('chat.errorPrefix', { msg: err.message }));
            }
        } finally {
            this._finalizeStream();
        }
    },

    // Stop the server-side generation for the current chat. The active stream
    // receives a 'stopped' event and finalizes itself.
    async stopGeneration() {
        try { await App.api('POST', '/chat/stop'); } catch (e) { /* ignore */ }
    },

    // If a generation is still running for the current chat (e.g. we just came
    // back to it), re-attach to its live stream: replay what happened so far,
    // then follow the tail. Fire-and-forget.
    async _attachToLive() {
        if (this.viewingArchived) return;
        this.setSending(true);
        const ui = this._newAssistantBubble();
        try {
            this.abortController = new AbortController();
            const resp = await fetch(App.apiUrl('/chat/stream/attach'), {
                signal: this.abortController.signal,
                headers: App.authHeaders(),
            });
            if (!resp.ok) throw new Error('attach failed');
            await this._consumeStream(resp, ui);
        } catch (e) {
            if (ui.thinking && ui.thinking.parentNode) ui.thinking.remove();
        } finally {
            this._finalizeStream();
        }
    },

    // Build the streaming assistant bubble. The body is a .msg-flow: an ordered
    // sequence of .msg-content text segments and .tool-calls groups appended in
    // arrival order, so a multi-iteration turn (text → tools → text → …) reads
    // top-to-bottom in real chronology. Segments and groups are created lazily
    // by _consumeStream; session reload builds the same anatomy (one group +
    // one segment — the stored turn has no finer order).
    _newAssistantBubble() {
        const msgDiv = this.createMessageDiv('assistant');
        const flow = document.createElement('div');
        flow.className = 'msg-flow';
        msgDiv.appendChild(flow);
        const thinking = document.createElement('div');
        thinking.className = 'typing-indicator';
        thinking.innerHTML = '<span></span><span></span><span></span>';
        flow.appendChild(thinking);
        const c = document.getElementById('chat-messages');
        if (c) c.scrollTop = c.scrollHeight;
        return { msgDiv, flow, thinking };
    },

    // A thinking model's chain-of-thought, collapsed above the tool calls and
    // the answer. Plain text on purpose (textContent): it is the model talking
    // to itself, half-formed markdown included, and it must never be able to
    // inject markup. Kept out of msgDiv._md, so copy / "show source" / the
    // stored turn keep meaning the answer alone.
    setReasoning(msgDiv, text, live) {
        if (!text) return null;
        let box = msgDiv.querySelector(':scope > .msg-reasoning');
        const fresh = !box;
        if (fresh) {
            box = document.createElement('details');
            box.className = 'msg-reasoning';
            const sum = document.createElement('summary');
            sum.innerHTML = '<i class="bi bi-lightbulb"></i> <span class="rs-label"></span>';
            box.appendChild(sum);
            const body = document.createElement('div');
            body.className = 'reasoning-body';
            box.appendChild(body);
            msgDiv.insertBefore(box, msgDiv.firstChild);
        }
        box.querySelector('.reasoning-body').textContent = text;
        box.querySelector('.rs-label').textContent =
            i18n(live ? 'chat.reasoningLive' : 'chat.reasoning');
        box.classList.toggle('reasoning-live', !!live);
        return fresh ? box : null;  // non-null only when it just appeared
    },

    // How full the model's context got during the turn. Not decoration: on a
    // local model the window is small and the executor silently compresses the
    // oldest tool results to stay inside it, so "this answer was written from a
    // compressed context" is the fact that explains a vague reply. Rendered
    // from ChatResponse.context, which is persisted on the assistant message —
    // the trace's top-level fields are not, so a gauge fed from there would
    // show live and vanish on reload.
    setContext(msgDiv, ctx) {
        if (!ctx || !ctx.window) return;
        const used = Math.max(0, ctx.peak_used || ctx.used || 0);
        const pct = Math.min(100, Math.round((used / ctx.window) * 100));
        const k = (n) => (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k'
                                    : String(n));
        let el = msgDiv.querySelector(':scope > .msg-context');
        if (!el) {
            el = document.createElement('div');
            el.className = 'msg-context';
            el.innerHTML = '<span class="ctx-bar"><i></i></span>'
                         + '<span class="ctx-text"></span>';
            msgDiv.appendChild(el);
        }
        el.querySelector('.ctx-bar i').style.width = pct + '%';
        // Amber past the point where the executor starts compressing, red once
        // it actually did: the reader should be able to tell "close" from "cut".
        el.classList.toggle('ctx-warn', pct >= 75);
        el.classList.toggle('ctx-cut', !!ctx.demoted);
        let text = i18n('chat.context') + ' ' + k(used) + ' / ' + k(ctx.window);
        if (ctx.demoted) {
            text += ' · ' + i18n('chat.contextCompacted').replace('{n}', ctx.demoted);
        }
        el.querySelector('.ctx-text').textContent = text;
        el.title = i18n('chat.contextTitle')
            .replace('{window}', ctx.window)
            .replace('{used}', used)
            .replace('{reserve}', ctx.reserve || 0)
            .replace('{source}', ctx.source || '?');
    },

    // Read an SSE stream into the given bubble. Shared by send and re-attach.
    // If the bubble leaves the DOM (user navigated away) it stops consuming but
    // the server keeps generating, so we can re-attach later.
    async _consumeStream(resp, ui) {
        const { msgDiv, flow, thinking } = ui;
        const clearThinking = () => { if (thinking && thinking.parentNode) thinking.remove(); };
        const mc = document.getElementById('chat-messages');
        const scroll = () => { if (mc) mc.scrollTop = mc.scrollHeight; };
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '', errored = false, stopped = false, idle = false;
        let reasoningText = '', reasoningLive = false;
        // Interleaved flow state: tokens go into the current text segment, tool
        // entries into the current .tool-calls group — opening one closes the
        // other, so the bubble preserves the turn's real text/tool chronology.
        // A segment never spans two LLM calls: an iteration only continues when
        // it made tool calls, and those open a group in between.
        let curSeg = null;      // {div, text} — open text segment
        let curTools = null;    // group open for NEW tool entries
        let lastTools = null;   // latest group: where tool_result / agent_event land
        const textSegs = [];
        const allText = () => textSegs.map(s => s.text).filter(Boolean).join('\n\n');
        const textSeg = () => {
            if (!curSeg) {
                const div = document.createElement('div');
                div.className = 'msg-content';
                flow.appendChild(div);
                curSeg = { div, text: '' };
                textSegs.push(curSeg);
                curTools = null;  // next tool_start opens a group BELOW this text
            }
            return curSeg;
        };
        const toolGroup = () => {
            if (!curTools) {
                curTools = document.createElement('div');
                curTools.className = 'tool-calls';
                flow.appendChild(curTools);
                lastTools = curTools;
                curSeg = null;  // next token opens a segment BELOW this group
            }
            return curTools;
        };
        // Live sub-agent containers keyed by delegation path ("A" or "A/B").
        // Stream-local on purpose: a re-attach replays the whole event buffer,
        // so the nested blocks rebuild identically from the events alone.
        const subAgents = new Map();
        // Drop the "reasoning..." label once the model moves on to the answer.
        // Guarded: this runs on every token, and rewriting the whole block
        // each time would be O(reasoning) per character of the answer.
        const settleReasoning = () => {
            if (!reasoningLive) return;
            reasoningLive = false;
            this.setReasoning(msgDiv, reasoningText, false);
        };

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            if (!document.body.contains(flow)) { this._abortClient(); return; }  // navigated away
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                let event;
                try { event = JSON.parse(line.slice(6)); } catch (e) { continue; }
                switch (event.type) {
                    case 'token': {
                        clearThinking();
                        settleReasoning();
                        const seg = textSeg();
                        seg.text += event.data;
                        msgDiv._md = allText();
                        seg.div.innerHTML = this.renderMarkdown(seg.text);
                        scroll();
                        break;
                    }
                    case 'reasoning':
                        // Collapsed while it streams: the typing dots already
                        // say "working", and an auto-expanding block would push
                        // the answer off screen on every turn.
                        reasoningText += event.data;
                        reasoningLive = true;
                        if (this.setReasoning(msgDiv, reasoningText, true)) scroll();
                        break;
                    case 'clear_tokens':
                        // Reclassify: what streamed as answer was thought. The
                        // splitter is per LLM call, and a segment never spans
                        // calls — dropping the open segment drops exactly the
                        // reclassified tokens (earlier segments are earlier
                        // calls' text and stay).
                        if (curSeg) {
                            curSeg.div.remove();
                            textSegs.pop();       // curSeg is always the newest
                            curSeg = null;
                            curTools = lastTools; // reuse the adjacent group, not a twin
                        }
                        msgDiv._md = allText();
                        break;
                    case 'tool_start':
                        clearThinking();
                        this.addLiveTool(toolGroup(), event.data);
                        scroll();
                        break;
                    case 'agent_event':
                        // A delegated sub-agent's live activity, nested inside
                        // the running call_agent entry (in the latest group).
                        // Replaced by the authoritative sub_trace on 'done'.
                        clearThinking();
                        this.handleAgentEvent(lastTools, subAgents, event.data, msgDiv);
                        scroll();
                        break;
                    case 'tool_result':
                        this.completeLiveTool(lastTools || toolGroup(), event.data);
                        // A settled delegation is over: forget its live paths,
                        // or a LATER call_agent to the same agent would stream
                        // into this finished block instead of its own.
                        if (event.data && event.data.tool === 'call_agent') subAgents.clear();
                        // Resources render the moment the tool finishes, before
                        // the answer streams; the 'done' settle below replaces
                        // this with the authoritative list from the trace.
                        if (event.data && event.data.resources) {
                            msgDiv._resources = this.collectResources(
                                [event.data], msgDiv._resources);
                            this.renderResources(msgDiv, msgDiv._resources);
                            scroll();
                        }
                        break;
                    case 'done': {
                        clearThinking();
                        settleReasoning();
                        if (errored) break;
                        if (!allText() && event.data && event.data.reply) {
                            const seg = textSeg();
                            seg.text = event.data.reply;
                            msgDiv._md = allText();
                            seg.div.innerHTML = this.renderMarkdown(seg.text);
                        }
                        const trace = event.data && event.data.trace;
                        if (trace && trace.steps && trace.steps.length) {
                            // Authoritative settle IN PLACE: live entries are
                            // upgraded to the full trace render, the interleaved
                            // layout streaming built is kept as-is.
                            this._settleFlowTools(flow, trace.steps);
                            // Authoritative resource strip: same settle-from-
                            // trace rule as the tool entries, and the only
                            // source that sees sub-agent resources (sub_trace).
                            msgDiv._resources = this.collectResources(trace.steps);
                            this.renderResources(msgDiv, msgDiv._resources);
                        }
                        this.setContext(msgDiv, event.data && event.data.context);
                        msgDiv.appendChild(this._timeEl());
                        // Auto mode: name the agent that actually answered.
                        // The trace carries the RESOLVED agent_id (the server
                        // routed "auto" before the executor was built), and a
                        // re-attach replays the same done event, so this works
                        // there too.
                        if (this.currentAgentId === 'auto' && trace && trace.agent_id) {
                            this._tagAgent(msgDiv, trace.agent_id);
                        }
                        break;
                    }
                    case 'stopped':
                        clearThinking();
                        settleReasoning();
                        stopped = true;
                        textSeg().div.insertAdjacentHTML('beforeend',
                            `<div class="text-secondary small fst-italic mt-1">${i18n('chat.stopped')}</div>`);
                        msgDiv.appendChild(this._timeEl());
                        break;
                    case 'error':
                        errored = true;
                        clearThinking();
                        settleReasoning();
                        if (!allText() && !flow.querySelector('.tool-calls')) msgDiv.remove();
                        this.appendMessage('error', i18n('chat.errorPrefix', { msg: event.data }));
                        break;
                    case 'agent':
                        // Auto mode: the server announces the resolved agent
                        // before the first token, so the label is visible while
                        // the answer is still being written.
                        this._tagAgent(msgDiv, event.data);
                        break;
                    case 'notice':
                        // Inserted ABOVE the answer bubble, which already exists:
                        // it explains the answer the user is about to read.
                        this.insertNoticeBefore(msgDiv, event.data);
                        break;
                    case 'idle':
                        idle = true;  // nothing was actually running
                        break;
                }
            }
        }

        clearThinking();
        if (idle) {
            msgDiv.remove();  // attach found no live run
        } else if (!errored && !stopped && !allText()) {
            const seg = textSeg();
            if (!seg.div.innerHTML) {
                seg.div.innerHTML = `<em class="text-secondary">${i18n('chat.noResponse')}</em>`;
            }
        }
    },

    _finalizeStream() {
        // Only reset UI if we're still on the chat page (not navigated away —
        // in which case the server keeps running and we re-attach on return).
        if (!document.getElementById('chat-send')) return;
        this.setSending(false);
        this.abortController = null;
        this._decorateMessages();  // the finished bubble is now the last one
    },

    // Abort only the CLIENT-side consumption of the stream; the server-side
    // generation keeps running (decoupled), so it can be re-attached later.
    _abortClient() {
        if (this.abortController) {
            try { this.abortController.abort(); } catch (e) { /* ignore */ }
            this.abortController = null;
        }
    },

    setSending(on) {
        this.sending = on;
        // Editing/regenerating mid-generation would race the live run (the
        // rewind endpoint refuses it anyway): take the actions away while it
        // streams — _finalizeStream() puts them back.
        const box = document.getElementById('chat-messages');
        if (box && on) box.querySelectorAll('.msg-actions').forEach(el => el.remove());
        const btn = document.getElementById('chat-send');
        if (!btn) return;
        const spinner = btn.querySelector('.spinner-chat');
        const icon = btn.querySelector('.bi');
        if (spinner) spinner.classList.remove('active');  // stop icon conveys activity now
        if (on) {
            btn.classList.remove('btn-primary');
            btn.classList.add('btn-danger');
            if (icon) icon.className = 'bi bi-stop-fill';
            btn.title = i18n('chat.stop');
        } else {
            btn.classList.remove('btn-danger');
            btn.classList.add('btn-primary');
            if (icon) icon.className = 'bi bi-send';
            btn.removeAttribute('title');
        }
        btn.disabled = false;  // stays clickable to act as the Stop button
    },

    createMessageDiv(role) {
        const container = document.getElementById('chat-messages');
        const div = document.createElement('div');
        div.className = `msg msg-${role}`;
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
        return div;
    },

    // Format an ISO timestamp (server local time) for display. Omitted ts = now.
    // Locale-aware like dayLabel(): a hardcoded dd/mm read as "month 30" in en.
    fmtTime(ts) {
        let d = ts ? new Date(ts) : new Date();
        if (isNaN(d.getTime())) d = new Date();
        const hhmm = d.toLocaleTimeString(I18n.getDateLocale(),
            { hour: '2-digit', minute: '2-digit' });
        const sameDay = d.toDateString() === new Date().toDateString();
        if (sameDay) return hhmm;
        const dm = d.toLocaleDateString(I18n.getDateLocale(),
            { day: '2-digit', month: '2-digit' });
        return `${dm} ${hhmm}`;
    },

    _timeEl(ts) {
        const el = document.createElement('div');
        el.className = 'msg-time';
        el.textContent = this.fmtTime(ts);
        if (ts) el.title = String(ts).replace('T', ' ');
        return el;
    },

    /** "via <agent>" label at the TOP of an answer, shown only in Auto mode:
     * it makes the router's per-message pick visible and verifiable. Placed
     * first (not under the timestamp) because the reader wants to know who is
     * speaking before reading what was said — on a long answer a footer label
     * is off-screen. Prepended: at `done` the bubble already holds the text.
     * Idempotent: live turns get it from the early `agent` event AND from
     * `done` (the fallback for a run started before this event existed). */
    _tagAgent(msgDiv, agentId) {
        if (msgDiv.querySelector(':scope > .msg-agent')) return;
        const el = document.createElement('div');
        el.className = 'msg-agent';
        el.textContent = i18n('chat.viaAgent', { name: this.agentName(agentId) });
        msgDiv.insertBefore(el, msgDiv.firstChild);
    },

    /** A server-side notice (e.g. the configured default model was down and
     * another one answered), placed just above the bubble it explains.
     * textContent, like every other server string we render. */
    insertNoticeBefore(anchor, text) {
        const div = document.createElement('div');
        div.className = 'msg msg-notice';
        div.textContent = text || '';
        div.appendChild(this._timeEl());
        const container = document.getElementById('chat-messages');
        if (anchor && anchor.parentNode === container) container.insertBefore(div, anchor);
        else if (container) container.appendChild(div);
    },

    // Only error banners come through here (assistant text streams via the
    // live-run path), so plain textContent is all it needs.
    appendMessage(role, content, ts) {
        const div = this.createMessageDiv(role);
        div.textContent = content;
        div.appendChild(this._timeEl(ts));
        document.getElementById('chat-messages').scrollTop =
            document.getElementById('chat-messages').scrollHeight;
    },

    appendUserMessage(text, attachments, ts) {
        const div = this.createMessageDiv('user');
        // Kept for the inline prompt editor, which re-sends this turn as-is
        // except for the text the user rewrites.
        div._text = text || '';
        div._attachments = (attachments || []).map(a => this._attachForSend(a));
        if (attachments && attachments.length) {
            const wrap = document.createElement('div');
            wrap.className = 'd-flex flex-wrap gap-2 mb-1';
            attachments.forEach(a => {
                if (a.kind === 'image') {
                    const img = document.createElement('img');
                    img.src = a.data;
                    img.style.cssText = 'max-height:120px;max-width:160px;border-radius:6px;object-fit:cover';
                    wrap.appendChild(img);
                } else {
                    const chip = document.createElement('span');
                    chip.className = 'badge text-bg-secondary';
                    chip.innerHTML = `<i class="bi bi-file-earmark-text"></i> ${App.esc(a.name)}`;
                    wrap.appendChild(chip);
                }
            });
            div.appendChild(wrap);
        }
        if (text) {
            const t = document.createElement('div');
            t.textContent = text;
            div.appendChild(t);
        }
        div.appendChild(this._timeEl(ts));
        const c = document.getElementById('chat-messages');
        c.scrollTop = c.scrollHeight;
    },

    // --- Per-message actions (copy / markdown source / edit / regenerate) ----

    // (Re)build the action bar of every message. Called after any render and at
    // the end of a stream, because two things change as the chat grows: which
    // assistant message is the last one (only that one can be regenerated) and
    // which user turn each bubble maps to.
    _decorateMessages() {
        const box = document.getElementById('chat-messages');
        if (!box) return;
        const users = box.querySelectorAll(':scope > .msg-user');
        // The nth user bubble is the nth user message in the stored session, so
        // the rewind endpoint can be addressed by ordinal — no message ids needed.
        users.forEach((div, i) => { div._turn = i; });
        const assistants = box.querySelectorAll(':scope > .msg-assistant');
        const last = assistants[assistants.length - 1] || null;
        users.forEach(div => this._ensureActions(div, 'user'));
        assistants.forEach(div => this._ensureActions(div, 'assistant', div === last));
    },

    _ensureActions(div, role, isLast) {
        const old = div.querySelector(':scope > .msg-actions');
        if (old) old.remove();
        if (div._editing) return;  // the editor has its own buttons
        const bar = document.createElement('div');
        bar.className = 'msg-actions';
        const add = (icon, title, fn) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'btn msg-act';
            b.title = title;
            b.innerHTML = `<i class="bi bi-${icon}"></i>`;
            b.onclick = (e) => fn(e.currentTarget);
            bar.appendChild(b);
        };

        if (role === 'user') {
            if (this.viewingArchived) return;  // archived chats are read-only
            add('pencil', i18n('chat.editPrompt'), () => this.startEdit(div));
        } else {
            add('clipboard', i18n('chat.copyMarkdown'), (btn) => this.copyMarkdown(div, btn));
            // The bubble may already be showing its source: keep the toggle's
            // icon in sync, since the bar is rebuilt on every render.
            const src = !!div.querySelector(':scope > .msg-flow > pre.md-source');
            add(src ? 'eye' : 'markdown',
                i18n(src ? 'chat.viewRendered' : 'chat.viewMarkdown'),
                (btn) => this.toggleSource(div, btn));
            if (isLast && !this.viewingArchived) {
                add('arrow-clockwise', i18n('chat.regenerate'), () => this.regenerate());
            }
        }
        if (bar.children.length) div.appendChild(bar);
    },

    // The message's markdown source. Falls back to the rendered text for
    // bubbles that predate _md (or were built by another path).
    _markdownOf(div) {
        if (typeof div._md === 'string') return div._md;
        const segs = div.querySelectorAll(
            ':scope > .msg-flow > .msg-content, :scope > .msg-content');
        return Array.from(segs).map(s => s.innerText).join('\n\n');
    },

    async copyMarkdown(div, btn) {
        const ok = await this._writeClipboard(this._markdownOf(div));
        if (!ok) {
            App.toast(i18n('chat.copyFailed'), 'danger');
            return;
        }
        const icon = btn.querySelector('i');
        const title = btn.title;
        if (icon) icon.className = 'bi bi-check-lg';
        btn.title = i18n('chat.copied');
        setTimeout(() => {
            if (icon) icon.className = 'bi bi-clipboard';
            btn.title = title;
        }, 1500);
    },

    async _writeClipboard(text) {
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
                return true;
            }
        } catch (e) { /* fall through to the legacy path */ }
        // Opened over plain http on the LAN (only localhost is a secure
        // context): the async clipboard is unavailable, so select and copy.
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;top:0;left:-9999px';
        document.body.appendChild(ta);
        ta.select();
        let ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        ta.remove();
        return ok;
    },

    // Swap an assistant bubble between the rendered answer and its raw markdown
    // source (readable and selectable as plain text). The text segments hide
    // and ONE source block appears; tool blocks stay visible, as before.
    toggleSource(div, btn) {
        const flow = div.querySelector(':scope > .msg-flow');
        if (!flow) return;
        const icon = btn.querySelector('i');
        const pre = flow.querySelector(':scope > pre.md-source');
        if (pre) {
            pre.remove();
            flow.querySelectorAll(':scope > .msg-content').forEach(s => { s.style.display = ''; });
            if (icon) icon.className = 'bi bi-markdown';
            btn.title = i18n('chat.viewMarkdown');
        } else {
            const p = document.createElement('pre');
            p.className = 'md-source';
            p.textContent = this._markdownOf(div);
            flow.querySelectorAll(':scope > .msg-content').forEach(s => { s.style.display = 'none'; });
            flow.appendChild(p);
            if (icon) icon.className = 'bi bi-eye';
            btn.title = i18n('chat.viewRendered');
        }
    },

    // --- Regenerate / edit prompt -------------------------------------------
    // Both work the same way: rewind the stored chat to a user turn (dropping
    // that turn and everything after it), then send a message again through the
    // normal path. The server only rewinds — it never re-runs the agent itself.

    async _rewind(userTurn) {
        try {
            return await App.api('POST', '/sessions/current/rewind', { user_turn: userTurn });
        } catch (e) {
            App.toast(i18n('chat.errorPrefix', { msg: e.message }), 'danger');
            return null;
        }
    },

    // Only the fields the API accepts: attachments read back from a session also
    // carry the workspace `path` the executor stamped on them when it wrote the
    // file, and re-sending re-materializes it anyway.
    _attachForSend(a) {
        return { name: a.name, kind: a.kind, data: a.data, mime: a.mime || null };
    },

    async regenerate() {
        if (this.sending || this.viewingArchived) return;
        const res = await this._rewind(-1);  // -1 = the last user turn
        if (!res) return;
        this.renderSessionMessages(res.session);
        await this._send(res.message.text, (res.message.attachments || []).map(a => this._attachForSend(a)));
    },

    // Turn a user bubble into an inline editor. Nothing is dropped until the
    // user confirms — then the chat is rewound to this turn and the edited text
    // is sent, so the answer (and anything that followed) is produced again.
    startEdit(div) {
        if (this.sending || this.viewingArchived || div._editing) return;
        div._editing = true;
        const original = div.innerHTML;
        const restore = () => {
            div._editing = false;
            div.classList.remove('editing');
            div.innerHTML = original;
            this._decorateMessages();
        };

        div.classList.add('editing');  // widen the bubble: it's a form now
        div.innerHTML = '';
        const ta = document.createElement('textarea');
        ta.className = 'form-control form-control-sm msg-edit';
        ta.value = div._text || '';
        ta.rows = Math.min(12, Math.max(2, (div._text || '').split('\n').length));
        div.appendChild(ta);

        const n = (div._attachments || []).length;
        if (n) {
            const note = document.createElement('div');
            note.className = 'msg-edit-note';
            note.textContent = i18n('chat.editAttachments', { n });
            div.appendChild(note);
        }
        if (div.nextElementSibling) {
            const warn = document.createElement('div');
            warn.className = 'msg-edit-note';
            warn.textContent = i18n('chat.editWarning');
            div.appendChild(warn);
        }

        const bar = document.createElement('div');
        bar.className = 'msg-actions';
        const send = document.createElement('button');
        send.type = 'button';
        send.className = 'btn btn-sm btn-light';
        send.textContent = i18n('chat.editSend');
        send.onclick = () => this._submitEdit(div, ta.value);
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'btn btn-sm btn-outline-light';
        cancel.textContent = i18n('common.cancel');
        cancel.onclick = restore;
        bar.append(send, cancel);
        div.appendChild(bar);

        ta.onkeydown = (e) => {
            const isTouch = window.matchMedia('(pointer: coarse)').matches;
            if (e.key === 'Enter' && !e.shiftKey && !isTouch) {
                e.preventDefault();
                this._submitEdit(div, ta.value);
            } else if (e.key === 'Escape') {
                e.preventDefault();
                restore();
            }
        };
        ta.focus();
        ta.setSelectionRange(ta.value.length, ta.value.length);
    },

    async _submitEdit(div, text) {
        const turn = div._turn;
        const message = (text || '').trim();
        if (this.sending || typeof turn !== 'number') return;
        if (!message && !(div._attachments || []).length) return;
        const res = await this._rewind(turn);
        if (!res) return;
        div._editing = false;
        this.renderSessionMessages(res.session);
        await this._send(message, (res.message.attachments || []).map(a => this._attachForSend(a)));
    },

    // --- Inline expandable tool calls (rendered directly in the chat) --------

    // Summary label markup shared by the live entry and the settled render,
    // so a running delegation already reads "call_agent → <agent>".
    _toolLabel(tool, args) {
        if (tool === 'call_agent' && args && args.agent_id) {
            return `<i class="bi bi-diagram-3"></i> ${App.esc(tool)} <span class="tc-arrow">→</span> ${App.esc(args.agent_id)}`;
        }
        return `<i class="bi bi-gear-wide-connected"></i> ${App.esc(tool || 'tool')}`;
    },

    // Add a live "running" tool-call entry during streaming (spinner in the
    // summary). Completed/filled in by completeLiveTool on tool_result. The
    // whole area is replaced by the authoritative recursive trace on 'done'.
    addLiveTool(container, data) {
        const det = document.createElement('details');
        det.className = 'tool-call running';
        det.innerHTML =
            `<summary><span class="tc-name">${this._toolLabel(data.tool, data.arguments)}</span>` +
            ` <span class="spinner-border spinner-border-sm ms-1"></span></summary>`;
        container.appendChild(det);
        return det;
    },

    completeLiveTool(container, data) {
        // :scope > : a running sub-agent's tools live INSIDE this level's
        // call_agent entry (.tool-call-body), so an unscoped query would close
        // the wrong <details> when parent and sub tools are interleaved.
        const running = container.querySelectorAll(':scope > details.tool-call.running');
        const det = running[running.length - 1];
        if (!det) {
            this.renderToolCall(container, {
                tool: data.tool, arguments: data.arguments, result: data.result_preview,
            });
            return;
        }
        det.classList.remove('running');
        const sp = det.querySelector(':scope > summary .spinner-border');
        if (sp) sp.remove();
        const icon = det.querySelector(':scope > summary .tc-name i');
        if (icon) icon.className = 'bi bi-check-circle text-success';
        let body = det.querySelector(':scope > .tool-call-body');
        if (body) {
            // call_agent whose sub-agent streamed live: its reply is already
            // in the body (renderToolCall's sub_trace branch also omits the
            // redundant result) — just settle the live header.
            const live = body.querySelector(':scope > .sub-agent.sub-agent-live');
            if (live) this._settleSubAgent(live);
            return;
        }
        body = document.createElement('div');
        body.className = 'tool-call-body';
        const args = document.createElement('div');
        args.className = 'tc-args';
        args.textContent = JSON.stringify(data.arguments || {});
        body.appendChild(args);
        const pre = document.createElement('pre');
        pre.className = 'tc-result';
        pre.textContent = data.result_preview || '';
        body.appendChild(pre);
        det.appendChild(body);
    },

    // --- Live sub-agent activity (agent_event envelopes) ---------------------
    //
    // A delegated sub-agent's tokens/tools stream as
    // {path: [ids...], event: {inner}} while the parent's call_agent entry is
    // still running. The live block mirrors the shape renderAgentTrace builds
    // from sub_trace, so the authoritative 'done' rebuild looks the same.

    _settleSubAgent(root) {
        root.classList.remove('sub-agent-live');
        const label = root.querySelector(':scope > .sub-agent-head .sa-label');
        if (label) label.textContent = i18n('chat.subAgent', { id: root.dataset.agentId || '?' });
    },

    // Resolve (or lazily create) the live .sub-agent container for a
    // delegation path, nesting inside the running call_agent <details> at
    // each level (recursive on the parent path, so depth ≥ 2 just nests).
    _liveSubContainer(toolsInline, subAgents, path) {
        const key = path.join('/');
        let entry = subAgents.get(key);
        if (entry) return entry;
        const parentScope = path.length === 1
            ? toolsInline
            : this._liveSubContainer(toolsInline, subAgents, path.slice(0, -1)).root;
        const running = parentScope.querySelectorAll(':scope > details.tool-call.running');
        const det = running[running.length - 1] || null;
        let host = parentScope;  // fallback: append flat, degrade but never break
        if (det) {
            let body = det.querySelector(':scope > .tool-call-body');
            if (!body) {
                body = document.createElement('div');
                body.className = 'tool-call-body';
                det.appendChild(body);
            }
            host = body;
            det.open = true;  // the whole point: show the activity while it runs
        }
        const id = path[path.length - 1] || '?';
        const root = document.createElement('div');
        root.className = 'sub-agent sub-agent-live';
        root.dataset.agentId = id;
        const head = document.createElement('div');
        head.className = 'sub-agent-head';
        head.innerHTML = `<i class="bi bi-robot"></i> <span class="sa-label"></span>`;
        head.querySelector('.sa-label').textContent = i18n('chat.subAgentLive', { id });
        root.appendChild(head);
        const replyEl = document.createElement('div');
        replyEl.className = 'sub-agent-reply';
        root.appendChild(replyEl);
        host.appendChild(root);
        entry = { root, head, replyEl, replyText: '', reasonEl: null, reasonText: '' };
        subAgents.set(key, entry);
        return entry;
    },

    handleAgentEvent(toolsInline, subAgents, data, msgDiv) {
        const path = (data && data.path) || [];
        const inner = (data && data.event) || {};
        if (!path.length || !toolsInline || !document.body.contains(toolsInline)) return;
        const entry = this._liveSubContainer(toolsInline, subAgents, path);
        switch (inner.type) {
            case 'token':
                // Plain text on purpose: cheap per-token, and it matches the
                // final renderAgentTrace shape ('↳ ' + reply, textContent).
                entry.replyText += inner.data || '';
                entry.replyEl.textContent = '↳ ' + entry.replyText;
                break;
            case 'clear_tokens':
                entry.replyText = '';
                entry.replyEl.textContent = '';
                break;
            case 'reasoning':
                // Same collapsed idiom as setReasoning, scoped to this block.
                if (!entry.reasonEl) {
                    const box = document.createElement('details');
                    box.className = 'msg-reasoning';
                    box.innerHTML = '<summary><i class="bi bi-lightbulb"></i> ' +
                        `<span class="rs-label">${App.esc(i18n('chat.reasoning'))}</span></summary>`;
                    const bodyEl = document.createElement('div');
                    bodyEl.className = 'reasoning-body';
                    box.appendChild(bodyEl);
                    entry.root.insertBefore(box, entry.head.nextSibling);
                    entry.reasonEl = bodyEl;
                }
                entry.reasonText += inner.data || '';
                entry.reasonEl.textContent = entry.reasonText;
                break;
            case 'tool_start': {
                const det = this.addLiveTool(entry.root, inner.data || {});
                entry.root.insertBefore(det, entry.replyEl);  // reply stays last
                break;
            }
            case 'tool_result':
                this.completeLiveTool(entry.root, inner.data || {});
                // A nested delegation settled: drop its live path keys, so a
                // later call to the same agent gets a fresh block (same rule
                // as the parent's subAgents.clear()).
                if (inner.data && inner.data.tool === 'call_agent') {
                    const prefix = path.join('/') + '/';
                    for (const k of [...subAgents.keys()]) {
                        if (k.startsWith(prefix)) subAgents.delete(k);
                    }
                }
                // Same live resource strip as the parent's tool_result case.
                if (inner.data && inner.data.resources) {
                    msgDiv._resources = this.collectResources([inner.data], msgDiv._resources);
                    this.renderResources(msgDiv, msgDiv._resources);
                }
                break;
            case 'error': {
                const err = document.createElement('div');
                err.className = 'text-danger small';
                err.textContent = String(inner.data || '');
                entry.root.insertBefore(err, entry.replyEl);
                break;
            }
        }
    },

    // Authoritative settle on 'done': replace each live tool entry with its
    // trace step render (full result instead of the preview, recursive
    // sub_trace instead of the live approximation) — IN PLACE, so the
    // interleaved text/tool layout built during streaming survives. Steps map
    // 1:1 in document order to live entries: the executor emits exactly one
    // tool_start per trace step (dedup-skipped calls emit neither).
    _settleFlowTools(flow, steps) {
        const groups = Array.from(flow.querySelectorAll(':scope > .tool-calls'));
        const lives = [];
        for (const g of groups) {
            lives.push(...g.querySelectorAll(':scope > details.tool-call'));
        }
        const scratch = document.createElement('div');
        steps.forEach((step, i) => {
            const det = this.renderToolCall(scratch, step);
            const live = lives[i];
            if (live) {
                det.open = live.open;  // don't collapse what the user expanded
                live.replaceWith(det);
            } else {
                // No live entry for this step (an attach that raced the event
                // buffer): still show it, appended to the last group.
                let g = groups[groups.length - 1];
                if (!g) {
                    g = document.createElement('div');
                    g.className = 'tool-calls';
                    flow.appendChild(g);
                    groups.push(g);
                }
                g.appendChild(det);
            }
        });
        // Extra live entries beyond the steps (a stopped/raced stream) are
        // left as-is: they still show what actually ran.
    },

    // Render one tool call as an expandable <details>. For call_agent, recurses
    // into the called agent's own trace, so arbitrarily nested agents/calls are
    // all shown (each collapsible on its own).
    renderToolCall(container, step) {
        if (!container) return;
        const args = step.arguments || {};
        const det = document.createElement('details');
        det.className = 'tool-call';

        const summary = document.createElement('summary');
        summary.innerHTML = `<span class="tc-name">${this._toolLabel(step.tool, args)}</span>`;
        det.appendChild(summary);

        const body = document.createElement('div');
        body.className = 'tool-call-body';

        const argsEl = document.createElement('div');
        argsEl.className = 'tc-args';
        argsEl.textContent = JSON.stringify(args);
        body.appendChild(argsEl);

        // For call_agent the result duplicates the sub-agent's final reply,
        // which the nested trace already shows — so render the sub-agent flow
        // instead of a redundant result block.
        if (step.sub_trace) {
            body.appendChild(this.renderAgentTrace(step.sub_trace));
        } else {
            const pre = document.createElement('pre');
            pre.className = 'tc-result';
            pre.textContent = (step.result != null ? step.result : (step.result_preview || ''));
            body.appendChild(pre);
        }

        det.appendChild(body);
        container.appendChild(det);
        return det;
    },

    // Render a sub-agent's trace: header + its tool calls (recursive) + reply.
    renderAgentTrace(trace) {
        const wrap = document.createElement('div');
        wrap.className = 'sub-agent';
        const head = document.createElement('div');
        head.className = 'sub-agent-head';
        head.innerHTML = `<i class="bi bi-robot"></i> ${App.esc(i18n('chat.subAgent', { id: trace.agent_id || '?' }))}`;
        wrap.appendChild(head);
        for (const step of (trace.steps || [])) this.renderToolCall(wrap, step);
        if (trace.reply) {
            const rep = document.createElement('div');
            rep.className = 'sub-agent-reply';
            rep.textContent = '↳ ' + trace.reply;
            wrap.appendChild(rep);
        }
        return wrap;
    },

    // --- Tool resources (files a tool delivered to the chat) ----------------
    //
    // A trace step / persisted tool message may carry `resources`:
    // [{path, mime, title, size}] — workspace files flagged by a tool through
    // the resource marker (see server/app/tools/resources.py). Three render
    // paths must agree: live SSE tool_result, the 'done' rebuild from the
    // trace, and session reload. The strip sits OUTSIDE the collapsed tool
    // <details>, so the user sees the image without expanding anything.

    // Walk steps (recursively through sub_trace, so a sub-agent's images reach
    // the caller's bubble) and accumulate resources, deduped by path. `acc`
    // lets the live path grow one list across events; mutated and returned.
    collectResources(steps, acc) {
        const list = acc || [];
        const seen = new Set(list.map(r => r && r.path));
        const walk = (ss) => {
            for (const s of (ss || [])) {
                for (const r of (s.resources || [])) {
                    if (r && r.path && !seen.has(r.path)) {
                        seen.add(r.path);
                        list.push(r);
                    }
                }
                if (s.sub_trace) walk(s.sub_trace.steps);
            }
        };
        walk(steps);
        return list;
    },

    // Create/refresh the message's resource strip (idempotent: rebuilt on each
    // call, so live growth and the 'done' rebuild both just re-render).
    renderResources(msgDiv, list) {
        if (!msgDiv || !list || !list.length) return;
        let strip = msgDiv.querySelector(':scope > .msg-resources');
        if (!strip) {
            strip = document.createElement('div');
            strip.className = 'msg-resources';
            // Right after the flow (text before files, same rule as the
            // connectors), before the timestamp when it exists.
            const flow = msgDiv.querySelector(':scope > .msg-flow');
            msgDiv.insertBefore(strip, flow ? flow.nextSibling : null);
        }
        strip.innerHTML = '';
        for (const r of list) strip.appendChild(this._resourceItem(r));
    },

    _resourceItem(r) {
        const mime = String(r.mime || '');
        const name = String(r.path || '').split('/').pop();
        const title = r.title || name;

        if (mime.startsWith('image/')) {
            const a = document.createElement('a');
            a.className = 'res-thumb';
            a.href = App.fileUrl(r.path);
            a.target = '_blank';
            a.rel = 'noopener';
            a.title = title;
            const img = document.createElement('img');
            img.src = App.fileUrl(r.path);
            img.alt = title;
            img.loading = 'lazy';
            // Dangling reference (the file was cleaned from _resources/):
            // degrade to a labeled chip instead of a broken-image icon.
            img.onerror = () => {
                const chip = document.createElement('span');
                chip.className = 'res-missing';
                chip.textContent = `${title} — ${i18n('chat.resourceMissing')}`;
                a.replaceWith(chip);
            };
            a.appendChild(img);
            return a;
        }

        // text/html previews inline (same scheme as viewer.html: Bearer fetch
        // + srcdoc sandbox, the api key never enters a URL a page's own
        // scripts could read). Oversized pages fall back to the card — a
        // heavy page in every reloaded session would drag the whole chat.
        const isHtml = mime === 'text/html';
        if (isHtml && (r.size || 0) <= 512 * 1024) {
            return this._htmlPreview(r, title);
        }

        // Non-image: a small card. HTML opens through viewer.html; anything
        // else is a download link.
        const a = document.createElement('a');
        a.className = 'res-card';
        a.target = '_blank';
        a.rel = 'noopener';
        if (isHtml) {
            a.href = 'viewer.html?path=' + encodeURIComponent(r.path)
                + '&title=' + encodeURIComponent(title);
        } else {
            a.href = App.fileUrl(r.path, true);
        }
        const icon = document.createElement('i');
        icon.className = isHtml ? 'bi bi-window-fullscreen'
                                : 'bi bi-file-earmark-arrow-down';
        a.appendChild(icon);
        const label = document.createElement('span');
        label.className = 'res-title';
        label.textContent = title;
        a.appendChild(label);
        const hint = document.createElement('span');
        hint.className = 'res-hint';
        hint.textContent = isHtml
            ? i18n('chat.resourceOpen')
            : `${i18n('chat.resourceDownload')}${r.size ? ' · ' + this._humanSize(r.size) : ''}`;
        a.appendChild(hint);
        return a;
    },

    // Collapsible inline preview of an HTML resource. The iframe is
    // sandbox="allow-scripts" WITHOUT allow-same-origin and fed via srcdoc:
    // the page's own scripts run, but in an opaque origin — no localStorage
    // (where the api key lives), no credentialed calls. Content is fetched
    // with the Bearer header, so no URL ever carries the key.
    _htmlPreview(r, title) {
        const det = document.createElement('details');
        det.className = 'res-html';
        det.open = true;
        const sum = document.createElement('summary');
        const icon = document.createElement('i');
        icon.className = 'bi bi-window-fullscreen';
        sum.appendChild(icon);
        const label = document.createElement('span');
        label.className = 'res-title';
        label.textContent = title;
        sum.appendChild(label);
        const open = document.createElement('a');
        open.className = 'res-open';
        open.href = 'viewer.html?path=' + encodeURIComponent(r.path)
            + '&title=' + encodeURIComponent(title);
        open.target = '_blank';
        open.rel = 'noopener';
        open.textContent = i18n('chat.resourceOpen');
        // A link inside <summary> would also toggle the fold.
        open.onclick = (e) => e.stopPropagation();
        sum.appendChild(open);
        det.appendChild(sum);
        const body = document.createElement('div');
        body.className = 'res-html-body';
        body.textContent = '…';
        det.appendChild(body);
        const p = r.path.split('/').map(encodeURIComponent).join('/');
        fetch(App.apiUrl('/files/' + p), { headers: App.authHeaders() })
            .then(res => {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.text();
            })
            .then(html => {
                const f = document.createElement('iframe');
                f.setAttribute('sandbox', 'allow-scripts');
                f.className = 'res-iframe';
                // Grow the iframe to its content so the preview has no inner
                // scrollbar. The parent CANNOT measure it (no
                // allow-same-origin — reading contentDocument back would cost
                // exactly the isolation that keeps the api key safe), so a
                // probe injected into the page posts its height out instead.
                this._ensureIframeResizer();
                f.srcdoc = html + this._HEIGHT_PROBE;
                body.textContent = '';
                body.appendChild(f);
            })
            .catch(() => {
                const chip = document.createElement('span');
                chip.className = 'res-missing';
                chip.textContent = `${title} — ${i18n('chat.resourceMissing')}`;
                body.textContent = '';
                body.appendChild(chip);
            });
        return det;
    },

    // Appended to every previewed page: reports the document height to the
    // parent on load and on every resize (charts render late, images load
    // late — the two timeouts catch what ResizeObserver misses at startup).
    // targetOrigin must be '*': from inside the opaque origin the page cannot
    // know who embeds it.
    _HEIGHT_PROBE:
        '<script>(function(){var p=function(){try{parent.postMessage({myagentResourceHeight:' +
        'document.documentElement.scrollHeight},"*")}catch(e){}};' +
        'addEventListener("load",p);setTimeout(p,50);setTimeout(p,500);' +
        'if(window.ResizeObserver)new ResizeObserver(p).observe(document.documentElement)})()' +
        '<' + '/script>',

    // One window-level listener for every preview iframe. The sender is
    // untrusted (it IS the generated page): the only thing taken from the
    // message is a number, clamped — past the ceiling the iframe keeps its
    // own scrollbar, so a huge report cannot swallow the chat.
    _ensureIframeResizer() {
        if (this._iframeResizerBound) return;
        this._iframeResizerBound = true;
        window.addEventListener('message', (e) => {
            const h = e.data && e.data.myagentResourceHeight;
            if (typeof h !== 'number' || !isFinite(h) || h <= 0) return;
            for (const f of document.querySelectorAll('iframe.res-iframe')) {
                if (f.contentWindow === e.source) {
                    const max = Math.round(window.innerHeight * 0.65);
                    f.style.height = Math.max(120, Math.min(Math.ceil(h) + 4, max)) + 'px';
                    break;
                }
            }
        });
    },

    _humanSize(n) {
        if (!n && n !== 0) return '';
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
        return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    },

    // ---- Math -----------------------------------------------------------
    //
    // KaTeX needs the RAW LaTeX, so math is pulled OUT of the source before
    // App.esc() runs and put back as rendered MathML at the very end, under
    // \x02 placeholders. The vendored KaTeX is MathML-only: no stylesheet and
    // no font files, the browser draws the <math> it emits.

    // Opening delimiter -> [closing, display mode]. Longest first: `$$` must
    // be tried before `$`.
    _MATH_DELIMS: [
        ['$$', '$$', true],
        ['\\[', '\\]', true],
        ['\\(', '\\)', false],
        ['$', '$', false],
    ],

    // Rewrite `text`, replacing every math span with a \x02<n>\x02 placeholder
    // and pushing {html, display} onto `math`. Fenced blocks and code spans are
    // copied verbatim: a `$` inside code is a dollar sign, never a delimiter.
    _extractMath(text, math) {
        if (typeof katex === 'undefined' || !/[$\\]/.test(text)) return text;
        let out = '';
        let i = 0;
        while (i < text.length) {
            if (text.startsWith('```', i)) {
                const end = text.indexOf('```', i + 3);
                const stop = end === -1 ? text.length : end + 3;
                out += text.slice(i, stop); i = stop; continue;
            }
            if (text[i] === '`') {
                const end = text.indexOf('`', i + 1);
                const stop = end === -1 ? text.length : end + 1;
                out += text.slice(i, stop); i = stop; continue;
            }
            if (text[i] === '\\' && text[i + 1] === '$') { out += '$'; i += 2; continue; }
            const delim = this._MATH_DELIMS.find(d => text.startsWith(d[0], i));
            if (delim) {
                const [open, close, display] = delim;
                const span = this._mathBody(text, i + open.length, close, display);
                const html = span && this._renderMath(span.body, display);
                if (html) {
                    out += `\x02${math.push({ html, display }) - 1}\x02`;
                    i = span.end;
                    continue;
                }
            }
            out += text[i]; i++;
        }
        return out;
    },

    // Locate the closing delimiter and return {body, end}, or null when this is
    // not math after all — unterminated (which is the normal state mid-stream:
    // it stays literal text until the closing delimiter arrives) or a stray `$`
    // in prose.
    _mathBody(text, from, close, display) {
        const single = close === '$';
        // "costa $5 e $10 euro": a `$` hugging whitespace is currency, not math.
        if (single && /^\s|^$/.test(text[from] || '')) return null;
        let j = from;
        for (;;) {
            j = text.indexOf(close, j);
            if (j === -1) return null;
            if (single && text[j - 1] === '\\') { j += 1; continue; }
            break;
        }
        const body = text.slice(from, j);
        if (!body.trim()) return null;
        if (single && (/\s$/.test(body) || /\d/.test(text[j + 1] || ''))) return null;
        // Inline math never crosses a blank line.
        if (!display && /\n\s*\n/.test(body)) return null;
        return { body, end: j + close.length };
    },

    // Rendered formulas are memoized: renderMarkdown re-runs on EVERY streamed
    // token, so without this a message with ten formulas re-parses all ten of
    // them hundreds of times over one answer. Keyed on the exact source, so a
    // formula still growing mid-stream simply misses until it settles.
    _mathCache: new Map(),
    _MATH_CACHE_MAX: 300,

    _renderMath(tex, display) {
        const key = (display ? 'D' : 'I') + tex;
        if (this._mathCache.has(key)) return this._mathCache.get(key);
        const html = this._renderMathUncached(tex, display);
        if (this._mathCache.size >= this._MATH_CACHE_MAX) this._mathCache.clear();
        this._mathCache.set(key, html);
        return html;
    },

    _renderMathUncached(tex, display) {
        try {
            const html = katex.renderToString(tex, {
                output: 'mathml',     // no CSS, no fonts: the browser renders it
                displayMode: display,
                throwOnError: false,  // bad LaTeX shows in red, never breaks the turn
                trust: false,         // \href / \includegraphics stay disabled
                strict: false,
            });
            // Wrapped, so a long equation scrolls instead of widening the bubble.
            return display ? `<div class="math-block">${html}</div>` : html;
        } catch (e) {
            return null;              // fall back to the literal source text
        }
    },

    // Inline markdown: bold, italic, strikethrough, inline code, links.
    // Operates on already HTML-escaped text.
    renderInline(s) {
        if (!s) return '';
        // Protect inline code spans so emphasis markers inside them are literal
        const codes = [];
        s = s.replace(/`([^`]+)`/g, (m, c) => {
            codes.push(c);
            return `\x01${codes.length - 1}\x01`;
        });
        s = s
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
            .replace(/~~([^~]+)~~/g, '<del>$1</del>')
            // Images: ONLY our own delivered resources (_resources/<name>, one
            // path segment, safe charset) render as <img> — the model can place
            // a tool's image inside its answer. External URLs and any other
            // path stay escaped text: no tracking pixels, no traversal.
            .replace(/!\[([^\]]*)\]\((_resources\/[A-Za-z0-9_.-]+)\)/g, (m, alt, path) =>
                `<img class="md-img" src="${App.fileUrl(path).replace(/"/g, '%22')}"` +
                ` alt="${alt.replace(/"/g, '&quot;')}" loading="lazy">`)
            // Links: the URL is untrusted (model/tool output). App.esc only
            // escapes <>&, NOT quotes, so a " in the URL could break out of the
            // href attribute and inject event handlers — neutralize quotes.
            .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (m, text, url) =>
                `<a href="${url.replace(/"/g, '%22')}" target="_blank" rel="noopener">${text}</a>`);
        s = s.replace(/\x01(\d+)\x01/g, (m, n) => `<code>${codes[parseInt(n, 10)]}</code>`);
        return s;
    },

    // Block-level markdown with GitHub-style pipe tables. Renders incrementally
    // so partial markdown during token streaming still displays sensibly.
    renderMarkdown(text) {
        if (!text) return '';
        // Math first: it is read from the UNESCAPED source and comes back as
        // MathML at the bottom of this function.
        const math = [];
        let src = App.esc(this._extractMath(text, math));

        // Protect fenced code blocks
        const blocks = [];
        src = src.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
            blocks.push(`<pre><code>${code.replace(/\n$/, '')}</code></pre>`);
            return `\x00${blocks.length - 1}\x00`;
        });

        const lines = src.split('\n');
        const out = [];
        const inline = (s) => this.renderInline(s);
        const isTableSep = (l) => l && l.includes('-') && /^\s*\|?[\s:|-]+\|?\s*$/.test(l);
        // Display math standing on its own line is a block, not a paragraph.
        const displayMath = (l) => {
            const m = (l || '').trim().match(/^\x02(\d+)\x02$/);
            return m && math[parseInt(m[1], 10)].display ? math[parseInt(m[1], 10)].html : null;
        };
        const parseRow = (r) => r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
        let i = 0;

        while (i < lines.length) {
            const line = lines[i];
            const trimmed = line.trim();

            // Protected code block
            if (/^\x00\d+\x00$/.test(trimmed)) { out.push(trimmed); i++; continue; }

            // Display math on a line of its own
            const dm = displayMath(line);
            if (dm) { out.push(dm); i++; continue; }

            // Table: a row with pipes followed by a separator row
            if (line.includes('|') && isTableSep(lines[i + 1])) {
                const header = parseRow(line);
                i += 2;
                const rows = [];
                while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
                    rows.push(parseRow(lines[i]));
                    i++;
                }
                let t = '<table class="md-table"><thead><tr>' +
                    header.map(h => `<th>${inline(h)}</th>`).join('') + '</tr></thead><tbody>';
                for (const row of rows) {
                    t += '<tr>' + header.map((_, c) => `<td>${inline(row[c] || '')}</td>`).join('') + '</tr>';
                }
                out.push(t + '</tbody></table>');
                continue;
            }

            // Heading
            const hm = trimmed.match(/^(#{1,6})\s+(.*)$/);
            if (hm) { out.push(`<h${hm[1].length}>${inline(hm[2])}</h${hm[1].length}>`); i++; continue; }

            // Horizontal rule
            if (/^([-*_])\1{2,}$/.test(trimmed)) { out.push('<hr>'); i++; continue; }

            // Blockquote
            if (/^\s*&gt;\s?/.test(line)) {
                const q = [];
                while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) { q.push(lines[i].replace(/^\s*&gt;\s?/, '')); i++; }
                out.push(`<blockquote>${inline(q.join(' '))}</blockquote>`);
                continue;
            }

            // Unordered list
            if (/^\s*[-*+]\s+/.test(line)) {
                const items = [];
                while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*+]\s+/, '')); i++; }
                out.push('<ul>' + items.map(it => `<li>${inline(it)}</li>`).join('') + '</ul>');
                continue;
            }

            // Ordered list
            if (/^\s*\d+[.)]\s+/.test(line)) {
                const items = [];
                while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+[.)]\s+/, '')); i++; }
                out.push('<ol>' + items.map(it => `<li>${inline(it)}</li>`).join('') + '</ol>');
                continue;
            }

            // Blank line
            if (trimmed === '') { i++; continue; }

            // Paragraph: gather consecutive plain lines
            const para = [];
            while (i < lines.length) {
                const l = lines[i];
                if (l.trim() === '' || /^\x00\d+\x00$/.test(l.trim()) || displayMath(l) ||
                    /^(#{1,6})\s+/.test(l.trim()) || /^\s*[-*+]\s+/.test(l) ||
                    /^\s*\d+[.)]\s+/.test(l) || /^\s*&gt;\s?/.test(l) ||
                    (l.includes('|') && isTableSep(lines[i + 1]))) break;
                para.push(l);
                i++;
            }
            out.push(`<p>${inline(para.join('<br>'))}</p>`);
        }

        let html = out.join('\n');
        html = html.replace(/\x00(\d+)\x00/g, (m, n) => blocks[parseInt(n, 10)]);
        html = html.replace(/\x02(\d+)\x02/g, (m, n) => math[parseInt(n, 10)].html);
        return html;
    },
};
