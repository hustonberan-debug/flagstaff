"""Tests for notify.py against a fake Worker that speaks the same protocol.

The Worker itself - and notify.py driving the REAL Worker with Cloudflare's
limits enforced - is tested in the push-worker project (npm test, and npm run
test:integration). This file covers the pipeline's side of the conversation,
offline, so it can run in CI on every push:

  - who is announced (never a move to Unclear; one broadcast for a national
    order), and a notification id that stays the same when a job is re-run
  - chunking, deferred subscribers sent later, failed ones retried once
  - a lost answer (timeout) retried with the SAME id and keys, and so
    recognised by the Worker as a repeat
  - an older Worker without /notify/plan: the old call, no timeout retry,
    and a failed job when people were missed
"""

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import notify as N

ok = bad = 0


def t(label, got, want):
    global ok, bad
    if got == want:
        ok += 1
        print(f"  PASS  {label}")
    else:
        bad += 1
        print(f"  FAIL  {label}\n        want {want!r}\n        got  {got!r}")


class FakeWorker:
    """Plan pages of `page`, chunks that reach at most `capacity` keys per call
    and defer the rest, a dedup store keyed on (id, sorted keys)."""

    def __init__(self, n, page=1000, capacity=14, states=lambda i: ["US"], plan=True,
                 fail_once=(), drop_answer_calls=(), legacy_reach=50):
        self.subs = {f"sub:{i:05d}": states(i) for i in range(n)}
        self.page, self.capacity, self.plan = page, capacity, plan
        self.fail_once = set(fail_once)
        self.drop_answer_calls = set(drop_answer_calls)
        self.legacy_reach = legacy_reach
        self.delivered = {}
        self.dedup = {}
        self.chunk_calls = 0
        self.seen_ids = []

    def handle(self, path, body):
        if path == "/notify/plan":
            if not self.plan:
                return 404, {"error": "not found"}
            target = body["state"]
            names = sorted(self.subs)
            start = int(body.get("cursor") or 0) + int(body.get("offset") or 0)
            batch = names[start:start + self.page]
            keys = [k for k in batch if target == "US" or target in self.subs[k]]
            end = start + len(batch)
            return 200, {"keys": keys, "next": {"cursor": str(end), "offset": 0}
                         if end < len(names) else None}
        if path == "/notify" and "keys" not in body:
            reached = [k for k in sorted(self.subs)][:self.legacy_reach]
            for k in reached:
                self.delivered[k] = self.delivered.get(k, 0) + 1
            return 200, {"sent": len(reached), "failed": len(self.subs) - len(reached), "errors": []}
        if path == "/notify":
            self.chunk_calls += 1
            self.seen_ids.append(body["id"])
            dk = (body["id"], tuple(sorted(body["keys"])))
            if dk in self.dedup:
                return 200, dict(self.dedup[dk], duplicate=True)
            out = {"sent": 0, "gone": 0, "missing": 0, "skipped": 0,
                   "failed_keys": [], "deferred": [], "errors": []}
            for i, k in enumerate(body["keys"]):
                if i >= self.capacity:
                    out["deferred"].append(k)
                elif k in self.fail_once:
                    self.fail_once.discard(k)
                    out["failed_keys"].append(k)
                    out["errors"].append("500: push service")
                else:
                    self.delivered[k] = self.delivered.get(k, 0) + 1
                    out["sent"] += 1
            out["failed"] = len(out["failed_keys"])
            self.dedup[dk] = out
            if self.chunk_calls in self.drop_answer_calls:
                return "DROP", out
            return 200, out
        return 404, {"error": "not found"}


def serve(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.headers.get("Authorization") != "Bearer s":
                code, out = 401, {"error": "unauthorized"}
            else:
                code, out = fake.handle(self.path, body)
            if code == "DROP":
                time.sleep(1.5)            # the work is done; the answer is late
                code = 200
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(out).encode())
            except OSError:
                pass          # the client gave up waiting - that is the point
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def run(fake, status, timeout="10"):
    srv, url = serve(fake)
    os.environ.update(NOTIFY_URL=url, NOTIFY_SECRET="s")
    N.TIMEOUT_S = float(timeout)
    path = os.path.join(os.environ.get("TEMP", "/tmp"), f"notify-test-{os.getpid()}.json")
    json.dump(status, open(path, "w"))
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = N.main([path], sleep=lambda s: None)
    srv.shutdown()
    return code, buf.getvalue()


