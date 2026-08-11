// Exists to make the app installable (Chrome/Android requires an active
// service worker with a fetch handler before it'll offer "Install app") —
// not to turn this into an offline-first app. Network-first: try the
// network, fall back to the last cached copy only when that fails. Static
// assets are already served with Cache-Control: no-cache (see
// RevalidatingStaticFiles in main.py) specifically so an upgrade's new
// CSS/JS is picked up on the very next load; a cache-first strategy here
// would quietly work against that. Library/queue data is always fetched
// fresh whenever the network is up — this only kicks in when it isn't.
const CACHE_NAME = "spotea-v1";

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
