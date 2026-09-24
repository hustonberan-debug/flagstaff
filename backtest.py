"""Backtest the CURRENT pipeline against what was actually true on past dates.

This is not the history replay. test-history.py checks that we were consistent
with ourselves; this checks us against reality. For each case:

  - truth    comes from an independent record: the order document itself
             (the governor's own release, which states its window), the
             statute, or a finding we established by reading the page.
  - input    is the official source AS IT EXISTED ON THAT DATE. We never stored
             page text - cache.json keeps hashes and verdicts only - so the
             listing or status page comes from the Internet Archive's capture
             of that official URL, taken at or before the end of that day.
             That is an archive of the government page, not another site's
             reading of it, and it is used here only: the live pipeline never
             reads archive.org.
  - verdict  is what run.py's logic TODAY produces from that input, with the
             clock pinned to the test date and an empty cache (a fresh read,
             no carried answer).

Two limits, reported per case rather than hidden:
  - The capture may be older than the test date. Its age is printed; a
    capture from before the order was posted cannot show the order.
  - Order documents linked from a listing are fetched live. Press releases do
    not change after publication, so that is the page as it was - but if one
    has been edited or removed, the case says so.

    python3 backtest.py            # all cases
    python3 backtest.py --case 3   # one case, verbose
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime

import requests

import parsers as P
import run as R

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".backtest-cache")

# Each truth is stated with its basis. "record" is the document that settles
# it, and it is never one of our own outputs or another flag site's answer.
CASES = [
    {"date": "2026-08-20", "state": "ID", "truth": "full",
     "record": "Idaho's own flag page said 'full staff'; our parser read "
               "protocol prose ('flags to be flown at half-staff') as a "
               "declaration. Established 2026-09-16 by reading the page.",
     "record_url": "https://gov.idaho.gov/flag-status/"},
    {"date": "2026-08-29", "state": "OH", "truth": "half",
     "record": "Presidential proclamation 'Honoring the Memory of Dolly "
               "Parton': half-staff throughout the United States until sunset "
               "September 1, 2026.",
     "record_url": "https://www.whitehouse.gov/presidential-actions/2026/08/honoring-the-memory-of-dolly-parton/"},
    {"date": "2026-09-10", "state": "ME", "published": "2026-09-10", "truth": "full",
     "record": "Maine's order: 'lowered from sunrise to sunset on Friday, "
               "September 11, 2026'. Posted Sept 10 for the next day.",
     "record_url": "https://www.maine.gov/governor/mills/news/governor-mills-orders-flags-lowered-honor-victims-and-survivors-september-11th-2026-09-10"},
    {"date": "2026-09-11", "state": "KS", "truth": "half",
     "record": "Patriot Day, 36 U.S.C. 144(b): half-staff nationwide by statute.",
     "record_url": "https://www.law.cornell.edu/uscode/text/36/144"},
    {"date": "2026-09-12", "state": "WV", "published": "2026-09-10", "truth": "full",
     "record": "West Virginia's order: half-staff 'from dawn to dusk' for the "
               "September 11 anniversary - one day.",
     "record_url": "https://governor.wv.gov/article/governor-morrisey-orders-flags-half-staff-honor-25th-anniversary-september-11-attacks"},
    {"date": "2026-09-14", "state": "ND", "published": "2026-09-09", "truth": "full",
     "record": "North Dakota's directive: half-staff 'on Friday' (Sept 11) "
               "only. Posted Wednesday Sept 9.",
     "record_url": "https://www.governor.nd.gov/news/armstrong-directs-flags-flown-half-staff-friday-memory-911-victims-25th-anniversary-attacks"},
    {"date": "2026-09-17", "state": "IA", "published": "2026-09-17", "truth": "full",
     "record": "Iowa's order: 'from sunrise on Friday, September 18, 2026, "
               "until sunset on Sunday, September 20'. Posted Thursday the 17th.",
     "record_url": "https://governor.iowa.gov/press-release/2026-09-17/gov-reynolds-orders-flags-half-staff-honor-and-remembrance-national-ag-leader-exceptional-iowan-ray"},
    {"date": "2026-09-19", "state": "IA", "published": "2026-09-17", "truth": "half",
     "record": "Same Iowa order, inside its window (Sept 18-20).",
     "record_url": "https://governor.iowa.gov/press-release/2026-09-17/gov-reynolds-orders-flags-half-staff-honor-and-remembrance-national-ag-leader-exceptional-iowan-ray"},
    {"date": "2026-09-21", "state": "NE", "published": "2026-09-21", "truth": "full",
     "record": "Nebraska's release delegates authority to the Mayor of Yutan: "
               "'flags within the City of Yutan'. Not a statewide order.",
     "record_url": "https://governor.nebraska.gov/gov-pillen-orders-flags-flown-half-staff-honor-yutan-firefighter"},
    {"date": "2026-09-22", "state": "AK", "published": "2026-09-22", "truth": "half",
     "record": "Alaska's flag page carried a governor's order dated for Sept "
               "22 only (verified 2026-09-22).",
     "record_url": "https://gov.alaska.gov/services/status-of-the-flag/"},
]


def _cache_path(key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json")


def _cached(key, fn):
    p = _cache_path(key)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    v = fn()
    # Only cache successes: a transient archive.org error must not become a
    # permanent "no capture" for this case.
    if v and v.get("text") is not None:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(v, f)
    return v


def capture(url, day, session):
    """The newest Internet Archive capture of `url` taken on or before the end
    of `day`. Returns {text, timestamp, error}."""
    def go():
        try:
            r = session.get("https://web.archive.org/cdx/search/cdx",
                            params={"url": url, "to": day.strftime("%Y%m%d") + "235959",
                                    "filter": "statuscode:200", "fl": "timestamp",
                                    "limit": "-1"},
                            timeout=60)
            stamps = r.text.split()
            if not stamps:
                return {"text": None, "timestamp": None,
                        "error": "no capture on or before this date"}
            ts = stamps[-1]
            # id_ serves the archived bytes without the archive's toolbar.
            page = session.get(f"https://web.archive.org/web/{ts}id_/{url}", timeout=60)
            if page.status_code != 200:
                return {"text": None, "timestamp": ts,
                        "error": f"capture HTTP {page.status_code}"}
            return {"text": page.text, "timestamp": ts, "error": None}
        except requests.RequestException as e:
            return {"text": None, "timestamp": None, "error": f"archive.org: {type(e).__name__}"}
    return _cached(f"{url}|{day}", go)


def run_case(case, session, verbose=False):
    day = date.fromisoformat(case["date"])
    reg = {r["state_code"]: r for r in json.load(open(R.REGISTRY, encoding="utf-8"))}
    rec = reg[case["state"]]
    fetched = {}

    # The registered source(s) come from the archive at that date; anything
    # else the pipeline follows (order documents) is fetched live.
    sources = {u for u in (R.pick_url(rec), rec.get("press_url"),
                           rec.get("flag_page_url"), rec.get("rss_url")) if u}
    real_fetch = R.fetch

    def shim(url, sess):
        if url in sources:
            c = capture(url, day, session)
            fetched[url] = ("archive", c.get("timestamp"), c.get("error"))
            return (c["text"], None) if c.get("text") is not None else (None, c["error"])
        text, err = real_fetch(url, sess)
        fetched[url] = ("live", None, err)
        return text, err

    real_today = R.today
    R.fetch, R.today = shim, (lambda: day)
    # The consensus layer decides whether to read an email second source
    # from the CURRENT inbox, which knows nothing about past dates.
    real_delivered = R._DELIVERED
    R._DELIVERED = set()
    try:
        fed = R.federal_statutory(day)
        fed_src = "statute" if fed else None
        if not fed:
            sources.update(R.FEDERAL_SOURCES)
            fed, ferr = R.federal_proclamation(session, {})
            fed_src = "proclamation" if fed else (f"check failed: {ferr}" if ferr else None)
        if rec.get("ingest_mode") == "email":
            code, out = case["state"], {"state_status": P.UNKNOWN,
                                        "error": "email state: past inbox not replayable"}
        else:
            code, out, _ = R.check_state_consensus(rec, {}, session)
    finally:
        R.fetch, R.today, R._DELIVERED = real_fetch, real_today, real_delivered

    if fed:
        ours, why = P.HALF, f"federal ({fed_src}): {fed.get('reason')}"
    else:
        ours = out.get("state_status")
        o = out.get("state_order") or {}
        why = (o.get("coverage_reason") or o.get("evidence") or o.get("title")
               or out.get("error") or "no order in effect")
    return {"ours": ours, "why": why, "out": out, "fetched": fetched,
            "basis": out.get("confidence_basis")}


def order_document_verdict(case, session):
    """Run the ORDER DOCUMENT itself through the functions the pipeline uses
    on an order page (strip_html -> facts_from_text -> covers_today -> scope
    rule), with the clock pinned to the test date. Only for cases whose record
    is an order page, not a status page or statute.

    This exists because the archive often has no capture of a listing between
    an order's posting and the test date - and a listing captured before the
    order was posted will say 'full staff' for the wrong reason."""
    url = case.get("record_url") or ""
    if not case.get("published") or "flag-status" in url or "status-of-the-flag" in url:
        return None
    html, err = R.fetch(url, session)
    if err:
        return {"ours": None, "why": f"order page: {err}"}
    reg = {r["state_code"]: r for r in json.load(open(R.REGISTRY, encoding="utf-8"))}
    f = R.facts_from_text(P.strip_html(html), reg[case["state"]]["state"])
    # The listing path falls back to the item's publication date as the
    # start when the text yields none - same here.
    start = f["body_start"] or case["published"]
    v, why = R.covers_today(start, f["body_end"], date.fromisoformat(case["date"]))
    if f["scope"] == "limited":
        return {"ours": P.FULL, "why": f"limited scope: {f['scope_evidence']}", "facts": f}
    if v is True and f["scope"] != "statewide":
        return {"ours": P.UNKNOWN, "why": "covers today but unscoped", "facts": f}
    ours = P.HALF if v is True else P.UNKNOWN if v is None else P.FULL
    return {"ours": ours, "why": f"parsed window {start}..{f['body_end']}: {why}", "facts": f}


def published_then(case):
    """What status.json actually said at the end of that day."""
    import subprocess
    h = subprocess.run(["git", "log", "-1", "--format=%H",
                        f"--until={case['date']}T23:59:59Z", "--", "status.json"],
                       capture_output=True, text=True).stdout.strip()
    if not h:
        return None
    try:
        s = json.loads(subprocess.run(["git", "show", f"{h}:status.json"],
                                      capture_output=True, text=True).stdout)
        return (s["states"].get(case["state"]) or {}).get("effective_status")
    except Exception:
        return None


def grade(ours, truth):
    if ours is None:
        return "INCONCLUSIVE"
    if ours == truth:
        return "RIGHT"
    if ours == P.UNKNOWN:
        return "GAP"          # honest: we said we did not know
    return "WRONG"            # confident and wrong - the failure that matters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int, help="1-based case number")
    args = ap.parse_args()
    session = requests.Session()
    cases = CASES if not args.case else [CASES[args.case - 1]]
    tally = {"RIGHT": 0, "GAP": 0, "WRONG": 0, "INCONCLUSIVE": 0}
    then_tally = {"RIGHT": 0, "GAP": 0, "WRONG": 0, "INCONCLUSIVE": 0}
    rows = []
    for n, case in enumerate(cases, 1 if not args.case else args.case):
        r = run_case(case, session, verbose=bool(args.case))
        # A whole-pipeline run on a capture taken before the order was
        # posted cannot see the order. Its answer proves nothing either way,
        # so it is not graded - the order document is, when there is one.
        stale_input = False
        if case.get("published"):
            state_caps = [ts for u, (kind, ts, e) in r["fetched"].items()
                          if kind == "archive" and ts and "whitehouse.gov" not in u]
            if not state_caps or max(state_caps)[:8] < case["published"].replace("-", ""):
                stale_input = True
        doc = order_document_verdict(case, session)
        if r["ours"] == P.HALF and "federal" in str(r["why"]):
            pass                      # a federal verdict does not depend on the state's capture
        elif stale_input:
            r["why"] = (f"pipeline input predates the order (inconclusive); "
                        + (f"order document: {doc['why']}" if doc else "no order document to test"))
            r["ours"] = doc["ours"] if doc else None
        g = grade(r["ours"], case["truth"])
        then = published_then(case)
        gt = grade(then, case["truth"]) if then else None
        tally[g] = tally.get(g, 0) + 1
        if gt:
            then_tally[gt] = then_tally.get(gt, 0) + 1
        arch = [(u, ts, e) for u, (kind, ts, e) in r["fetched"].items() if kind == "archive"]
        print(f"\n[{n}] {case['date']} {case['state']}  truth={case['truth']}  "
              f"now={r['ours']} ({g})  then={then} ({gt})")
        print(f"     why now : {str(r['why'])[:160]}")
        if r.get("basis"):
            print(f"     basis   : {r['basis']}")
        print(f"     record  : {case['record'][:160]}")
        for u, ts, e in arch:
            age = ""
            if ts:
                cap = datetime.strptime(ts[:8], "%Y%m%d").date()
                age = f" (capture {cap}, {(date.fromisoformat(case['date']) - cap).days}d before)"
            print(f"     input   : {u}{age}{' ERROR ' + e if e else ''}")
        rows.append({**case, "now": r["ours"], "grade": g, "why": r["why"],
                     "then": then, "then_grade": gt, "basis": r.get("basis"),
                     "inputs": [{"url": u, "capture": ts, "error": e} for u, ts, e in arch]})
    print(f"\nNOW : {tally}")
    print(f"THEN: {then_tally}")
    with open("backtest-results.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now().isoformat(timespec="seconds"),
                   "now": tally, "then": then_tally, "cases": rows}, f, indent=2)
    return 1 if tally["WRONG"] else 0


if __name__ == "__main__":
    sys.exit(main())
