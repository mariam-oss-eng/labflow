/* LabFlow service worker — minimal offline shell (v0.9).
 *
 * Strategy:
 *   - Install: precache the offline shell (HTML page + CSS).
 *   - Fetch: network-first for HTML/JSON to keep data fresh; cache-first
 *     for static assets to make repeat loads instant; offline fallback to
 *     the cached shell when the network is gone.
 *
 * No build step required — the file is served as-is from /static.
 */
const CACHE = "labflow-v1";
const SHELL = [
  "/",
  "/static/style.css",
  "/static/offline.html",
  "/static/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Static assets: cache-first.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }))
    );
    return;
  }

  // HTML / JSON: network-first with offline fallback.
  event.respondWith(
    fetch(req)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      })
      .catch(() =>
        caches.match(req).then((hit) => hit || caches.match("/static/offline.html"))
      )
  );
});
