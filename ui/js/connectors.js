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
            token: '', access_mode: 'allowlist', allowed_ids: [],
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
            App.api('GET', '/connectors/bindings/types').catch(() => ({ types: ['telegram'] })),
            App.api('GET', '/agents?selectable=true').catch(() => []),
            App.api('GET', '/connectors/contacts').catch(() => []),
        ]);

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
                            ${(types.types || ['telegram']).map(t =>
                                `<option value="${App.escAttr(t)}" ${t === b.type ? 'selected' : ''}>${App.esc(t)}</option>`
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
                    <div class="form-text">${i18n('connectors.tokenHint')}</div>
                    <div id="test-result" class="mt-2"></div>
                </div>

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
                    <div class="form-text" id="allowed-hint">${i18n('connectors.allowedHint')}</div>
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
        document.getElementById('f-allowed').oninput = () => this._renderChips();
        document.getElementById('btn-test').onclick = (e) => this._testToken(e.currentTarget, isEdit, b.id);
        document.getElementById('binding-form').onsubmit = (e) => this._save(e, isEdit, bindingId);
        const del = document.getElementById('btn-delete');
        if (del) del.onclick = () => this._delete(bindingId);
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

    _matches(token, contact) {
        const t = token.toLowerCase();
        if (contact.user_id != null && t === String(contact.user_id)) return true;
        return !!contact.username && t.replace(/^@/, '') === contact.username.toLowerCase();
    },

    _identifier(contact) {
        return contact.user_id != null ? String(contact.user_id)
             : contact.username ? '@' + contact.username : '';
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
                                { type: document.getElementById('f-type').value, token });
            out.innerHTML = `<div class="alert alert-success mb-0">
                ${i18n('connectors.testOk', { bot: App.esc(res.bot || '?'), name: App.esc(res.name || '') })}</div>`;
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
                           c.user_id != null ? String(c.user_id) : '',
                           c.username ? '@' + c.username : '',
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
        let c = { id: '', name: '', user_id: null, username: '', notes: '' };
        if (isEdit) {
            try {
                c = await App.api('GET', `/connectors/contacts/${encodeURIComponent(contactId)}`);
            } catch (err) {
                App.toast(err.message, 'danger');
                location.hash = '#/connectors/contacts';
                return;
            }
        }
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
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="c-userid">${i18n('connectors.contactUserId')}</label>
                        <input type="text" inputmode="numeric" class="form-control" id="c-userid"
                               value="${c.user_id != null ? App.escAttr(String(c.user_id)) : ''}">
                    </div>
                    <div class="col-12 col-md-6">
                        <label class="form-label" for="c-username">${i18n('connectors.contactUsername')}</label>
                        <input type="text" class="form-control" id="c-username" value="${App.escAttr(c.username)}">
                    </div>
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
        const rawId = val('c-userid');
        if (!val('c-name')) return App.toast(i18n('connectors.contactNameRequired'), 'danger');
        if (rawId && !/^-?\d+$/.test(rawId)) {
            return App.toast(i18n('connectors.contactUserIdNumeric'), 'danger');
        }
        if (!rawId && !val('c-username')) {
            return App.toast(i18n('connectors.contactIdentifierRequired'), 'danger');
        }
        const data = {
            id: val('c-id'),
            name: val('c-name'),
            user_id: rawId ? Number(rawId) : null,
            username: val('c-username'),
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