NATIONAL = {"federal": {"reason": "Patriot Day", "observance": "patriot-day:2026-09-11",
                        "_changed": True}, "states": {}}

print("\n--- who is announced ---")
st = {"federal": None, "states": {
    "OH": {"effective_status": "half", "changed": True, "reason": "r", "last_changed_at": "2026-09-24T12:00:00Z"},
    "IA": {"effective_status": "unknown", "changed": True},
    "NE": {"effective_status": "full", "changed": False}}}
t("a move to Unclear is never announced; only real changes are",
  [c[0] for c in N.changes(st)], ["OH"])
t("a national order is one broadcast, not 51", [c[0] for c in N.changes(NATIONAL)], ["US"])
t("a national order already announced is not announced again",
  N.changes({"federal": {"reason": "x", "_changed": False}, "states": {}}), [])
t("the id names the change itself, so a re-run of the job is recognised",
  N.changes(st)[0][3], "OH:half:2026-09-24T12:00:00Z")
t("a national order's id is its observance, not its wording",
  N.changes(NATIONAL)[0][3], "US:half:patriot-day:2026-09-11")

print("\n--- the fan-out ---")
for n in (200, 1500):
    f = FakeWorker(n)
    code, out = run(f, NATIONAL)
    t(f"{n} subscribers: every one told exactly once",
      (code, sorted(set(f.delivered.values())), len(f.delivered)), (0, [1], n))
f = FakeWorker(2500, page=1000)
code, out = run(f, NATIONAL)
t("2,500 subscribers across three plan pages", (code, len(f.delivered)), (0, 2500))

f = FakeWorker(300, states=lambda i: ["OH"] if i % 2 else ["NE"])
code, _ = run(f, {"states": {"OH": {"effective_status": "half", "changed": True,
                                    "last_changed_at": "x"}}})
t("a state order reaches only that state",
  (code, all(k in f.delivered for k, s in f.subs.items() if "OH" in s),
   any(k in f.delivered for k, s in f.subs.items() if "OH" not in s)), (0, True, False))

f = FakeWorker(60, fail_once={"sub:00007", "sub:00031"})
code, _ = run(f, NATIONAL)
t("a push that failed is retried once and reaches them",
  (code, f.delivered.get("sub:00007"), f.delivered.get("sub:00031"), max(f.delivered.values())),
  (0, 1, 1, 1))

f = FakeWorker(20, capacity=0)
code, out = run(f, NATIONAL)
t("a Worker that makes no progress fails the job instead of looping",
  (code, "no progress" in out), (1, True))

print("\n--- retries are safe ---")
f = FakeWorker(100, drop_answer_calls={1})
code, out = run(f, NATIONAL, timeout="0.5")
t("a lost answer is retried with the same id and keys, and nobody is told twice",
  (code, "timed out" in out, max(f.delivered.values()), len(f.delivered)), (0, True, 1, 100))
t("every chunk carried the same notification id", set(f.seen_ids), {"US:half:patriot-day:2026-09-11"})

f = FakeWorker(80)
run(f, NATIONAL)
before = dict(f.delivered)
code, out = run(f, NATIONAL)
t("the whole job re-run sends nothing new", (code, f.delivered == before), (0, True))

print("\n--- an older Worker ---")
f = FakeWorker(200, plan=False)
code, out = run(f, NATIONAL)
t("without /notify/plan: the old call, and the job fails because people were missed",
  (code, "has not been redeployed" in out, "were NOT told" in out, len(f.delivered)),
  (1, True, True, 50))

os.environ["NOTIFY_URL"] = ""
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = N.main(["status.json"])
t("not configured fails the job", (code, "not set" in buf.getvalue()), (1, True))

print(f"\n{'=' * 52}\n  {ok} passed, {bad} failed\n{'=' * 52}\n")
raise SystemExit(1 if bad else 0)
