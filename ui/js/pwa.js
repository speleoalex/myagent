/**
 * Installability: service-worker registration + the install prompt.
 *
 * Loaded from <head> because `beforeinstallprompt` fires once and cannot be
 * replayed — miss it and the Install button in Settings never lights up. The
 * event is stashed here so any page can offer the prompt later.
 *
 * Both halves need a SECURE CONTEXT (https, or localhost). MyAgent binds
 * 127.0.0.1 by default, so the normal setup qualifies; reaching it over plain
 * http on a LAN address does not, and the browser silently offers neither. We
 * detect that and say so rather than showing a button that does nothing.
 */
const PWA = {
    // The deferred `beforeinstallprompt` event, or null when the browser has
    // not offered one (already installed, criteria unmet, or Safari/Firefox,
    // which never fire it).
    deferred: null,
    registration: null,
    // Settings hooks this to re-render when the prompt arrives after the page.
    onchange: null,

    get supported() {
        return 'serviceWorker' in navigator && window.isSecureContext;
    },

    /** True when running as an installed app rather than a browser tab. */
    get installed() {
        return window.matchMedia('(display-mode: standalone)').matches ||
               window.matchMedia('(display-mode: window-controls-overlay)').matches ||
               navigator.standalone === true;
    },

    get isIos() {
        const ua = navigator.userAgent;
        // iPadOS 13+ claims to be a Mac; the touch points give it away.
        return /iPad|iPhone|iPod/.test(ua) ||
               (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
    },

    init() {
        window.addEventListener('beforeinstallprompt', (e) => {
            // Without this the browser shows its own mini-infobar and never
            // hands us the event.
            e.preventDefault();
            this.deferred = e;
            this._changed();
        });
        window.addEventListener('appinstalled', () => {
            this.deferred = null;
            this._changed();
        });
        if (!this.supported) return;
        // After load: the worker's install step refetches index.html and its
        // assets, which must not compete with the page's own first paint.
        window.addEventListener('load', () => {
            // Relative path on purpose — the UI may be served from a subpath,
            // and the worker's scope is the directory it is served from.
            navigator.serviceWorker.register('sw.js')
                .then(reg => { this.registration = reg; this._changed(); })
                .catch(() => { /* nothing to offer; Settings reports it */ });
        });
    },

    _changed() {
        if (typeof this.onchange === 'function') this.onchange();
    },

    /** Show the browser's install prompt. Returns true if the user accepted. */
    async install() {
        if (!this.deferred) return false;
        const evt = this.deferred;
        // Single-use: a second prompt() on the same event throws.
        this.deferred = null;
        evt.prompt();
        const { outcome } = await evt.userChoice;
        this._changed();
        return outcome === 'accepted';
    },

    /** Drop the offline cache and the worker, then reload from the network.
     *
     * The escape hatch for a shell that cached badly (an edited asset whose
     * ?v= was not bumped, a half-written deploy). Cheap to offer and it saves
     * explaining browser devtools to someone running this on a phone. */
    async reset() {
        if ('serviceWorker' in navigator) {
            const regs = await navigator.serviceWorker.getRegistrations();
            await Promise.all(regs.map(r => r.unregister()));
        }
        if (window.caches) {
            const names = await caches.keys();
            await Promise.all(names.map(n => caches.delete(n)));
        }
        location.reload();
    },
};

PWA.init();
