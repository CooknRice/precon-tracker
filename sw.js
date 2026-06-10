// Service worker for The Precon Ledger — offline + installable PWA.
//
// Two strategies, chosen per request:
//   - DATA (*.json): network-FIRST. Always try the network so daily price
//     updates show immediately; fall back to cache only when offline.
//     (Stale-while-revalidate caused day-old prices/box-history to show.)
//   - SHELL (html, icons, manifest): stale-while-revalidate for instant load.
const CACHE = "precon-ledger-v4";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon.svg",
];
// Same-origin paths treated as live data → network-first.
const DATA_RE = /\.json(\?.*)?$/i;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  if (!req.url.startsWith(self.location.origin)) return;  // skip CDN fonts etc.

  if (DATA_RE.test(new URL(req.url).pathname)) {
    // NETWORK-FIRST for JSON data: fresh prices win; cache is offline fallback.
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        try {
          const res = await fetch(req, { cache: "no-store" });
          if (res && res.status === 200) cache.put(req, res.clone());
          return res;
        } catch (e) {
          const cached = await cache.match(req);
          return cached || Response.error();
        }
      })
    );
    return;
  }

  // STALE-WHILE-REVALIDATE for the app shell.
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(req);
      const network = fetch(req)
        .then((res) => {
          if (res && res.status === 200) cache.put(req, res.clone());
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
