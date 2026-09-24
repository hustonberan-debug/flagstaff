"""notify.py - tell subscribers about every real change in status.json.

Run by the workflow after status.json is published. Exits non-zero when any
subscriber who should have been told was not, so the job goes red and a
person hears about it.

THE FAN-OUT, and why the pipeline drives it
    The Worker's free plan allows 50 subrequests per invocation, and each
    push is one. Sending a notification in a single call reached 50 people -
    on a national proclamation, the most important day this app has,
    subscriber 51 onward got nothing. So the Worker now does the work in
    chunks, and this script drives them:

      1. POST /notify/plan   who should hear about this? (paged: every
                             subscriber, not the first 1,000)
      2. POST /notify        send to these keys; the Worker sends what fits
                             in one invocation and hands back the rest as
                             `deferred`, which go in the next chunk.

RETRIES ARE SAFE, SO THEY HAPPEN
    Every chunk carries an id for the change it announces. The Worker stores
    each chunk's result for a day and answers a repeat from that record
    without sending. So a chunk that times out - which may or may not have
    gone out - is sent again: a duplicate alert is far less harmful than a
    missed one, and the record makes a duplicate unlikely. (The one window
    left: a retry that lands while the first attempt is still sending.)
    Subscribers whose push failed at the push service are retried once, as a
    new chunk, so the ones who did get it are not sent it twice.

OLDER WORKER
    A Worker without /notify/plan (not yet redeployed) gets the old single
    call, without timeout retries, because it cannot deduplicate. It stops at
    its budget and reports who it missed, which fails the job.
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

CHUNK = 40                # keys per /notify call; the Worker defers what does not fit
ATTEMPTS = 4              # per request, including the first
BACKOFF_S = (2, 5, 10)
TIMEOUT_S = float(os.environ.get("NOTIFY_TIMEOUT", "30"))
NO_PROGRESS_LIMIT = 5     # calls in a row that reach nobody before giving up
SITE = "https://halfstaffnow.com/"

UA = "halfstaffnow-pipeline/1.0 (+https://halfstaffnow.com)"


class WorkerError(Exception):
    def __init__(self, msg, status=None, retryable=False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


def post(base, secret, path, body, timeout=None):
    """One request. Raises WorkerError; retryable when a repeat could help."""
    # urllib's default "Python-urllib/3.x" is rejected by Cloudflare with a
    # 403 before the request ever reaches the Worker.
    req = urllib.request.Request(
        base.rstrip("/") + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {secret}",
                 "User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_S) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:200]
        raise WorkerError(f"HTTP {e.code} {text}", status=e.code, retryable=e.code >= 500 or e.code == 429)
    except (socket.timeout, TimeoutError) as e:
        raise WorkerError(f"timed out ({e})", retryable=True)
    except urllib.error.URLError as e:
        raise WorkerError(f"could not reach the Worker: {e.reason}", retryable=True)
    except json.JSONDecodeError as e:
        raise WorkerError(f"unreadable answer ({e})", retryable=True)


def post_retrying(base, secret, path, body, sleep=time.sleep):
    """post(), repeated on anything retryable. Only for requests that are safe
    to repeat: /notify/plan (reads only) and /notify with keys (deduplicated)."""
    for attempt in range(ATTEMPTS):
        try:
            return post(base, secret, path, body)
        except WorkerError as e:
            if not e.retryable or attempt == ATTEMPTS - 1:
                raise
            print(f"  {path}: {e} - retrying ({attempt + 2}/{ATTEMPTS})")
            sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])


def notification_id(code, status, state, fed):
    """What this change IS, stable across a re-run of the same job, so a
    re-run is recognised by the Worker as a repeat and not sent twice."""
    if code == "US":
        f = fed or {}
        what = f.get("observance") or f.get("source_url") or f.get("reason") or "national"
        return f"US:half:{what}"[:200]
    return f"{code}:{status}:{state.get('last_changed_at') or state.get('checked_at') or ''}"[:200]


def changes(status):
    """[(code, status, reason, id)] for every change that should be announced.
    A move to Unclear is never announced: the service worker would once have
    rendered it as "back to full staff", and it is not a change in the flag."""
    fed = status.get("federal")
    if fed:
        # A national order moves every state at once: ONE broadcast, not 51.
        if not fed.get("_changed"):
            return []
        return [("US", "half", fed.get("reason") or "National half-staff order",
                 notification_id("US", "half", {}, fed))]
    out = []
    for code, st in sorted(status["states"].items()):
        eff = st.get("effective_status")
        if st.get("changed") and eff in ("half", "full"):
            out.append((code, eff, st.get("reason"), notification_id(code, eff, st, None)))
    return out


def fan_out(base, secret, code, status, reason, nid, sleep=time.sleep):
    """Plan, then send in chunks. Returns a result dict; result['missed'] is
    the number of subscribers who should have been told and were not."""
    planned, nxt = [], {}
    while True:
        page = post_retrying(base, secret, "/notify/plan", {"state": code, **nxt}, sleep=sleep)
        planned.extend(page.get("keys") or [])
        nxt = page.get("next")
        if not nxt:
            break
    res = {"planned": len(planned), "sent": 0, "gone": 0, "missing": 0, "skipped": 0,
           "calls": 0, "duplicates": 0, "missed": 0, "errors": []}
    queue, retried, stalled = list(planned), set(), 0
    body = {"id": nid, "state": code, "status": status, "reason": reason, "url": SITE}
    while queue:
        keys, queue = queue[:CHUNK], queue[CHUNK:]
        try:
            r = post_retrying(base, secret, "/notify", dict(body, keys=keys), sleep=sleep)
        except WorkerError as e:
            res["missed"] += len(keys)
            res["errors"].append(str(e))
            continue
        res["calls"] += 1
        res["duplicates"] += bool(r.get("duplicate"))
        reached = 0
        for k in ("sent", "gone", "missing", "skipped"):
            res[k] += int(r.get(k) or 0)
            reached += int(r.get(k) or 0)
        for k in r.get("failed_keys") or []:
            if k in retried:
                res["missed"] += 1
            else:
                retried.add(k)
                queue.append(k)
        res["errors"].extend((r.get("errors") or [])[:3])
        deferred = r.get("deferred") or []
        # A chunk that reached nobody and handed everything back means the
        # Worker cannot make progress at all; do not loop forever.
        stalled = stalled + 1 if (reached == 0 and len(deferred) == len(keys)) else 0
        if stalled >= NO_PROGRESS_LIMIT:
            res["missed"] += len(deferred) + len(queue)
            res["errors"].append("the Worker made no progress in "
                                 f"{NO_PROGRESS_LIMIT} calls in a row")
            break
        queue = deferred + queue
    return res


def legacy(base, secret, code, status, reason):
    """The old single call, for a Worker without /notify/plan. Not retried on
    timeout: that Worker cannot deduplicate, and would alert everyone twice."""
    try:
        r = post(base, secret, "/notify",
                 {"state": code, "status": status, "reason": reason, "url": SITE})
    except WorkerError as e:
        return {"missed": -1, "errors": [str(e)], "legacy": True}
    missed = int(r.get("failed") or 0) + int(r.get("deferred_count") or 0)
    return {"planned": None, "sent": r.get("sent"), "gone": r.get("gone"), "missed": missed,
            "errors": r.get("errors") or [], "legacy": True}


def main(argv=None, sleep=time.sleep):
    base, secret = os.environ.get("NOTIFY_URL"), os.environ.get("NOTIFY_SECRET")
    if not base or not secret:
        print("::error::NOTIFY_URL / NOTIFY_SECRET not set - no push notifications are being sent")
        return 1
    path = (argv or sys.argv[1:] or ["status.json"])[0]
    try:
        status = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"::error::{path} unreadable ({e}) - could not notify")
        return 1

    todo = changes(status)
    if not todo:
        print("No changes this run - nothing to notify.")
        return 0

    failed = []
    for code, st, reason, nid in todo:
        try:
            res = fan_out(base, secret, code, st, reason, nid, sleep=sleep)
        except WorkerError as e:
            if e.status == 404:
                print(f"::warning::the Worker has no /notify/plan - it has not been redeployed. "
                      f"Using the old single call for {code}, which reaches at most ~50 people.")
                res = legacy(base, secret, code, st, reason)
            else:
                res = {"missed": -1, "errors": [f"could not plan: {e}"]}
        summary = {k: v for k, v in res.items() if k != "errors" and v is not None}
        print(f"{code} {st}: {json.dumps(summary)}")
        if res["missed"]:
            who = "an unknown number of" if res["missed"] < 0 else str(res["missed"])
            print(f"::error::notify for {code}: {who} subscriber(s) were NOT told. "
                  f"{'; '.join(res['errors'][:3])}")
            failed.append(code)
    if failed:
        print(f"notifications incomplete for {' '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
