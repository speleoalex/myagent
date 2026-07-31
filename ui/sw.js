/* MyAgent service worker — caches the static app shell so an installed UI
 * opens without the network.
 *
 * Three rules, each load-bearing:
 *
 * 1. NOTHING under /api/ is ever cached or served from cache. The shell is
 *    static and versioned; API answers are live state, and a stale agent list
 *    or a replayed chat turn is worse than an error. Requests to a REMOTE
 *    server (Settings → "MyAgent server") are cross-origin and skipped here
 *    too, so that case needs no extra rule.
 *
 * 2. index.html is fetched network-first. Every asset in it carries a ?v=N
 *    cache buster, so the HTML is the ONLY unversioned file — serve it from
 *    cache first and a UI upgrade would never be seen. Offline, the cached
 *    copy answers.
 *
 * 3. The precache list is DERIVED from index.html, not written out here.
 *    Duplicating the ?v=N stamps in two files means one of them rots; parsing
 *    the markup keeps index.html the single source of truth. A missed asset
 *    degrades to runtime caching, so the parse failing is not fatal.
 */

const CACHE = 'myagent-shell-v1';

// Referenced from CSS or the manifest rather than the markup, so the parser
// above cannot see them.
const EXTRA_ASSETS = [
    './',
    'manifest.webmanifest',
    'icons/icon-192.png',
    'icons/icon-512.png',
    'icons/icon-maskable-512.png',
    'icons/apple-touch-icon.png',
    'vendor/bootstrap-icons/fonts/bootstrap-icons.woff2',
];

/** Absolute URLs of every shell asset, given index.html's markup. */
function shellUrls(html) {
    const base = self.registration.scope;
    const urls = new Set(EXTRA_ASSETS.map(u => new URL(u, base).href));
    for (const m of html.matchAll(/\b(?:src|href)="([^"]+)"/g)) {
        const raw = m[1];
        // Route links, the inline SVG favicon, CDNs: not our static files.
        if (/^(#|data:|blob:|mailto:|https?:|\/\/)/.test(raw)) continue;
        const url = new URL(raw, base);
        if (url.origin === self.location.origin && url.href.startsWith(base)) {
            urls.add(url.href);
        }
    }
    return urls;
}

/** Cache what is missing and drop superseded ?v= copies of the same files. */
async function syncShell(html) {
    const wanted = shellUrls(html);
    const cache = await caches.open(CACHE);
    const have = new Set((await cache.keys()).map(r => r.url));
    await Promise.all([...wanted]
        .filter(u => !have.has(u))
        // One at a time, not addAll(): that is atomic, so a single 404 (an
        // asset removed from the markup we still list) would cache nothing.
        .map(u => cache.add(u).catch(() => {})));
    // Prune only entries carrying a cache buster: those are the ones that
    // accumulate on every upgrade, and the query makes them unambiguous.
    await Promise.all([...have]
        .filter(u => u.includes('?v=') && !wanted.has(u))
        .map(u => cache.delete(u)));
}

self.addEventListener('install', event => {
    event.waitUntil((async () => {
        try {
            const res = await fetch(new URL('index.html', self.registration.scope),
                                    { cache: 'reload' });
            if (res.ok) await syncShell(await res.text());
        } catch (e) {
            // Installed offline, or the server is down: the shell fills in on
            // the next successful navigation.
        }
        await self.skipWaiting();
    })());
});

self.addEventListener('activate', event => {
    event.waitUntil((async () => {
        const names = await caches.keys();
        await Promise.all(names.filter(n => n !== CACHE).map(n => caches.delete(n)));
        await self.clients.claim();
    })());
});

/** Not-cacheable: the live API, and FastAPI's own endpoints. */
function isApi(url) {
    return /^\/(api|docs|redoc|openapi\.json)(\/|$)/.test(url.pathname);
}

self.addEventListener('fetch', event => {
    const req = event.request;
    if (req.method !== 'GET') return;
    const url = new URL(req.url);
    if (url.origin !== self.location.origin || isApi(url)) return;

    if (req.mode === 'navigate') {
        event.respondWith((async () => {
            try {
                const res = await fetch(req);
                if (res.ok) {
                    // The response we just got IS index.html: re-derive the
                    // shell from it instead of fetching the page twice.
                    event.waitUntil(res.clone().text().then(syncShell).catch(() => {}));
                    (await caches.open(CACHE)).put(req, res.clone());
                }
                return res;
            } catch (e) {
                const cache = await caches.open(CACHE);
                return (await cache.match(req)) ||
                       (await cache.match(new URL('index.html', self.registration.scope))) ||
                       (await cache.match(self.registration.scope)) ||
                       Response.error();
            }
        })());
        return;
    }

    event.respondWith((async () => {
        const cached = await caches.match(req);
        if (cached) return cached;
        const res = await fetch(req);
        if (res.ok && res.type === 'basic') {
            (await caches.open(CACHE)).put(req, res.clone());
        }
        return res;
    })());
});
