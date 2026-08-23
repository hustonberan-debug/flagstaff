#!/usr/bin/env python3
"""
run.py — the pipeline. Reads registry.json, checks every source, writes
status.json.

    python3 run.py              # normal run
    python3 run.py --dry-run    # fetch and report, write nothing
    python3 run.py --state NE   # one state, verbose

DESIGN NOTES

Hash-diff first. Every source is fetched, but a source whose content hash is
unchanged since the last run is NOT re-parsed and never touches an LLM. State
sites change a few times a week; polling runs every 30 minutes. That ratio is
why this costs nothing to operate.

Two independent half-staff authorities stack:
  - FEDERAL: statutory days + presidential proclamations. Apply to all states.
  - STATE:   governor's order. Applies to that state only.
A state is at half-staff if EITHER is active. Both are reported separately so
the UI can say WHY, which is the whole differentiator.

Never guess. A state we cannot read is `coverage: "not_covered"` — an explicit
visible gap. It is never silently reported as full-staff. A competitor showing
full-staff because their scraper broke is the failure this product exists to
beat; shipping that same failure with nicer fonts would be worthless.
"""

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import requests

import parsers as P

# Bump whenever parsing logic changes. A cache keyed only on page content is
# wrong when the CODE changes: identical pages produce stale verdicts computed
# by the old parser. The key must be (content, code version).
PARSER_VERSION = "23"

REGISTRY = "registry.json"
CACHE = "cache.json"
CALENDAR = "statutory-calendar.json"
EMAIL_ORDERS = "email-orders.json"
OUTPUT = "status.json"
HISTORY = "history.jsonl"

TIMEOUT = 20
WORKERS = 10
MAX_ORDER_AGE_DAYS = 21      # how far back to look for candidate orders
AMBIGUOUS_WINDOW_DAYS = 2    # undated order this recent -> unknown, not full
RECENT_ORDER_GRACE_DAYS = 2  # an order this fresh with no end date is treated as live
FROZEN_PAGE_DAYS = 180       # a status page unchanged this long is not trusted


def covers_today(start, end, d):
    """Does an order provably cover date d? Returns (verdict, why).

    verdict is True / False / None, where None means "cannot tell".

    THE DEFAULT MATTERS. A flag's normal state is full. Half-staff is the
    claim, so half-staff is what needs proof. An earlier version of this
    treated any order from the last 60 days as active unless it could prove
    expiry — which reported ten states at half-staff on a day when the real
    answer was roughly zero. Orders typically last one to five days; assuming
    they persist is assuming wrong.
    """
    s = date.fromisoformat(start) if start else None
    e = date.fromisoformat(end) if end else None

    if s and e:
        return (s <= d <= e), f"window {s}..{e}"
    if e:
        return (d <= e), f"ends {e}"
    if s:
        if s == d:
            return True, f"order dated {s}"
        if s > d:
            return False, f"scheduled for {s}, not yet active"
        age = (d - s).days
        # Governors routinely announce an order a day or two before it takes
        # effect, and many run "until the date of interment" with no stated
        # end. Treating those as concluded caused the app to report FULL on a
        # day when seven states were genuinely at half-staff — the worst
        # failure this product can have. A short grace window fixes that
        # without reviving the 60-day false positives from before.
        if age <= RECENT_ORDER_GRACE_DAYS:
            return True, f"order dated {s} ({age}d ago), no stated end - treated as live"
        return False, f"started {s}, no end date, {age}d old, presumed concluded"
    return None, "no dates parsed"
FETCH_BODY_LIMIT = 400_000   # don't hash megabytes of junk

