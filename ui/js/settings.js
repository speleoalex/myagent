const SettingsPage = {
    async render() {
        let settings = {};
        try { settings = await App.api('GET', '/system/settings'); } catch (e) { /* empty */ }

        let models = [];
        try { models = await App.api('GET', '/models'); } catch (e) { /* empty */ }

        App.container.innerHTML = `
            <div class="row">
                <div class="col-lg-8 mx-auto">
                    <h3><i class="bi bi-gear"></i> ${i18n('settings.title')}</h3>

                    <!-- Appearance: client-side preferences (apply immediately,
                         stored in localStorage — not part of the server form). -->
                    <h5 class="mt-3">${i18n('settings.appearance')}</h5>
                    <div class="row g-3 mb-2">
                        <div class="col-sm-6">
                            <label class="form-label"><i class="bi bi-translate"></i> ${i18n('settings.language')}</label>
                            <select class="form-select" id="f-language">
                                <option value="en" ${I18n.locale === 'en' ? 'selected' : ''}>English</option>
                                <option value="it" ${I18n.locale === 'it' ? 'selected' : ''}>Italiano</option>
                            </select>
                        </div>
                        <div class="col-sm-6">
                            <label class="form-label"><i class="bi bi-circle-half"></i> ${i18n('settings.theme')}</label>
                            <select class="form-select" id="f-theme">
                                <option value="light" ${ThemeManager.getCurrentTheme() === 'light' ? 'selected' : ''}>${i18n('settings.themeLight')}</option>
                                <option value="dark" ${ThemeManager.getCurrentTheme() === 'dark' ? 'selected' : ''}>${i18n('settings.themeDark')}</option>
                            </select>
                        </div>
                        <!-- Which server this UI talks to — a BROWSER preference
                             like the two above, because the UI is static HTML that
                             may be hosted anywhere (Apache, nginx) away from the
                             API. Empty = same origin, i.e. the classic setup. -->
                        <div class="col-12">
                            <label class="form-label"><i class="bi bi-hdd-network"></i> ${i18n('settings.serverBase')}</label>
                            <div class="input-group">
                                <input type="text" class="form-control" id="f-server-base"
                                       value="${App.escAttr(App.serverBase)}"
                                       placeholder="${i18n('settings.serverBasePlaceholder')}">
                                <button type="button" class="btn btn-outline-primary" id="btn-server-base">${i18n('settings.serverBaseApply')}</button>
                            </div>
                            <small class="text-secondary">${i18n('settings.serverBaseHint')}</small>
                        </div>
                    </div>

                    <hr class="my-4">

                    <form id="settings-form">
                        <div class="mb-3">
                            <label class="form-label">${i18n('settings.defaultModel')}</label>
                            <select class="form-select" id="f-default-model">
                                <option value="">${i18n('settings.noDefaultModel')}</option>
                                ${models.map(m => `<option value="${App.escAttr(m.id)}" ${m.id === settings.default_model_id ? 'selected' : ''}>${App.esc(m.name)} (${App.esc(m.provider)})</option>`).join('')}
                            </select>
                            <small class="text-secondary">${i18n('settings.defaultModelHint')}</small>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('settings.ollamaUrl')}</label>
                            <input type="text" class="form-control" id="f-ollama-url" value="${App.esc(settings.ollama_base_url || 'http://localhost:11434')}">
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('settings.llamacppUrl')}</label>
                            <input type="text" class="form-control" id="f-llamacpp-url" value="${App.esc(settings.llamacpp_base_url || 'http://localhost:8080')}">
                        </div>
                        <button type="submit" class="btn btn-primary">${i18n('settings.save')}</button>
                    </form>

                    <hr class="my-4">

                    <!-- API key: a SERVER setting, but with its own box because
                         saving it also has to update this browser's stored copy
                         (the next request would 401 otherwise). -->
                    <h5>${i18n('settings.apiKey')}</h5>
                    <div id="api-key-box" class="mb-3">
                        <div class="spinner-border spinner-border-sm"></div>
                    </div>

                    <hr class="my-4">

                    <!-- Install the UI as an app. Client-side only: nothing
                         here touches the server's settings. -->
                    <h5>${i18n('settings.install')}</h5>
                    <div id="install-box" class="mb-3"></div>

                    <hr class="my-4">

                    <h5>${i18n('settings.systemStatus')}</h5>
                    <div id="status-checks" class="mb-3">
                        <div class="spinner-border spinner-border-sm"></div> ${i18n('settings.checking')}
                    </div>
                </div>
            </div>`;

        // Language: applies immediately and re-renders the app (so this page
        // re-renders with the new locale, keeping the select in sync).
        document.getElementById('f-language').onchange = (e) => I18n.setLocale(e.target.value);
        // Theme: applies immediately (localStorage-persisted via ThemeManager).
        document.getElementById('f-theme').onchange = (e) => ThemeManager.setTheme(e.target.value);
        // Server: stored locally, then a full reload — everything already
        // fetched (plugins probe, agents, models) came from the OLD server, and
        // a reload is the one honest way to drop all of it at once. An explicit
        // Apply button, not onchange: half-typed URLs must not trigger it.
        document.getElementById('btn-server-base').onclick = () => {
            const raw = document.getElementById('f-server-base').value;
            const clean = App.normalizeServerBase(raw);
            if (clean && !/^https?:\/\//.test(clean)) {
                App.toast(i18n('settings.serverBaseInvalid'), 'danger');
                return;
            }
            if (clean) localStorage.setItem('myagent_server', clean);
            else localStorage.removeItem('myagent_server');
            location.reload();
        };

        document.getElementById('settings-form').onsubmit = async (e) => {
            e.preventDefault();
            const data = {
                ollama_base_url: document.getElementById('f-ollama-url').value.trim(),
                llamacpp_base_url: document.getElementById('f-llamacpp-url').value.trim(),
                default_model_id: document.getElementById('f-default-model').value || null,
            };
            try {
                await App.api('PUT', '/system/settings', data);
                App.toast(i18n('settings.saved'));
            } catch (err) {
                App.toast(err.message, 'danger');
            }
        };

        this.renderApiKey();

        this.renderInstall();
        // The install prompt often arrives after this page has rendered, and
        // installing/resetting changes what the box should say — re-render on
        // every PWA state change instead of snapshotting it once.
        PWA.onchange = () => this.renderInstall();

        this.checkStatus();
    },

    /** The API-key box. Re-rendered from the server's answer after every
     *  action, so what is displayed is always what the gate now enforces. */
    async renderApiKey() {
        const box = document.getElementById('api-key-box');
        if (!box) return;

        let st;
        try {
            st = await App.api('GET', '/system/api-key');
        } catch (e) {
            // Older server without the endpoint, or an unreachable one: say so
            // instead of drawing a form that cannot save.
            box.innerHTML = `<div class="text-secondary"><i class="bi bi-dash-circle"></i> ${App.esc(e.message)}</div>`;
            return;
        }

        let line, actions;
        if (!st.editable) {
            line = `<i class="bi bi-lock text-secondary"></i> ${i18n('settings.apiKeyEnv', { env: App.esc(st.env_var) })}`;
            actions = '';
        } else if (st.configured) {
            line = `<i class="bi bi-shield-check text-success"></i> ${i18n('settings.apiKeyOn')}`;
            actions = `<button type="button" class="btn btn-primary btn-sm" id="btn-key-save">${i18n('settings.apiKeySave')}</button>
                       <button type="button" class="btn btn-outline-secondary btn-sm ms-2" id="btn-key-gen">
                           <i class="bi bi-arrow-clockwise"></i> ${i18n('settings.apiKeyRotate')}
                       </button>
                       <button type="button" class="btn btn-outline-danger btn-sm ms-2" id="btn-key-off">${i18n('settings.apiKeyRemove')}</button>`;
        } else {
            line = `<i class="bi bi-shield-exclamation text-warning"></i> ${i18n('settings.apiKeyOff')}`;
            actions = `<button type="button" class="btn btn-primary btn-sm" id="btn-key-gen">
                           <i class="bi bi-shield-lock"></i> ${i18n('settings.apiKeyGenerate')}
                       </button>
                       <button type="button" class="btn btn-outline-secondary btn-sm ms-2" id="btn-key-save">${i18n('settings.apiKeySaveOwn')}</button>`;
        }

        // The key is shown in clear on purpose: the point of looking at it is
        // to carry it to another device. Whoever sees this page already got
        // past the gate with it.
        const link = st.configured
            ? `${location.origin}${location.pathname}?api_key=${encodeURIComponent(st.key)}`
            : '';

        box.innerHTML = `
            <div class="mb-2">${line}</div>
            <div class="input-group mb-1">
                <input type="text" class="form-control font-monospace" id="f-api-key"
                       value="${App.escAttr(st.key || '')}"
                       placeholder="${i18n('settings.apiKeyPlaceholder')}"
                       ${st.editable ? '' : 'readonly'}>
                <button type="button" class="btn btn-outline-secondary" id="btn-key-copy"
                        title="${i18n('settings.apiKeyCopy')}"><i class="bi bi-clipboard"></i></button>
            </div>
            <small class="text-secondary d-block mb-2">${i18n('settings.apiKeyHint')}</small>
            ${actions}
            ${link ? `<div class="mt-3">
                <label class="form-label small mb-1">${i18n('settings.apiKeyLink')}</label>
                <div class="input-group input-group-sm">
                    <input type="text" class="form-control font-monospace" id="f-api-key-link" value="${App.escAttr(link)}" readonly>
                    <button type="button" class="btn btn-outline-secondary" id="btn-link-copy"
                            title="${i18n('settings.apiKeyCopy')}"><i class="bi bi-clipboard"></i></button>
                </div>
            </div>` : ''}`;

        document.getElementById('btn-key-copy').onclick =
            () => this.copyField('f-api-key');
        const linkCopy = document.getElementById('btn-link-copy');
        if (linkCopy) linkCopy.onclick = () => this.copyField('f-api-key-link');

        const gen = document.getElementById('btn-key-gen');
        if (gen) gen.onclick = () => {
            // Rotating locks out every OTHER device that stored the old key.
            if (st.configured && !confirm(i18n('settings.apiKeyRotateConfirm'))) return;
            this.saveApiKey({ generate: true });
        };
        const save = document.getElementById('btn-key-save');
        if (save) save.onclick = () => {
            const key = document.getElementById('f-api-key').value.trim();
            this.saveApiKey({ key });
        };
        const off = document.getElementById('btn-key-off');
        if (off) off.onclick = () => {
            if (!confirm(i18n('settings.apiKeyRemoveConfirm'))) return;
            this.saveApiKey(null);
        };
    },

    /** Write the key server-side (null = turn auth off) and, on success, adopt
     *  it in THIS browser — the very next request already goes through the new
     *  gate, so skipping this step would 401 the page we are standing on. */
    async saveApiKey(payload) {
        try {
            const st = payload
                ? await App.api('PUT', '/system/api-key', payload)
                : await App.api('DELETE', '/system/api-key');
            App.apiKey = st.key || null;
            if (st.key) localStorage.setItem('myagent_api_key', st.key);
            else localStorage.removeItem('myagent_api_key');
            App.toast(i18n(st.key ? 'settings.apiKeySaved' : 'settings.apiKeyRemoved'));
        } catch (e) {
            App.toast(e.message, 'danger');
        }
        this.renderApiKey();
    },

    /** Copy an input's value. The clipboard API needs a secure context, which
     *  is exactly what a LAN http install does not have — fall back to
     *  selecting the text so the user can copy it by hand. */
    async copyField(id) {
        const input = document.getElementById(id);
        if (!input) return;
        try {
            await navigator.clipboard.writeText(input.value);
            App.toast(i18n('settings.apiKeyCopied'));
        } catch (e) {
            input.select();
            App.toast(i18n('settings.apiKeyCopyManual'), 'warning');
        }
    },

    /** The "Install app" box: one state at a time, never a dead button. */
    renderInstall() {
        const box = document.getElementById('install-box');
        // Guard: PWA.onchange outlives this page (the user may have navigated
        // away before the browser offered the prompt).
        if (!box) return;

        let line, actions = '';
        if (PWA.installed) {
            line = `<i class="bi bi-check-circle text-success"></i> ${i18n('settings.installDone')}`;
        } else if (!PWA.supported) {
            // Almost always plain http on a LAN address: the browser hides
            // both the worker and the install prompt, without saying why.
            line = `<i class="bi bi-exclamation-circle text-warning"></i> ${i18n('settings.installInsecure')}`;
        } else if (PWA.deferred) {
            line = i18n('settings.installHint');
            actions = `<button type="button" class="btn btn-primary btn-sm" id="btn-install">
                           <i class="bi bi-download"></i> ${i18n('settings.installAction')}
                       </button>`;
        } else {
            // Safari and Firefox never fire beforeinstallprompt; Chrome also
            // withholds it once the app is installed in another profile.
            line = PWA.isIos ? i18n('settings.installIos') : i18n('settings.installManual');
        }

        if (PWA.supported) {
            actions += `<button type="button" class="btn btn-outline-secondary btn-sm ms-2" id="btn-pwa-reset">
                            <i class="bi bi-arrow-clockwise"></i> ${i18n('settings.installClearCache')}
                        </button>`;
        }

        box.innerHTML = `<div class="mb-2">${line}</div>${actions}`;

        const install = document.getElementById('btn-install');
        if (install) install.onclick = () => PWA.install();
        const reset = document.getElementById('btn-pwa-reset');
        if (reset) reset.onclick = () => PWA.reset();
    },

    async checkStatus() {
        const container = document.getElementById('status-checks');
        let html = '';

        // Health
        try {
            await App.api('GET', '/system/health');
            html += `<div><i class="bi bi-check-circle text-success"></i> ${i18n('settings.apiOk')}</div>`;
        } catch (e) {
            html += `<div><i class="bi bi-x-circle text-danger"></i> ${i18n('settings.apiError')}</div>`;
        }

        // Ollama
        try {
            const models = await App.api('GET', '/models/ollama/available');
            html += `<div><i class="bi bi-check-circle text-success"></i> ${i18n('settings.ollamaModels', { count: models.length })}</div>`;
        } catch (e) {
            html += `<div><i class="bi bi-x-circle text-danger"></i> ${i18n('settings.ollamaUnreachable')}</div>`;
        }

        // llama.cpp — one line per registered instance (each has its own base_url)
        try {
            const res = await App.api('GET', '/models/llamacpp/status');
            const instances = res.instances || [];
            if (instances.length === 0) {
                html += `<div><i class="bi bi-dash-circle text-secondary"></i> ${i18n('settings.llamacppNone')}</div>`;
            }
            for (const inst of instances) {
                const label = App.esc(inst.name ? `${inst.name} (${inst.base_url})` : inst.base_url);
                if (inst.status === 'ok') {
                    html += `<div><i class="bi bi-check-circle text-success"></i> ${i18n('settings.llamacppOkNamed', { label })}</div>`;
                } else {
                    html += `<div><i class="bi bi-exclamation-circle text-warning"></i> ${i18n('settings.llamacppUnreachableNamed', { label })}</div>`;
                }
            }
        } catch (e) {
            html += `<div><i class="bi bi-exclamation-circle text-warning"></i> ${i18n('settings.llamacppUnreachable')}</div>`;
        }

        // Connectors plugin. Reuses the probe App already did at startup, so
        // this costs no extra request; the bot count comes from the plugin's own
        // summary and is only asked for when the plugin is actually there.
        const plugin = await App.plugin('connectors');
        if (!plugin) {
            html += `<div><i class="bi bi-dash-circle text-secondary"></i> ${i18n('settings.connectorsPluginOff')}</div>`;
        } else if (!plugin.loaded) {
            html += `<div><i class="bi bi-exclamation-circle text-warning"></i> ${i18n('connectors.loadFailed')} ${App.esc(plugin.error)}</div>`;
        } else {
            let count = 0;
            try {
                count = (await App.api('GET', '/connectors/status')).bindings || 0;
            } catch (e) { /* the line is informational; a failure just shows 0 */ }
            html += `<div><i class="bi bi-check-circle text-success"></i> ${i18n('settings.connectorsPluginOk', { n: count })}</div>`;
        }

        container.innerHTML = html;
    },
};
