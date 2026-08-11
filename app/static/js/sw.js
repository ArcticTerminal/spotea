// Exists to make the app installable (Chrome/Android requires an active
// service worker with a fetch handler before it'll offer "Install app") —
// not to turn this into an offline-first app. Network-first: try the
// network, fall back to the last cached copy only when that fails. Static
// assets are already served with Cache-Control: no-cache (see
// RevalidatingStaticFiles in main.py) specifically so an upgrade's new
// CSS/JS is picked up on the very next load; a cache-first strategy here
// would quietly work against that. Library/queue data is always fetched
// fresh whenever the network is up — this only kicks in when it isn't.
// Bumped from v1: earlier versions cached /content/... API responses
// (including audio stream Range chunks and error bodies — see API_PREFIXES
// below), so any client still holding a v1 cache needs it purged, not just
// left alone because the name didn't change.
const CACHE_NAME = "spotea-v2";

// These routers (see app/routers/*.py) are all live API traffic, never
// static assets — caching them is actively harmful, not just useless:
//   - /content/{id}/stream serves the <audio> element, which issues Range
//     requests while seeking. The Cache API keys purely on URL and knows
//     nothing about Range, so a cached response for one byte range gets
//     replayed for a request asking for a totally different range.
//   - Every dynamic GET here can legitimately 404/409 (not-ready content,
//     a since-deleted row, ...); nothing here checks response.ok before
//     caching, so an error body can get cached and later replayed as if
//     it were a real payload.
// A transient network hiccup is enough to hit the catch() fallback below
// and serve one of these stale/wrong bodies — to the <audio> tag, that's
// indistinguishable from a real playback error.
const API_PREFIXES = ["/content/", "/feeds/", "/profiles/", "/settings/", "/storage/"];

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  const path = new URL(event.request.url).pathname;
  if (API_PREFIXES.some((prefix) => path.startsWith(prefix))) return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