# Edge WAFs fingerprint the whole header set, not just the User-Agent. Sending
# a Chrome UA with none of Chrome's other headers still reads as automation,
# which is why AZ, KS and MA returned 403 even after the UA was fixed.
BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    # NOTE: no Accept-Encoding here on purpose. requests sets it from the
    # codecs actually installed. Hardcoding "gzip, deflate, br" made servers
    # reply with Brotli, which requests cannot decode without the optional
    # brotli package — r.text came back as binary noise and 14 states silently
    # stopped parsing. Never advertise a codec you cannot decode.
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# --- robots.txt -------------------------------------------------------------
# Looking like a browser is a grey area; ignoring a site's stated policy is
# not. robots.txt is the machine-readable rule a site publishes on purpose, so
# it is honoured absolutely. A WAF filtering on User-Agent is a blunt tool and
# not a policy statement; robots.txt is.
_ROBOTS = {}


def robots_allows(url, session=None):
    """False only when a site's robots.txt explicitly disallows this path.

    Two things this gets right that the stdlib default does not:

    1. It fetches robots.txt with the SAME headers as every other request.
       RobotFileParser uses urllib's default Python user-agent, which the very
       WAFs we are dealing with reject — so robots.txt itself came back 403.

    2. It treats an unreadable robots.txt as "no rules", not "forbidden".
       RobotFileParser sets disallow_all on a 403, which turns "I could not
       read your policy" into "your policy forbids everything". RFC 9309 says
       a 4xx makes robots.txt unavailable and the crawler may proceed. The
       stdlib behaviour blocked 15 states that had never said no.

    Explicit Disallow rules from a real 200 response are still obeyed
    absolutely. That is the part that is actually a policy statement.
    """
    from urllib.parse import urlparse
    from urllib.robotparser import RobotFileParser

    try:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
    except Exception:
        return True

    if base not in _ROBOTS:
        rp = None
        try:
            s = session or requests
            resp = s.get(base + "/robots.txt", timeout=10, headers=BROWSER)
            ctype = (resp.headers.get("Content-Type") or "").lower()
            # Only a real 200 text/plain robots.txt counts as a policy. Some
            # sites serve an HTML 200 error page for missing files; parsing
            # that as rules is meaningless.
            if resp.status_code == 200 and "html" not in ctype:
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
        except Exception:
            rp = None
        _ROBOTS[base] = rp

    rp = _ROBOTS[base]
    if rp is None:
        return True
    try:
        return rp.can_fetch("*", url)
    except Exception:
        return True


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def today():
    return datetime.now(timezone.utc).date()


def fetch(url, session):
    """Returns (text, error). Never raises."""
    if not url:
        return None, "no url"
    if not robots_allows(url, session):
        return None, "blocked by robots.txt"
    try:
        r = session.get(url, timeout=TIMEOUT, headers=BROWSER, allow_redirects=True)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        return r.text[:FETCH_BODY_LIMIT], None
    except requests.Timeout:
        return None, "timeout"
    except Exception as e:
        return None, f"{type(e).__name__}"


def content_hash(text):
    return hashlib.sha256(
        P.strip_html(text or "").encode("utf-8", "replace")
    ).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Federal layer
# ---------------------------------------------------------------------------

def federal_statutory(d):
    """Any statutory half-staff observance active on date d."""
    cal = load_json(CALENDAR, {})
    for obs in cal.get("years", {}).get(str(d.year), []):
        if obs.get("active") and obs.get("date") == d.isoformat():
            return {
                "status": P.HALF,
                "scope": obs.get("scope"),
                "reason": obs.get("name"),
                "detail": obs.get("reason"),
                "authority": "statute",
                "citation": obs.get("citation"),
                "source_url": None,
            }
    return None


