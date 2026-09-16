#!/usr/bin/env python3
"""
run.py — the pipeline. Reads registry.json, checks every source, writes
status.json.

    python3 run.py              # normal run
    python3 run.py --dry-run    # fetch and report, write nothing
    python3 run.py --state NE   # one state, verbose

DESIGN NOTES

Every source is fetched and re-parsed every run, because every verdict depends
on today's date as well as the page. What is cached is the expensive part:
facts read from individual order pages, keyed by URL, so an order page is
fetched once rather than every 30 minutes. The content hash is kept only to
report whether the page's text moved (content_changed).

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

# Bump whenever parsing logic changes. Verdicts are no longer cached (they are
# recomputed every run), but facts extracted from individual order pages are,
# and those were produced by whatever parser was current at the time. A bump
# discards them while keeping the memory of last known answers.
PARSER_VERSION = "26"

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
# An email channel that has sent nothing at all (any message, not just flag
# orders) for this long is no longer evidence of "no order". The email states
# use governors' press lists, which send weekly; a flag-only list with long
# quiet stretches can raise it per state with channel_max_silence_days.
CHANNEL_MAX_SILENCE_DAYS = 60
# How long a source that will not load keeps answering from its last read.
# Past this it reports unknown: an unreadable source is a gap, not a verdict.
STALE_MAX_DAYS = 3
MAX_LISTING_PAGES = 3        # hard cap on listing requests per state per run
EMAIL_INGEST_MAX_AGE_DAYS = 1    # email-orders.json older than this is not today's inbox


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
                # An explicit one-day window, so a carried-forward copy of
                # this order expires instead of living on undated.
                "start_date": d.isoformat(),
                "end_date": d.isoformat(),
            }
    return None


# The White House proclamations listing. Presidential half-staff orders are
# published here — and they are titled things like "Honoring the Victims of
# the Tragedy in Minneapolis, Minnesota". The words "flag" and "half-staff"
# appear nowhere in the title, only in the body. So this cannot be a headline
# scan; each recent proclamation has to be opened and read.
FEDERAL_SOURCES = [
    "https://www.whitehouse.gov/presidential-actions/proclamations/",
    "https://www.whitehouse.gov/presidential-actions/",
]
FEDERAL_LOOKBACK_DAYS = 21
# Proclamation pages are ~3,000 characters of navigation, then the text, and
# the flag sentence sits in the closing "NOW, THEREFORE" section. Bodies used
# to be cut at 6,000 characters before the flag search, so any proclamation
# longer than a short one was read as having no flag language at all — the
# Patriot Day 2026 proclamation has its flag sentence at character 10,310.
FEDERAL_BODY_LIMIT = 100_000
# Bodies stored under the old key were truncated at 6,000 characters, so they
# are discarded rather than trusted.
FEDERAL_ARTICLE_CACHE = "_federal_articles_v2"
ARTICLE_URL_RE = re.compile(r"/presidential-actions/20\d\d/\d{2}/")

# Titles that carry a half-staff order almost always take one of these forms.
# Used only to prioritise which articles to open first, never to reject one.
FEDERAL_TITLE_HINTS = re.compile(
    r"honoring\s+the\s+(?:victims|memory|life)|in\s+memory\s+of|death\s+of"
    r"|passing\s+of|tragedy|honoring\s+", re.I)


def federal_proclamation(session, cache):
    """Active presidential half-staff proclamation. Returns (order, error).

    order is None both when there is no order and when we could not look, so
    error is what tells them apart: None means "checked, nothing active",
    a string means "could not determine". Treating an unreachable
    whitehouse.gov as "no national order" would drop a live order for every
    state and then re-announce it when the site came back.

    Opens recent proclamations and reads their bodies. Article text is cached
    by URL, so each proclamation is fetched once — the listing is the only
    thing re-fetched each run. A failed fetch is NOT cached: storing it as
    empty text made one timeout permanently hide that proclamation.
    """
    cache.pop("_federal_articles", None)
    url = os.environ.get("FEDERAL_PROCLAMATION_URL") or FEDERAL_SOURCES[0]
    listing, err = fetch(url, session)
    if err or not listing:
        for alt in FEDERAL_SOURCES:
            if alt == url:
                continue
            listing, err = fetch(alt, session)
            if listing:
                url = alt
                break
    if not listing:
        return None, f"could not fetch proclamations listing ({err})"

    items = P.parse_index(listing, url)
    cutoff = today() - timedelta(days=FEDERAL_LOOKBACK_DAYS)

    # Only proclamation article URLs, newest first, recent ones only. Nav
    # links ("Skip to content", "Executive Orders") used to take up slots in
    # the 12-article budget.
    cands = []
    for i in items:
        u = i.get("url") or ""
        if not ARTICLE_URL_RE.search(u):
            continue
        d = None
        m = re.search(r"/(20\d\d)/(\d{2})/", u)
        if i.get("date"):
            try:
                d = date.fromisoformat(i["date"])
            except ValueError:
                d = None
        if d is None and m:
            try:
                d = date(int(m.group(1)), int(m.group(2)), 1)
            except ValueError:
                d = None
        if d and d < cutoff.replace(day=1):
            continue
        cands.append((d, i))

    # Likely half-staff titles first, so the usual case costs one fetch.
    cands.sort(key=lambda x: (not FEDERAL_TITLE_HINTS.search(x[1]["title"] or ""),
                              -(x[0].toordinal() if x[0] else 0)))

    art_cache = cache.setdefault(FEDERAL_ARTICLE_CACHE, {})
    checked, unread = 0, []
    for d, i in cands:
        if checked >= 12:
            break
        u = i["url"]
        body = art_cache.get(u)
        if body is None:
            text, ferr = fetch(u, session)
            checked += 1
            if ferr or not text:
                unread.append(ferr or "empty")
                continue
            body = P.strip_html(text)[:FEDERAL_BODY_LIMIT]
            art_cache[u] = body
        if not body:
            continue

        m = P.FLAG_RE.search(body)
        if not m:
            continue

        # Classify a window around the flag language, NOT the top of the page.
        # whitehouse.gov article pages open with ~3000 characters of site
        # navigation, so classifying body[:1500] classified a menu: the flag
        # regex matched further down and the status came back unknown while
        # a live national proclamation sat right there.
        lo = max(0, m.start() - 300)
        window = body[lo:m.start() + 2000]
        status, sev = P.classify_status(window)
        if status != P.HALF:
            continue
        auth, aev = P.classify_authority(window)
        if auth == P.GOVERNOR:
            continue          # a governor's order does not belong here
        start, end = P.date_range(window)

        # A proclamation usually states only its END ("...until sunset,
        # August 31, 2026"). With no earlier date in the text, date_range
        # returns that same date as the start too, which makes the order look
        # like it has not begun yet — and the site keeps showing full staff
        # through the entire order. If the only date we have is the end, the
        # start is when it was published.
        if start and end and start >= end:
            start = d if (d and d < end) else None
        v, why = covers_today(start.isoformat() if start else None,
                              end.isoformat() if end else None, today())
        if v is not True:
            continue

        return {
            "status": P.HALF,
            "scope": "until-noon" if P.until_noon(window) else "full-day",
            "reason": i["title"],
            "authority": "presidential proclamation",
            "citation": None,
            "source_url": u,
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "coverage_reason": why,
        }, None
    if unread:
        # The one page we could not open may be the order.
        return None, (f"{len(unread)} recent proclamation page(s) could not be "
                      f"read ({', '.join(sorted(set(unread)))})")
    return None, None


def channel_url(rec):
    """A link a reader can actually open for an email-ingested state.

    notification_channel.detail is prose for some states ("Send a blank email
    to subscribe-...@listserv.state.ma.us") and a URL with a note appended for
    others, so take the first real URL from the channel, then the signup or
    flag page.
    """
    nc = rec.get("notification_channel") or {}
    for cand in (nc.get("url"), nc.get("detail"), rec.get("signup_url"),
                 rec.get("flag_page_url")):
        m = re.search(r"https?://\S+", cand or "")
        if m:
            return m.group(0).rstrip(".,;")
    return None


def carry_federal(prev_fed, d):
    """Keep a previously read proclamation while its stated window lasts.

    This run may not return an order we already read: we could not look, or
    it has scrolled off the first page of the listing. The Dolly Parton
    proclamation was gone from page 1 within two weeks; a 30-day order would
    have ended early for all 50 states. Statutory days are excluded — they
    are recomputed from the calendar every run.
    """
    if not prev_fed or prev_fed.get("authority") == "statute":
        return None
    if revalidate({"state_status": P.HALF, "state_order": prev_fed}, d)[0] != P.HALF:
        return None
    return dict(prev_fed, carried_forward=True)


def pick_url(rec):
    url = _pick_url(rec)
    # Some archives are per-year (New Jersey: /news/2026/approved/...). A
    # hardcoded year keeps reading last year's archive after January 1 and
    # reports "no current order" from a page that no longer gets updates.
    return url.replace("{year}", str(today().year)) if url else url


def _pick_url(rec):
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


def revalidate(prev, d):
    """Re-derive a carried-forward verdict for date d.

    A verdict is a function of (page, date), not of the page alone. Serving a
    cached HALF because the page had not changed kept North Dakota at
    half-staff from Sept 9 to Sept 15 2026 for an order covering one Friday:
    the RSS feed simply did not change, so the order was never re-checked
    against the calendar. Returns (status, order, expired_order_or_None).
    """
    st, order = prev.get("state_status", P.UNKNOWN), prev.get("state_order")
    if st != P.HALF or not order:
        return st, order, None
    start = order.get("start_date") or order.get("date")
    end = order.get("end_date")
    if not (start or end):
        return st, order, None
    v, why = covers_today(start, end, d)
    if v:
        return P.HALF, dict(order, coverage_reason=why), None
    expired = {"title": order.get("title"), "url": order.get("url"), "why": why}
    return (P.FULL if v is False else P.UNKNOWN), None, expired


def article_facts(url, session, known, keep):
    """What an individual order page says, fetched once per URL.

    This is what the old whole-verdict cache was really saving: re-reading
    order pages. Caching facts about each page instead lets the verdict be
    recomputed against today's date on every run. A failed fetch returns None
    and is not stored, so it is retried next run.
    """
    f = known.get(url)
    if f is None:
        art, _ = fetch(url, session)
        if not art:
            return None
        text = P.strip_html(art)
        opening = " ".join(re.split(r"(?<=[.!?])\s+", text)[:3])[:700]
        st, sev = P.classify_status(opening)
        au, aev = P.classify_authority(opening)
        bs, be = P.date_range(text[:8000])
        f = {"opening_status": st, "opening_evidence": sev,
             "opening_authority": au, "opening_authority_evidence": aev,
             "body_start": bs.isoformat() if bs else None,
             "body_end": be.isoformat() if be else None,
             "opening": opening}
    keep[url] = f
    return f


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
        # checked_at: we fetched this source on this run; always advances.
        # last_changed_at: the ANSWER last moved (set in main from status
        # transitions). content_changed: the page's text moved this run.
        # The page and the answer are two facts. Keying notifications and
        # "unchanged since" off page edits sent Nevada subscribers 479
        # "back to full staff" pushes in a month while the flag never moved.
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_changed_at": None,
        "content_changed": False,
        "error": None,
    }

    # --- email mode: read what the state sent us, not what its site serves --
    if rec.get("ingest_mode") == "email":
        mail = load_json(EMAIL_ORDERS, {})
        o = (mail.get("orders") or {}).get(code)
        out["source_url"] = channel_url(rec)

        # Silence means different things, and they must not share a value:
        #   - we did not read the inbox (credentials missing, IMAP down)
        #   - the channel has never delivered (signup unconfirmed, spam folder)
        #   - it delivered once but has gone quiet for months (subscription
        #     lapsed, sender changed domain, list retired)
        #   - it is alive and simply has no order today
        # Only the last is evidence of full staff. The third used to read as
        # FULL forever: one bulletin in 2026 would have kept a state "covered"
        # indefinitely after its list stopped reaching us.
        # Two dates. seen: the channel has delivered a flag ORDER — proof the
        # flag topic actually reaches us, which a signup confirmation is not.
        # heard: it has sent anything at all — proof it is still alive.
        seen = (mail.get("channels_seen") or {}).get(code)
        heard = max((mail.get("channels_heard") or {}).get(code) or "", seen or "") or None
        out["channel_last_heard"] = heard
        limit = rec.get("channel_max_silence_days") or CHANNEL_MAX_SILENCE_DAYS
        silent_for = (today() - date.fromisoformat(heard)).days if heard else None
        gen = mail.get("generated_at")
        if mail.get("skipped") or not gen:
            ingest_err = "email ingest did not run (no mail credentials)"
        elif (today() - datetime.fromisoformat(gen).date()).days > EMAIL_INGEST_MAX_AGE_DAYS:
            ingest_err = f"email ingest has not run since {gen[:10]}"
        else:
            ingest_err = None

        v, why = (covers_today(o.get("start_date"), o.get("end_date"), today())
                  if o else (None, None))
        if v:
            # A bulletin we did read that covers today stands on its own.
            out["state_status"] = P.HALF
            out["state_order"] = {
                "title": o.get("subject"), "url": out["source_url"],
                "status": P.HALF, "authority": o.get("authority"),
                "start_date": o.get("start_date"), "end_date": o.get("end_date"),
                "until_noon": o.get("until_noon"), "coverage_reason": why,
                "via": "official notification email",
            }
            out["error"] = ingest_err
        elif seen is None:
            out.update(state_status=P.UNKNOWN, coverage="not_covered",
                       error=ingest_err or ("subscription pending - no flag bulletin "
                                            "received from this channel yet"))
        elif silent_for > limit:
            out.update(state_status=P.UNKNOWN, coverage="frozen",
                       error=(f"notification channel silent since {heard} "
                              f"({silent_for}d, limit {limit}d) - silence is no "
                              f"longer read as full staff"))
        elif ingest_err:
            # Like a failed fetch: serve the last known value, marked stale.
            st, order, expired = revalidate(prev, today())
            out.update(state_status=st, state_order=order, coverage="stale",
                       error=ingest_err)
            if expired:
                out["last_expired_order"] = expired
        else:
            out["state_status"] = P.FULL
        h = content_hash(json.dumps(o, sort_keys=True))
        out["content_changed"] = bool(prev.get("hash")) and prev["hash"] != h
        return code, out, dict(prev, hash=h, state_status=out["state_status"],
                               state_order=out["state_order"],
                               last_parsed=out["checked_at"],
                               last_checked=out["checked_at"])

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
        # A fetch failure is not evidence of full-staff. Serve the last known
        # value, marked stale — but only for a few days. A source we have not
        # read all week is a gap, and saying "full staff, last read Tuesday"
        # about a flag that may have come down since is the confident-wrong
        # answer this whole project exists to avoid.
        st, order, expired = revalidate(prev, today())
        last_ok = (prev.get("last_parsed") or "")[:10]
        dark = (today() - date.fromisoformat(last_ok)).days if last_ok else None
        fails = (prev.get("consecutive_errors") or 0) + 1
        out["consecutive_errors"] = fails
        if not prev.get("hash"):
            out.update(state_status=P.UNKNOWN, coverage="not_covered", error=err)
        elif dark is not None and dark > STALE_MAX_DAYS:
            out.update(state_status=P.UNKNOWN, state_order=None,
                       coverage="not_covered",
                       error=(f"{err}; unreadable since {last_ok} ({dark}d, "
                              f"{fails} consecutive failures)"))
        else:
            out.update(state_status=st, state_order=order, coverage="stale",
                       error=f"{err}; last read {last_ok or 'never'}")
            if expired:
                out["last_expired_order"] = expired
        return code, out, dict(prev, last_checked=out["checked_at"],
                               consecutive_errors=fails)

    # Extra listing pages, for sites whose first page covers only a day or
    # two. Hard-capped: New York sits behind a Cloudflare challenge, so every
    # request is a chance to be blocked for the rest of the run. Best effort —
    # a failure on page 2 keeps page 1 rather than failing the state.
    if rec.get("listing_pages") and mode in ("index", "archive", "feed"):
        extra = []
        for n in range(1, min(int(rec["listing_pages"]), MAX_LISTING_PAGES)):
            more, perr = fetch(f"{url}{'&' if '?' in url else '?'}page={n}", session)
            if perr or not more:
                break
            extra.append(more)
        out["listing_pages_read"] = 1 + len(extra)
        if extra:
            text = text + "\n" + "\n".join(extra)

    h = content_hash(text)
    out["content_changed"] = bool(prev.get("hash")) and prev["hash"] != h

    # Always parse. There used to be a shortcut here that reused the cached
    # verdict whenever the page hash was unchanged. Every verdict depends on
    # today's date (order windows, grace periods, freshness age), so the
    # shortcut served expired orders (ND), missed scheduled ones, and threw
    # away the freshness alarm's reason after its first run (AL, CO). Parsing
    # is local; the only costly part — opening order pages — is cached per
    # URL in article_facts.
    prev_articles = prev.get("articles") or {}
    articles = {}

    frozen = False
    if mode == "diff":
        # A status page unchanged for months is not reporting today's status,
        # it is reporting the day it froze. Arizona's half-staff page once
        # announced a January 2025 order for most of a year; trusting it would
        # mean half-staff every day — a confident lie, worse than a gap.
        lastmod = P.page_last_modified(text)
        if lastmod:
            age = (today() - lastmod).days
            out["source_last_modified"] = lastmod.isoformat()
            out["source_age_days"] = age
            frozen = age > FROZEN_PAGE_DAYS
        else:
            # No date anywhere on the page means the freshness alarm cannot
            # run. Say so rather than letting the state look guarded when it
            # is not — an unverifiable source should be visibly unverifiable.
            out["source_age_days"] = None
            out["freshness_unknown"] = True

    if mode == "toggle":
        st, ev = P.parse_toggle(text, url)
        out["state_status"] = st
        if st != P.UNKNOWN:
            out["state_order"] = {"title": None, "url": url, "status": st,
                                  "authority": P.GOVERNOR, "evidence": ev,
                                  "start_date": None, "end_date": None}
        else:
            out["error"] = ev
    elif mode == "diff" and frozen:
        # "frozen", not "stale". Stale means "fetch failed, serving the last
        # known value"; a frozen page has no value to serve. Sharing one word
        # made the UI say "serving last known value" beside "Unclear".
        out["state_status"] = P.UNKNOWN
        out["coverage"] = "frozen"
        out["error"] = (f"source appears frozen: newest date on page is "
                        f"{lastmod} ({age}d old) - not trusted")
    elif mode == "diff":
        # No history exists on these pages. The page IS the status.
        d = P.parse_diff(text, previous_hash=prev.get("hash"),
                         selector_hint="flag")
        out["state_status"] = d["status"]
        if d["status"] == P.UNKNOWN:
            # Record why. With error left empty, "page read but states no
            # status" was indistinguishable from a healthy state, and the CI
            # warning step (which lists states with errors) never saw it.
            out["error"] = d["evidence"] or "no status declaration found on page"
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
        elif d["status"] == P.HALF:
            # No advertised window, but the page may list the order itself.
            # If every half-staff order on the page has ended, the widget has
            # not been reset: "the page says half" and "an order is in
            # effect" disagree, so we report that rather than either one.
            wins = P.listed_order_windows(text)
            live = [w for w in wins if covers_today(
                w[0].isoformat() if w[0] else None, w[1].isoformat(), today())[0]]
            if wins and not live:
                latest = max(e for _, e in wins)
                d["status"] = P.UNKNOWN
                out["state_status"] = P.UNKNOWN
                out["error"] = (f"page still declares half-staff, but the order "
                                f"it lists ended {latest}")
                out["last_expired_order"] = {"why": f"listed order ended {latest}",
                                             "url": url}
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
        listing_ok, listing_ev = P.listing_evidence(items, url)
        if mode == "index":
            out["listing_evidence"] = listing_ev

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
            f = None                    # facts from this item's own order page

            # New York states the fact in the URL: "governor-hochul-directs-
            # flags-half-staff-honor-...". Taking the slug as the status is
            # what makes the listing classifiable without opening anything,
            # which matters on a site that blocks us intermittently.
            if rec_o["status"] == P.UNKNOWN and P.is_flag_slug(i.get("url")):
                rec_o["status"] = P.HALF
                rec_o["status_evidence"] = "half-staff stated in the URL slug"

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
                f = article_facts(i["url"], session, prev_articles, articles)
                if f and f["opening_status"] != P.UNKNOWN:
                    st2 = f["opening_status"]
                    rec_o["status"] = st2
                    rec_o["status_evidence"] = f"from order body: {f['opening_evidence']}"
                    if f["opening_authority"] != P.UNKNOWN:
                        rec_o["authority"] = f["opening_authority"]
                        rec_o["authority_evidence"] = f["opening_authority_evidence"]
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
                f = article_facts(i["url"], session, prev_articles, articles)
                if f:
                    if f["body_start"] and not rec_o["start_date"]:
                        rec_o["start_date"] = f["body_start"]
                    if f["body_end"]:
                        rec_o["end_date"] = f["body_end"]
                    rec_o["dates_from"] = "order body"
                elif not rec_o["start_date"] and not d:
                    # We have a flag headline and no date anywhere, because
                    # its page would not load. New York's articles sit behind
                    # the same Cloudflare challenge as its listing; calling
                    # that full staff would be a confident answer about an
                    # order we can see but cannot read.
                    rec_o["dates_unavailable"] = True

            # "Half-staff Friday": a weekday with no date. Resolve it against
            # when the order was published, from the headline first, then the
            # order's opening. A weekday beats a start-only date here, because
            # that date is usually the press release's own dateline.
            if not rec_o["end_date"]:
                ref = item_date or (date.fromisoformat(f["body_start"])
                                    if f and f.get("body_start") else None)
                ws, we = P.weekday_window(i["title"], ref)
                if not we and f:
                    ws, we = P.weekday_window(f.get("opening") or "", ref)
                if we:
                    rec_o["start_date"], rec_o["end_date"] = ws.isoformat(), we.isoformat()
                    rec_o["dates_from"] = f"weekday resolved against {ref}"

            rec_o["date"] = d
            # Fall back to the item's publication date as the start when
            # neither the headline nor the body carries one.
            start = rec_o["start_date"] or d
            v, w = covers_today(start, rec_o["end_date"], today())
            if v:                       # provably active — take it and stop
                order, verdict, why = rec_o, True, w
                break
            if v is None and rec_o.get("dates_unavailable"):
                order, verdict, why = (rec_o, None,
                                       "order page could not be read to date it")
            elif v is None and item_date and \
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
            out["error"] = ("flag order found, but its page could not be read "
                            "to date it" if order.get("dates_unavailable")
                            else "recent order found but dates unparseable")
        elif items and mode == "index" and not listing_ok:
            # "No flag headline among these links" is only evidence of full
            # staff if the links are press releases. These are not.
            out["state_status"] = P.UNKNOWN
            out["error"] = (f"page read, but it has no press listing ({listing_ev}) "
                            f"- likely navigation only; not read as full staff")
        elif items:
            # Source read cleanly; no order proves it covers today.
            out["state_status"] = P.FULL
            out["last_expired_order"] = (
                {"title": order["title"], "url": order["url"], "why": why}
                if order else None)
        else:
            out["state_status"] = P.UNKNOWN
            out["error"] = "source readable but no items parsed"

        # What the parser actually found, so a page that only yielded site
        # navigation is visible in status.json rather than hidden inside FULL.
        dated = sorted(i["date"] for i in items if i.get("date"))
        out["items_parsed"] = len(items)
        out["flag_items"] = len(flags)
        out["dated_items"] = len(dated)
        # A feed whose newest item is years old is not reporting today.
        # South Carolina's registered feed last published in January 2020 and
        # was being read as "no current order" every 30 minutes.
        if mode in ("feed", "archive") and dated:
            newest = date.fromisoformat(dated[-1])
            age = (today() - newest).days
            out["source_last_modified"] = newest.isoformat()
            out["source_age_days"] = age
            if age > FROZEN_PAGE_DAYS:
                out.update(state_status=P.UNKNOWN, state_order=None,
                           coverage="frozen",
                           error=(f"feed appears frozen: newest item is {newest} "
                                  f"({age}d old) - not trusted"))

    new_cache = dict(prev)
    new_cache.update({
        "hash": h,
        "state_status": out["state_status"],
        "state_order": out["state_order"],
        "last_parsed": out["checked_at"],
        "last_checked": out["checked_at"],
        "articles": articles,
        "consecutive_errors": 0,
    })
    if verbose:
        print(json.dumps(out, indent=2))
    return code, out, new_cache


def last_national_day(d):
    """The most recent statutory half-staff day before d, or None."""
    cal = load_json(CALENDAR, {}).get("years", {})
    days = [o["date"] for y in (d.year - 1, d.year) for o in cal.get(str(y), [])
            if o.get("active") and o.get("date", "") < d.isoformat()]
    return max(days) if days else None


def track_changes(results, cache, new_cache, legacy_floor=None):
    """Mark which states' ANSWER changed, not which pages changed.

    `changed` drives push notifications and history. It used to mean "the
    source page's text moved", which fired on every edit to a governor's site:
    85% of history entries were not status changes at all. It now means a
    known answer (half/full) differs from the last known answer. A move to or
    from "unknown" is not announced — telling subscribers "back to full staff"
    because a page broke would be a false claim.
    """
    for code, s in results.items():
        prev = cache.get(code) or {}
        last_known = prev.get("last_known_status")
        if last_known is None and prev.get("state_status") in (P.HALF, P.FULL):
            last_known = prev["state_status"]
        eff = s["effective_status"]
        s["changed"] = (eff in (P.HALF, P.FULL) and last_known is not None
                        and eff != last_known)
        # Entries written before answer-changes were tracked only have the
        # page-edit stamp. Every state's answer moved on the last national
        # half-staff day, so an older stamp would claim "unchanged since
        # August" across a day the flag was down everywhere.
        legacy = prev.get("last_changed_at")
        if legacy and legacy_floor and legacy < legacy_floor:
            legacy = legacy_floor
        s["last_changed_at"] = (s["checked_at"] if s["changed"] else
                                prev.get("last_status_change_at") or legacy)
        s["authority"] = (s.get("state_order") or {}).get("authority")
        entry = dict(new_cache.get(code) or {})
        entry["last_known_status"] = eff if eff in (P.HALF, P.FULL) else last_known
        entry["last_status_change_at"] = s["last_changed_at"]
        new_cache[code] = entry


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
        # Parsed facts are discarded; memory of what was ANNOUNCED is not.
        # Dropping it made every deploy forget the last known answers (so
        # real changes could not be detected) and forget an announced
        # national order (so it would be pushed again).
        keep = {k: cache[k] for k in ("_federal_announced", "_federal_active")
                if k in cache}
        for code, e in cache.items():
            if not code.startswith("_") and isinstance(e, dict):
                keep[code] = {k: e[k] for k in ("last_known_status",
                                                "last_status_change_at")
                              if k in e}
        cache = keep
    session = requests.Session()

    d = today()
    fed, fed_check = federal_statutory(d), "ok (statutory)"
    if not fed:
        fed, ferr = federal_proclamation(session, cache)
        fed_check = f"failed: {ferr}" if ferr else "ok"
        if ferr:
            print(f"  federal: could not determine ({ferr})")
        if not fed:
            fed = carry_federal(cache.get("_federal_active"), d)

    targets = [r for r in registry
               if not args.state or r["state_code"] == args.state.upper()]

    results, new_cache = {}, dict(cache)
    new_cache["_parser_version"] = PARSER_VERSION
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(check_state, r, cache, requests.Session(),
                          bool(args.state)): r
                for r in targets}
        for f in cf.as_completed(futs):
            try:
                code, out, cent = f.result()
            except Exception as e:
                # Never drop a state from the output: the page builds its
                # dropdown from status.json, so a missing state also erased
                # the saved choice of everyone who had picked it.
                rec = futs[f]
                print(f"  ERROR {rec['state_code']} {type(e).__name__}: {e}")
                code = rec["state_code"]
                out = {"state": rec["state"], "state_code": code,
                       "coverage": "stale", "ingest_mode": rec.get("ingest_mode"),
                       "confidence": rec.get("confidence"),
                       "state_status": P.UNKNOWN, "state_order": None,
                       "source_url": pick_url(rec),
                       "checked_at": datetime.now(timezone.utc).isoformat(
                           timespec="seconds"),
                       "last_changed_at": None, "content_changed": False,
                       "error": f"pipeline error: {type(e).__name__}"}
                cent = None
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

    # Has this exact national order already been announced?
    #
    # This lived in the notify step and wrote federal-last.json — but notify
    # runs AFTER the commit step, so that file was never committed. Every run
    # started from a repo without it, concluded the order was new, and pushed
    # again. Every 30 minutes, all night.
    #
    # It belongs here: run.py writes cache.json BEFORE the commit, so the
    # memory actually survives to the next run.
    fed_key = (fed or {}).get("reason")
    already = cache.get("_federal_announced")
    fed_is_new = bool(fed_key) and fed_key != already
    if fed or not fed_check.startswith("failed"):
        new_cache["_federal_announced"] = fed_key  # None clears it when it ends
    # else: we could not look. Forgetting the announcement here meant the
    # next successful run re-announced a live order to every subscriber.
    new_cache["_federal_active"] = (
        {k: v for k, v in fed.items() if k != "carried_forward"} if fed else
        (cache.get("_federal_active") if fed_check.startswith("failed") else None))
    if fed:
        fed["_changed"] = fed_is_new

    nat = last_national_day(d)
    track_changes(results, cache, new_cache, legacy_floor=(
        (date.fromisoformat(nat) + timedelta(days=1)).isoformat() + "T00:00:00+00:00"
        if nat else None))

    vals = results.values()
    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": d.isoformat(),
        "federal": fed,
        # "No national order" and "could not check for one" must not look the
        # same. federal is null in both cases; this field says which.
        "federal_check": fed_check,
        "states": dict(sorted(results.items())),
        "meta": {
            "covered": sum(1 for s in vals if s["coverage"] == "covered"),
            # covered means "we have a source"; answered means "it told us
            # half or full today". AL, CO and KY were counted as covered for
            # weeks while answering nothing.
            "answered": sum(1 for s in vals if s["state_status"] in (P.HALF, P.FULL)),
            "not_covered": sum(1 for s in vals if s["coverage"] == "not_covered"),
            "stale": sum(1 for s in vals if s["coverage"] == "stale"),
            "frozen": sum(1 for s in vals if s["coverage"] == "frozen"),
            "errors": sum(1 for s in vals if s["error"]),
            "total": len(results),
        },
    }

    changed = [c for c, s in results.items() if s["changed"]]
    half = [c for c, s in results.items() if s["effective_status"] == P.HALF]
    m = status["meta"]

    print(f"\n{'='*54}")
    print(f"  {status['date']}   {m['covered']}/{m['total']} covered, "
          f"{m['answered']} answered")
    print(f"  federal check: {fed_check}")
    if fed:
        print(f"  FEDERAL: half-staff — {fed['reason']}"
              + (" (carried forward)" if fed.get("carried_forward") else ""))
    print(f"  half-staff: {' '.join(sorted(half)) or 'none'}")
    print(f"  CHANGED this run: {' '.join(sorted(changed)) or 'none'}")
    print(f"  not covered: {m['not_covered']}   stale: {m['stale']}   "
          f"frozen: {m['frozen']}   errors: {m['errors']}")
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
                r = results[c]
                # The window is recorded so test-history.py can check "never
                # shown half past the order's stated window" exactly, instead
                # of re-deriving it from the title.
                o = (r.get("federal") if r.get("reason_source") == "federal"
                     else r.get("state_order")) or {}
                f.write(json.dumps({
                    "at": status["generated_at"], "state": c,
                    "status": r["effective_status"],
                    "reason": r["reason"],
                    "reason_source": r.get("reason_source"),
                    "start_date": o.get("start_date"),
                    "end_date": o.get("end_date"),
                }) + "\n")
    print(f"\nWrote {OUTPUT} and {CACHE}")


if __name__ == "__main__":
    main()
