const SettingsPage = {
    async render() {
        let settings = {};
        try { settings = await App.api('GET', '/system/settings'); } catch (e) { /* empty */ }

        let models = [];
        try { models = await App.api('GET', '/models'); } catch (e) { /* empty */ }
        // Only a LOCAL model can embed — see the select below for why.
        const localModels = models.filter(m => m.provider === 'ollama' || m.provider === 'llamacpp');

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
                            <label class="form-label">${i18n('settings.embeddingModel')}</label>
                            <!-- LOCAL providers only, and this filter is NOT the
                                 enforcement: indexing sends the CONTENTS of every
                                 document to this endpoint, so app/engine/embedding.py
                                 refuses a remote one whatever is stored. Listing
                                 remote models here would only offer a 400. -->
                            <select class="form-select" id="f-embedding-model">
                                <option value="">${i18n('settings.noEmbeddingModel')}</option>
                                ${localModels.map(m => `<option value="${App.escAttr(m.id)}" ${m.id === settings.embedding_model_id ? 'selected' : ''}>${App.esc(m.name)} (${App.esc(m.provider)})</option>`).join('')}
                            </select>
                            <small class="text-secondary">${i18n('settings.embeddingModelHint')}</small>
                            <div id="index-rebuild-warn" class="form-text text-warning-emphasis d-none">
                                <i class="bi bi-exclamation-triangle"></i> ${i18n('settings.embeddingModelChanged')}
                            </div>
                            ${localModels.length ? '' : `<div class="form-text">
                                <i class="bi bi-info-circle"></i> ${i18n('settings.noEmbeddingModelHint')}
                                <code>ollama pull embeddinggemma:300m</code>
                            </div>`}
                            <div id="index-status" class="mt-2"></div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('settings.ollamaUrl')}</label>
                            <input type="text" class="form-control" id="f-ollama-url" value="${App.esc(settings.ollama_base_url || 'http://localhost:11434')}">
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('settings.llamacppUrl')}</label>
                            <input type="text" class="form-control" id="f-llamacpp-url" value="${App.esc(settings.llamacpp_base_url || 'http://localhost:8080')}">
                        </div>
                        <div class="mb-3 form-check form-switch">
                            <input class="form-check-input" type="checkbox" id="f-debug"
                                   ${settings.debug ? 'checked' : ''}>
                            <label class="form-check-label" for="f-debug">${i18n('settings.debug')}</label>
                            <div class="form-text">${i18n('settings.debugHint')}</div>
                            <div id="debug-box" class="mt-2"></div>
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

                    <!-- Server identity: which account the process runs as
                         and which directories it really uses. Read-only. -->
                    <h5>${i18n('settings.server')}</h5>
                    <div id="server-info" class="mb-3">
                        <div class="spinner-border spinner-border-sm"></div> ${i18n('settings.checking')}
                    </div>

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
                embedding_model_id: document.getElementById('f-embedding-model').value || null,
                debug: document.getElementById('f-debug').checked,
            };
            try {
                await App.api('PUT', '/system/settings', data);
                App.toast(i18n('settings.saved'));
                // Choosing an embedder is what turns indexing on: show the
                // state now, not on the next visit to this page.
                settings.embedding_model_id = data.embedding_model_id;
                settings.debug = data.debug;
                this.renderDebugBox();
                document.getElementById('index-rebuild-warn').classList.add('d-none');
                this.renderIndexStatus();
            } catch (err) {
                App.toast(err.message, 'danger');
            }
        };

        // Changing the embedder discards every index. Say so BEFORE the save,
        // not after hours of re-indexing.
        document.getElementById('f-embedding-model').onchange = (e) => {
            const prev = settings.embedding_model_id || '';
            document.getElementById('index-rebuild-warn')
                .classList.toggle('d-none', !prev || e.target.value === prev);
        };

        this.renderApiKey();

        this.renderIndexStatus();

        this.renderDebugBox();

        this.renderInstall();
        // The install prompt often arrives after this page has rendered, and
        // installing/resetting changes what the box should say — re-render on
        // every PWA state change instead of snapshotting it once.
        PWA.onchange = () => this.renderInstall();

        this.loadServerInfo();

        this.checkStatus();
    },

    /** The semantic index box, drawn under the embedding-model select.
     *
     * It exists because the indexer is otherwise INVISIBLE: it competes with
     * the chat model for the same backend, so "why is the assistant slow right
     * now" needs an answer, and there has to be a way to stop it. Only
     * exceptional state is drawn — with no embedder chosen nothing is wrong,
     * semantic search is simply off, and the box says nothing at all.
     */
    async renderIndexStatus() {
        const box = document.getElementById('index-status');
        if (!box) return;                       // navigated away mid-fetch
        let st = null;
        try { st = await App.api('GET', '/index/status'); } catch (e) { /* no service */ }
        if (!st) { box.innerHTML = ''; return; }

        if (st.problem) {
            box.innerHTML = `<div class="form-text text-danger">
                <i class="bi bi-exclamation-triangle"></i>
                ${App.esc(i18n('settings.indexProblem', { problem: st.problem }))}</div>`;
            return;
        }
        if (!st.configured || !st.roots.length) { box.innerHTML = ''; return; }

        const rows = st.roots.map(r => {
            const pct = r.total ? Math.round(100 * r.indexed / r.total) : 0;
            const running = r.state === 'running';
            const done = r.indexed >= r.total && r.total > 0 && r.state !== 'paused';
            const label = done ? i18n('settings.indexDone', { chunks: r.chunks })
                : i18n(`settings.indexState.${r.state}`, { n: r.indexed, total: r.total });
            const btn = r.state === 'paused'
                ? `<button type="button" class="btn btn-sm btn-outline-success" data-index-start="${App.escAttr(r.key)}">
                       <i class="bi bi-play-fill"></i> ${i18n('settings.indexResume')}</button>`
                : (done ? '' : `<button type="button" class="btn btn-sm btn-outline-danger" data-index-stop="${App.escAttr(r.key)}">
                       <i class="bi bi-stop-fill"></i> ${i18n('settings.indexStop')}</button>`);
            return `<div class="d-flex align-items-center gap-2 small py-1">
                <span class="text-truncate flex-grow-1" title="${App.escAttr(r.root)}">
                    ${running ? '<span class="spinner-border spinner-border-sm me-1"></span>' : ''}
                    <code>${App.esc(r.root)}</code> — ${App.esc(label)}
                    ${r.error ? `<span class="text-danger" title="${App.escAttr(r.error)}">
                        <i class="bi bi-exclamation-triangle"></i></span>` : ''}
                </span>
                ${!done && r.total ? `<div class="progress flex-shrink-0" style="width:80px;height:6px">
                    <div class="progress-bar" style="width:${pct}%"></div></div>` : ''}
                ${btn}
            </div>`;
        }).join('');

        box.innerHTML = `<div class="border rounded p-2">
            <div class="small text-secondary mb-1">${i18n('settings.indexTitle')}</div>${rows}</div>`;

        box.querySelectorAll('[data-index-stop]').forEach(b => {
            b.onclick = () => this.indexAction(b.dataset.indexStop, 'stop');
        });
        box.querySelectorAll('[data-index-start]').forEach(b => {
            b.onclick = () => this.indexAction(b.dataset.indexStart, 'start');
        });

        // Poll only while something is actually moving, and STOP when it is
        // not: setPageInterval replaces the timer but never cancels it, so
        // simply not re-arming would leave the previous one polling forever
        // on a page that has nothing left to say.
        if (st.roots.some(r => r.state === 'running' || r.state === 'queued')) {
            App.setPageInterval(() => this.renderIndexStatus(), 3000);
        } else {
            clearInterval(App.pageInterval);
        }
    },

    async indexAction(key, action) {
        try {
            await App.api('POST', `/index/${encodeURIComponent(key)}/${action}`);
        } catch (err) {
            App.toast(err.message, 'danger');
        }
        this.renderIndexStatus();
    },

    /** State of the debug trace, under its switch.

     * Only exceptional state is drawn: with tracing off the box says nothing
     * at all. With it on it must say WHERE the file is and HOW BIG it has got,
     * because what is accumulating there is the full text of every
     * conversation — and it offers the one click that removes it.
     */
    async renderDebugBox() {
        const box = document.getElementById('debug-box');
        const sw = document.getElementById('f-debug');
        if (!box || !sw) return;                 // navigated away mid-fetch
        let st = null;
        try { st = await App.api('GET', '/system/debug'); } catch (e) { /* older server */ }
        if (!st) { box.innerHTML = ''; return; }

        sw.checked = st.enabled;
        const files = (st.files || []).filter(f => st.enabled || f.size);
        // Drawn when tracing is on, and ALSO when it is off but a file is still
        // there: that is the state where something has to say "there is a
        // transcript of your conversations on disk" and offer the delete.
        if (!files.length) { box.innerHTML = ''; return; }

        box.innerHTML = `<div class="border rounded p-2 small">${files.map(f => `
            <div class="d-flex align-items-center gap-2 py-1">
                <span class="badge text-bg-secondary">${App.esc(i18n('settings.debugFile.' + f.key))}</span>
                <code class="text-truncate flex-grow-1">${App.esc(f.path)}</code>
                <span class="text-secondary">${App.esc(this.humanSize(f.size))}</span>
                <button type="button" class="btn btn-sm btn-outline-secondary"
                        data-debug-view="${App.escAttr(f.key)}">
                    <i class="bi bi-eye"></i> ${i18n('settings.debugView')}</button>
                <button type="button" class="btn btn-sm btn-outline-danger"
                        data-debug-clear="${App.escAttr(f.key)}">
                    <i class="bi bi-trash"></i></button>
            </div>`).join('')}
            <pre id="debug-tail" class="mt-2 mb-0 d-none" style="max-height:40vh;overflow:auto"></pre>
        </div>`;

        box.querySelectorAll('[data-debug-view]').forEach(b => {
            b.onclick = async () => {
                const pre = document.getElementById('debug-tail');
                const key = b.dataset.debugView;
                if (!pre.classList.contains('d-none') && pre.dataset.key === key) {
                    pre.classList.add('d-none'); return;
                }
                try {
                    const r = await App.api('GET',
                        `/system/debug/log/${encodeURIComponent(key)}?tail=400`);
                    // textContent, never innerHTML: these files hold whatever
                    // the user and the model wrote, markup included.
                    pre.textContent = r.lines.join('\n') || i18n('settings.debugEmpty');
                    pre.dataset.key = key;
                    pre.classList.remove('d-none');
                    pre.scrollTop = pre.scrollHeight;
                } catch (err) { App.toast(err.message, 'danger'); }
            };
        });
        box.querySelectorAll('[data-debug-clear]').forEach(b => {
            b.onclick = async () => {
                if (!confirm(i18n('settings.debugConfirmClear'))) return;
                try {
                    await App.api('DELETE',
                        `/system/debug/log/${encodeURIComponent(b.dataset.debugClear)}`);
                    App.toast(i18n('settings.debugCleared'));
                } catch (err) { App.toast(err.message, 'danger'); }
                this.renderDebugBox();
            };
        });
    },

    humanSize(n) {
        if (!n) return '0 B';
        const u = ['B', 'KB', 'MB', 'GB'];
        const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
        return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${u[i]}`;
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

    async loadServerInfo() {
        const box = document.getElementById('server-info');
        let info;
        try {
            info = await App.api('GET', '/system/info');
        } catch (e) {
            box.innerHTML = `<div class="text-secondary"><i class="bi bi-dash-circle"></i> ${i18n('settings.serverUnavailable')}</div>`;
            return;
        }
        const row = (label, value, extra = '') =>
            `<tr><th class="text-nowrap fw-normal text-secondary pe-3">${label}</th><td><code>${App.esc(String(value ?? '?'))}</code>${extra}</td></tr>`;
        const rootWarn = info.is_root
            ? ` <span class="badge text-bg-warning ms-1">${i18n('settings.serverRoot')}</span>` : '';
        box.innerHTML = `
            <table class="table table-sm table-borderless mb-0 w-auto">
                ${row(i18n('settings.serverUser'), info.uid != null ? `${info.user} (uid ${info.uid})` : info.user, rootWarn)}
                ${row(i18n('settings.serverHost'), info.hostname)}
                ${row(i18n('settings.serverHome'), info.home_dir)}
                ${row(i18n('settings.serverCode'), info.app_dir)}
                ${row(i18n('settings.serverProcess'), `pid ${info.pid} · Python ${info.python} · ${info.platform}`)}
            </table>`;
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
