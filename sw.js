// Service worker for MTG Tracker: offline support and an installable PWA.
//
// Three strategies, chosen per request:
//   - PAGE (navigations): network first. Every copy of the app shell is stored
//     under one key, ./index.html, whatever query string the URL carries
//     (filters and share links live in the query), so an old copy can never
//     shadow a newer deploy. The cached shell is used when the network fails,
//     answers with an error, or stalls past NETWORK_TIMEOUT_MS (the network
//     answer still refreshes the cache for next time).
//   - DATA (*.json): network first, revalidated with the server (ETag, so an
//     unchanged file costs a 304, not a full download), and rewritten in the
//     cache only when its version changed. Kept in its own cache so a shell
//     version bump does not wipe offline prices. The cached copy is used on
//     the same three conditions as the shell; the page shows the data's own
//     date, so the fallback stays honest.
//   - ASSETS (manifest, icons): stale while revalidate, only for the files in
//     ASSETS, so the cache cannot grow without limit.
const PREFIX = "mtg-tracker-";
const CACHE = PREFIX + "v7";         // bump whenever this file or ASSETS change
const DATA_CACHE = PREFIX + "data";  // not versioned: survives shell updates
// Caches this app has ever created (precon-ledger-* is its former name).
// Cache Storage is shared by every project on the cooknrice.github.io origin,
// so activate only ever deletes these.
const OWN_CACHE_RE = /^(mtg-tracker-|precon-ledger-)/;
const SHELL_KEY = "./index.html";
const ASSETS = [
  "./manifest.webmanifest",
  "./icon.svg",
  "./icon-192.png",
  "./icon-512.png",
  "./icon-maskable-192.png",
  "./icon-maskable-512.png",
  "./apple-touch-icon.png",
];
const ASSET_URLS = new Set(ASSETS.map((u) => new URL(u, self.location).href));
const DATA_RE = /\.json$/i;
// The JSON the page loads at startup (cards_index.json loads only on demand).
const DATA_PRECACHE = ["./decks.json", "./prices.json", "./prices_history.json"];
// How long a navigation or data request waits on the network before it
// answers from the cache instead (only when a cached copy exists).
const NETWORK_TIMEOUT_MS = 4000;

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // cache: "reload" skips the HTTP cache, so the precache is the live deploy.
    // The shell is required: if it fails, the install fails and the previous
    // worker keeps running with its cache intact. Assets are best effort; any
    // missed now are cached on first use.
    await cache.add(new Request(SHELL_KEY, { cache: "reload" }));
    await Promise.all([
      ...ASSETS.map((u) => cache.add(new Request(u, { cache: "reload" })).catch(() => {})),
      // The first visit fetches its JSON before this worker controls the page,
      // so store it now too (best effort) or that visit would not work offline.
      // The "no-cache" mode revalidates, so the copy the page just loaded
      // costs a 304.
      ...DATA_PRECACHE.map((u) => {
        const url = new URL(u, self.location);
        return fetch(url, { cache: "no-cache" }).then((res) =>
          res.status === 200 ? storeData(url.origin + url.pathname, res) : null,
        ).catch(() => {});
      }),
    ]);
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (!OWN_CACHE_RE.test(name) || name === CACHE || name === DATA_CACHE) continue;
      // Versions up to v6 kept the JSON in the shell cache: carry it over so
      // offline prices survive this update. Best effort: if the copy fails
      // (QuotaExceededError, say), the old cache is still deleted and the
      // worker still claims its clients.
      try {
        const data = await caches.open(DATA_CACHE);
        const old = await caches.open(name);
        for (const req of await old.keys()) {
          const url = new URL(req.url);
          if (url.origin !== self.location.origin || !DATA_RE.test(url.pathname)) continue;
          const key = url.origin + url.pathname;
          if (!(await data.match(key))) await data.put(key, await old.match(req));
        }
      } catch (e) {}
      await caches.delete(name).catch(() => {});
    }
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;  // skip CDN fonts etc.

  if (DATA_RE.test(url.pathname)) event.respondWith(fromData(event, url));
  else if (req.mode === "navigate") event.respondWith(fromPage(event, url));
  else if (ASSET_URLS.has(url.origin + url.pathname)) event.respondWith(fromAsset(event));
});

