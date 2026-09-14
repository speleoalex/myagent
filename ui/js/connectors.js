// Connectors page: channels bound to agents — a messaging bot (Telegram) or a
// device that calls us (the voice satellite) — plus the address book of people
// you can authorize on them. The wording stays "connector" and never "bot": a
// kitchen speaker is neither. Served by the connectors plugin — when it isn't
// installed these endpoints don't exist, so every view starts by checking
// App.plugin('connectors') and says so explicitly instead of rendering an empty
// list, which would read as "you have no connectors".

const ConnectorsPage = {
    async render(params) {
        const info = await App.plugin('connectors');
        if (!info || !info.loaded) return this.renderUnavailable(info);
        // 'contacts' must be tested before the catch-all id below, or the
        // address book would be looked up as a binding called "contacts".
        if (params[0] === 'contacts') return this.renderContacts(params.slice(1));
        if (params[0] === 'new') return this.renderForm();
        if (params[0]) return this.renderForm(params[0]);
        return this.renderList();
    },

    // Plain buttons, not .nav-link: updateActiveNav() matches by href prefix and
    // would light both pills up on #/connectors/contacts.
    tabs(active) {
        const pill = (id, href, label, icon) => `
            <a href="${href}" class="btn btn-sm ${active === id ? 'btn-primary' : 'btn-outline-secondary'}">
                <i class="bi ${icon}"></i> ${label}
            </a>`;
        return `<div class="d-flex flex-wrap gap-2 mb-3">
            ${pill('bindings', '#/connectors', i18n('connectors.title'), 'bi-plug')}
            ${pill('contacts', '#/connectors/contacts', i18n('connectors.contactsTitle'), 'bi-person-lines-fill')}
        </div>`;
    },

    renderUnavailable(info) {
        const broken = info && !info.loaded;
        App.container.innerHTML = `
            <div class="row"><div class="col-lg-8 mx-auto">
                <div class="alert alert-warning mt-3">
                    <h5><i class="bi bi-plug"></i> ${i18n('connectors.unavailable')}</h5>
                    <p class="mb-2">${i18n('connectors.unavailableHint', { path: '~/myagent/plugins/connectors/' })}</p>
                    <pre class="small mb-0">bash connectors/install.sh</pre>
                </div>
                ${broken ? `
                <div class="alert alert-danger">
                    <strong>${i18n('connectors.loadFailed')}</strong>
                    <pre class="small font-monospace mb-0 mt-2">${App.esc(info.error)}</pre>
                </div>` : ''}
            </div></div>`;
    },

    // -------------------------------------------------------------- bindings
    _loading(tab) {
        App.container.innerHTML = this.tabs(tab) + `
            <div class="text-secondary"><span class="spinner-border spinner-border-sm"></span>
            ${i18n('common.loading')}</div>`;
    },

    async renderList() {
        this._loading('bindings');
        let bindings;
        try {
            bindings = await App.api('GET', '/connectors/bindings');
        } catch (err) {
            // An explicit error, NOT an empty list: a server that is down must
            // never be reported as "no connectors configured".
            App.container.innerHTML = this.tabs('bindings') + `
                <div class="alert alert-danger">${i18n('connectors.loadError', { msg: App.esc(err.message) })}</div>`;
            return;
        }

        const header = `
            <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-2">
                <h3 class="mb-0"><i class="bi bi-plug"></i> ${i18n('connectors.title')}</h3>
                <a href="#/connectors/new" class="btn btn-primary"><i class="bi bi-plus-lg"></i> ${i18n('connectors.new')}</a>
            </div>
            <p class="text-secondary">${i18n('connectors.hint')}</p>`;

        const body = bindings.length === 0
            ? `<div class="alert alert-secondary">
                   <strong>${i18n('connectors.empty')}</strong><br>${i18n('connectors.emptyHint')}
               </div>`
            : `<div class="table-responsive"><table class="table table-hover align-middle">
                   <thead><tr>
                       <th>${i18n('common.id')}</th>
                       <th>${i18n('common.name')}</th>
                       <th>${i18n('common.status')}</th>
                       <th>${i18n('connectors.colMessages')}</th>
                       <th></th>
                   </tr></thead>
                   <tbody>${bindings.map(b => this.row(b)).join('')}</tbody>
               </table></div>`;

        App.container.innerHTML = this.tabs('bindings') + header + body;
        if (bindings.length) {
            App.setPageInterval(() => this.tick(), 5000);
        }
    },

    row(b) {
        const sub = [b.type, b.agent_id, b.access_mode].filter(Boolean).join(' · ');
        return `
            <tr>
                <td><code>${App.esc(b.id)}</code></td>
                <td>
                    ${App.esc(b.name || b.id)}
                    <div class="small text-secondary text-truncate" style="max-width:18rem"
                         title="${App.escAttr(sub)}">${App.esc(sub)}</div>
                </td>
                <td data-state="${App.escAttr(b.id)}">${this.stateBadge(b)}</td>
                <td data-msgs="${App.escAttr(b.id)}">${b.status?.messages ?? 0}</td>
                <td class="text-end text-nowrap">
                    <a href="#/connectors/${encodeURIComponent(b.id)}" class="btn btn-sm btn-outline-primary">
                        ${i18n('common.edit')}
                    </a>
                </td>
            </tr>`;
    },

    stateBadge(b) {
        if (b.enabled === false) {
            return `<span class="badge bg-secondary">${i18n('mcp.stateDisabled')}</span>`;
        }
        const state = b.status?.state || 'stopped';
        const detail = b.status?.detail || '';
        const map = {
            running: ['bg-success', 'connectors.stateRunning'],
            error: ['bg-danger', 'connectors.stateError'],
            paused: ['bg-danger', 'connectors.statePaused'],
            starting: ['bg-warning text-dark', 'connectors.stateStarting'],
        };
        const [cls, key] = map[state] || ['bg-secondary', 'connectors.stateStopped'];
        let html = `<span class="badge ${cls}">${i18n(key)}</span>`;
        if ((state === 'error' || state === 'paused') && detail) {
            html += `<div class="small text-danger text-truncate" style="max-width:16rem"
                          title="${App.escAttr(detail)}">${App.esc(detail)}</div>`;
        }
        return html;
    },

    /** Refresh only the live cells. Re-rendering the whole list every 5s would
     * throw away scroll position and any focus the user has. */
    async tick() {
        let bindings;
        try {
            bindings = await App.api('GET', '/connectors/bindings');
        } catch (e) {
            return;  // transient: keep showing the last good list
        }
        for (const b of bindings) {
            const cell = App.container.querySelector(`[data-state="${CSS.escape(b.id)}"]`);
            if (!cell) return this.renderList();  // the set of connectors changed
            cell.innerHTML = this.stateBadge(b);
            const msgs = App.container.querySelector(`[data-msgs="${CSS.escape(b.id)}"]`);
            if (msgs) msgs.textContent = b.status?.messages ?? 0;
        }
    },

    // ------------------------------------------------------- connector form
    async renderForm(bindingId) {
        const isEdit = !!bindingId;
        let b = {
            id: '', name: '', type: 'telegram', enabled: true, agent_id: '',
            token: '', url: '', access_mode: 'allowlist', allowed_ids: [],
            allowed_usernames: [], password: '', session_prefix: '',
            welcome: '', help_text: '', disclose_ai: true, ai_disclosure: '',
            settings: {},
        };
        if (isEdit) {
            try {
                b = await App.api('GET', `/connectors/bindings/${encodeURIComponent(bindingId)}`);
            } catch (err) {
                App.toast(err.message, 'danger');
                location.hash = '#/connectors';
                return;
            }
        }
        // Both lists are optional extras: the form must still work if either
        // fetch fails, so a failure degrades that one control, not the page.
        const [types, agents, contacts] = await Promise.all([
            App.api('GET', '/connectors/bindings/types').catch(() => ({ types: [] })),
            App.api('GET', '/agents?selectable=true').catch(() => []),
            App.api('GET', '/connectors/contacts').catch(() => []),
        ]);
        // Each channel declares its label and which hint keys to show, so the
        // form stops telling every channel to go ask @BotFather.
        this._types = (types.types || []).length ? types.types
                                                 : [{ type: b.type || 'telegram', label: b.type || 'telegram' }];

        const allowed = [
            ...(b.allowed_ids || []),
            ...(b.allowed_usernames || []).map(u => '@' + u),
        ].join(', ');

        // One card per installed channel instead of a <select>: the type is the
        // FIRST decision and everything below follows it, so it deserves more
        // than a dropdown row. The hidden #f-type keeps the value for the code
        // that reads the form (_channelType, _readForm).
        const typeCards = this._types.map(t => `
            <button type="button" class="btn type-card ${t.type === b.type ? 'active' : ''}"
                    data-type="${App.escAttr(t.type)}" aria-pressed="${t.type === b.type}">
                <i class="bi ${App.escAttr(t.icon || 'bi-plug')}"></i>
                <span>${App.esc(t.label || t.type)}</span>
            </button>`).join('');

        // The form reads as numbered steps (CSS counters on .setup-step, so a
        // step hidden for a device channel does not leave a gap): channel →
        // credentials → agent → who can write → messages. That is the order in
        // which someone actually sets a bot up, and the channel's own guide
        // sits under the type cards, right where the person has just chosen it.
        App.container.innerHTML = `
        <div class="row"><div class="col-lg-8 mx-auto">
            <h3 class="mb-3">${isEdit ? i18n('connectors.editTitle') : i18n('connectors.newTitle')}</h3>
            <form id="binding-form" class="setup-steps" novalidate>

                <section class="setup-step" id="step-channel">
                    <h5 class="setup-step-title">${i18n('connectors.stepChannel')}</h5>
                    <input type="hidden" id="f-type" value="${App.escAttr(b.type)}">
                    <div class="type-cards" role="group" aria-label="${App.escAttr(i18n('connectors.type'))}">${typeCards}</div>
                    <div id="block-guide"></div>
                </section>

                <section class="setup-step" id="step-credentials">
                    <h5 class="setup-step-title">${i18n('connectors.stepCredentials')}</h5>

                    <!-- The secret. A password field (browsers' own password managers
                         are kept out with autocomplete=new-password: one of them
                         silently overwriting the token is not a bug we can see), with
                         an eye to reveal it. _renderSettings moves this block INTO the
                         first section, right after its first field, when the channel
                         declares sections: the mailbox password beside the mailbox
                         login (see _placeToken). -->
                    <div class="mb-3" id="block-token">
                        <label class="form-label" for="f-token" id="token-label"
                               >${i18n(this._label('token', 'connectors.token', b.type))}</label>
                        <div class="input-group">
                            <input type="password" class="form-control font-monospace" id="f-token"
                                   autocomplete="new-password" spellcheck="false" value="${App.escAttr(b.token)}">
                            <button type="button" class="btn btn-outline-secondary" id="btn-token-eye"
                                    title="${i18n('connectors.tokenShow')}" aria-label="${i18n('connectors.tokenShow')}">
                                <i class="bi bi-eye"></i>
                            </button>
                            <button type="button" class="btn btn-outline-info" id="btn-test">
                                <i class="bi bi-plug"></i> ${i18n('connectors.test')}
                            </button>
                        </div>
                        <div class="form-text" id="token-hint">${i18n(this._hint('token', 'connectors.tokenHint', b.type))}</div>
                        <div id="test-result" class="mt-2"></div>
                    </div>

                    <!-- Per-channel settings: fieldsets driven by the manifest
                         (sections / settings), rendered by _renderSettings(). -->
                    <div class="mb-3 d-none" id="block-settings"></div>

                    <div class="mb-3 ${this._channel(b.type).url ? '' : 'd-none'}" id="block-url">
                        <label class="form-label" for="f-url">${i18n('connectors.url')}</label>
                        <input type="text" class="form-control" id="f-url" value="${App.escAttr(b.url || '')}"
                               placeholder="${App.escAttr(this._channel(b.type).url?.example || '')}">
                        <div class="form-text" id="url-hint">${i18n(this._hint('url', 'connectors.urlHint', b.type))}</div>
                    </div>

                    <div class="mb-3 d-none" id="block-device"></div>
                </section>

                <section class="setup-step" id="step-agent">
                    <h5 class="setup-step-title">${i18n('connectors.stepAgent')}</h5>
                    <div class="mb-3">
                        <label class="form-label" for="f-agent">${i18n('connectors.agent')}</label>
                        <select class="form-select" id="f-agent">
                            <option value="">${i18n('connectors.agentNone')}</option>
                            <option value="auto" ${b.agent_id === 'auto' ? 'selected' : ''}>${i18n('chat.agentAuto')}</option>
                            ${agents.map(a =>
                                `<option value="${App.escAttr(a.id)}" ${a.id === b.agent_id ? 'selected' : ''}>${App.esc(a.name || a.id)}</option>`
                            ).join('')}
                            ${b.agent_id && b.agent_id !== 'auto' && !agents.some(a => a.id === b.agent_id)
                                ? `<option value="${App.escAttr(b.agent_id)}" selected>${App.esc(b.agent_id)} — ${i18n('agents.bindingMissing')}</option>`
                                : ''}
                        </select>
                        <div class="form-text">${i18n('connectors.agentHint')}</div>
                    </div>
                    <div class="row g-3 mb-3">
                        <div class="col-12 col-md-6">
                            <label class="form-label" for="f-name">${i18n('common.name')}</label>
                            <input type="text" class="form-control" id="f-name" value="${App.escAttr(b.name)}"
                                   placeholder="${App.escAttr(i18n('connectors.namePlaceholder'))}">
                            <div class="form-text">${i18n('connectors.nameHint')}</div>
                        </div>
                        <div class="col-12 col-md-6">
                            <label class="form-label" for="f-id">${i18n('common.id')}</label>
                            <input type="text" class="form-control" id="f-id" value="${App.escAttr(b.id)}"
                                   ${isEdit ? 'readonly' : ''} required pattern="[A-Za-z0-9][A-Za-z0-9._\\-]*">
                            <div class="form-text">${i18n('connectors.idHint')}</div>
                        </div>
                    </div>
                </section>

                <section class="setup-step" id="step-access">
                    <h5 class="setup-step-title">${i18n('connectors.stepAccess')}</h5>
                    <div class="mb-3" id="block-access">
                        <label class="form-label" for="f-access">${i18n('connectors.access')}</label>
                        <select class="form-select" id="f-access">
                            <option value="allowlist" ${b.access_mode === 'allowlist' ? 'selected' : ''}>${i18n('connectors.accessAllowlist')}</option>
                            <option value="password" ${b.access_mode === 'password' ? 'selected' : ''}>${i18n('connectors.accessPassword')}</option>
                            <option value="open" ${b.access_mode === 'open' ? 'selected' : ''}>${i18n('connectors.accessOpen')}</option>
                        </select>
                        <div class="form-text" id="access-hint"></div>
                    </div>

                    <div class="mb-3" id="block-allowed">
                        <label class="form-label" for="f-allowed">${i18n('connectors.allowed')}</label>
                        <input type="text" class="form-control" id="f-allowed" value="${App.escAttr(allowed)}">
                        <div class="form-text" id="allowed-hint">${i18n(this._hint('allowed', 'connectors.allowedHint', b.type))}</div>
                        <div id="allowed-chips" class="d-flex flex-wrap gap-2 mt-2"></div>
                    </div>

                    <div class="mb-3" id="block-password">
                        <label class="form-label" for="f-password">${i18n('connectors.password')}</label>
                        <input type="text" class="form-control" id="f-password" value="${App.escAttr(b.password)}">
                        <div class="form-text">${i18n('connectors.passwordHint')}</div>
                    </div>
                </section>

                <section class="setup-step" id="step-messages">
                    <h5 class="setup-step-title">${i18n('connectors.stepMessages')}</h5>
                    <div class="mb-3" id="block-welcome">
                        <label class="form-label" for="f-welcome">${i18n('connectors.welcome')}</label>
                        <textarea class="form-control" id="f-welcome" rows="2">${App.esc(b.welcome)}</textarea>
                    </div>
                    <div class="mb-3">
                        <label class="form-label" for="f-help">${i18n('connectors.help')}</label>
                        <textarea class="form-control" id="f-help" rows="2">${App.esc(b.help_text)}</textarea>
                    </div>

                    <div class="mb-3" id="block-disclosure">
                        <div class="form-check">
                            <input class="form-check-input" type="checkbox" id="f-disclose"
                                   ${b.disclose_ai ? 'checked' : ''}>
                            <label class="form-check-label" for="f-disclose">
                                ${i18n('connectors.discloseAi')}
                            </label>
                        </div>
                        <div class="form-text mb-2">${i18n('connectors.discloseAiHint')}</div>
                        <textarea class="form-control" id="f-ai-disclosure" rows="2"
                                  placeholder="${App.escAttr(i18n('connectors.aiDisclosurePlaceholder'))}"
                                  >${App.esc(b.ai_disclosure)}</textarea>
                        <div class="form-text">${i18n('connectors.aiDisclosureHint')}</div>
                    </div>
                </section>

                <!-- Rarely touched: folded so the five steps above stay the whole
                     story for a first setup. -->
                <details class="setup-advanced mb-4" id="block-advanced" ${b.session_prefix ? 'open' : ''}>
                    <summary>${i18n('connectors.advanced')}</summary>
                    <div class="mt-3">
                        <label class="form-label" for="f-prefix">${i18n('connectors.prefix')}</label>
                        <input type="text" class="form-control" id="f-prefix" value="${App.escAttr(b.session_prefix)}">
                        <div class="form-text">${i18n('connectors.prefixHint')}</div>
                    </div>
                </details>

                <div class="form-check form-switch mb-4">
                    <input class="form-check-input" type="checkbox" role="switch" id="f-enabled" ${b.enabled ? 'checked' : ''}>
                    <label class="form-check-label" for="f-enabled">${i18n('connectors.enabled')}</label>
                </div>

                <div class="d-flex flex-wrap gap-2 mb-4">
                    <button type="submit" class="btn btn-primary">
                        ${isEdit ? i18n('common.save') : i18n('common.create')}
                    </button>
                    <a href="#/connectors" class="btn btn-secondary">${i18n('common.cancel')}</a>
                    ${isEdit ? `<button type="button" class="btn btn-danger ms-auto" id="btn-delete">
                        ${i18n('common.delete')}</button>` : ''}
                </div>
            </form>
        </div></div>`;

        if (!isEdit) App.autoId('f-name', 'f-id');
        this._form = { isEdit, id: b.id };
        this._contacts = contacts;
        document.getElementById('btn-token-eye').onclick = e => {
            const inp = document.getElementById('f-token');
            const show = inp.type === 'password';
            inp.type = show ? 'text' : 'password';
            e.currentTarget.querySelector('i').className = show ? 'bi bi-eye-slash' : 'bi bi-eye';
        };
        this._renderGuide(!isEdit);
        this._renderSettings(b.settings || {}, !isEdit);
        this._syncTestButton();
        this._syncAccess();
        this._renderChips();
        document.getElementById('f-access').onchange = () => this._syncAccess();
        // Labels, hints, guide and address-book chips are per channel: they
        // follow the type. A "bot token" and a device's shared key are not the
        // same credential, and calling both "bot token" mislabels the satellite.
        const selectType = (type) => {
            document.getElementById('f-type').value = type;
            document.querySelectorAll('.type-card').forEach(card => {
                const on = card.dataset.type === type;
                card.classList.toggle('active', on);
                card.setAttribute('aria-pressed', String(on));
            });
            const tokLabel = document.getElementById('token-label');
            if (tokLabel) tokLabel.textContent = i18n(this._label('token', 'connectors.token'));
            const tok = document.getElementById('token-hint');
            if (tok) tok.textContent = i18n(this._hint('token', 'connectors.tokenHint'));
            const allow = document.getElementById('allowed-hint');
            if (allow) allow.textContent = i18n(this._hint('allowed', 'connectors.allowedHint'));
            // The device-URL field only exists for channels whose manifest
            // declares a `url` shape (e.g. satellite) — mirror of `handle`.
            const urlSpec = this._channel().url;
            document.getElementById('block-url').classList.toggle('d-none', !urlSpec);
            document.getElementById('f-url').placeholder = urlSpec?.example || '';
            const uh = document.getElementById('url-hint');
            if (uh) uh.textContent = i18n(this._hint('url', 'connectors.urlHint'));
            // A test result belongs to the credential it checked: clear it.
            document.getElementById('test-result').innerHTML = '';
            // Channel settings are per manifest too: a stored binding's values
            // only make sense for its own type, so switching type starts fresh.
            this._renderGuide(!isEdit);
            this._renderSettings(b.type === this._channelType() ? (b.settings || {}) : {}, true);
            this._syncTestButton();
            this._syncAccess();
            this._renderChips();
            this._loadDevice(isEdit ? b.id : '');
        };
        document.querySelectorAll('.type-card').forEach(card => {
            card.onclick = () => selectType(card.dataset.type);
        });
        document.getElementById('f-allowed').oninput = () => this._renderChips();
        document.getElementById('btn-test').onclick = (e) =>
            this._runTest(e.currentTarget, document.getElementById('test-result'), '');
        document.getElementById('binding-form').onsubmit = (e) => this._save(e, isEdit, bindingId);
        const del = document.getElementById('btn-delete');
        if (del) del.onclick = () => this._delete(bindingId);
        this._loadDevice(isEdit ? b.id : '');
    },

    /** The channel's step-by-step setup, from its manifest (`guide`: title,
     * steps, links — all i18n keys but the URLs). Open while creating, folded
     * while editing: the person has done it already. Each `{Name}` in a step
     * becomes a link to `links[Name]`, so the translations carry no URLs and
     * the form still names no channel. Nothing is rendered for a channel
     * without a guide. */
    _renderGuide(open) {
        const box = document.getElementById('block-guide');
        if (!box) return;
        const g = this._channel().guide;
        const steps = g?.steps || [];
        if (!steps.length) { box.innerHTML = ''; return; }
        const links = {};
        for (const [name, url] of Object.entries(g.links || {})) {
            links[name] = `<a href="${App.escAttr(url)}" target="_blank" rel="noopener">${App.esc(name)}</a>`;
        }
        box.innerHTML = `<details class="setup-guide mt-3" ${open ? 'open' : ''}>
            <summary><i class="bi bi-info-circle"></i> ${i18n(g.title || 'connectors.guideTitle')}</summary>
            <ol class="mb-0 mt-2">${steps.map(k => `<li>${i18n(k, links)}</li>`).join('')}</ol>
        </details>`;
    },

    // ------------------------------------------------------------- device box
    // Settings that live ON the far end, for channels whose manifest declares
    // `device` (the voice satellite). Its own file remains the source of truth
    // and stays hand-editable — this is a second door onto it, for the values
    // that can only be found by trial (the silence threshold of THAT room).
    //
    // Nothing here names a channel type, and nothing hardcodes the field list:
    // the box renders what the device answered. A device that grows a knob
    // grows a control, and one that has no microphone shows no thresholds.
    async _loadDevice(savedId) {
        const box = document.getElementById('block-device');
        if (!box) return;
        clearTimeout(this._devicePoll);
        const spec = this._channel().device;
        box.classList.toggle('d-none', !spec?.config);
        if (!spec?.config) return;
        if (!savedId) {
            // The device is reached with the STORED token, which does not exist
            // before the first save. Say so instead of failing a fetch.
            box.innerHTML = `<div class="border rounded p-3">
                <div class="fw-semibold mb-1">${i18n('connectors.deviceTitle')}</div>
                <div class="form-text">${i18n('connectors.deviceSaveFirst')}</div></div>`;
            return;
        }
        box.innerHTML = `<div class="border rounded p-3">
            <div class="fw-semibold mb-1">${i18n('connectors.deviceTitle')}</div>
            <div class="form-text"><span class="spinner-border spinner-border-sm"></span>
                ${i18n('connectors.deviceLoading')}</div></div>`;
        try {
            this._device = await App.api('GET', `/connectors/bindings/${encodeURIComponent(savedId)}/device`);
        } catch (err) {
            // Off, moved, asleep: normal for a device, so it is a state with a
            // retry, never an error banner over the whole form.
            box.innerHTML = `<div class="border rounded p-3">
                <div class="fw-semibold mb-1">${i18n('connectors.deviceTitle')}</div>
                <div class="alert alert-warning mb-2">${App.esc(err.message)}</div>
                <button type="button" class="btn btn-sm btn-outline-secondary" id="btn-device-retry">
                    <i class="bi bi-arrow-clockwise"></i> ${i18n('connectors.deviceRetry')}</button></div>`;
            document.getElementById('btn-device-retry').onclick = () => this._loadDevice(savedId);
            return;
        }
        this._renderDevice(savedId);
    },

    _renderDevice(savedId) {
        const box = document.getElementById('block-device');
        const d = this._device || {};
        const info = d.device || {};
        const spec = this._channel().device || {};
        const install = d.voice_install || {};
        const busy = install.state === 'downloading';
        const voices = info.voices || [];
        // The current voice may be a path (voices/x.onnx) while the list holds
        // bare names: match on the stem so the select shows what is in use.
        const stem = p => String(p || '').split('/').pop().replace(/\.onnx$/, '');
        const current = stem(d.voice);
        // The device serves its own page (type / Talk / settings) on the same
        // address we reach it at. Read from the FORM field, not from the stored
        // binding: someone editing the URL is about to open the new one.
        const deviceUrl = (document.getElementById('f-url')?.value || '').trim();
        const audio = d.audio || {};
        box.innerHTML = `<div class="border rounded p-3">
            <div class="d-flex align-items-center mb-2">
                <span class="fw-semibold">${i18n('connectors.deviceTitle')}</span>
                <span class="badge bg-${info.mic ? 'success' : 'secondary'} ms-2">
                    ${i18n(info.mic ? 'connectors.deviceMic' : 'connectors.deviceNoMic')}</span>
                <span class="badge bg-${info.tts ? 'success' : 'warning'} ms-1">
                    ${i18n(info.tts ? 'connectors.deviceTts' : 'connectors.deviceNoTts')}</span>
                ${deviceUrl ? `<a class="btn btn-sm btn-outline-secondary ms-auto"
                        href="${App.escAttr(deviceUrl)}" target="_blank" rel="noopener">
                    <i class="bi bi-box-arrow-up-right"></i> ${i18n('connectors.deviceOpen')}</a>` : ''}
                <button type="button" class="btn btn-sm btn-outline-secondary ${deviceUrl ? 'ms-1' : 'ms-auto'}"
                        id="btn-device-retry" title="${App.escAttr(i18n('connectors.deviceRetry'))}">
                    <i class="bi bi-arrow-clockwise"></i></button>
            </div>
            <div class="row g-3">
                ${'language' in d ? `<div class="col-12 col-md-6">
                    <label class="form-label" for="d-language">${i18n('connectors.deviceLanguage')}</label>
                    <input type="text" class="form-control" id="d-language" list="d-languages"
                           value="${App.escAttr(d.language || '')}" placeholder="it">
                    <!-- language and voice suggestions: keep in sync with the
                         datalists in satellite/ui.html, which cannot import them
                         (the device page must work with no server in sight) -->
                    <datalist id="d-languages">
                        ${['it', 'en', 'fr', 'de', 'es', 'pt', 'nl'].map(c => `<option value="${c}">`).join('')}
                    </datalist>
                    <div class="form-text">${i18n('connectors.deviceLanguageHint')}</div>
                </div>` : ''}
                ${'voice' in d ? `<div class="col-12 col-md-6">
                    <label class="form-label" for="d-voice">${i18n('connectors.deviceVoice')}</label>
                    <select class="form-select" id="d-voice">
                        ${voices.map(v => `<option value="voices/${App.escAttr(v)}.onnx"
                            ${v === current ? 'selected' : ''}>${App.esc(v)}</option>`).join('')}
                        ${(!voices.length || !voices.includes(current)) ? `<option value="${App.escAttr(d.voice || '')}" selected>
                            ${App.esc(d.voice || i18n('connectors.deviceNoVoice'))}</option>` : ''}
                    </select>
                    <div class="form-text">${i18n('connectors.deviceVoiceHint')}</div>
                </div>` : ''}
            </div>
            ${Object.keys(audio).length ? `<div class="row g-3 mt-1">
                ${Object.entries(audio).map(([k, v]) => `<div class="col-6 col-md-3">
                    <label class="form-label small" for="d-audio-${k}">${App.esc(this._audioLabel(k))}</label>
                    <input type="number" class="form-control form-control-sm" id="d-audio-${k}"
                           data-audio="${App.escAttr(k)}" value="${App.escAttr(v)}">
                </div>`).join('')}
            </div>` : ''}
            ${spec.voices ? `<div class="mt-3">
                <label class="form-label" for="d-newvoice">${i18n('connectors.deviceInstallVoice')}</label>
                <div class="input-group">
                    <input type="text" class="form-control font-monospace" id="d-newvoice" list="d-voicelist"
                           placeholder="it_IT-paola-medium" ${busy ? 'disabled' : ''}>
                    <button type="button" class="btn btn-outline-info" id="btn-device-voice" ${busy ? 'disabled' : ''}>
                        <i class="bi bi-download"></i> ${i18n('connectors.deviceInstall')}</button>
                </div>
                <datalist id="d-voicelist">
                    ${['it_IT-paola-medium', 'it_IT-riccardo-x_low', 'en_US-lessac-medium',
                       'en_GB-alba-medium', 'fr_FR-siwis-medium', 'de_DE-thorsten-medium',
                       'es_ES-davefx-medium'].map(v => `<option value="${v}">`).join('')}
                </datalist>
                <div class="form-text" id="d-voice-state">${this._installText(install)}</div>
            </div>` : ''}
            <div class="d-flex flex-wrap gap-2 align-items-center mt-3">
                <button type="button" class="btn btn-sm btn-primary" id="btn-device-apply">
                    ${i18n('connectors.deviceApply')}</button>
                <span class="form-text mb-0" id="d-apply-state"></span>
            </div>
            <div class="form-text mt-2">${i18n('connectors.deviceFile', { path: App.esc(info.config_file || '') })}</div>
        </div>`;
        document.getElementById('btn-device-retry').onclick = () => this._loadDevice(savedId);
        document.getElementById('btn-device-apply').onclick = (e) => this._applyDevice(e.currentTarget, savedId);
        const vb = document.getElementById('btn-device-voice');
        if (vb) vb.onclick = (e) => this._installVoice(e.currentTarget, savedId);
        // A voice is tens of MB: the device downloads in the background and
        // publishes the state, so the box follows it instead of blocking.
        if (busy) this._devicePoll = setTimeout(() => this._loadDevice(savedId), 2500);
    },

    /** A knob's label, falling back to its own name. The field list comes from
     * the device, so a firmware that adds one must not render "connectors.
     * audio.newthing" — I18n.t() returns the key when there is no translation. */
    _audioLabel(key) {
        const label = i18n('connectors.audio.' + key);
        return label.startsWith('connectors.') ? key : label;
    },

    _installText(install) {
        if (install.state === 'downloading') return i18n('connectors.deviceDownloading', { name: App.esc(install.name || '') });
        if (install.state === 'error') return `<span class="text-danger">${App.esc(install.error || 'error')}</span>`;
        if (install.state === 'done') return i18n('connectors.deviceDownloaded', { name: App.esc(install.name || '') });
        return i18n('connectors.deviceInstallHint');
    },

    /** What the form is asking the device to become. Sent whole rather than as a
     * diff: the DEVICE reports which fields actually changed, and duplicating
     * that comparison here would be a second opinion free to disagree. */
    _readDevice() {
        const patch = {};
        const lang = document.getElementById('d-language');
        if (lang) patch.language = lang.value.trim();
        const voice = document.getElementById('d-voice');
        if (voice) patch.voice = voice.value;
        const audio = {};
        document.querySelectorAll('[data-audio]').forEach(el => {
            const n = Number(el.value);
            if (Number.isFinite(n)) audio[el.dataset.audio] = n;
        });
        if (Object.keys(audio).length) patch.audio = audio;
        return patch;
    },

    async _applyDevice(btn, savedId) {
        const out = document.getElementById('d-apply-state');
        btn.disabled = true;
        try {
            const res = await App.api('PUT', `/connectors/bindings/${encodeURIComponent(savedId)}/device`,
                                      this._readDevice());
            this._device = res;
            const changed = res.changed || [];
            out.innerHTML = changed.length
                ? `<span class="text-success">${i18n('connectors.deviceSaved', { fields: App.esc(changed.join(', ')) })}</span>`
                : `<span class="text-secondary">${i18n('connectors.deviceUnchanged')}</span>`;
        } catch (err) {
            out.innerHTML = `<span class="text-danger">${App.esc(err.message)}</span>`;
        } finally {
            btn.disabled = false;
        }
    },

    async _installVoice(btn, savedId) {
        const name = document.getElementById('d-newvoice').value.trim();
        if (!name) return;
        const out = document.getElementById('d-voice-state');
        btn.disabled = true;
        try {
            const res = await App.api('POST', `/connectors/bindings/${encodeURIComponent(savedId)}/device/voices`,
                                      { name, use: true });
            out.innerHTML = this._installText(res.voice_install || {});
            this._devicePoll = setTimeout(() => this._loadDevice(savedId), 2500);
        } catch (err) {
            out.innerHTML = `<span class="text-danger">${App.esc(err.message)}</span>`;
            btn.disabled = false;
        }
    },

    /** Per-channel settings, rendered from the manifest so this file never
     * names a channel type. `settings` descriptors ({key, type, label, hint,
     * default, placeholder, options, section, when, required}) are
     * stored as Binding.settings and handed back to the connector as-is.
     *
     * - `sections` (optional, ordered) turns the flat grid into titled
     *   fieldsets; a descriptor whose `section` is missing or unknown lands in
     *   a final untitled group, which is the look channels without sections
     *   always had. A section may declare `tests`: one button each, calling
     *   verify(check=<id>) on the server with the result shown inline under
     *   that section — the mail channel proves IMAP and SMTP separately.
     * - `when: {key: value | [values]}` hides a cell unless every listed
     *   setting currently holds one of the values (see _applyWhen).
     * - `required` is checked client-side on VISIBLE fields only (_validateSettings). */
    _renderSettings(values, isNew) {
        const box = document.getElementById('block-settings');
        if (!box) return;
        const ch = this._channel();
        const fields = ch.settings || [];
        box.classList.toggle('d-none', !fields.length);
        this._placeToken(null);
        if (!fields.length) { box.innerHTML = ''; return; }
        const current = { ...(values || {}) };
        const cell = f => {
            const id = `f-set-${f.key}`;
            const v = current[f.key] ?? f.default ?? '';
            const label = App.esc(i18n(f.label || f.key))
                        + (f.required ? ' <span class="text-danger">*</span>' : '');
            const hint = f.hint ? `<div class="form-text">${App.esc(i18n(f.hint))}</div>` : '';
            const attrs = `id="${id}" data-setting="${App.escAttr(f.key)}" ${f.required ? 'required' : ''}`;
            const wrap = `<div class="col-12 col-md-6" data-field="${App.escAttr(f.key)}">`;
            let control;
            if (f.type === 'checkbox') {
                return `${wrap}
                    <div class="form-check mt-md-4">
                        <input class="form-check-input" type="checkbox" ${attrs} ${v ? 'checked' : ''}>
                        <label class="form-check-label" for="${id}">${label}</label>
                    </div>${hint}</div>`;
            } else if (f.type === 'select') {
                const opts = (f.options || []).map(o =>
                    `<option value="${App.escAttr(o.value)}" ${String(o.value) === String(v) ? 'selected' : ''}>${App.esc(i18n(o.label || o.value))}</option>`).join('');
                control = `<select class="form-select" ${attrs}>${opts}</select>`;
            } else {
                control = `<input type="${f.type === 'number' ? 'number' : 'text'}" class="form-control" ${attrs}
                    value="${App.escAttr(String(v))}" placeholder="${App.escAttr(f.placeholder || '')}"
                    ${f.type === 'number' ? 'min="0"' : ''}>`;
            }
            return `${wrap}<label class="form-label" for="${id}">${label}</label>${control}${hint}</div>`;
        };
        const sections = ch.sections || [];
        const known = new Set(sections.map(s => s.id));
        const groups = sections.map(s => ({ ...s, fields: fields.filter(f => f.section === s.id) }));
        groups.push({ id: '', fields: fields.filter(f => !known.has(f.section)) });
        box.innerHTML = groups.filter(g => g.fields.length).map(g => {
            const rows = `<div class="row g-3">${g.fields.map(cell).join('')}</div>`;
            if (!g.id) return `<div class="mb-3">${rows}</div>`;
            const tests = (g.tests || []).length ? `
                <div class="mt-3 d-flex flex-wrap gap-2">${g.tests.map(t =>
                    `<button type="button" class="btn btn-outline-info btn-sm" data-check="${App.escAttr(t.id)}">
                        <i class="bi bi-plug"></i> ${App.esc(i18n(t.label || t.id))}</button>`).join('')}
                </div>
                <div class="mt-2" data-test-result="${App.escAttr(g.id)}"></div>` : '';
            // Bootstrap 5 resets <legend> to a full-width block: float-none
            // w-auto gives back the classic title-on-the-border look.
            return `<fieldset class="border rounded p-3 mb-3" data-section="${App.escAttr(g.id)}">
                <legend class="float-none w-auto px-2 fs-6 fw-semibold mb-0">${App.esc(i18n(g.label || g.id))}</legend>
                ${rows}${tests}</fieldset>`;
        }).join('');
        this._placeToken(box.querySelector('fieldset .row'));

        box.querySelectorAll('[data-check]').forEach(btn => {
            const out = box.querySelector(`[data-test-result="${btn.closest('fieldset')?.dataset.section || ''}"]`);
            btn.onclick = () => this._runTest(btn, out || document.getElementById('test-result'), btn.dataset.check);
        });
        // Bubbling listeners: any edit may change what `when` shows.
        box.oninput = () => this._applyWhen();
        box.onchange = () => this._applyWhen();
        this._applyWhen();
    },

    /** Where the secret lives in the form. With sections it is the SECOND
     * cell of the first one — right after the first field, i.e. login then
     * password, the order anyone reads an account in; a user who sees
     * "Account" looks for the password there, not above the box. Without
     * sections, its classic place above the settings. The node is MOVED,
     * never re-created: its listeners and whatever was typed survive a type
     * switch. */
    _placeToken(row) {
        const block = document.getElementById('block-token');
        const box = document.getElementById('block-settings');
        if (!block || !box) return;
        if (row) {
            block.classList.remove('mb-3');
            block.classList.add('col-12', 'col-md-6');
            const first = row.firstElementChild;
            if (first) first.after(block); else row.prepend(block);
        } else if (block.parentNode !== box.parentNode) {
            block.classList.add('mb-3');
            block.classList.remove('col-12', 'col-md-6');
            box.parentNode.insertBefore(block, box);
        }
    },

    /** The per-section test buttons a channel declares — sections[].tests
     * flattened with their section id. Empty for channels without sections. */
    _tests(type) {
        const out = [];
        for (const sec of this._channel(type).sections || []) {
            for (const t of sec.tests || []) out.push({ ...t, section: sec.id });
        }
        return out;
    },

    /** A channel with per-section tests hides the generic Test button next to
     * the token: each server gets its own button. */
    _syncTestButton() {
        const btn = document.getElementById('btn-test');
        if (btn) btn.classList.toggle('d-none', this._tests().length > 0);
    },

    /** Applies the descriptors' `when` clauses. Hidden fields are still read
     * and saved by _readSettings(): switching IMAP → POP3 → IMAP keeps the
     * folder, and the connector ignores what does not apply anyway. */
    _applyWhen() {
        const box = document.getElementById('block-settings');
        if (!box) return;
        const value = k => {
            const el = document.getElementById(`f-set-${k}`);
            if (!el) return '';
            return el.type === 'checkbox' ? String(el.checked) : String(el.value);
        };
        for (const f of this._channel().settings || []) {
            if (!f.when) continue;
            const cell = box.querySelector(`[data-field="${CSS.escape(f.key)}"]`);
            if (!cell) continue;
            const show = Object.entries(f.when).every(([k, want]) =>
                (Array.isArray(want) ? want : [want]).map(String).includes(value(k)));
            cell.classList.toggle('d-none', !show);
        }
    },

    /** The first `required` setting that is visible and empty, marked
     * is-invalid; null when everything needed is filled in. */
    _validateSettings() {
        const box = document.getElementById('block-settings');
        let missing = null;
        for (const f of this._channel().settings || []) {
            const el = document.getElementById(`f-set-${f.key}`);
            if (!el) continue;
            el.classList.remove('is-invalid');
            if (missing || !f.required || f.type === 'checkbox') continue;
            const cell = box?.querySelector(`[data-field="${CSS.escape(f.key)}"]`);
            if (cell?.classList.contains('d-none')) continue;
            if (!String(el.value).trim()) { el.classList.add('is-invalid'); missing = f; }
        }
        return missing;
    },

    _readSettings() {
        const out = {};
        for (const f of this._channel().settings || []) {
            const el = document.getElementById(`f-set-${f.key}`);
            if (!el) continue;
            if (f.type === 'checkbox') out[f.key] = el.checked;
            else if (f.type === 'number') {
                const n = parseInt(el.value, 10);
                out[f.key] = Number.isFinite(n) ? n : (f.default ?? 0);
            } else out[f.key] = el.value.trim();
        }
        return out;
    },

    _syncAccess() {
        // A device channel (satellite) authenticates with the binding token and
        // never runs the user-facing pipeline: access modes, the welcome
        // message and the AI disclosure would be dead controls there (a device
        // connector answers through its own ask() path and never runs
        // process_message — and the owner installed the speaker, which is the
        // Act's "obvious from the context" case). Both steps are hidden whole,
        // and the CSS counter renumbers the rest. A user who could set
        // "password" on a device would believe it protects something.
        const device = !!this._channel().device;
        const mode = document.getElementById('f-access').value;
        document.getElementById('step-access').classList.toggle('d-none', device);
        document.getElementById('step-messages').classList.toggle('d-none', device);
        document.getElementById('block-allowed').classList.toggle('d-none', mode !== 'allowlist');
        document.getElementById('block-password').classList.toggle('d-none', mode !== 'password');
        const hint = document.getElementById('access-hint');
        if (hint) hint.textContent = i18n({ allowlist: 'connectors.accessAllowlistHint',
                                            password: 'connectors.accessPasswordHint',
                                            open: 'connectors.accessOpenHint' }[mode] || 'connectors.accessAllowlistHint');
    },

    // Address-book chips over the free-text field. The text field stays the
    // single source of truth on save: the list must also accept ids that are not
    // in the address book, so a closed control (a multi-select) can't replace it.
    _tokens() {
        return (document.getElementById('f-allowed').value || '')
            .split(',').map(t => t.trim()).filter(Boolean);
    },

    // A contact's identifier on the channel this binding uses. Contacts carry one
    // handle per channel type, so the same person can be reached on Telegram and
    // elsewhere without the address book guessing which id is which.
    _identifier(contact) {
        return (contact.handles || {})[this._channelType()] || '';
    },

    _channelType() {
        const sel = document.getElementById('f-type');
        return (sel && sel.value) || (this._types?.[0]?.type) || 'telegram';
    },

    /** The manifest of the channel currently selected in the form. */
    _channel(type) {
        const wanted = type || this._channelType();
        return (this._types || []).find(t => t.type === wanted) || {};
    },

    /** A channel's hint key for one field, falling back to the generic one.
     * I18n.t() degrades to the key itself when a translation is missing, so a
     * channel may ship a key the dictionaries don't have yet without breaking.
     * `type` matters during the FIRST render: the type <select> is not in the
     * DOM yet, so _channelType() would fall back to the first discovered
     * channel — which showed the satellite hints on a telegram form. */
    _hint(field, fallback, type) {
        return this._channel(type).hints?.[field] || fallback;
    },

    /** Same as _hint() for the field LABEL: a channel that calls its credential
     * something else than a token says so in its manifest, instead of the form
     * naming channel types it must not know about. */
    _label(field, fallback, type) {
        return this._channel(type).labels?.[field] || fallback;
    },

    _matches(token, contact) {
        const handle = this._identifier(contact);
        if (!handle) return false;
        const norm = s => s.toLowerCase().replace(/^@/, '');
        return norm(token) === norm(handle);
    },

    _renderChips() {
        const box = document.getElementById('allowed-chips');
        if (!box) return;
        const contacts = (this._contacts || []).filter(c => this._identifier(c));
        if (!contacts.length) {
            box.innerHTML = `<div class="form-text">${i18n('connectors.allowedNoContacts')}
                <a href="#/connectors/contacts/new">${i18n('connectors.contactsNew')}</a></div>`;
            return;
        }
        const tokens = this._tokens();
        box.innerHTML = contacts.map((c, i) => {
            const on = tokens.some(t => this._matches(t, c));
            return `<button type="button" class="btn btn-sm chip-toggle ${on ? 'btn-primary' : 'btn-outline-secondary'}"
                        data-chip="${i}" aria-pressed="${on}"
                        title="${App.escAttr(this._identifier(c))}">${App.esc(c.name || c.id)}</button>`;
        }).join('');
        box.querySelectorAll('[data-chip]').forEach(btn => {
            btn.onclick = () => this._toggleChip(contacts[Number(btn.dataset.chip)]);
        });
    },

    _toggleChip(contact) {
        const field = document.getElementById('f-allowed');
        const tokens = this._tokens();
        const kept = tokens.filter(t => !this._matches(t, contact));
        if (kept.length === tokens.length) kept.push(this._identifier(contact));
        field.value = kept.join(', ');
        this._renderChips();
    },

    _readForm() {
        const val = id => document.getElementById(id).value.trim();
        // Identifiers are STRINGS end to end (a phone number keeps its '+', a
        // Telegram id may outgrow 2^53): never Number() them.
        const ids = [], usernames = [];
        for (const token of this._tokens()) {
            if (/^[+-]?\d+$/.test(token)) ids.push(token);
            else usernames.push(token.replace(/^@/, '').toLowerCase());
        }
        return {
            id: val('f-id'),
            name: val('f-name'),
            type: val('f-type'),
            enabled: document.getElementById('f-enabled').checked,
            agent_id: val('f-agent'),
            token: document.getElementById('f-token').value.trim(),
            url: val('f-url'),
            access_mode: val('f-access'),
            allowed_ids: ids,
            allowed_usernames: usernames,
            password: document.getElementById('f-password').value.trim(),
            session_prefix: val('f-prefix'),
            welcome: document.getElementById('f-welcome').value,
            help_text: document.getElementById('f-help').value,
            disclose_ai: document.getElementById('f-disclose').checked,
            ai_disclosure: document.getElementById('f-ai-disclosure').value,
            settings: this._readSettings(),
        };
    },

    /** Runs one verification on the server and shows the outcome in `out`.
     * `check` is a section test id (see _renderSettings) or '' for the
     * channel's whole verify(); `btn` is the button that asked, so it can
     * spin. The UI only ever sees the token mask, so it cannot send the real
     * token back: for a saved binding with the mask (or nothing) in the field,
     * the server tests what it stored. */
    async _runTest(btn, out, check) {
        const { isEdit, id: savedId } = this._form || {};
        const fail = html => { out.innerHTML = `<div class="alert alert-danger mb-0">${html}</div>`; };
        const missing = this._validateSettings();
        if (missing) {
            return fail(i18n('connectors.settingRequired', { field: App.esc(i18n(missing.label || missing.key)) }));
        }
        const token = document.getElementById('f-token').value.trim();
        const stored = isEdit && (token === '********' || !token);
        if (!stored && !token) return fail(i18n('connectors.tokenRequired'));
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm"></span> ${i18n('mcp.testing')}`;
        try {
            const res = stored
                ? await App.api('POST', `/connectors/bindings/${encodeURIComponent(savedId)}/test`,
                                { check: check || '' })
                : await App.api('POST', '/connectors/bindings/test',
                                { type: document.getElementById('f-type').value, token,
                                  url: document.getElementById('f-url').value.trim(),
                                  settings: this._readSettings(), check: check || '' });
            out.innerHTML = `<div class="alert alert-success mb-0">${this._testResultHtml(res)}</div>`;
            // What the far end calls itself is a fine default for the channel's
            // own name — only while creating, only into an empty field, and
            // through the input event so the id follows (App.autoId).
            const nameEl = document.getElementById('f-name');
            if (!isEdit && res.name && nameEl && !nameEl.value.trim()) {
                nameEl.value = res.name;
                nameEl.dispatchEvent(new Event('input'));
            }
        } catch (err) {
            fail(App.esc(err.message));
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    },

    /** A bot answers with its @handle; an account (mailbox) with its address
     * and what was checked; a device (satellite) only has a name. */
    _testResultHtml(res) {
        if (res.bot) return i18n('connectors.testOk', { bot: App.esc(res.bot), name: App.esc(res.name || '') });
        if (res.account) return i18n('connectors.testOkAccount', { name: App.esc(res.account), detail: App.esc(res.detail || '') });
        return i18n('connectors.testOkDevice', { name: App.esc(res.name || '?') });
    },

    async _save(event, isEdit, bindingId) {
        event.preventDefault();
        const data = this._readForm();
        if (!data.id) return App.toast(i18n('connectors.idRequired'), 'danger');
        if (!data.agent_id) return App.toast(i18n('connectors.agentRequired'), 'danger');
        const missing = this._validateSettings();
        if (missing) {
            return App.toast(i18n('connectors.settingRequired', { field: i18n(missing.label || missing.key) }), 'danger');
        }
        try {
            if (isEdit) {
                await App.api('PUT', `/connectors/bindings/${encodeURIComponent(bindingId)}`, data);
            } else {
                await App.api('POST', '/connectors/bindings', data);
            }
            App.toast(i18n('connectors.saved'));
            location.hash = '#/connectors';
        } catch (err) {
            App.toast(err.message, 'danger');
        }
    },

    async _delete(bindingId) {
        if (!confirm(i18n('connectors.confirmDelete'))) return;
        try {
            await App.api('DELETE', `/connectors/bindings/${encodeURIComponent(bindingId)}`);
            App.toast(i18n('connectors.deleted'));
            location.hash = '#/connectors';
        } catch (err) {
            App.toast(err.message, 'danger');
        }
    },

    // -------------------------------------------------------- address book
    async renderContacts(params) {
        if (params[0] === 'new') return this.renderContactForm();
        if (params[0]) return this.renderContactForm(params[0]);

        this._loading('contacts');
        let contacts;
        try {
            contacts = await App.api('GET', '/connectors/contacts');
        } catch (err) {
            App.container.innerHTML = this.tabs('contacts') + `
                <div class="alert alert-danger">${i18n('connectors.loadError', { msg: App.esc(err.message) })}</div>`;
            return;
        }

        const header = `
            <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-2">
                <h3 class="mb-0"><i class="bi bi-person-lines-fill"></i> ${i18n('connectors.contactsTitle')}</h3>
                <a href="#/connectors/contacts/new" class="btn btn-primary">
                    <i class="bi bi-plus-lg"></i> ${i18n('connectors.contactsNew')}</a>
            </div>
            <p class="text-secondary">${i18n('connectors.contactsHint')}</p>`;

        const body = contacts.length === 0
            ? `<div class="alert alert-secondary">
                   <strong>${i18n('connectors.contactsEmpty')}</strong><br>${i18n('connectors.contactsEmptyHint')}
               </div>`
            : `<div class="table-responsive"><table class="table table-hover align-middle">
                   <thead><tr>
                       <th>${i18n('common.name')}</th>
                       <th>${i18n('connectors.colIdentifiers')}</th>
                       <th></th>
                   </tr></thead>
                   <tbody>${contacts.map(c => {
                       const ids = [
                           ...Object.entries(c.handles || {}).map(([k, v]) => `${k}: ${v}`),
                           c.notes || '',
                       ].filter(Boolean).join(' · ');
                       return `<tr>
                           <td>${App.esc(c.name || c.id)}</td>
                           <td class="small text-secondary">${App.esc(ids)}</td>
                           <td class="text-end text-nowrap">
                               <a href="#/connectors/contacts/${encodeURIComponent(c.id)}"
                                  class="btn btn-sm btn-outline-primary">${i18n('common.edit')}</a>
                           </td>
                       </tr>`;
                   }).join('')}</tbody>
               </table></div>`;

        App.container.innerHTML = this.tabs('contacts') + header + body;
    },

    async renderContactForm(contactId) {
        const isEdit = !!contactId;
        let c = { id: '', name: '', handles: {}, notes: '' };
        const [loaded, types] = await Promise.all([
            isEdit ? App.api('GET', `/connectors/contacts/${encodeURIComponent(contactId)}`)
                        .catch(err => ({ _error: err.message }))
                   : null,
            App.api('GET', '/connectors/bindings/types').catch(() => ({ types: [] })),
        ]);
        if (loaded && loaded._error) {
            App.toast(loaded._error, 'danger');
            location.hash = '#/connectors/contacts';
            return;
        }
        if (loaded) c = loaded;
        // One field per installed channel: the same person has a Telegram id AND
        // a phone number, and each channel labels its own identifier.
        this._types = types.types || [];
        App.container.innerHTML = `
        <div class="row"><div class="col-lg-8 mx-auto">
            <h3 class="mb-3">${isEdit ? i18n('connectors.contactEditTitle') : i18n('connectors.contactNewTitle')}</h3>
            <form id="contact-form" novalidate>
                <div class="row g-3 mb-3">
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="c-name">${i18n('common.name')}</label>
                        <input type="text" class="form-control" id="c-name" value="${App.escAttr(c.name)}" required>
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="c-id">${i18n('common.id')}</label>
                        <input type="text" class="form-control" id="c-id" value="${App.escAttr(c.id)}"
                               ${isEdit ? 'readonly' : ''} required pattern="[A-Za-z0-9][A-Za-z0-9._\\-]*">
                        <div class="form-text">${i18n('connectors.contactIdHint')}</div>
                    </div>
                </div>
                <div class="row g-3 mb-3">
                    ${(this._types.length ? this._types : [{ type: 'telegram', label: 'Telegram' }]).map(t => `
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="c-h-${App.escAttr(t.type)}">
                            ${App.esc(i18n(t.handle?.label || t.label || t.type))}</label>
                        <input type="text" class="form-control" id="c-h-${App.escAttr(t.type)}"
                               data-handle="${App.escAttr(t.type)}"
                               placeholder="${App.escAttr(t.handle?.example || '')}"
                               value="${App.escAttr((c.handles || {})[t.type] || '')}">
                    </div>`).join('')}
                </div>
                <div class="mb-3">
                    <label class="form-label" for="c-notes">${i18n('connectors.contactNotes')}</label>
                    <textarea class="form-control" id="c-notes" rows="2">${App.esc(c.notes)}</textarea>
                    <div class="form-text">${i18n('connectors.contactHint')}</div>
                </div>
                <div class="d-flex flex-wrap gap-2 mb-4">
                    <button type="submit" class="btn btn-primary">
                        ${isEdit ? i18n('common.save') : i18n('common.create')}</button>
                    <a href="#/connectors/contacts" class="btn btn-secondary">${i18n('common.cancel')}</a>
                    ${isEdit ? `<button type="button" class="btn btn-danger ms-auto" id="c-delete">
                        ${i18n('common.delete')}</button>` : ''}
                </div>
            </form>
        </div></div>`;

        if (!isEdit) App.autoId('c-name', 'c-id');
        document.getElementById('contact-form').onsubmit = (e) => this._saveContact(e, isEdit, contactId);
        const del = document.getElementById('c-delete');
        if (del) del.onclick = () => this._deleteContact(contactId);
    },

    async _saveContact(event, isEdit, contactId) {
        event.preventDefault();
        const val = id => document.getElementById(id).value.trim();
        if (!val('c-name')) return App.toast(i18n('connectors.contactNameRequired'), 'danger');
        const handles = {};
        document.querySelectorAll('[data-handle]').forEach(el => {
            const v = el.value.trim();
            if (v) handles[el.dataset.handle] = v;
        });
        // A contact nobody can be reached at is not useful — and would show up as
        // a dead chip in every bot form.
        if (!Object.keys(handles).length) {
            return App.toast(i18n('connectors.contactIdentifierRequired'), 'danger');
        }
        const data = {
            id: val('c-id'),
            name: val('c-name'),
            handles,
            notes: document.getElementById('c-notes').value,
        };
        try {
            if (isEdit) {
                await App.api('PUT', `/connectors/contacts/${encodeURIComponent(contactId)}`, data);
            } else {
                await App.api('POST', '/connectors/contacts', data);
            }
            App.toast(i18n('connectors.saved'));
            location.hash = '#/connectors/contacts';
        } catch (err) {
            App.toast(err.message, 'danger');
        }
    },

    async _deleteContact(contactId) {
        if (!confirm(i18n('connectors.contactConfirmDelete'))) return;
        try {
            await App.api('DELETE', `/connectors/contacts/${encodeURIComponent(contactId)}`);
            App.toast(i18n('connectors.deleted'));
            location.hash = '#/connectors/contacts';
        } catch (err) {
            App.toast(err.message, 'danger');
        }
    },
};
