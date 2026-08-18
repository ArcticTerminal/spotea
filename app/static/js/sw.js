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
//
// Bumped again to v3 for the same reason: v2 intercepted cross-origin
// requests, so its cache can hold opaque i.ytimg.com/yt3.ggpht.com entries
// that the fetch handler no longer has any use for.
//
// Bumped again to v4: two separate gaps in API_PREFIXES let per-profile
// data get cached that never should have been. First, its entries all
// carried a trailing slash ("/settings/", "/profiles/", ...), which never
// matches the *bare* route — "/settings" itself has no trailing slash, so
// `path.startsWith("/settings/")` was always false for it and it fell
// through into the *cached* branch below, the opposite of what this list
// exists to prevent. Second, /recommendations, /partials/* and
// /onboarding/* weren't listed at all. A v3 client could be holding a
// cached /settings (or /recommendations, or a /partials/* fragment)
// response from a profile other than whichever one is actually active now.
const CACHE_NAME = "spotea-v4";

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
//   - /partials/*, /recommendations, /settings and /onboarding/* are all
//     per-profile data (fragment refreshes, "For you", the interests editor,
//     onboarding's channel suggestions) — a stale cached copy served after a
//     profile switch is indistinguishable from the *previous* profile's data
//     leaking into the new one, which is exactly what a network hiccup while
//     switching used to look like before this list covered them.
// A transient network hiccup is enough to hit the catch() fallback below
// and serve one of these stale/wrong bodies.
const API_PREFIXES = [
  "/content",
  "/feeds",
  "/profiles",
  "/settings",
  "/storage",
  "/partials",
  "/recommendations",
  "/onboarding",
];

// A prefix match that also requires a path boundary right after it — plain
// startsWith("/settings") would be right for the bare route but would also
// wrongly swallow some future, unrelated "/settingsfoo" route; requiring the
// next character (if any) to be "/" keeps this exact without needing every
// prefix listed twice (with and without a trailing slash), which is the bug
// that let bare GETs like /settings and /profiles get cached in the first
// place — those routes carry no trailing slash of their own.
function isApiPath(path) {
  return API_PREFIXES.some((prefix) => path === prefix || path.startsWith(`${prefix}/`));
}

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

  const url = new URL(event.request.url);

  // Cross-origin requests are none of this worker's business, and handling
  // them actively broke things: an <img> pointing at i.ytimg.com is a no-cors
  // request whose response is opaque, and passing it through the fetch/clone/
  // cache.put path below made it fail outright. Measured against the live app
  // — with the worker registered, 23 of Explore's remote thumbnails failed
  // with ERR_FAILED; with it blocked, none did. Uncached Explore artwork was
  // therefore broken in the installed PWA and in any browser once the worker
  // had activated, while looking fine on the very first load before it did.
  //
  // This worker exists to make the app installable and to fall back to a
  // cached copy of our *own* assets when the network is down (see the header
  // above), and remote artwork is neither.
  if (url.origin !== self.location.origin) return;

  if (isApiPath(url.pathname)) return;

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
