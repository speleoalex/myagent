// Connectors page: messaging bots (Telegram) bound to agents, plus the address
// book of people you can authorize on them. Served by the connectors plugin —
// when it isn't installed these endpoints don't exist, so every view starts by
// checking App.plugin('connectors') and says so explicitly instead of rendering
// an empty list, which would read as "you have no bots".

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
            ${pill('bots', '#/connectors', i18n('connectors.title'), 'bi-robot')}
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

    // ------------------------------------------------------------------ bots
    _loading(tab) {
        App.container.innerHTML = this.tabs(tab) + `
            <div class="text-secondary"><span class="spinner-border spinner-border-sm"></span>
            ${i18n('common.loading')}</div>`;
    },

    async renderList() {
        this._loading('bots');
        let bindings;
        try {
            bindings = await App.api('GET', '/connectors/bindings');
        } catch (err) {
            // An explicit error, NOT an empty list: a server that is down must
            // never be reported as "no bots configured".
            App.container.innerHTML = this.tabs('bots') + `
                <div class="alert alert-danger">${i18n('connectors.loadError', { msg: App.esc(err.message) })}</div>`;
            return;
        }

        const header = `
            <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-2">
                <h3 class="mb-0"><i class="bi bi-robot"></i> ${i18n('connectors.title')}</h3>
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

        App.container.innerHTML = this.tabs('bots') + header + body;
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
            if (!cell) return this.renderList();  // the set of bots changed
            cell.innerHTML = this.stateBadge(b);
            const msgs = App.container.querySelector(`[data-msgs="${CSS.escape(b.id)}"]`);
            if (msgs) msgs.textContent = b.status?.messages ?? 0;
        }
    },

    // ------------------------------------------------------------- bot form
    async renderForm(bindingId) {
        const isEdit = !!bindingId;
        let b = {
            id: '', name: '', type: 'telegram', enabled: true, agent_id: '',
            token: '', url: '', access_mode: 'allowlist', allowed_ids: [],
            allowed_usernames: [], password: '', session_prefix: '',
            welcome: '', help_text: '',
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

        App.container.innerHTML = `
        <div class="row"><div class="col-lg-8 mx-auto">
            <h3 class="mb-3">${isEdit ? i18n('connectors.editTitle') : i18n('connectors.newTitle')}</h3>
            <form id="binding-form" novalidate>
                <div class="row g-3 mb-3">
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="f-name">${i18n('common.name')}</label>
                        <input type="text" class="form-control" id="f-name" value="${App.escAttr(b.name)}">
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="f-id">${i18n('common.id')}</label>
                        <input type="text" class="form-control" id="f-id" value="${App.escAttr(b.id)}"
                               ${isEdit ? 'readonly' : ''} required pattern="[A-Za-z0-9][A-Za-z0-9._\\-]*">
                        <div class="form-text">${i18n('connectors.idHint')}</div>
                    </div>
                </div>

                <div class="row g-3 mb-3">
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="f-type">${i18n('connectors.type')}</label>
                        <select class="form-select" id="f-type">
                            ${this._types.map(t =>
                                `<option value="${App.escAttr(t.type)}" ${t.type === b.type ? 'selected' : ''}>${App.esc(t.label || t.type)}</option>`
                            ).join('')}
                        </select>
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="f-agent">${i18n('connectors.agent')}</label>
                        <select class="form-select" id="f-agent">
                            <option value="">${i18n('connectors.agentNone')}</option>
                            ${agents.map(a =>
                                `<option value="${App.escAttr(a.id)}" ${a.id === b.agent_id ? 'selected' : ''}>${App.esc(a.name || a.id)}</option>`
                            ).join('')}
                            ${b.agent_id && !agents.some(a => a.id === b.agent_id)
                                ? `<option value="${App.escAttr(b.agent_id)}" selected>${App.esc(b.agent_id)} — ${i18n('agents.bindingMissing')}</option>`
                                : ''}
                        </select>
                    </div>
                </div>

                <div class="mb-3">
                    <label class="form-label" for="f-token">${i18n('connectors.token')}</label>
                    <div class="input-group">
                        <input type="text" class="form-control font-monospace" id="f-token" value="${App.escAttr(b.token)}">
                        <button type="button" class="btn btn-outline-info" id="btn-test">
                            <i class="bi bi-plug"></i> ${i18n('connectors.test')}
                        </button>
                    </div>
                    <div class="form-text" id="token-hint">${i18n(this._hint('token', 'connectors.tokenHint', b.type))}</div>
                    <div id="test-result" class="mt-2"></div>
                </div>

                <div class="mb-3 ${this._channel(b.type).url ? '' : 'd-none'}" id="block-url">
                    <label class="form-label" for="f-url">${i18n('connectors.url')}</label>
                    <input type="text" class="form-control" id="f-url" value="${App.escAttr(b.url || '')}"
                           placeholder="${App.escAttr(this._channel(b.type).url?.example || '')}">
                    <div class="form-text" id="url-hint">${i18n(this._hint('url', 'connectors.urlHint', b.type))}</div>
                </div>

                <div class="mb-4 d-none" id="block-device"></div>

                <div class="row g-3 mb-3">
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="f-access">${i18n('connectors.access')}</label>
                        <select class="form-select" id="f-access">
                            <option value="allowlist" ${b.access_mode === 'allowlist' ? 'selected' : ''}>${i18n('connectors.accessAllowlist')}</option>
                            <option value="password" ${b.access_mode === 'password' ? 'selected' : ''}>${i18n('connectors.accessPassword')}</option>
                            <option value="open" ${b.access_mode === 'open' ? 'selected' : ''}>${i18n('connectors.accessOpen')}</option>
                        </select>
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="f-prefix">${i18n('connectors.prefix')}</label>
                        <input type="text" class="form-control" id="f-prefix" value="${App.escAttr(b.session_prefix)}">
                        <div class="form-text">${i18n('connectors.prefixHint')}</div>
                    </div>
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

                <div class="mb-3">
                    <label class="form-label" for="f-welcome">${i18n('connectors.welcome')}</label>
                    <textarea class="form-control" id="f-welcome" rows="2">${App.esc(b.welcome)}</textarea>
                </div>
                <div class="mb-3">
                    <label class="form-label" for="f-help">${i18n('connectors.help')}</label>
                    <textarea class="form-control" id="f-help" rows="2">${App.esc(b.help_text)}</textarea>
                </div>

                <div class="form-check mb-4">
                    <input class="form-check-input" type="checkbox" id="f-enabled" ${b.enabled ? 'checked' : ''}>
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
        this._contacts = contacts;
        this._syncAccess();
        this._renderChips();
        document.getElementById('f-access').onchange = () => this._syncAccess();
        document.getElementById('f-type').onchange = () => {
            // Hints and address-book chips are per channel: both follow the type.
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
            this._renderChips();
            this._loadDevice(isEdit ? b.id : '');
        };
        document.getElementById('f-allowed').oninput = () => this._renderChips();
        document.getElementById('btn-test').onclick = (e) => this._testToken(e.currentTarget, isEdit, b.id);
        document.getElementById('binding-form').onsubmit = (e) => this._save(e, isEdit, bindingId);
        const del = document.getElementById('btn-delete');
        if (del) del.onclick = () => this._delete(bindingId);
        this._loadDevice(isEdit ? b.id : '');
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

    _syncAccess() {
        const mode = document.getElementById('f-access').value;
        document.getElementById('block-allowed').classList.toggle('d-none', mode !== 'allowlist');
        document.getElementById('block-password').classList.toggle('d-none', mode !== 'password');
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
        const ids = [], usernames = [];
        for (const token of this._tokens()) {
            if (/^-?\d+$/.test(token)) ids.push(Number(token));
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
        };
    },

    async _testToken(btn, isEdit, savedId) {
        const token = document.getElementById('f-token').value.trim();
        const out = document.getElementById('test-result');
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm"></span> ${i18n('mcp.testing')}`;
        try {
            // The UI only ever sees the mask, so it cannot send the real token
            // back: for a saved binding, ask the server to test what it stored.
            const res = (isEdit && (token === '********' || !token))
                ? await App.api('POST', `/connectors/bindings/${encodeURIComponent(savedId)}/test`)
                : await App.api('POST', '/connectors/bindings/test',
                                { type: document.getElementById('f-type').value, token,
                                  url: document.getElementById('f-url').value.trim() });
            // A bot answers with its @handle; a device (satellite) only has a name.
            const okMsg = res.bot
                ? i18n('connectors.testOk', { bot: App.esc(res.bot), name: App.esc(res.name || '') })
                : i18n('connectors.testOkDevice', { name: App.esc(res.name || '?') });
            out.innerHTML = `<div class="alert alert-success mb-0">${okMsg}</div>`;
        } catch (err) {
            out.innerHTML = `<div class="alert alert-danger mb-0">${App.esc(err.message)}</div>`;
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    },

    async _save(event, isEdit, bindingId) {
        event.preventDefault();
        const data = this._readForm();
        if (!data.id) return App.toast(i18n('connectors.idRequired'), 'danger');
        if (!data.agent_id) return App.toast(i18n('connectors.agentRequired'), 'danger');
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
                            ${App.esc(t.handle?.label || t.label || t.type)}</label>
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