def federal_proclamation(session, cache):
    """Active presidential half-staff proclamation, if any.

    NOTE: the source URL below is a placeholder and must be replaced with a
    verified White House presidential-actions endpoint before this is trusted.
    Until then it returns None and the federal layer relies on statute alone.
    We return None rather than a guess, deliberately.
    """
    url = os.environ.get("FEDERAL_PROCLAMATION_URL")
    if not url:
        return None
    text, err = fetch(url, session)
    if err or not text:
        return None
    h = content_hash(text)
    if cache.get("_federal", {}).get("hash") == h:
        return cache["_federal"].get("result")
    orders = P.parse_index(text, url) or P.parse_feed(text, url)
    for o in orders:
        if not o.get("is_flag"):
            continue
        rec = P.extract_order(o["title"], o["url"], o["title"])
        if rec["status"] == P.HALF:
            result = {
                "status": P.HALF,
                "scope": "full-day",
                "reason": o["title"],
                "authority": "presidential proclamation",
                "source_url": o["url"],
                "end_date": rec["end_date"],
            }
            cache["_federal"] = {"hash": h, "result": result}
            return result
    cache["_federal"] = {"hash": h, "result": None}
    return None


# ---------------------------------------------------------------------------
# Per-state check
# ---------------------------------------------------------------------------

def pick_url(rec):
    mode = rec.get("ingest_mode")
    if mode == "email":
        return None                 # nothing to fetch; the state emails us
    if mode == "toggle":
        # Read the hub page, not the status pages. The signal is which of the
        # two static pages the site links to.
        return rec.get("toggle_hub_url") or rec.get("press_url")
    if mode == "feed":
        return rec.get("rss_url")
    if mode in ("archive", "diff"):
        return rec.get("flag_page_url") or rec.get("press_url")
    return rec.get("press_url") or rec.get("flag_page_url")


