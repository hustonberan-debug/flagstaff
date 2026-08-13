/* sw.js — service worker.
 *
 * Two jobs:
 *   1. Serve the app shell offline so a dead connection shows the last known
 *      status rather than a browser error page.
 *   2. Receive push notifications. On iOS this ONLY works once the app has
 *      been added to the home screen — which is why the install prompt is a
 *      gate on the notification flow, not a nice-to-have.
 *
 * CACHE STRATEGY, and the reason it differs per file:
 *   shell (html/css/js/manifest) -> cache-first. It rarely changes and we want
 *                                   instant loads.
 *   status.json                  -> network-first. It is the whole point of
 *                                   the app; a cached flag status is worse
 *                                   than a slow one. We fall back to cache
 *                                   only when the network fails, and the UI
 *                                   shows the age of what it is displaying.
 */

const VERSION = 'flagstaff-v1';
const SHELL = [
  './',
  './index.html',
  './manifest.json',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(VERSION)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())   // a missing shell file must not brick install
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;

  // status.json: always try the network first.
  if (url.pathname.endsWith('status.json')) {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          const copy = r.clone();
          caches.open(VERSION).then((c) => c.put(e.request, copy));
          return r;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Everything else: cache first, refresh in the background.
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request))
  );
});

/* --- Push -----------------------------------------------------------------
 * Payload shape the server should send:
 *   { "state": "NE", "status": "half", "reason": "...", "url": "..." }
 */
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = {}; }

  const half = d.status === 'half';
  const title = half
    ? `Flags to half-staff${d.state ? ' in ' + d.state : ''}`
    : `Flags back to full staff${d.state ? ' in ' + d.state : ''}`;

  e.waitUntil(self.registration.showNotification(title, {
    body: d.reason || (half ? 'An order is now in effect.' : 'No order is in effect.'),
    icon: './icon-192.png',
    badge: './icon-192.png',
    tag: 'flag-' + (d.state || 'us'),   // replaces rather than stacks
    renotify: true,
    data: { url: d.url || './index.html' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || './index.html';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((list) => {
        for (const c of list) {
          if ('focus' in c) { c.navigate(target); return c.focus(); }
        }
        return self.clients.openWindow(target);
      })
  );
});
