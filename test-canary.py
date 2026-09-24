"""Tests for canary.py: a canary that has only ever passed proves nothing.

Each failure the canary exists for is reproduced here and must be caught:
wiped VAPID keys, conflict markers in status.json, a stale status.json, the
sample-data fallback, a default state nobody chose, a stale build served
through the service worker, a stale site version, JavaScript errors.

The file checks run anywhere. The browser scenarios need Playwright and run
a broken copy of the site locally; they are skipped (and say so) without it.
"""

import http.server
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import canary as C

ok = bad = 0


def t(label, got, want):
    global ok, bad
    if got == want:
        ok += 1
        print(f"  PASS  {label}")
    else:
        bad += 1
        print(f"  FAIL  {label}\n        want {want!r}\n        got  {got!r}")


def caught(problems, needle):
    return any(needle in p for p in problems)


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
GOOD = {"generated_at": "2026-09-24T11:40:00+00:00", "meta": {"covered": 43},
        "states": {f"S{i:02d}": {"effective_status": "full"} for i in range(51)}}
KEY = "BLxqFxBsh6CnsC4KfpQHg-0oBDF8s-A0gGz7E8A8T-odCsVtFN0-DZvdL2DUokAHJWH0ErkFwNjbArTejV9i6Ao"
CONFIG = f"""window.FLAGSTAFF_CONFIG = {{
  VAPID_PUBLIC_KEY: '{KEY}',
  SUBSCRIBE_ENDPOINT: 'https://halfstaff-push.example.workers.dev/subscribe',
}};"""

print("\n--- status.json ---")
t("a good status.json passes", C.check_status(json.dumps(GOOD), NOW), [])
t("git conflict markers are caught",
  caught(C.check_status("<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> x\n", NOW), "conflict markers"), True)
t("a file that does not parse is caught", caught(C.check_status("{nope", NOW), "does not parse"), True)
t("a status.json 3 hours old is caught",
  caught(C.check_status(json.dumps(dict(GOOD, generated_at="2026-09-24T09:00:00+00:00")), NOW),
         "stopped publishing"), True)
t("the sample payload is caught",
  caught(C.check_status(json.dumps(dict(GOOD, _sample=True)), NOW), "sample"), True)
t("missing states are caught",
  caught(C.check_status(json.dumps(dict(GOOD, states={"OH": {}})), NOW), "1 states"), True)
t("collapsed coverage is caught",
  caught(C.check_status(json.dumps(dict(GOOD, meta={"covered": 12})), NOW), "only 12"), True)

print("\n--- config.js and the Worker ---")
t("a good config.js passes", C.check_config(CONFIG), [])
t("WIPED VAPID keys are caught (the bug that stopped all subscriptions)",
  caught(C.check_config(CONFIG.replace(KEY, "")), "no VAPID_PUBLIC_KEY"), True)
t("a truncated key is caught",
  caught(C.check_config(CONFIG.replace(KEY, KEY[:40])), "not a valid"), True)
t("a missing endpoint is caught",
  caught(C.check_config(CONFIG.replace("SUBSCRIBE_ENDPOINT", "X")), "SUBSCRIBE_ENDPOINT"), True)
t("the Worker holding a different key is caught",
  caught(C.check_worker_key("BAAAA", KEY), "DIFFERENT"), True)
t("a Worker with no key is caught", caught(C.check_worker_key(None, KEY), "did not return"), True)
t("matching keys pass", C.check_worker_key(KEY, KEY), [])

print("\n--- the site version ---")
latest = ("a" * 40, NOW - timedelta(hours=3))
t("the latest version passes", C.check_version({"site_commit": "a" * 40}, latest, NOW), [])
t("a stale version is caught",
  caught(C.check_version({"site_commit": "b" * 40}, latest, NOW), "latest site commit"), True)
t("a missing version is caught", caught(C.check_version({}, latest, NOW), "missing"), True)
t("a change made minutes ago is given time to be stamped",
  C.check_version({"site_commit": "b" * 40}, ("a" * 40, NOW - timedelta(minutes=10)), NOW), [])

# --- The browser ---------------------------------------------------------------
try:
    import playwright  # noqa: F401
    have_browser = True
