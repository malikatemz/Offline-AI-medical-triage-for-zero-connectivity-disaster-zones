// RescueNet — Service Worker
// Offline-first caching: app shell + API responses cached for zero-connectivity use

const CACHE_NAME = "rescuenet-v2";
const API_CACHE  = "rescuenet-api-v2";

// App shell — cache on install
const STATIC_ASSETS = [
  "/",
  "/manifest.json",
  "/icon-192.png",
  "/icon-512.png",
];

// ── Install: pre-cache app shell ─────────────────────────────────────────────
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log("[SW] Pre-caching app shell");
      return cache.addAll(STATIC_ASSETS);
    })
  );
  self.skipWaiting();
});

// ── Activate: purge old caches ───────────────────────────────────────────────
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME && k !== API_CACHE)
          .map((k) => {
            console.log("[SW] Deleting old cache:", k);
            return caches.delete(k);
          })
      )
    )
  );
  self.clients.claim();
});

// ── Fetch: offline-first strategy ────────────────────────────────────────────
self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // API calls — network first, fall back to cache
  if (url.pathname.startsWith("/triage") || url.pathname.startsWith("/system")) {
    event.respondWith(networkFirstWithCache(request, API_CACHE));
    return;
  }

  // Static assets — cache first
  event.respondWith(cacheFirstWithNetwork(request, CACHE_NAME));
});

// ── Strategies ────────────────────────────────────────────────────────────────

async function networkFirstWithCache(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request.clone());
    // Only cache successful GET responses
    if (response.ok && request.method === "GET") {
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    // Offline — return cached response if available
    const cached = await cache.match(request);
    if (cached) {
      console.log("[SW] Offline — serving cached API response:", request.url);
      return cached;
    }
    // No cache — return offline placeholder for health endpoint
    if (request.url.includes("/system/health")) {
      return new Response(
        JSON.stringify({
          connectivity: "C-BLACKOUT",
          offline_badge: true,
          model_loaded: null,
          qdrant_ready: null,
          redis_ready: null,
          ram_percent: null,
          cpu_percent: null,
          cpu_temp_c: null,
          _sw_note: "API unreachable — fully offline",
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    }
    return new Response(JSON.stringify({ error: "Offline — no cached response" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}

async function cacheFirstWithNetwork(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    return new Response("Offline", { status: 503 });
  }
}
