// Service worker for The Precon Ledger — offline + installable PWA.
// Strategy: stale-while-revalidate. Serve from cache instantly, refresh in
// the background. Only caches same-origin GET requests (skips CDN fonts).
const CACHE = "precon-ledger-v3";
const ASSETS = [
  "./",
  "./index.html",
  "./decks.json",
  "./prices.json",
  "./prices_history.json",
  "./manifest.webmanifest",
  "./icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(ASSETS)).catch(() => {})
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
  // Only handle same-origin requests; let the browser handle CDN fonts etc.
  if (!req.url.startsWith(self.location.origin)) return;
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
