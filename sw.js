/* sw.js — service worker.
 *
 * CACHE STRATEGY, and why it differs per file
 *
 *   HTML  -> NETWORK FIRST. This is the one that matters. The previous version
 *            served index.html cache-first under a version string that never
 *            changed, so anyone who had visited once kept that copy forever.
 *            A friend testing the app sat on a build from before the push keys
 *            were added and saw "not wired up yet" no matter what shipped.
 *            An app that cannot update itself is worse than no cache at all.
 *
 *   status.json -> NETWORK FIRST. It is the entire point of the app. A cached
 *            flag status is worse than a slow one. Cache is the offline
 *            fallback only, and the UI shows the age of what it displays.
 *
 *   icons, manifest -> cache first. They genuinely never change, and when they
 *            do the filename changes with them.
 *
 * Bump VERSION on any release that changes cached assets.
 */

const VERSION = 'flagstaff-v3';
const SHELL = ['./index.html', './manifest.json'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(VERSION)
      .then((c) => c.addAll(SHELL))
      .catch(() => {})            // a missing shell file must not brick install
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function networkFirst(request) {
  return fetch(request)
    .then((resp) => {
      const copy = resp.clone();
      caches.open(VERSION).then((c) => c.put(request, copy)).catch(() => {});
      return resp;
    })
    .catch(() => caches.match(request).then((hit) => hit || Response.error()));
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Any page navigation, plus the HTML itself.
  const isPage = req.mode === 'navigate'
    || url.pathname.endsWith('/')
    || url.pathname.endsWith('.html');

  if (isPage || url.pathname.endsWith('status.json')) {
    e.respondWith(networkFirst(req));
    return;
  }

  // Static assets: cache first, refresh quietly in the background.
  e.respondWith(
    caches.match(req).then((hit) => {
      const net = fetch(req).then((resp) => {
        const copy = resp.clone();
        caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
        return resp;
      }).catch(() => hit);
      return hit || net;
    })
  );
});

/* --- Push -----------------------------------------------------------------
 * Payload from the worker:
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
    body: d.reason || (half ? 'An order is now in effect.'
                            : 'No order is in effect.'),
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
