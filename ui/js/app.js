const App = {
    container: null,
    apiKey: null,
    // Base URL of the MyAgent server ('' = same origin as the page). The UI is
    // static HTML, so it can be hosted by any web server (Apache, nginx, a file
    // share) and pointed back at the API — this is that pointer, per browser.
    serverBase: '',
    _authPrompting: false,
    // Installed plugins, keyed by id (null until the first probe resolves).
    plugins: null,
    _pluginsPromise: null,
    // The one live timer of the current page, cleared on navigation.
    pageInterval: null,

    init() {
        this.container = document.getElementById('app');
        this.serverBase = this._initServerBase();
        this.apiKey = this._initApiKey();
        I18n.init();
        this.applyStaticI18n();
        // Not awaited: the other pages must not wait on the network to render.
        // Pages that need it await pluginsReady(), sharing this same promise.
        this.pluginsReady();
        window.addEventListener('hashchange', () => this.route());
        this._wireReveal();
        this.route();
        this.updateActiveNav();
    },

    /** Markup for the "show it" button beside a secret input. A password field
     * you cannot read is a field you cannot check for a typo, and these are
     * pasted values (API keys, bearer tokens, a device's shared key) where one
     * wrong character produces an authentication error that names nothing.
     *
     * Put it inside a Bootstrap .input-group with the input. */
    revealButton(inputId) {
        return `<button type="button" class="btn btn-outline-secondary" tabindex="-1"
                data-reveal="${this.escAttr(inputId)}" aria-pressed="false"
                title="${this.escAttr(i18n('common.reveal'))}"
                aria-label="${this.escAttr(i18n('common.reveal'))}"><i class="bi bi-eye"></i></button>`;
    },

    /** One delegated listener, installed once. The SPA re-renders whole forms,
     * so per-button wiring would have to be redone by every page that grows a
     * secret field — and the one that forgets ships a dead button. */
    _wireReveal() {
        document.addEventListener('click', (event) => {
            const btn = event.target.closest?.('[data-reveal]');
            if (!btn) return;
            const input = document.getElementById(btn.dataset.reveal);
            if (!input) return;
            const show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            btn.setAttribute('aria-pressed', String(show));
            btn.title = btn.ariaLabel = i18n(show ? 'common.hide' : 'common.reveal');
            btn.innerHTML = `<i class="bi bi-eye${show ? '-slash' : ''}"></i>`;
        });
    },

    /** Which optional plugins this server has, probed once per page load.
     *
     * Menu entries marked [data-plugin="<id>"] start hidden in the HTML and are
     * revealed here. That direction is deliberate: on a server without the
     * plugin nothing ever appears, and the alternative (render then hide) makes
     * a menu entry visibly vanish on every load. */
    pluginsReady() {
        if (!this._pluginsPromise) {
            this._pluginsPromise = this.api('GET', '/plugins')
                .catch(() => ({ plugins: [] }))
                .then(res => {
                    this.plugins = {};
                    (res.plugins || []).forEach(p => { this.plugins[p.id] = p; });
                    document.querySelectorAll('[data-plugin]').forEach(el => {
                        el.classList.toggle('d-none', !this.plugins[el.dataset.plugin]);
                    });
                    this.updateActiveNav();
                    return this.plugins;
                });
        }
        return this._pluginsPromise;
    },

    /** The plugin's record, or undefined when it isn't installed. A record with
     * loaded=false means installed but broken — worth showing, not hiding. */
    async plugin(id) {
        return (await this.pluginsReady())[id];
    },

    /** One repeating timer at a time, owned by the current page.
     *
     * route() clears it, so a page can poll without cleaning up after itself.
     * Guarding inside the callback instead would leave the timer alive for the
     * whole session and stack a new one on every re-render. */
    setPageInterval(fn, ms) {
        clearInterval(this.pageInterval);
        this.pageInterval = setInterval(fn, ms);
    },

    // Translate the static chrome (navbar labels) that lives outside the SPA
    // container. Page content is translated on (re-)render via i18n() calls.
    // Language and theme controls live in the Settings screen.
    applyStaticI18n() {
        document.querySelectorAll('[data-i18n]').forEach(el => {
            el.textContent = i18n(el.dataset.i18n);
        });
        document.querySelectorAll('[data-i18n-title]').forEach(el => {
            el.title = i18n(el.dataset.i18nTitle);
        });
    },

    route() {
        // Stop the previous page's polling before anything else: its callback
        // would otherwise keep fetching (and writing into a DOM that is gone).
        clearInterval(this.pageInterval);
        this.pageInterval = null;

        // A Bootstrap modal (e.g. the chat history window) may still be open.
        // SPA re-renders replace #app without Bootstrap's own cleanup, which
        // would leave a stuck backdrop and a scroll-locked body — clear those.
        document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
        document.body.classList.remove('modal-open');
        document.body.style.removeProperty('overflow');
        document.body.style.removeProperty('padding-right');

        // Mobile: close the collapsed navbar menu after navigating (hash links
        // don't reload the page, so Bootstrap would leave it open over the content).
        const navMenu = document.getElementById('navMenu');
        if (navMenu && navMenu.classList.contains('show')) {
            bootstrap.Collapse.getOrCreateInstance(navMenu).hide();
        }

        const hash = location.hash || '#/';
        const parts = hash.slice(2).split('/');
        const page = parts[0] || '';
        const params = parts.slice(1);

        switch (page) {
            case 'agents':  AgentsPage.render(params); break;
            case 'tools':   ToolsPage.render(params); break;
            case 'models':  ModelsPage.render(params); break;
            case 'chat':    ChatPage.render(params); break;
            case 'tasks':   TasksPage.render(params); break;
            case 'settings': SettingsPage.render(params); break;
            case 'connectors': ConnectorsPage.render(params); break;
            default:        this.renderHome(); break;
        }
        this.updateActiveNav();
    },

    updateActiveNav() {
        const hash = location.hash || '#/';
        document.querySelectorAll('.nav-link').forEach(link => {
            const href = link.getAttribute('href');
            // Only route links (#/...) participate; skip the language/theme chrome.
            if (!href || !href.startsWith('#/')) return;
            link.classList.toggle('active', hash.startsWith(href));
        });
    },

    // Server address handling, same one-shot URL pattern as the API key below:
    // ?server=http://host:8888 stores the address and is stripped from the URL;
    // ?server= (empty) clears it back to same-origin. Persisted in localStorage
    // (a browser preference, like theme and language — the server form in
    // Settings edits the SERVER's settings, this decides which server that is).
    _initServerBase() {
        const url = new URL(location.href);
        const fromUrl = url.searchParams.get('server');
        if (fromUrl !== null) {
            const clean = this.normalizeServerBase(fromUrl);
            if (clean) localStorage.setItem('myagent_server', clean);
            else localStorage.removeItem('myagent_server');
            url.searchParams.delete('server');
            history.replaceState(null, '', url);
        }
        return localStorage.getItem('myagent_server') || '';
    },

    /** '' for same-origin, otherwise the address without its trailing slash —
     * apiUrl() glues '/api...' right after it. */
    normalizeServerBase(raw) {
        return String(raw || '').trim().replace(/\/+$/, '');
    },

    /** Absolute URL of an API endpoint on the configured server.
     * EVERY request must be built here — a literal '/api/...' fetch would
     * silently talk to whatever host serves the static files. */
    apiUrl(path) {
        return `${this.serverBase}/api${path}`;
    },

    // API key handling (only enforced when the server sets MYAGENT_API_KEY):
    // accept ?api_key=... in the page URL once — stored locally, then stripped
    // from the address bar so it doesn't linger in bookmarks/history.
    _initApiKey() {
        const url = new URL(location.href);
        const fromUrl = url.searchParams.get('api_key');
        if (fromUrl) {
            localStorage.setItem('myagent_api_key', fromUrl.trim());
            url.searchParams.delete('api_key');
            history.replaceState(null, '', url);
        }
        return localStorage.getItem('myagent_api_key') || null;
    },

    authHeaders() {
        return this.apiKey ? { 'Authorization': `Bearer ${this.apiKey}` } : {};
    },

    // The server requires a key and ours is missing/wrong: ask for it once
    // (concurrent 401s share the prompt) and reload with the new key.
    _handleUnauthorized() {
        if (!this._authPrompting) {
            this._authPrompting = true;
            const key = window.prompt(i18n('auth.promptKey'));
            if (key && key.trim()) {
                localStorage.setItem('myagent_api_key', key.trim());
                location.reload();
            } else {
                this._authPrompting = false;
            }
        }
        throw new Error(i18n('auth.required'));
    },

    async api(method, path, body = null) {
        const opts = {
            method,
            headers: { 'Content-Type': 'application/json', ...this.authHeaders() },
        };
        if (body) opts.body = JSON.stringify(body);
        const res = await fetch(this.apiUrl(path), opts);
        if (res.status === 401) this._handleUnauthorized();
        if (!res.ok) {
            const text = await res.text();
            throw new Error(this.errorText(text) || `HTTP ${res.status}`);
        }
        return res.json();
    },

    /** The human part of a failed response. FastAPI answers `{"detail": …}`, and
     * throwing the raw body meant every form showed the user a JSON blob
     * ('{"detail":"Invalid credentials: …"}' — screenshotted). Unwrapped here,
     * once, because every caller renders err.message. `detail` is a string for
     * an HTTPException and a list of {msg, loc} for a validation error. */
    errorText(body) {
        try {
            const detail = JSON.parse(body).detail;
            if (typeof detail === 'string') return detail;
            if (Array.isArray(detail)) {
                return detail.map(e => e.msg || JSON.stringify(e)).join('; ');
            }
        } catch (e) { /* not JSON (a proxy error page, a stack): show as it came */ }
        return body;
    },

    toast(message, type = 'success') {
        const id = 'toast-' + Date.now();
        const html = `
            <div id="${id}" class="toast align-items-center text-bg-${type} border-0 show position-fixed bottom-0 end-0 m-3" style="z-index:9999">
                <div class="d-flex">
                    <div class="toast-body">${this.esc(message)}</div>
                    <button type="button" class="btn-close btn-close-white me-2 m-auto" onclick="document.getElementById('${id}').remove()"></button>
                </div>
            </div>`;
        document.body.insertAdjacentHTML('beforeend', html);
        setTimeout(() => document.getElementById(id)?.remove(), 3000);
    },

    esc(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    },

    /** Escape for use INSIDE an attribute value.
     *
     * esc() serializes a text node, which escapes &, < and > but NOT quotes —
     * safe in text position, unsafe in `attr="..."`, where a quote in the value
     * closes the attribute and lets the rest inject further attributes (e.g. an
     * event handler). Required for any attribute fed by text we do not control,
     * such as an MCP server's error output. */
    escAttr(str) {
        return App.esc(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    slugify(text) {
        return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    },

    autoId(nameInputId, idInputId) {
        const nameEl = document.getElementById(nameInputId);
        const idEl = document.getElementById(idInputId);
        if (!nameEl || !idEl || idEl.readOnly) return;
        nameEl.addEventListener('input', () => {
            idEl.value = this.slugify(nameEl.value);
        });
    },

    async renderHome() {
        let agentCount = 0, modelCount = 0, toolCount = 0;
        try {
            const [agents, models, tools] = await Promise.all([
                this.api('GET', '/agents'),
                this.api('GET', '/models'),
                this.api('GET', '/tools'),
            ]);
            agentCount = agents.length;
            modelCount = models.length;
            toolCount = tools.length;
        } catch (e) { /* ignore */ }

        this.container.innerHTML = `
            <div class="row mt-4">
                <div class="col-md-8 mx-auto text-center">
                    <h1><i class="bi bi-robot"></i> MyAgent</h1>
                    <p class="lead text-secondary">${i18n('home.subtitle')}</p>
                    <div class="row mt-4 g-3">
                        <div class="col-md-4">
                            <a href="#/agents" class="text-decoration-none">
                                <div class="card text-center p-3">
                                    <h2>${agentCount}</h2>
                                    <div class="text-secondary">${i18n('home.agents')}</div>
                                </div>
                            </a>
                        </div>
                        <div class="col-md-4">
                            <a href="#/models" class="text-decoration-none">
                                <div class="card text-center p-3">
                                    <h2>${modelCount}</h2>
                                    <div class="text-secondary">${i18n('home.models')}</div>
                                </div>
                            </a>
                        </div>
                        <div class="col-md-4">
                            <a href="#/tools" class="text-decoration-none">
                                <div class="card text-center p-3">
                                    <h2>${toolCount}</h2>
                                    <div class="text-secondary">${i18n('home.tools')}</div>
                                </div>
                            </a>
                        </div>
                    </div>
                </div>
            </div>`;
    },
};

document.addEventListener('DOMContentLoaded', () => App.init());
