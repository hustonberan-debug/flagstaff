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
 *   config.js -> NETWORK FIRST. It holds the push key and endpoint, and it
 *            does change. Served cache-first, a returning visitor ran one
 *            visit on the old endpoint after every edit to it.
 *
 *   icons, manifest -> cache first. They genuinely never change, and when they
 *            do the filename changes with them.
 *
 * Bump VERSION on any release that changes cached assets or this file's
 * caching rules. HTML does not need a bump: it is network-first.
 */

const VERSION = 'flagstaff-v6';
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
        // PREFS holds what is needed to reconnect alerts; a version bump
        // must not throw it away with the old asset cache.
        keys.filter((k) => k !== VERSION && k !== PREFS).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function networkFirst(request) {
  return fetch(request)
    .then((resp) => {
      // Only a good response replaces the offline copy. Caching a 404 or
      // 500 status.json would make it the thing served when offline.
      if (resp.ok) {
        const copy = resp.clone();
        caches.open(VERSION).then((c) => c.put(request, copy)).catch(() => {});
      }
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

  if (isPage || url.pathname.endsWith('status.json')
      || url.pathname.endsWith('config.js')) {
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

  // Three outcomes, not two. Anything that is not explicitly half or full
  // used to fall through to "back to full staff" — a false all-clear.
  const where = d.state && d.state !== 'US' ? ' in ' + d.state : '';
  const half = d.status === 'half';
  const full = d.status === 'full';
  const title = half ? `Flags to half-staff${where}`
    : full ? `Flags back to full staff${where}`
    : `Flag status unclear${where}`;

  e.waitUntil(self.registration.showNotification(title, {
    body: d.reason || (half ? 'An order is now in effect.'
                       : full ? 'No order is in effect.'
                       : 'Check the official source before relying on this.'),
    icon: './icon-192.png',
    badge: './icon-192.png',
    tag: 'flag-' + (d.state || 'us'),   // replaces rather than stacks
    renotify: true,
    data: { url: d.url || './index.html' },
  }));
});

/* --- Subscription rotation -------------------------------------------------
 * A browser can replace a push subscription at any time: the push service
 * expires it, or rotates its keys. It says so once, here. There was no
 * handler, so the Worker kept sending to the dead endpoint and the person
 * simply stopped hearing from us - with nothing, on either side, to show it.
 *
 * The page mirrors what is needed to reconnect (the Worker's address, the
 * public key and the states subscribed to) into PREFS, because a service
 * worker cannot read the page's localStorage.
 */
const PREFS = 'flagstaff-prefs';
const PREFS_KEY = './__prefs.json';

async function readPrefs() {
  try {
    const hit = await (await caches.open(PREFS)).match(PREFS_KEY);
    return hit ? await hit.json() : {};
  } catch (_) { return {}; }
}
async function writePrefs(p) {
  try {
    await (await caches.open(PREFS)).put(PREFS_KEY,
      new Response(JSON.stringify(p), { headers: { 'Content-Type': 'application/json' } }));
  } catch (_) { /* nothing to do; the page re-checks on its next visit */ }
}
function b64ToU8(s) {
  const pad = '='.repeat((4 - s.length % 4) % 4);
  const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}
async function postWorker(base, path, body) {
  const r = await fetch(base + path, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json().catch(() => ({}));
}

self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'prefs') e.waitUntil(writePrefs(e.data.prefs || {}));
});

self.addEventListener('pushsubscriptionchange', (e) => {
  e.waitUntil((async () => {
    const p = await readPrefs();
    if (!p.base || !p.states || !p.states.length) return;   // never subscribed
    try {
      const key = (e.oldSubscription && e.oldSubscription.options
                   && e.oldSubscription.options.applicationServerKey)
                  || (p.vapid && b64ToU8(p.vapid));
      const sub = e.newSubscription
        || await self.registration.pushManager.subscribe({ userVisibleOnly: true,
                                                           applicationServerKey: key });
      for (const state of p.states) {
        await postWorker(p.base, '/subscribe', { subscription: sub.toJSON(), state });
      }
      // Only a different endpoint is dropped. A rotation can keep the
      // endpoint and change only the keys, and dropping it would delete the
      // subscription just re-registered above.
      if (e.oldSubscription && e.oldSubscription.endpoint !== sub.endpoint) {
        await postWorker(p.base, '/unsubscribe',
                         { endpoint: e.oldSubscription.endpoint }).catch(() => {});
      }
      await writePrefs(Object.assign(p, { endpoint: sub.endpoint, lost: null }));
    } catch (err) {
      // Could not reconnect. Say so - the one thing that must not happen is
      // silence that looks exactly like "no flag news".
      await writePrefs(Object.assign(p, { lost: new Date().toISOString() }));
      await self.registration.showNotification('Half Staff Now alerts stopped', {
        body: 'Your browser reset its alert connection and it could not be '
            + 'restored. Open Half Staff Now to turn alerts back on.',
        icon: './icon-192.png', badge: './icon-192.png', tag: 'flagstaff-alerts-lost',
        data: { url: './index.html#alerts' },
      });
    }
  })());
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