def check_state(rec, cache, session, verbose=False):
    code = rec["state_code"]
    mode = rec.get("ingest_mode")
    prev = cache.get(code, {})
    out = {
        "state": rec["state"],
        "state_code": code,
        "coverage": "covered",
        "ingest_mode": mode,
        "confidence": rec.get("confidence"),
        "state_status": P.UNKNOWN,
        "state_order": None,
        "source_url": None,
        # checked_at means "we fetched this source on this run" — it always
        # advances. last_changed_at means "the source's content last moved",
        # which may be days ago and that is fine. Collapsing the two makes a
        # healthy unchanged source look like a stale one, and a user who
        # thinks the data is stale goes and checks the governor's site
        # instead. Two facts, two fields.
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_changed_at": prev.get("last_changed_at"),
        "changed": False,
        "error": None,
    }

    # --- email mode: read what the state sent us, not what its site serves --
    if rec.get("ingest_mode") == "email":
        mail = load_json(EMAIL_ORDERS, {})
        inbox = mail.get("orders", {})
        seen = mail.get("channels_seen", {})
        o = inbox.get(code)
        out["source_url"] = (rec.get("notification_channel") or {}).get("detail")

        if not o:
            # Silence means two very different things, and they must not share
            # a value. A channel that has delivered before and is quiet today
            # is telling us there is no order. A channel we have never heard
            # from might simply not be working — an unconfirmed signup, a
            # spam-foldered bulletin, a sender domain we cannot attribute.
            # Reporting FULL on that is claiming knowledge we do not have.
            if code in seen:
                out["state_status"] = P.FULL
                out["coverage"] = "covered"
                out["channel_last_heard"] = seen.get(code)
            else:
                out["state_status"] = P.UNKNOWN
                out["coverage"] = "not_covered"
                out["error"] = ("subscription pending - no bulletin received "
                                "from this channel yet")
            out["last_changed_at"] = prev.get("last_changed_at")
            return code, out, dict(prev, last_checked=out["checked_at"])
        v, why = covers_today(o.get("start_date"), o.get("end_date"), today())
        if v:
            out["state_status"] = P.HALF
            out["state_order"] = {
                "title": o.get("subject"), "url": out["source_url"],
                "status": P.HALF, "authority": o.get("authority"),
                "start_date": o.get("start_date"), "end_date": o.get("end_date"),
                "until_noon": o.get("until_noon"), "coverage_reason": why,
                "via": "official notification email",
            }
        else:
            out["state_status"] = P.FULL
        h = content_hash(json.dumps(o, sort_keys=True))
        out["changed"] = bool(prev.get("hash")) and prev["hash"] != h
        if out["changed"] or not prev.get("hash"):
            out["last_changed_at"] = out["checked_at"]
        else:
            out["last_changed_at"] = prev.get("last_changed_at")
        return code, out, {"hash": h, "state_status": out["state_status"],
                           "state_order": out["state_order"],
                           "last_parsed": out["checked_at"],
                           "last_checked": out["checked_at"],
                           "last_changed_at": out["last_changed_at"]}

    if not rec.get("buildable"):
        out.update(coverage="not_covered",
                   error="no verified source for this jurisdiction")
        return code, out, prev

    url = pick_url(rec)
    out["source_url"] = url
    text, err = fetch(url, session)

    # Some states 403 one hostname and serve another perfectly well. Try the
    # recorded alternates rather than writing the state off on one failure.
    if err and rec.get("url_candidates"):
        for alt in rec["url_candidates"]:
            if alt == url:
                continue
            text, err2 = fetch(alt, session)
            if not err2 and text:
                url, err = alt, None
                out["source_url"] = alt
                out["used_alternate_url"] = True
                break

    if err:
        # Keep serving the last known good value, but mark it stale so the UI
        # can show its age. A fetch failure is not evidence of full-staff.
        out.update(error=err,
                   state_status=prev.get("state_status", P.UNKNOWN),
                   state_order=prev.get("state_order"),
                   last_changed_at=prev.get("last_changed_at"),
                   coverage="stale" if prev else "not_covered")
        return code, out, prev

    h = content_hash(text)
    out["changed"] = bool(prev.get("hash")) and prev["hash"] != h

    # --- Unchanged: reuse the cached verdict, skip all parsing -------------
    if prev.get("hash") == h and "state_status" in prev:
        out["state_status"] = prev["state_status"]
        out["state_order"] = prev.get("state_order")
        out["last_changed_at"] = prev.get("last_changed_at")
        # checked_at already reflects this run; the cache keeps the older
        # last_changed_at untouched.
        new_cache = dict(prev, hash=h, last_checked=out["checked_at"])
        return code, out, new_cache

    # --- Changed or first seen: parse ---------------------------------------
    if mode == "toggle":
        st, ev = P.parse_toggle(text, url)
        out["state_status"] = st
        out["changed"] = bool(prev.get("hash")) and prev["hash"] != h
        if st != P.UNKNOWN:
            out["state_order"] = {"title": None, "url": url, "status": st,
                                  "authority": P.GOVERNOR, "evidence": ev,
                                  "start_date": None, "end_date": None}
        else:
            out["error"] = ev
    elif mode == "diff":
        # A status page unchanged for months is not reporting today's status,
        # it is reporting the day it froze. Arizona's half-staff page still
        # announces a January 2025 order; trusting it would mean half-staff
        # every day forever — a confident lie, which is worse than a gap.
        lastmod = P.page_last_modified(text)
        if not lastmod:
            # No date anywhere on the page means the freshness alarm cannot
            # run. Say so rather than letting the state look guarded when it
            # is not — an unverifiable source should be visibly unverifiable.
            out["source_age_days"] = None
            out["freshness_unknown"] = True
        if lastmod:
            age = (today() - lastmod).days
            out["source_last_modified"] = lastmod.isoformat()
            out["source_age_days"] = age
            if age > FROZEN_PAGE_DAYS:
                out["state_status"] = P.UNKNOWN
                out["coverage"] = "stale"
                out["error"] = (f"source appears frozen: newest date on page is "
                                f"{lastmod} ({age}d old) - not trusted")
                return code, out, {
                    "hash": h, "state_status": P.UNKNOWN, "state_order": None,
                    "last_parsed": out["checked_at"],
                    "last_checked": out["checked_at"],
                    "last_changed_at": prev.get("last_changed_at")}

        # No history exists on these pages. The page IS the status.
        d = P.parse_diff(text, previous_hash=prev.get("hash"),
                         selector_hint="flag")
        out["state_status"] = d["status"]
        out["changed"] = d["changed"]
        if d.get("counties"):
            out["county_exceptions"] = d["counties"]
        # A status page can keep advertising an order that already ended.
        # Alaska was still showing the expired July 12-18 federal proclamation
        # in mid-August. If the page states a window, honour it.
        if d["status"] == P.HALF and (d.get("start_date") or d.get("end_date")):
            v, why = covers_today(d.get("start_date"), d.get("end_date"), today())
            if v is False:
                d["status"] = P.FULL
                out["state_status"] = P.FULL
                out["last_expired_order"] = {"why": why, "url": url}
        if d["status"] != P.UNKNOWN:
            out["state_order"] = {
                "title": None,
                "url": url,
                "status": d["status"],
                "authority": P.GOVERNOR if not rec.get("signature_check_required")
                             else P.UNKNOWN,
                "evidence": d["evidence"],
                "start_date": None,
                "end_date": None,
            }
        # Alaska-class sources mix federal reposts into the same page. Gate
        # only HALF claims on proven authority — a FULL reading needs no
        # signature, since "the flag is up" is not an order attributable to
        # anyone. Requiring proof there would discard good data.
        if rec.get("signature_check_required") and out["state_status"] == P.HALF:
            auth, ev = P.classify_authority(text)
            if auth != P.GOVERNOR:
                out["state_status"] = P.UNKNOWN
                out["state_order"] = None
                out["error"] = f"authority unproven ({auth}); not claimed as state order"
            elif out["state_order"]:
                out["state_order"]["authority"] = P.GOVERNOR
                out["state_order"]["evidence"] = ev
    else:
        items = (P.parse_feed(text, url) if mode == "feed"
                 else P.parse_archive(text, url) if mode == "archive"
                 else P.parse_index(text, url))
        if rec.get("dedupe_translations"):
            items = P.dedupe_orders(items)
        flags = [i for i in items if i.get("is_flag")]

        order, verdict, why = None, None, None
        cutoff = today() - timedelta(days=MAX_ORDER_AGE_DAYS)
        for i in flags:
            d = i.get("date")
            item_date = None
            if d:
                try:
                    item_date = date.fromisoformat(d)
                except ValueError:
                    pass
                if item_date and item_date < cutoff:
                    continue
            # Use each source for what it is actually reliable at.
            #
            #   HEADLINE -> status and authority. "Governor Orders Flags to
            #     Half-Staff" is unambiguous by construction.
            #   BODY     -> dates. "from sunrise to sunset on Friday, August
            #     14" only ever appears in the body.
            #
            # Feeding the whole body to the status classifier backfires: a
            # press release routinely contains both "half-staff" and "full
            # staff" (orders usually say when the flag goes back up), and the
            # classifier correctly refuses to guess when it sees both. That
            # turned real orders into UNKNOWN and dropped them.
            rec_o = P.extract_order(i["title"], i["url"], i["title"])

            # A flag headline that does not state its status is common:
            # "Gov. Whitmer Lowers Flags to Honor Detroit Fire Fighter
            # Patrick Trout" never says half-staff. When that happens, read
            # the OPENING of the order, which almost always says it outright
            # ("...to be lowered to half-staff on Tuesday, August 18").
            #
            # Only the first few sentences, not the whole body: further down,
            # a release routinely mentions returning to full staff, and the
            # classifier correctly refuses to choose when it sees both.
            if rec_o["status"] == P.UNKNOWN and i.get("url") and i["url"] != url:
                art, _ = fetch(i["url"], session)
                if art:
                    opening = " ".join(
                        re.split(r"(?<=[.!?])\s+", P.strip_html(art))[:3])[:700]
                    st2, ev2 = P.classify_status(opening)
                    if st2 != P.UNKNOWN:
                        rec_o["status"] = st2
                        rec_o["status_evidence"] = f"from order body: {ev2}"
                        a2, aev2 = P.classify_authority(opening)
                        if a2 != P.UNKNOWN:
                            rec_o["authority"] = a2
                            rec_o["authority_evidence"] = aev2
                        rec_o["usable_as_state_order"] = (
                            st2 == P.HALF and rec_o["authority"] == P.GOVERNOR)

            if rec_o["status"] != P.HALF:
                continue
            # A capitol-only or single-county order is not a statewide
            # half-staff day. South Dakota issues both kinds and titles them
            # differently; counting them the same over-reports the state.
            scope, scope_ev = P.order_scope(i["title"])
            if scope == "limited":
                out.setdefault("limited_orders", []).append(
                    {"title": i["title"], "url": i["url"], "scope": scope_ev})
                continue
            if rec.get("signature_check_required") and \
                    rec_o["authority"] != P.GOVERNOR:
                continue
            # Now open the order for its dates only.
            if i.get("url") and i["url"] != url and not rec_o["end_date"]:
                art, _ = fetch(i["url"], session)
                if art:
                    bstart, bend = P.date_range(P.strip_html(art)[:8000])
                    if bstart and not rec_o["start_date"]:
                        rec_o["start_date"] = bstart.isoformat()
                    if bend:
                        rec_o["end_date"] = bend.isoformat()
                    rec_o["dates_from"] = "order body"

            rec_o["date"] = d
            # Fall back to the item's publication date as the start when
            # neither the headline nor the body carries one.
            start = rec_o["start_date"] or d
            v, w = covers_today(start, rec_o["end_date"], today())
            if v:                       # provably active — take it and stop
                order, verdict, why = rec_o, True, w
                break
            if v is None and item_date and \
                    (today() - item_date).days <= AMBIGUOUS_WINDOW_DAYS:
                # Recent but undated: we genuinely cannot tell. Remember it,
                # but keep looking for something provable.
                order, verdict, why = rec_o, None, w
            elif order is None:
                order, verdict, why = rec_o, False, w

        if verdict is True:
            out["state_status"] = P.HALF
            out["state_order"] = dict(order, coverage_reason=why)
        elif verdict is None and order is not None:
            out["state_status"] = P.UNKNOWN
            out["state_order"] = dict(order, coverage_reason=why)
            out["error"] = "recent order found but dates unparseable"
        elif items:
            # Source read cleanly; no order proves it covers today.
            out["state_status"] = P.FULL
            out["last_expired_order"] = (
                {"title": order["title"], "url": order["url"], "why": why}
                if order else None)
        else:
            out["state_status"] = P.UNKNOWN
            out["error"] = "source readable but no items parsed"

    # Content moved (or this is the first sighting), so the change stamp
    # advances. On a first sighting we have no prior state to compare against,
    # so we record now rather than claiming a change we did not observe.
    out["last_changed_at"] = out["checked_at"]
    new_cache = {
        "hash": h,
        "state_status": out["state_status"],
        "state_order": out["state_order"],
        "last_parsed": out["checked_at"],
        "last_checked": out["checked_at"],
        "last_changed_at": out["checked_at"],
    }
    if verbose:
        print(json.dumps(out, indent=2))
    return code, out, new_cache


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state")
    args = ap.parse_args()

    registry = load_json(REGISTRY, [])
    if not registry:
        print("registry.json missing or empty — run merge.py first.")
        sys.exit(1)
    cache = load_json(CACHE, {})
    if cache.get("_parser_version") != PARSER_VERSION:
        if cache:
            print(f"Parser version changed -> discarding cache "
                  f"({cache.get('_parser_version')} -> {PARSER_VERSION})")
        cache = {}
    session = requests.Session()

    d = today()
    fed = federal_statutory(d) or federal_proclamation(session, cache)

    targets = [r for r in registry
               if not args.state or r["state_code"] == args.state.upper()]

    results, new_cache = {}, dict(cache)
    new_cache["_parser_version"] = PARSER_VERSION
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(check_state, r, cache, requests.Session(),
                          bool(args.state))
                for r in targets]
        for f in cf.as_completed(futs):
            try:
                code, out, cent = f.result()
            except Exception as e:
                print(f"  ERROR {type(e).__name__}: {e}")
                continue
            results[code] = out
            if cent:
                new_cache[code] = cent

    # --- Merge federal over state ------------------------------------------
    for code, s in results.items():
        if fed:
            s["effective_status"] = P.HALF
            s["reason"] = fed["reason"]
            s["reason_source"] = "federal"
            s["scope"] = fed.get("scope", "full-day")
            s["federal"] = fed
        elif s["state_status"] == P.HALF:
            s["effective_status"] = P.HALF
            s["reason"] = (s["state_order"] or {}).get("title") or "State order"
            s["reason_source"] = "state"
            s["scope"] = ("until-noon" if (s["state_order"] or {}).get("until_noon")
                          else "full-day")
        elif s["state_status"] == P.FULL:
            s["effective_status"] = P.FULL
            s["reason"] = None
            s["reason_source"] = None
            s["scope"] = None
        else:
            s["effective_status"] = P.UNKNOWN
            s["reason"] = None
            s["reason_source"] = None
            s["scope"] = None

    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": d.isoformat(),
        "federal": fed,
        "states": dict(sorted(results.items())),
        "meta": {
            "covered": sum(1 for s in results.values() if s["coverage"] == "covered"),
            "not_covered": sum(1 for s in results.values()
                               if s["coverage"] == "not_covered"),
            "stale": sum(1 for s in results.values() if s["coverage"] == "stale"),
            "errors": sum(1 for s in results.values() if s["error"]),
            "total": len(results),
        },
    }

    changed = [c for c, s in results.items() if s["changed"]]
    half = [c for c, s in results.items() if s["effective_status"] == P.HALF]

    print(f"\n{'='*54}")
    print(f"  {status['date']}   {status['meta']['covered']}/"
          f"{status['meta']['total']} covered")
    if fed:
        print(f"  FEDERAL: half-staff — {fed['reason']}")
    print(f"  half-staff: {' '.join(sorted(half)) or 'none'}")
    print(f"  CHANGED this run: {' '.join(sorted(changed)) or 'none'}")
    print(f"  not covered: {status['meta']['not_covered']}   "
          f"stale: {status['meta']['stale']}   errors: {status['meta']['errors']}")
    # Break the error total out by cause. "16 errors" hides whether the
    # pipeline is blocked, broken, or simply pointed at nothing.
    causes = {}
    for s in results.values():
        e = s.get("error")
        if not e:
            continue
        if "robots.txt" in e:            k = "blocked by robots.txt"
        elif "HTTP 4" in e or "HTTP 5" in e: k = "HTTP error"
        elif "timeout" in e.lower():     k = "timeout"
        elif "frozen" in e:              k = "frozen source"
        elif "no verified source" in e:  k = "no source"
        elif "no items parsed" in e:     k = "parsed nothing"
        else:                            k = e[:40]
        causes[k] = causes.get(k, 0) + 1
    for k, v in sorted(causes.items(), key=lambda x: -x[1]):
        print(f"      {v:3d}  {k}")
    print(f"{'='*54}")

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return

    with open(OUTPUT, "w") as f:
        json.dump(status, f, indent=2)
    with open(CACHE, "w") as f:
        json.dump(new_cache, f, indent=2)
    # Append-only log of changes. Cheap, and invaluable when something breaks.
    if changed:
        with open(HISTORY, "a") as f:
            for c in changed:
                f.write(json.dumps({
                    "at": status["generated_at"], "state": c,
                    "status": results[c]["effective_status"],
                    "reason": results[c]["reason"],
                }) + "\n")
    print(f"\nWrote {OUTPUT} and {CACHE}")


if __name__ == "__main__":
    main()
