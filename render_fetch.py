#!/usr/bin/env python3
"""
render_fetch.py — rendered snapshots for states whose pages need JavaScript.

    python3 render_fetch.py --check   # which snapshots are due? (no browser)
    python3 render_fetch.py           # render the due ones with Playwright

Montana, Oklahoma and South Dakota serve nothing useful to a plain HTTP
request: their listings or status widgets are filled in by script. This
renders them in headless Chromium and saves a snapshot per state under
rendered/, which run.py reads instead of fetching.

It runs as its own workflow step so a browser failure cannot touch the states
that work without one: run.py treats a missing, failed or old snapshot
exactly like a page that would not load.

Rules this keeps:
  - robots.txt is checked first, with the pipeline's own robots_allows().
  - Playwright's default headless Chromium, as it comes. No user-agent or
    fingerprint changes, no retries: a site that refuses a headless browser
    has refused us.
  - Aggressive caching. These pages change rarely, so a snapshot younger than
    render_max_age_hours (default 3) is reused and no browser starts at all.
  - A failed render never overwrites the last good snapshot. It records the
    error beside it; run.py decides whether the good one is still recent
    enough to use.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import run as R

OUT_DIR = R.RENDER_DIR
DEFAULT_MAX_AGE_HOURS = 3
NAV_TIMEOUT_MS = 30_000
SETTLE_MS = 1_500


def targets():
    return [r for r in R.load_json(R.REGISTRY, []) if r.get("render")]


def path_for(code):
    return os.path.join(OUT_DIR, f"{code}.json")


def due(rec, now=None):
    """Why this state needs a render now, or None if its snapshot is fresh."""
    now = now or datetime.now(timezone.utc)
    snap = R.load_json(path_for(rec["state_code"]), None)
    if not snap or not snap.get("rendered_at"):
        return "no snapshot"
    if snap.get("url") != R.pick_url(rec):
        return "URL changed"
    age = now - datetime.fromisoformat(snap["rendered_at"])
    limit = timedelta(hours=rec.get("render_max_age_hours") or DEFAULT_MAX_AGE_HOURS)
    if age > limit:
        return f"snapshot {age.total_seconds() / 3600:.1f}h old"
    return None


def save(code, snap):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path_for(code), "w", encoding="utf-8") as f:
        json.dump(snap, f)


def render(recs):
    from playwright.sync_api import sync_playwright

    stamp = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    t_start = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        print(f"  browser launched in {time.monotonic() - t_start:.1f}s")
        for rec in recs:
            code, url = rec["state_code"], R.pick_url(rec)
            prev = R.load_json(path_for(code), None) or {}
            keep = {k: prev[k] for k in ("url", "rendered_at", "html", "text",
                                         "status") if k in prev and prev.get("url") == url}
            t0 = time.monotonic()
            if not R.robots_allows(url):
                save(code, dict(keep, url=url, attempted_at=stamp(),
                                error="blocked by robots.txt"))
                print(f"  {code}  robots.txt disallows {url}")
                continue
            page = browser.new_page()
            try:
                resp = page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
                page.wait_for_timeout(SETTLE_MS)
                status = resp.status if resp else None
                if status and status >= 400:
                    raise RuntimeError(f"HTTP {status}")
                snap = {"url": url, "final_url": page.url, "status": status,
                        "rendered_at": stamp(), "attempted_at": stamp(),
                        "html": page.content(), "text": page.inner_text("body"),
                        "error": None,
                        "seconds": round(time.monotonic() - t0, 1)}
                save(code, snap)
                print(f"  {code}  {snap['seconds']:.1f}s  HTTP {status}  "
                      f"{len(snap['html']) // 1024}KB html, {len(snap['text'])} chars visible")
            except Exception as e:
                err = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
                save(code, dict(keep, url=url, attempted_at=stamp(), error=err))
                print(f"  {code}  FAILED after {time.monotonic() - t0:.1f}s: {err}")
            finally:
                page.close()
        browser.close()
    print(f"  total {time.monotonic() - t_start:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report which snapshots are due; never starts a browser")
    args = ap.parse_args()

    recs = targets()
    pending = [(r, why) for r in recs for why in [due(r)] if why]
    for r in recs:
        why = next((w for x, w in pending if x is r), None)
        print(f"  {r['state_code']}  {'due: ' + why if why else 'fresh, reused'}")

    if args.check:
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"needed={'true' if pending else 'false'}\n")
        return 0

    if not pending:
        print("  nothing to render")
        return 0
    render([r for r, _ in pending])
    # A failed render is recorded in its snapshot for run.py to judge. Exit
    # non-zero only if the browser itself never ran, so the step shows red.
    return 0


if __name__ == "__main__":
    sys.exit(main())
