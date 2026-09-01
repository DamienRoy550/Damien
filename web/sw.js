/* App-shell caching. The API is never cached (its responses are per-user and
 * change constantly); only the shell is, so a launch with the network off opens to
 * a working UI whose reads and writes go to the local server. */
const CACHE = "jarvis-shell-v4";
const SHELL = [
  "/web/index.html",
  "/web/styles.css",
  "/web/app.js",
  "/web/auth-return.js",
  "/web/manifest.webmanifest",
  "/web/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;            // always live
  if (SHELL.includes(url.pathname)) {                        // cache-first shell
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
        return response;
      }))
    );
    return;
  }
  // navigation fallback: opening the app offline should still render
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match("/web/index.html").then((r) => r || Response.error()))
    );
  }
});