except ImportError:
    have_browser = False

REPO = os.path.dirname(os.path.abspath(__file__))
SITE_FILES = ["index.html", "sw.js", "config.js", "manifest.json", "statutory-calendar.json",
              "icon-192.png", "favicon-64.png"]
SHA = "c0ffee" + "0" * 34
LIVE = json.load(open(os.path.join(REPO, "status.json"), encoding="utf-8"))


def site(mutate=None):
    """A local copy of the site, optionally broken."""
    d = tempfile.mkdtemp(prefix="canary-site-")
    for f in SITE_FILES:
        if os.path.exists(os.path.join(REPO, f)):
            shutil.copy(os.path.join(REPO, f), d)
    json.dump(LIVE, open(os.path.join(d, "status.json"), "w"))
    json.dump({"site_commit": SHA, "site_committed_at": "2026-09-24T01:00:00Z"},
              open(os.path.join(d, "version.json"), "w"))
    if mutate:
        mutate(d)
    return d


def serve(d):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=d, **k)
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # localhost, not 127.0.0.1: only "localhost" counts as a secure context
    # for service workers without https.
    return srv, f"http://localhost:{srv.server_address[1]}"


def browse(mutate=None, status=None):
    d = site(mutate)
    srv, url = serve(d)
    C.SITE = url
    try:
        return C.check_browser(status or LIVE, SHA)
    finally:
        srv.shutdown()


def edit(name, old, new):
    def m(d):
        p = os.path.join(d, name)
        s = open(p, encoding="utf-8").read()
        assert old in s, f"{old!r} not in {name}"
        open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
    return m


if not have_browser:
    print("\n--- the browser: SKIPPED (Playwright not installed) ---")
else:
    print("\n--- the browser (a broken local copy of the site) ---")
    t("a healthy site passes", browse(), [])
    t("the sample-data fallback is caught",
      caught(browse(lambda d: os.remove(os.path.join(d, "status.json"))), "SAMPLE DATA"), True)
    t("a default state nobody chose is caught (the Nebraska bug)",
      caught(browse(edit("index.html", "current=loadState()||null", "current=loadState()||'NE'")),
             "did not choose"), True)
    t("a stale site version in the footer is caught",
      caught(browse(lambda d: json.dump({"site_commit": "0ld" + "0" * 37,
                                         "site_committed_at": "2026-09-01T00:00:00Z"},
                                        open(os.path.join(d, "version.json"), "w"))),
             "stale build"), True)
    t("JavaScript errors are caught",
      caught(browse(edit("index.html", "</body>",
                         "<script>setTimeout(()=>{throw new Error('boom')},0)</script></body>")),
             "JavaScript errors"), True)
    other = json.loads(json.dumps(LIVE))
    other["states"][C.PROBE_STATE]["effective_status"] = (
        "half" if LIVE["states"][C.PROBE_STATE]["effective_status"] != "half" else "full")
    t("a page showing something other than the published answer is caught",
      caught(browse(status=other), "published data says"), True)
    # The stale-build bug: a service worker that serves cache-first, and whose
    # cache holds an old build. The first visit (network) is fine; the SECOND,
    # through the service worker, is not - which is exactly why it took hours
    # to find: whoever deployed always saw the new build.
    stale_sw = """
const OLD = new Response(JSON.stringify({site_commit: 'deadbee' + '0'.repeat(33),
  site_committed_at: '2026-08-01T00:00:00Z'}), {headers: {'Content-Type': 'application/json'}});
self.addEventListener('install', e => { self.skipWaiting(); e.waitUntil(
  caches.open('stale').then(c => c.put(new Request('./version.json'), OLD))); });
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.pathname.endsWith('version.json'))
    e.respondWith(caches.open('stale').then(c => c.match('./version.json')));
});
"""
    probs = browse(lambda d: open(os.path.join(d, "sw.js"), "w").write(stale_sw))
    t("a stale build served through the service worker is caught on the second visit",
      (caught(probs, "second visit"), caught(probs, "first visit")), (True, False))

print(f"\n{'=' * 52}\n  {ok} passed, {bad} failed\n{'=' * 52}\n")
raise SystemExit(1 if bad else 0)
