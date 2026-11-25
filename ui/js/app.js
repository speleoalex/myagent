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
        this._trackNavHeight();
        // Not awaited: the other pages must not wait on the network to render.
        // Pages that need it await pluginsReady(), sharing this same promise.
        this.pluginsReady();
        // Which server this is — name and color in the navbar. Not awaited
        // either: the pages render, the brand fills in when /system/info
        // answers (renderHome awaits it, it wants the name in its title).
        this.instancePromise = this.loadInstance();
        window.addEventListener('hashchange', () => this.route());
        this._wireReveal();
        this._wireLogout();
        this.route();
        this.updateActiveNav();
    },

    /** Identity of the server this UI is talking to: {label, name, color,
     * hostname, timezone_name}, or null before /system/info has answered. Two
     * MyAgent instances look identical otherwise, and "which one am I on" is a
     * question the URL bar answers badly (a reverse proxy, an installed PWA
     * and a phone all hide it). */
    instance: null,
    instancePromise: null,

    async loadInstance() {
        let info = null;
        try { info = await this.api('GET', '/system/info'); } catch (e) { /* older server, or down */ }
        this.applyInstance(info);
        return this.instance;
    },

    /** Paint the identity into the chrome that is outside the SPA container.
     *
     * Label = the name the user gave in Settings -> Server, else the host
     * name: a server that was never named is still told apart from the next
     * one. The label replaces "MyAgent" as the brand, and the color fills the
     * whole navbar, which is visible from across the room in a way a word is
     * not. */
    applyInstance(info) {
        const name = ((info && info.instance_name) || '').trim();
        const hostname = ((info && info.hostname) || '').trim();
        const color = /^#[0-9a-fA-F]{6}$/.test((info && info.instance_color) || '')
            ? info.instance_color.toLowerCase() : '';
        const label = name || hostname;
        // The server's own zone, carried here so the Settings placeholder can
        // say what an empty timezone falls back to. '' on an older server.
        const timezone_name = ((info && info.timezone_name) || '').trim();
        this.instance = { label, name, color, hostname, timezone_name };
        // The working folder in use, its default (the Settings placeholder)
        // and whether MYAGENT_WORKSPACE pins it.
        this.instance.workspace_dir = (info && info.workspace_dir) || '';
        this.instance.workspace_default = (info && info.workspace_default) || '';
        this.instance.workspace_pinned = !!(info && info.workspace_pinned);

        const brandName = document.getElementById('brand-name');
        if (brandName) brandName.textContent = label || 'MyAgent';
        // The account the server runs as, on every page: an agent's shell
        // tools act with its rights, so "as whom" is not a detail for the
        // Settings page alone. Root is flagged, not just named.
        const user = ((info && info.user) || '').trim();
        const isRoot = !!(info && info.is_root);
        this.instance.user = user;
        this.instance.is_root = isRoot;
        const brandUser = document.getElementById('brand-user');
        if (brandUser) {
            brandUser.innerHTML = user ? `<i class="bi ${isRoot ? 'bi-exclamation-triangle-fill' : 'bi-person'}"></i> ${this.esc(user)}` : '';
            brandUser.title = i18n(isRoot ? 'nav.runsAsRoot' : 'nav.runsAs', { user });
            brandUser.classList.toggle('d-none', !user);
            brandUser.classList.toggle('brand-user-root', isRoot);
        }
        const nav = document.querySelector('nav.navbar');
        if (nav) {
            // bg-dark is !important, so the inline color must be too. A light
            // color flips the navbar to Bootstrap's light variant, which
            // re-colors links and the toggler icon for contrast in one go.
            if (color) nav.style.setProperty('background-color', color, 'important');
            else nav.style.removeProperty('background-color');
            nav.dataset.bsTheme = color && this._contrastText(color) === '#000' ? 'light' : 'dark';
        }
        document.title = label ? `${label} · MyAgent` : 'MyAgent';
        // iOS reads the home-screen name from this meta at "Add to Home
        // Screen" time, not from the manifest; Android colors the installed
        // app's title bar from theme-color. Same fallbacks as the brand.
        const appTitle = document.querySelector('meta[name="apple-mobile-web-app-title"]');
        if (appTitle) appTitle.content = label || 'MyAgent';
        const themeMeta = document.querySelector('meta[name="theme-color"]');
        if (themeMeta) themeMeta.content = color || '#212529';
        this._applyFavicon(label, color);
        this._applyAccent(color);
    },

    /** The instance color as the accent of the whole UI, not just the navbar:
     * Bootstrap 5.3 writes its blue as literals into buttons, checks and focus
     * rings, so style.css re-points those at the --accent-* variables set
     * here, under :root[data-accent]. Links get a variant darkened (light
     * theme) or lightened (dark theme) to 4.5:1, because a color picked for a
     * navbar is not necessarily readable as text on the page background. */
    _applyAccent(color) {
        const root = document.documentElement;
        const names = ['--accent', '--accent-rgb', '--accent-hover', '--accent-contrast',
            '--accent-link-light', '--accent-link-light-rgb', '--accent-link-dark', '--accent-link-dark-rgb'];
        if (!color) {
            delete root.dataset.accent;
            names.forEach(n => root.style.removeProperty(n));
            return;
        }
        const rgb = this._rgb(color);
        const css = (c) => `rgb(${c.join(',')})`;
        const linkLight = this._readableOn(rgb, [255, 255, 255]);
        const linkDark = this._readableOn(rgb, [33, 37, 41]);   // Bootstrap's dark body
        const values = [color, rgb.join(','), css(this._mix(rgb, [0, 0, 0], 0.15)),
            this._contrastText(color), css(linkLight), linkLight.join(','), css(linkDark), linkDark.join(',')];
        names.forEach((n, i) => root.style.setProperty(n, values[i]));
        root.dataset.accent = '';
    },

    _rgb(hex) {
        const n = parseInt(hex.slice(1), 16);
        return [n >> 16, (n >> 8) & 255, n & 255];
    },

    _mix(rgb, target, t) {
        return rgb.map((c, i) => Math.round(c + (target[i] - c) * t));
    },

    /** WCAG relative luminance of an [r, g, b] color. */
    _luminance(rgb) {
        const lum = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
        return 0.2126 * lum(rgb[0]) + 0.7152 * lum(rgb[1]) + 0.0722 * lum(rgb[2]);
    },

    /** `rgb` moved toward black or white, whichever is away from `bg`, just
     * far enough to read as text on it (contrast 4.5:1). */
    _readableOn(rgb, bg) {
        const lb = this._luminance(bg);
        const target = lb > 0.5 ? [0, 0, 0] : [255, 255, 255];
        for (let t = 0; t <= 1; t += 0.05) {
            const c = this._mix(rgb, target, t);
            const lc = this._luminance(c);
            if ((Math.max(lc, lb) + 0.05) / (Math.min(lc, lb) + 0.05) >= 4.5) return c;
        }
        return target;
    },

    /** With several MyAgent tabs open the favicon is all that shows of each:
     * the instance's initial on its color (grey when it has none). An unnamed
     * page keeps the robot. */
    _applyFavicon(label, color) {
        const link = document.querySelector('link[rel="icon"]');
        if (!link) return;
        if (!label) {
            link.href = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🤖</text></svg>";
            return;
        }
        const fill = color || '#6c757d';
        const initial = this.esc(Array.from(label)[0].toUpperCase());
        const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">`
            + `<circle cx="50" cy="50" r="50" fill="${fill}"/>`
            + `<text x="50" y="50" dy=".35em" text-anchor="middle" font-family="sans-serif" font-weight="bold" font-size="62" fill="${this._contrastText(fill)}">${initial}</text></svg>`;
        link.href = 'data:image/svg+xml,' + encodeURIComponent(svg);
    },

    /** Black or white, whichever reads on this background (WCAG luminance). */
    _contrastText(hex) {
        return this._luminance(this._rgb(hex)) > 0.4 ? '#000' : '#fff';
    },

    /** Publish the navbar's REAL height as --nav-h, so the CSS that sizes the
     * page below it (#app min-height, .chat-wrap) subtracts what is actually
     * there. It used to be two hard-coded pixel values (70 / 80) that drifted
     * from the rem-sized navbar, and the page overflowed by a few pixels —
     * a full-height scrollbar on the right for nothing. The navbar wraps
     * onto two rows on narrow screens, hence the observer, not a one-shot. */
    _trackNavHeight() {
        const nav = document.querySelector('nav.navbar');
        if (!nav) return;
        const apply = () => document.documentElement.style.setProperty(
            '--nav-h', `${Math.ceil(nav.getBoundingClientRect().height)}px`);
        apply();
        if (window.ResizeObserver) new ResizeObserver(apply).observe(nav);
        else window.addEventListener('resize', apply);
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
        document.querySelectorAll('[data-i18n-aria]').forEach(el => {
            el.setAttribute('aria-label', i18n(el.dataset.i18nAria));
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
     * silently talk to whatever host serves the static files. Same origin
     * means the folder the page was served from, not the host root, so the
     * UI also works behind a reverse proxy under a path (/myagent/). */
    apiUrl(path) {
        return `${this.serverBase || App.pageBase()}/api${path}`;
    },

    /** Path of the folder holding this page, without the trailing slash:
     * '' at the host root, '/myagent' when proxied under /myagent/. */
    pageBase() {
        return new URL('.', location.href).pathname.replace(/\/+$/, '');
    },

    /** URL of a workspace file served by GET /api/files/ (the resource
     * channel). The api key rides as a query param because the consumer is an
     * <img src> or a download link, which cannot send an Authorization header
     * (the middleware accepts both). Built at render time and never persisted,
     * so the key doesn't land in session files. For text/html resources use
     * viewer.html instead: a page's scripts can read their own URL, an image
     * cannot. */
    fileUrl(relPath, download) {
        const p = String(relPath || '').split('/').map(encodeURIComponent).join('/');
        const q = [];
        if (download) q.push('download=1');
        if (this.apiKey) q.push('api_key=' + encodeURIComponent(this.apiKey));
        return this.apiUrl('/files/' + p) + (q.length ? '?' + q.join('&') : '');
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

    /** Forget the key this browser holds and go back to the key form. The
     * key itself stays valid on the server (rotating it is Settings -> Access):
     * this ends THIS browser's access, e.g. on a shared or lost device. A turn
     * still generating keeps running server-side, as when a tab is closed. */
    _wireLogout() {
        document.getElementById('nav-logout')?.classList.toggle('d-none', !this.apiKey);
        const btn = document.getElementById('btn-logout');
        if (!btn) return;
        btn.onclick = () => {
            if (!confirm(i18n('nav.logoutConfirm'))) return;
            localStorage.removeItem('myagent_api_key');
            location.reload();  // the first request 401s and shows the key form
        };
    },

    authHeaders() {
        return this.apiKey ? { 'Authorization': `Bearer ${this.apiKey}` } : {};
    },

    // The server requires a key and ours is missing/wrong: ask for it once
    // (concurrent 401s share the form) and reload with the new key. The form
    // is a panel over the whole page, not inside #app: pages that catch the
    // same 401 render their error into #app and must not replace it.
    _handleUnauthorized() {
        if (!this._authPrompting) {
            this._authPrompting = true;
            const panel = document.createElement('div');
            panel.className = 'position-fixed top-0 start-0 w-100 h-100 bg-body d-flex align-items-center justify-content-center p-3';
            panel.style.zIndex = '2000';
            panel.setAttribute('role', 'dialog');
            panel.setAttribute('aria-modal', 'true');
            panel.setAttribute('aria-labelledby', 'api-key-title');
            panel.innerHTML = `
                <form class="card card-body shadow-sm w-100" style="max-width: 26rem">
                    <h1 class="h5 mb-2" id="api-key-title"><i class="bi bi-key" aria-hidden="true"></i> ${i18n('auth.title')}</h1>
                    <div class="fw-semibold mb-2 d-none" id="api-key-server"></div>
                    <p class="text-secondary small mb-3">${i18n('auth.hint')}</p>
                    ${this.apiKey ? `<div class="alert alert-danger py-2 small" role="alert">${i18n('auth.wrong')}</div>` : ''}
                    <input type="text" name="username" value="myagent" autocomplete="username" hidden>
                    <label class="form-label" for="api-key-input">${i18n('auth.label')}</label>
                    <input type="password" id="api-key-input" class="form-control" autocomplete="current-password" required>
                    <button type="submit" class="btn btn-primary mt-3">${i18n('auth.submit')}</button>
                </form>`;
            panel.querySelector('form').addEventListener('submit', evt => {
                evt.preventDefault();
                const key = panel.querySelector('#api-key-input').value.trim();
                if (!key) return;
                localStorage.setItem('myagent_api_key', key);
                location.reload();
            });
            document.body.appendChild(panel);
            panel.querySelector('#api-key-input').focus();
            // Which server is asking: /system/info is behind the key, the
            // manifest is not and already carries the name (server/main.py).
            // Same origin only: a UI hosted elsewhere serves the bare template.
            if (!this.serverBase) {
                fetch('manifest.webmanifest').then(r => r.json()).then(m => {
                    const el = panel.querySelector('#api-key-server');
                    if (el && m.name) { el.textContent = m.name; el.classList.remove('d-none'); }
                }).catch(() => { /* the form works without it */ });
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

    /** `<option>`s for a timezone datalist — the ONE definition, used by the
     * agent form and by Settings.
     *
     * The list comes from the BROWSER (`Intl.supportedValuesOf`), not from a
     * new endpoint: it is the same IANA database the server reads, it costs no
     * round trip, and a suggestion list that lags the server's zoneinfo is a
     * cosmetic problem — the server validates the value on save either way.
     * Older browsers without the API get an empty datalist, which degrades to a
     * plain text field rather than to a broken one. */
    timezoneOptions() {
        let zones = [];
        try {
            zones = Intl.supportedValuesOf('timeZone') || [];
        } catch (e) { zones = []; }
        return zones.map(z => `<option value="${this.escAttr(z)}"></option>`).join('');
    },

    /** Folder picker on the SERVER's filesystem (GET /system/browse): the
     * browser's own picker cannot be used, it shows the client's disk and
     * never returns a path. Resolves to the chosen absolute path, or null
     * when cancelled. `start` empty = the working directory. */
    pickFolder(start = '') {
        return new Promise(resolve => {
            document.body.insertAdjacentHTML('beforeend', `
                <div class="modal fade" id="folder-picker" tabindex="-1" aria-labelledby="folder-picker-title">
                    <div class="modal-dialog modal-dialog-scrollable modal-fullscreen-sm-down">
                        <div class="modal-content">
                            <div class="modal-header">
                                <h5 class="modal-title" id="folder-picker-title"><i class="bi bi-folder2-open"></i> ${i18n('folderPicker.title')}</h5>
                                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="${this.escAttr(i18n('folderPicker.cancel'))}"></button>
                            </div>
                            <div class="modal-body">
                                <div class="d-flex gap-2 mb-2">
                                    <button type="button" class="btn btn-outline-secondary" id="folder-picker-up" title="${this.escAttr(i18n('folderPicker.up'))}">
                                        <i class="bi bi-arrow-up"></i> <span class="d-none d-sm-inline">${i18n('folderPicker.up')}</span>
                                    </button>
                                    <button type="button" class="btn btn-outline-secondary" id="folder-picker-home" title="${this.escAttr(i18n('folderPicker.home'))}">
                                        <i class="bi bi-house"></i> <span class="d-none d-sm-inline">${i18n('folderPicker.home')}</span>
                                    </button>
                                </div>
                                <code class="d-block text-break mb-2" id="folder-picker-path"></code>
                                <div class="list-group" id="folder-picker-list"></div>
                            </div>
                            <div class="modal-footer">
                                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">${i18n('folderPicker.cancel')}</button>
                                <button type="button" class="btn btn-primary" id="folder-picker-ok">${i18n('folderPicker.choose')}</button>
                            </div>
                        </div>
                    </div>
                </div>`);
            const el = document.getElementById('folder-picker');
            const list = el.querySelector('#folder-picker-list');
            let current = null;
            let chosen = null;

            const open = async (path) => {
                list.innerHTML = `<div class="list-group-item"><span class="spinner-border spinner-border-sm"></span> ${i18n('folderPicker.loading')}</div>`;
                let data;
                try {
                    data = await this.api('GET', '/system/browse?path=' + encodeURIComponent(path || ''));
                } catch (e) {
                    // A typed path that does not exist (yet): start from the workspace.
                    if (path) return open('');
                    list.innerHTML = `<div class="list-group-item text-danger">${this.esc(e.message)}</div>`;
                    return;
                }
                current = data;
                el.querySelector('#folder-picker-path').textContent = data.path;
                el.querySelector('#folder-picker-up').disabled = !data.parent;
                el.querySelector('#folder-picker-home').onclick = () => open(data.home);
                const sep = data.path.includes('\\') && !data.path.includes('/') ? '\\' : '/';
                const join = (name) => data.path.endsWith(sep) ? data.path + name : data.path + sep + name;
                if (!data.readable) {
                    list.innerHTML = `<div class="list-group-item text-warning-emphasis"><i class="bi bi-lock"></i> ${i18n('folderPicker.unreadable')}</div>`;
                } else if (!data.dirs.length) {
                    list.innerHTML = `<div class="list-group-item text-secondary">${i18n('folderPicker.empty')}</div>`;
                } else {
                    list.innerHTML = data.dirs.map(d => `
                        <button type="button" class="list-group-item list-group-item-action py-2" style="min-height:44px" data-path="${this.escAttr(join(d))}">
                            <i class="bi bi-folder"></i> ${this.esc(d)}
                        </button>`).join('');
                    list.querySelectorAll('[data-path]').forEach(b => { b.onclick = () => open(b.dataset.path); });
                }
            };

            el.querySelector('#folder-picker-up').onclick = () => current && current.parent && open(current.parent);
            el.querySelector('#folder-picker-ok').onclick = () => {
                if (!current) return;
                chosen = current.path;
                modal.hide();
            };
            el.addEventListener('hidden.bs.modal', () => { el.remove(); resolve(chosen); });
            const modal = new bootstrap.Modal(el);
            modal.show();
            open(start);
        });
    },

    /** Wire an "Explore" button to fill a text input from pickFolder(). The
     * typed value is the starting point, so the picker opens where it points. */
    wireFolderPicker(buttonId, inputId) {
        const btn = document.getElementById(buttonId);
        const input = document.getElementById(inputId);
        if (!btn || !input) return;
        btn.onclick = async () => {
            const path = await this.pickFolder(input.value.trim());
            if (path === null) return;
            input.value = path;
            input.dispatchEvent(new Event('input', { bubbles: true }));
        };
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

    /** The one thing the counters can't say: will a message get an answer?
     * Nothing is drawn when all is well — this page is seen constantly, and a
     * permanent green banner is noise. Driven strictly off `ready === false`,
     * never off how many models happen to be configured. */
    readinessBanner(r) {
        if (!r) return '';
        if (r.ready === false) {
            return `
                <div class="alert alert-warning text-start mt-3">
                    <strong>${this.esc(i18n('home.notReady'))}</strong>
                    <div class="small mt-1">${this.esc(r.detail || '')}</div>
                    <div class="mt-2">
                        <a href="#/models" class="btn btn-sm btn-warning">${this.esc(i18n('home.notReadyModels'))}</a>
                        <a href="#/settings" class="btn btn-sm btn-outline-secondary">${this.esc(i18n('nav.settings'))}</a>
                    </div>
                </div>`;
        }
        if (r.auto) {
            return `
                <div class="alert alert-secondary text-start mt-3 small">
                    ${this.esc(i18n('home.autoModel', { model: r.model || '' }))}
                    <a href="#/settings">${this.esc(i18n('home.autoModelFix'))}</a>
                </div>`;
        }
        return '';
    },

    /** Compact timestamp for dashboard rows: time alone if today, otherwise a
     * short date + time. Not a duration — the reader just needs to place the
     * row ("this morning" vs "last week") at a glance. */
    fmtWhen(iso) {
        const d = iso ? new Date(iso) : null;
        if (!d || isNaN(d.getTime())) return '';
        const loc = I18n.getDateLocale();
        const time = d.toLocaleTimeString(loc, { hour: '2-digit', minute: '2-digit' });
        if (d.toDateString() === new Date().toDateString()) return time;
        return `${d.toLocaleDateString(loc, { day: 'numeric', month: 'short' })} ${time}`;
    },

    async renderHome() {
        // Each call caught on its own: the page degrades panel by panel (an
        // older server without /tasks must not blank the whole page).
        const [agents, tasks, ready] = await Promise.all([
            // The agents a user can talk to: the same list the chat offers.
            this.api('GET', '/agents?selectable=true').catch(() => []),
            this.api('GET', '/tasks').catch(() => []),
            // Whether a message would actually get an answer.
            this.api('GET', '/system/ready').catch(() => null),
        ]);

        // Runtime state worth a line, and only when there is some: what runs
        // on its own (autonomous agents, scheduled runs).
        const liveCount = agents.filter(a => a.live).length;
        const nextRun = tasks.filter(t => t.enabled !== false && t.next_at)
            .map(t => t.next_at).sort()[0];
        const status = [
            liveCount ? `<a href="#/agents">${this.esc(i18n('home.liveAgents', { n: liveCount }))}</a>` : '',
            nextRun ? `<a href="#/tasks">${this.esc(i18n('home.nextRun', { when: this.fmtWhen(nextRun) }))}</a>` : '',
        ].filter(Boolean).join(' · ');

        // The page is about what can be done here: one card per agent, each
        // opening a chat with it. The question box above is the shortcut for
        // "I don't know which one": a new chat in auto mode.
        const card = (a) => `
            <div class="col-12 col-sm-6 col-lg-4">
                <a href="#/chat/${this.escAttr(a.id)}" class="card h-100 home-agent text-decoration-none">
                    <div class="card-body">
                        <div class="fw-semibold mb-1">
                            <i class="bi bi-chat-dots text-primary me-1" aria-hidden="true"></i>${this.esc(a.name || a.id)}
                            ${a.live ? `<span class="badge text-bg-success ms-1">${this.esc(i18n('home.liveBadge'))}</span>` : ''}
                        </div>
                        ${a.description ? `<div class="small text-secondary home-agent-desc">${this.esc(a.description)}</div>` : ''}
                    </div>
                </a>
            </div>`;

        this.container.innerHTML = `
            <div class="home-wrap mx-auto mt-3">
                <div class="text-center">
                    ${this.readinessBanner(ready)}
                    <form id="home-ask" class="home-ask mx-auto mt-3">
                        <div class="input-group input-group-lg">
                            <input type="text" id="home-ask-input" class="form-control" autocomplete="off"
                                   placeholder="${this.escAttr(i18n('home.askPlaceholder'))}"
                                   aria-label="${this.escAttr(i18n('home.askPlaceholder'))}">
                            <button class="btn btn-primary" type="submit" aria-label="${this.escAttr(i18n('home.askSend'))}">
                                <i class="bi bi-send"></i>
                            </button>
                        </div>
                    </form>
                    <a href="#/chat" class="d-inline-block mt-2 py-2">
                        <i class="bi bi-chat-text"></i> ${this.esc(i18n('home.continueChat'))}
                    </a>
                    ${status ? `<div class="small text-secondary mt-1">${status}</div>` : ''}
                </div>
                <h2 class="h5 mt-4 mb-3">${this.esc(i18n('home.capabilities'))}</h2>
                ${agents.length ? `<div class="row g-3">${agents.map(card).join('')}</div>` : `
                <div class="text-secondary">${this.esc(i18n('chat.noAgents'))}
                    <a href="#/agents/new">${this.esc(i18n('chat.createAgent'))}</a></div>`}
            </div>`;

        document.getElementById('home-ask').addEventListener('submit', (evt) => {
            evt.preventDefault();
            const text = document.getElementById('home-ask-input').value.trim();
            if (!text) return;
            // Picked up by ChatPage.render: a fresh chat in auto mode, sent.
            ChatPage.pendingPrompt = text;
            location.hash = '#/chat';
        });
    },
};

document.addEventListener('DOMContentLoaded', () => App.init());