// Resolves to TIMED_OUT after ms. A connection that stalls instead of failing
// (weak signal, a captive portal) would otherwise hold the cached copy back
// until the browser's own network timeout, which can take tens of seconds.
const TIMED_OUT = Symbol("timed out");
function timer(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms, TIMED_OUT));
}

async function fromPage(event, url) {
  const scope = new URL(self.registration.scope).pathname;
  const isShell = url.pathname === scope || url.pathname === scope + "index.html";
  // Fetch the app by its one URL (static hosting ignores the query), so the
  // worker's request for any filtered URL reuses one HTTP cache entry and
  // revalidates with a 304.
  const req = isShell
    ? new Request(SHELL_KEY, { cache: event.request.cache, mode: "same-origin" })
    : event.request;
  const network = (async () => {
    const res = await fetch(req);
    // A navigation must not be answered with a redirected response: if the
    // host redirects index.html, let the browser follow its own request.
    return isShell && res.redirected ? fetch(event.request) : res;
  })();
  // Store a good shell even when the timer below already answered from the
  // cache. This callback is registered first, so it clones the body before
  // the page can start reading it.
  event.waitUntil(network.then((res) => {
    if (!isShell || res.status !== 200 || res.type !== "basic") return;
    const copy = res.clone();
    return caches.open(CACHE).then((cache) => cache.put(SHELL_KEY, copy));
  }).catch(() => {}));
  const settled = network.catch(() => null);  // null: the network failed
  const cachedShell = () => caches.open(CACHE).then((cache) => cache.match(SHELL_KEY));

  let res = isShell ? await Promise.race([settled, timer(NETWORK_TIMEOUT_MS)]) : await settled;
  if (res === TIMED_OUT) {
    const cached = await cachedShell();
    if (cached) return cached;
    res = await settled;  // nothing cached yet: keep waiting for the network
  }
  // Offline: any page in scope opens the app. Server error on the app itself
  // (an outage, a bad deploy): serve the last good shell instead.
  if (!res || (isShell && res.status >= 400)) {
    const cached = await cachedShell();
    if (cached) return cached;
  }
  return res || Response.error();
}

async function fromData(event, url) {
  const key = url.origin + url.pathname;
  const network = fetch(event.request, { cache: "no-cache" });
  // Registered before anything reads the body, as in fromPage.
  event.waitUntil(network.then((res) => {
    if (res.status === 200) return storeData(key, res.clone());
  }).catch(() => {}));
  const settled = network.catch(() => null);  // null: the network failed
  const cache = await caches.open(DATA_CACHE);

  let res = await Promise.race([settled, timer(NETWORK_TIMEOUT_MS)]);
  if (res === TIMED_OUT) {
    // A stalled network: answer from the cache (the page shows the data's own
    // date) and let the fetch above refresh it for the next visit.
    const cached = await cache.match(key);
    if (cached) return cached;
    res = await settled;
  }
  if (res && res.ok) return res;
  return (await cache.match(key)) || res || Response.error();
}

// Puts a 200 response into DATA_CACHE unless the cached copy is the same
// version. A revalidated 304 reaches the worker as a 200, so without this
// check every page view would rewrite about 0.8 MB of unchanged JSON.
async function storeData(key, res) {
  const cache = await caches.open(DATA_CACHE);
  const old = await cache.match(key);
  if (old && sameVersion(old, res)) {
    if (res.body) res.body.cancel().catch(() => {});  // drop the unused copy
    return;
  }
  await cache.put(key, res);
}

function sameVersion(a, b) {
  const etag = b.headers.get("ETag");
  if (etag) return etag === a.headers.get("ETag");
  // No ETag (a plain static server): fall back to Last-Modified plus length.
  const modified = b.headers.get("Last-Modified");
  return !!modified && modified === a.headers.get("Last-Modified")
    && b.headers.get("Content-Length") === a.headers.get("Content-Length");
}

async function fromAsset(event) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(event.request, { ignoreSearch: true });
  const network = fetch(event.request).then((res) => {
    if (res.status === 200 && !new URL(event.request.url).search) {
      return cache.put(event.request, res.clone()).then(() => res);
    }
    return res;
  });
  if (!cached) return network;
  event.waitUntil(network.catch(() => {}));
  return cached;
}
