#!/usr/bin/env python3
"""
parsers.py — pure parsing logic for the flag status pipeline.

Every function here is a pure function of its input string. No network, no
disk, no clock. That is deliberate: it means the whole hard part of this
system is testable offline with fixtures, which is where parsing bugs
actually get caught.

FOUR INGEST MODES, from registry.json:

  feed    (13 states)  RSS/Atom. Dated items, trivial.
  archive  (9 states)  Dated HTML list of past orders.
  index   (18 states)  Press release index; scan headlines, follow links.
  diff     (9 states)  Current-status banner ONLY. No history exists. Hash
                       the status region and compare to last seen. This is
                       the group competitors silently miss.

TWO RULES THAT OVERRIDE EVERYTHING:

  1. NEVER GUESS. Every function returns UNKNOWN rather than a best guess.
     A wrong "full staff" is worse than an honest "we don't know" — the whole
     product is a trust claim.

  2. AUTHORITY MUST BE PROVEN. Alaska reposts PRESIDENTIAL proclamations into
     the same archive as GUBERNATORIAL orders. Attributing a federal order to
     a governor is the exact error this app exists to fix, so authority is
     parsed explicitly and defaults to unknown.
"""

import hashlib
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HALF = "half"
FULL = "full"
UNKNOWN = "unknown"

GOVERNOR = "governor"
PRESIDENT = "president"

FLAG_RE = re.compile(
    r"half[-\s]?(?:staff|mast)|flags?\s+(?:to|at|lowered|be\s+flown)"
    r"|lower\s+the\s+flag|flag\s+(?:order|honors|status|notification)",
    re.I,
)

# Explicit negations. These MUST be checked before HALF_SIGNALS, because the
# sentence "there is no current half-staff order" contains the literal string
# "half-staff" and would otherwise register as a half-staff signal. Negation
# beats keyword presence, always.
NEGATED_HALF = [
    r"\bno\s+(?:current|active|standing)?\s*half[-\s]?(?:staff|mast)\b",
    r"\bno\s+(?:current|active)\s+(?:flag\s+)?orders?\b",
    r"\bnot\s+(?:currently\s+)?(?:at|flying\s+at)\s+half[-\s]?(?:staff|mast)\b",
    r"\bhalf[-\s]?(?:staff|mast)\s+order\s+has\s+(?:expired|ended|concluded)\b",
    r"\bno\s+flags?\s+(?:are\s+)?(?:currently\s+)?lowered\b",
]

# ---------------------------------------------------------------------------
# Current-status pages (diff mode)
#
# These pages state today's status AND explain flag protocol in general. Both
# kinds of text contain "half-staff", which is why a naive keyword match sees
# every one of them as ambiguous and gives up. The distinction that matters:
#
#   DECLARATION  "Flag Status: Full Staff"        <- a fact about today
#   DESCRIPTION  "the flag may be flown at half-  <- a rule, always on the page
#                 staff by presidential order"
#
# So: look for a declaration first and let it win outright. Only if none
# exists do we fall back, and then only after deleting the descriptive text.
# ---------------------------------------------------------------------------

# High-precision declaration patterns. Group 1 must be 'half' or 'full'.
DECLARATION_RE = [
    # ORDER MATTERS: most specific first, most generic LAST.
    #
    # These are tried in order and the first match wins. An earlier version
    # had the generic "flags at half-staff" phrase in position 1, so it fired
    # on Pennsylvania's nav label and on Michigan's protocol prose before
    # either state's own specific declaration was ever reached. Both states
    # reported half-staff while their pages plainly said full.
    #
    # --- Tier 1: an explicit labelled status field --------------------------
    # "Flag Status: Full Staff" (Alabama, Louisiana, Mississippi, Texas, FL)
    re.compile(r"flags?\s*status\s*[:\-–]\s*(half|full)[-\s]?(?:staff|mast)", re.I),
    # "Flag Status Full Staff" (Ohio — no separator at all)
    re.compile(r"flags?\s*status\s+(half|full)[-\s]?(?:staff|mast)\b", re.I),
    # "Status: FULL STAFF" (District of Columbia — no "flag" prefix)
    re.compile(r"\bstatus\s*[:\-–]\s*(half|full)[-\s]?(?:staff|mast)\b", re.I),
    # "Current status: half-staff"
    re.compile(r"current(?:ly)?\s+(?:flag\s+)?status\s*[:\-–]\s*(half|full)", re.I),
    # "National Flag: Half Staff  State Flag: Half Staff" (Virginia)
    re.compile(r"national\s+flag\s*[:\-–]\s*(half|full)\s*staff", re.I),
    # "United States Flag: Full-Staff" (Pennsylvania). County-scoped lines are
    # stripped before this runs, so this is the statewide value.
    re.compile(r"united\s+states\s+flags?\s*:\s*(half|full)[-\s]?staff", re.I),
    # "Michigan Flag Honor status notification including text, Full Staff"
    re.compile(r"status\s+notification[^.]{0,40}?,\s*(half|full)[-\s]?staff", re.I),
    # "USA Flag Status Flag at full staff" (Idaho), "USA Flag Status: Flag at
    # Half Staff" (Colorado). Without this, Idaho's label was skipped and a
    # protocol sentence further down ("...authority to order ... flags to be
    # flown at half-staff...") answered instead: HALF on a page saying FULL.
    re.compile(r"flags?\s+status\s*[:\-–]?\s*flags?\s+(?:is\s+|are\s+)?at\s+"
               r"(half|full)[-\s]?(?:staff|mast)\b", re.I),

    # --- Tier 2: a present-tense sentence about right now -------------------
    # "...the flag of the state of Utah are currently at Half Staff"
    re.compile(r"\b(?:is|are)\s+currently\s+(?:being\s+flown\s+)?"
               r"(?:at\s+)?(half|full)[-\s]?(?:staff|mast)\b", re.I),
    # "Flags are currently flying at half-staff"
    re.compile(r"\bflags?\s+(?:is|are)\s+(?:currently\s+)?(?:flying\s+|being\s+flown\s+)?"
               r"(?:at\s+)?(half|full)[-\s]?(?:staff|mast)\b", re.I),
    # "The flag is being flown at half-staff today"
    re.compile(r"\bflags?\s+(?:will\s+be\s+|are\s+being\s+)?(?:flown|displayed)\s+"
               r"at\s+(half|full)[-\s]?(?:staff|mast)\s+(?:today|now|until)", re.I),
    # "Governor Healey has ordered that ... be lowered to half-staff at all
    # state buildings from sunrise until sunset on Friday, August 14, 2026"
    # (Massachusetts). The order and the status are the same sentence there,
    # so the date gate afterwards is what expires it.
    re.compile(r"\bhas\s+ordered\s+that\b[^.]{0,200}?\bbe\s+"
               r"(?:lowered|flown|raised|displayed)\s+(?:to|at)\s+"
               r"(half|full)[-\s]?(?:staff|mast)", re.I),
    # "United States flag to be flown at half staff" (Alaska)
    re.compile(r"\bflags?\s+(?:is\s+|are\s+)?to\s+be\s+flown\s+at\s+"
               r"(half|full)[-\s]?(?:staff|mast)", re.I),

    # --- Tier 3: bare phrase. LAST, and context-guarded ---------------------
    # "Flags at Full-Staff" (Nevada) is a standalone label. The same words
    # also appear inside every flag-protocol explainer in the country, so a
    # match here is only accepted if it is not sitting in descriptive prose.
    re.compile(r"\bflags?\s+at\s+(half|full)[-\s]?(?:staff|mast)\b", re.I),
]

# Index of the first context-guarded pattern: the Alaska "to be flown at"
# phrase and the bare "flags at" phrase.
GUARDED_FROM = 12

# If any of these appear just before a guarded match, the sentence is
# describing the rules rather than stating today's status.
PROSE_BEFORE = re.compile(
    r"\b(?:may|should|shall|when|whenever|if|authorized|authority|proclaim|code|"
    r"event|death|order(?:s|ed)?\s+that|policy|protocol|means|lower(?:ed|ing)?|"
    r"raise[sd]?|display(?:ed|s)?|fly|flown|flying|honou?r|memory|respect|"
    r"newsroom|archive|notices?|history|past|previous)\b", re.I)

# Pennsylvania reports statewide AND county status on one page:
#   "United States Flag: Full-Staff"
#   "Allegheny County Only United States Flags: Half-Staff"
# We report the statewide value and surface counties as a note. The data model
# is state-level; pretending to county precision we cannot maintain for all 51
# jurisdictions would be false precision.
# Matches a whole county-scoped clause so it can be deleted from the text
# before statewide classification.
# The (?<![-\w]) lookbehind matters more than it looks. Under re.I, [A-Z][a-z]+
# matches ANY word, so without it the pattern happily started at "Staff" inside
# "Full-Staff Allegheny County Only ..." and deleted the statewide line along
# with the county line, turning a clean FULL into UNKNOWN.
# "Only" is optional: Pennsylvania's page changed from "Allegheny County
# Only United States Flags: Half-Staff" to "Erie County: Half-Staff", and the
# Erie County order of Sept 23 2026 matched nothing - we showed plain "full"
# to fire stations in Erie County. A colon is still required, so narrative
# text ("...in Erie County to fly at half-staff") is not read as a line.
COUNTY_LINE_RE = re.compile(
    r"(?<![-\w])[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+count(?:y|ies)(?:\s+only)?"
    r"[^:.]{0,60}:\s*(?:half|full)[-\s]?staff", re.I)

# Same lookbehind as COUNTY_LINE_RE, and for a second reason: without it the
# pattern can start at every position inside a long word, and [a-z]+ backtracks
# across the whole run each time. 50,000 letters with no spaces took forever,
# which hung test-parsers.py and could hang a real run on minified page text.
COUNTY_SCOPED_RE = re.compile(
    r"(?<![-\w])([A-Z][a-z]{1,30}(?:\s+[A-Z][a-z]{1,30})?)\s+count(?:y|ies)(?:\s+only)?"
    r"[^:.]{0,60}:\s*(half|full)[-\s]?staff", re.I)

# Alaska advertises an explicit window: "From: Sunrise Sunday, July 12, 2026
# Until: Sunset Saturday, July 18, 2026". A status page can keep displaying an
# order that has already expired — Alaska was still showing the July 12-18
# Lindsey Graham proclamation in mid-August. Status words alone are not
# sufficient; the dates must be checked.
FROM_RE = re.compile(r"\bfrom\s*:?\s*(?:sunrise|sunset|noon)?\s*"
                     r"(?:[A-Z][a-z]+day,?\s*)?([^\n]{0,34})", re.I)
UNTIL_RE = re.compile(r"\b(?:until|through|thru)\s*:?\s*"
                      r"(?:sunset|sunrise|noon|\d{1,2}:\d{2}\s*[ap]\.?m\.?\s*on)?\s*"
                      r"(?:[A-Z][a-z]+day,?\s*)?([^\n]{0,34})", re.I)


def listed_order_windows(html):
    """[(start, end), ...] for every half-staff directive on a page that
    states when it ends.

    A status widget can lag the order it announces. Florida's page still read
    "Flag Status: Half Staff" on Sept 15 2026 directly above the only order it
    listed: "...at half-staff ... from sunrise to sunset on Friday, September
    11, 2026". The widget and the order are two statements; when they
    disagree, neither can be reported as the answer.
    """
    text = strip_html(html) if "<" in (html or "") else (html or "")
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(sentence) > 600 or not re.search(r"half[-\s]?(?:staff|mast)",
                                                sentence, re.I):
            continue
        if BOILERPLATE_RE.search(sentence):
            continue
        s, e = order_window(sentence)
        if e:
            out.append((s, e))
    return out


ORDER_MENTION_RE = re.compile(
    r"half[-\s]?(?:staff|mast)|flags?\s+(?:lowered|to\s+be\s+lowered)", re.I)
# A date right after one of these belongs to the page, not to an order.
CHROME_DATE_BEFORE_RE = re.compile(
    r"(?:updated|modified|posted|published|reviewed|copyright|©)\W{0,4}$", re.I)
ORDER_DATE_REACH = 160


def order_dates(html):
    """Dates written next to half-staff language on a page.

    A status page that says half-staff should show the order behind it.
    Delaware, Texas and Pennsylvania said half-staff for days after Patriot
    Day ended - and none of their pages carries a single dated order, so
    nothing on them could ever show the claim was stale. These are the dates
    of the orders a page shows. Protocol boilerplate is skipped, and so are
    page-chrome dates ("Last updated ..."), which would make a frozen widget
    look fresh.
    """
    text = strip_html(html) if "<" in (html or "") else (html or "")
    out = set()
    for m in ORDER_MENTION_RE.finditer(text):
        lo = max(0, m.start() - ORDER_DATE_REACH)
        seg = text[lo:m.end() + ORDER_DATE_REACH]
        if BOILERPLATE_RE.search(seg):
            continue
        for pat in DATE_PATTERNS:
            for dm in pat.finditer(seg):
                if CHROME_DATE_BEFORE_RE.search(seg[:dm.start()]):
                    continue
                d = parse_any_date(dm.group(0))
                if d:
                    out.add(d)
    return sorted(out)


def diff_page_dates(html):
    """(start, end) advertised on a current-status page, or (None, None)."""
    text = strip_html(html) if "<" in (html or "") else (html or "")
    m = FROM_RE.search(text)
    s = parse_any_date(m.group(1)) if m else None
    m = UNTIL_RE.search(text)
    e = parse_any_date(m.group(1)) if m else None
    return s, e


# --- Scope: statewide vs a single building ---------------------------------
# South Dakota titles orders two ways:
#   "Flags at Half-Staff at State Capitol in Honor of..."   <- capitol only
#   "Flags at Half-Staff in Honor of..."                    <- statewide
# A capitol-only order is NOT a statewide half-staff day. Reporting one boolean
# per state without this over-reports South Dakota constantly, and the same
# distinction almost certainly exists in other states' wording.
LIMITED_SCOPE_RE = re.compile(
    r"\bat\s+(?:the\s+)?state\s+capitol\b"
    r"|\bcapitol\s+(?:building\s+)?only\b"
    r"|\bat\s+the\s+capitol\s+complex\b"
    r"|\bonly\s+at\s+the\s+state\s+capitol\b"
    r"|\b(?:in|within)\s+[A-Z][a-z]+\s+County\s+only\b",
    re.I,
)


# An order is statewide only if it SAYS so. The old default was the other way
# round - statewide unless a known limiting phrase appeared - and Nebraska
# showed what that costs: "Governor Pillen ... has delegated authority to the
# mayor ... the Mayor of Yutan may direct that flags within the City of Yutan
# be lowered to half-staff" was published as Nebraska at half-staff. With 51
# governors writing however they please, a default of statewide guarantees
# over-reporting; the unrecognised phrasing is the common case, not the rare
# one.
STATEWIDE_SCOPE_RE = re.compile(
    r"\bstate[-\s]?wide\b"
    r"|\b(?:throughout|across|within)\s+the\s+(?:entire\s+)?(?:state|commonwealth)\b"
    r"|\bin\s+the\s+(?:entire\s+)?(?:state|commonwealth)\b"
    r"|\ball\s+(?:state[-\s]\w+\s+|state\s+|government\s+|public\s+)?"
    r"(?:buildings|facilities|offices|grounds|properties|institutions|agencies"
    r"|departments)\b"
    r"|\bat\s+(?:all\s+)?state\s+(?:facilities|buildings|offices|properties)\b"
    r"|\bevery\s+state\s+(?:building|facility|office)\b",
    re.I)
# "the Mayor of Yutan may direct that flags within the City of Yutan..."
LIMITED_SCOPE_EXTRA_RE = re.compile(
    r"\b(?:in|within|throughout)\s+the\s+(?:city|town|village|borough|county|"
    r"township)\s+of\s+[A-Z]"
    r"|\bdelegated\s+authority\s+to\s+the\s+(?:mayor|chair|board)\b"
    r"|\b(?:city|town|village|county)\s+of\s+[A-Z][a-z]+\s+(?:only|alone)\b",
    re.I)
# How close a scope marker must sit to the flag phrase to be about the flags.
SCOPE_NEAR_CHARS = 80
FLAG_SENTENCE_RE = re.compile(
    r"half[-\s]?(?:staff|mast)|flags?\s+(?:be\s+)?(?:lowered|flown|displayed)"
    r"|lower(?:s|ed|ing)?\s+(?:the\s+)?flags?", re.I)


def _letters(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def state_name_scope_re(name):
    """The state's own name where it is used as the reach of the order:
    "flags in Connecticut", "Hawai'i state flags", "the State of Hawai'i".
    Letters are matched with punctuation between them so Hawai'i matches.
    Proximity to the flag phrase is what keeps a page header out - every one
    of these sites carries its state's name in the chrome."""
    letters = _letters(name)
    if len(letters) < 4:
        return None
    n = r"[^A-Za-z]{0,2}".join(letters)
    return re.compile(
        rf"(?:state|commonwealth)\s+of\s+{n}"
        rf"|{n}\s+(?:state\s+)?flags?\b"
        rf"|flags?\s+(?:in|throughout|across)\s+{n}\b"
        rf"|(?:in|throughout|across)\s+{n}\b",
        re.I)


def order_scope(text, state_name=None):
    """('statewide' | 'limited' | 'unknown', evidence).

    Only sentences that are ABOUT the flags count. Iowa's Gaesser order calls
    the man "a respected leader statewide" while the flag sentence says "on
    all public buildings, grounds, and facilities throughout the state" - the
    first is biography, the second is scope, and a bare keyword search cannot
    tell them apart.

    A statewide marker beats a limiting one, because a statewide order often
    names the Capitol too ("on the State Capitol Building ... and on all
    public buildings throughout the state"). Nothing either way is UNKNOWN,
    and callers must not report half-staff on unknown.
    """
    t = strip_html(text) if "<" in (text or "") else (text or "")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", t) if FLAG_SENTENCE_RE.search(s)]
    if not sentences:
        return "unknown", "no sentence about the flags"
    name_re = state_name_scope_re(state_name) if state_name else None
    for s in sentences:
        # The marker has to sit next to the flag phrase. Iowa's order calls
        # Ray Gaesser "a respected leader statewide" in the same sentence as
        # "flags flown at half-staff": same sentence, 120 characters apart,
        # and not a statement about the flags at all.
        spans = [m.span() for m in FLAG_SENTENCE_RE.finditer(s)]
        for pat in (p for p in (STATEWIDE_SCOPE_RE, name_re) if p):
            for m in pat.finditer(s):
                if any(min(abs(m.start() - b), abs(a - m.end())) <= SCOPE_NEAR_CHARS
                       for a, b in spans):
                    return "statewide", m.group(0).strip()[:90]
    for s in sentences:
        m = LIMITED_SCOPE_RE.search(s) or LIMITED_SCOPE_EXTRA_RE.search(s)
        if m:
            return "limited", m.group(0).strip()
    return "unknown", f"no scope stated in: {sentences[0].strip()[:90]}"


# --- Freshness --------------------------------------------------------------
# Arizona's half-staff page fetches cleanly, is server-rendered, and has been
# frozen since January 2025 — it still announces the Jimmy Carter order. Wired
# up naively it would report half-staff every day forever. A page that cannot
# be observed changing is not a status source, and staleness is detectable:
# Idaho publishes DCTERMS.modified and a visible "last updated" line.
LASTMOD_PATTERNS = [
    re.compile(r'name=["\']DCTERMS\.modified["\'][^>]*content=["\']([^"\']+)', re.I),
    re.compile(r'property=["\']article:modified_time["\'][^>]*content=["\']([^"\']+)', re.I),
    re.compile(r'name=["\']last-modified["\'][^>]*content=["\']([^"\']+)', re.I),
    re.compile(r"last\s+updated\s*:?\s*([A-Z][a-z]+\s+\d{1,2},?\s+20\d\d)", re.I),
    re.compile(r"updated\s+on\s+([A-Z][a-z]+\s+\d{1,2},?\s+20\d\d)", re.I),
]


FUTURE_DATE_HORIZON_DAYS = 60

# "Aug. 15" / "July 29" — month and day with no year. Built on first use
# because MONTH_RE is defined further down the module.
_YEARLESS = None


def yearless_re():
    global _YEARLESS
    if _YEARLESS is None:
        _YEARLESS = re.compile(
            r"\b(" + MONTH_RE + r")\.?\s+(\d{1,2})\b(?!\s*,?\s*20\d\d)", re.I)
    return _YEARLESS


def page_last_modified(html, headers=None):
    """Best available freshness signal for a page, or None."""
    today = date.today()
    # Future dates, two kinds. "Dolly Parton Day on September 25, 2026" in a
    # news item ten days ahead of it is evidence the page was written about
    # now (Ohio's flag page). "The fiscal year ends September 30, 2027", or
    # 2099, is not: a page frozen for years would look current until then.
    # Near-future dates count as "today"; far-future ones are ignored.
    horizon = today + timedelta(days=FUTURE_DATE_HORIZON_DAYS)

    def usable(d):
        return min(d, today) if d and d <= horizon else None

    if headers:
        for k in ("Last-Modified", "last-modified"):
            if headers.get(k):
                d = usable(parse_any_date(headers[k]))
                if d:
                    return d
    for pat in LASTMOD_PATTERNS:
        m = pat.search(html or "")
        if m:
            d = usable(parse_any_date(m.group(1)))
            if d:
                return d
    # Fall back to the newest date mentioned anywhere in the visible text.
    text = strip_html(html)
    best = None
    for pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            d = usable(parse_any_date(m.group(0)))
            if d and (best is None or d > best):
                best = d
    if best:
        return best

    # Last resort: dates written without a year, e.g. Arizona's "Aug. 15".
    # Only used for FRESHNESS, never for an order window — guessing a year on
    # an order's end date could keep a state at half-staff for twelve months.
    # Here the worst case is a page looking fresher or staler than it is, and
    # having no signal at all is worse than an inferred one.
    for m in yearless_re().finditer(text):
        try:
            mo = _month_num(m.group(1))
            day = int(m.group(2))
        except (ValueError, TypeError):
            continue
        for yr in (today.year, today.year - 1):
            try:
                d = date(yr, mo, day)
            except ValueError:
                continue
            # A date more than a month ahead is last year's, not next year's.
            if (d - today).days > 31:
                continue
            if best is None or d > best:
                best = d
            break
    return best


# A container whose class marks it as the flag-status widget. Oklahoma's
# homepage carries two links to its flag pages: a live badge inside
# <div class="text flag-status">, and a stale nav link in a list of
# miscellaneous items whose own title attribute contradicts its text. Only the
# first is a status; scoping to the container is what separates them.
FLAG_WIDGET_RE = re.compile(
    r"<([a-z]+)[^>]*class=[\"'][^\"']*flag[-_]status[^\"']*[\"'][^>]*>(.{0,3000}?)</\1>",
    re.S | re.I)


def _status_votes(fragment):
    """Every independent half/full signal inside a fragment."""
    votes = []
    for m in re.finditer(r"href=[\"'][^\"']*flag[-_]?status[-_](half|full)",
                         fragment, re.I):
        votes.append(("href", m.group(1).lower()))
    for m in re.finditer(r"flags?\s*status\s*[:\-–]?\s*(half|full)[-\s]?staff",
                         strip_html(fragment), re.I):
        votes.append(("text", m.group(1).lower()))
    for m in re.finditer(r"(half|full)[-_]staff[^\"']*\.(?:png|jpg|svg|gif)",
                         fragment, re.I):
        votes.append(("image", m.group(1).lower()))
    return votes


def parse_toggle(html, base_url=None):
    """Two-static-page states (Oklahoma).

    The site serves flag-status-half.html and flag-status-full.html. Both
    always exist and each always says its own name, so reading either one
    directly returns a constant. The status is encoded in which one the live
    badge points at.

    Returns (status, evidence).
    """
    text = html or ""

    # Preferred: the badge container. Inside it, href, link text and image
    # filename are three independent statements of the same fact — if they
    # disagree, the widget is broken and we say so rather than pick one.
    # If BOTH variants appear anywhere in the source, the page is shipping
    # both and choosing one at render time. Oklahoma does exactly this: a
    # widget whose href, text and image all said "half" while the site
    # displayed full staff. Agreement inside one fragment proves nothing when
    # its twin is sitting in the same document.
    all_half = re.search(r"flag[-_]?status[-_]half", text, re.I)
    all_full = re.search(r"flag[-_]?status[-_]full", text, re.I)
    if all_half and all_full:
        return UNKNOWN, ("both half and full variants present in the source; "
                         "the visible one is chosen client-side")

    for m in FLAG_WIDGET_RE.finditer(text):
        votes = _status_votes(m.group(0))
        if not votes:
            continue
        vals = {v for _, v in votes}
        if len(vals) == 1:
            val = vals.pop()
            how = "+".join(sorted({k for k, _ in votes}))
            return (HALF if val == "half" else FULL,
                    f"flag-status widget ({how} all say {val})")
        return UNKNOWN, f"flag-status widget disagrees with itself: {votes}"

    # Fallback: no widget container. Only usable if exactly one of the two
    # pages is linked anywhere.
    hrefs = re.findall(r"href=[\"']([^\"']*flag[-_]?status[^\"']*)", text, re.I)
    half = [h for h in hrefs if re.search(r"[-_]half", h, re.I)]
    full = [h for h in hrefs if re.search(r"[-_]full", h, re.I)]
    if half and not full:
        return HALF, f"only half page linked: {half[0]}"
    if full and not half:
        return FULL, f"only full page linked: {full[0]}"
    if half and full:
        return UNKNOWN, "both half and full pages linked, and no flag-status widget"
    return UNKNOWN, "no flag-status link found on this page"


def county_exceptions(html):
    """Sub-state scoped statuses, e.g. Pennsylvania's per-county lines."""
    text = strip_html(html) if "<" in (html or "") else (html or "")
    out = []
    for m in COUNTY_SCOPED_RE.finditer(text):
        # "...Half-Staff Allegheny County Only..." - the preceding line's
        # trailing word gets captured, so trim known non-county words.
        name = re.sub(r"^(?:Staff|Flag|Flags|Only)\s+", "", m.group(1).strip(), flags=re.I)
        entry = {"county": name,
                 "status": HALF if m.group(2).lower() == "half" else FULL}
        if entry not in out:
            out.append(entry)
    return out

# Sentences containing any of these are RULES, not statements about today.
# Alabama's and Kentucky's pages are mostly this.
BOILERPLATE_RE = re.compile(
    r"\bmay\s+(?:be\s+(?:flown|displayed|lowered)|order|proclaim)\b"
    r"|\bshould\s+(?:be|first|again)\b"
    r"|\bshall\s+be\s+(?:displayed|flown)\b"
    r"|\bwhen\s+flown\s+at\b"
    r"|\bupon\s+the\s+death\s+of\b"
    r"|\bin\s+the\s+event\s+of\b"
    r"|\baccording\s+to\s+the\s+u\.?s\.?\s+(?:flag\s+)?code\b"
    r"|\bflag\s+code\s+authorizes\b"
    r"|\bhas\s+authority\s+(?:over|to)\b"
    r"|\bit\s+is\s+proper\s+flag\s+protocol\b"
    r"|\bhalf[-\s]?staff\s+means\b"
    r"|\bby\s+order\s+of\s+the\s+president,\s+the\s+u\.?s\.?\s+flag\s+should\b",
    re.I,
)


def strip_boilerplate(text):
    """Drop sentences that state flag RULES rather than today's status."""
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return " ".join(p for p in parts if not BOILERPLATE_RE.search(p))


def classify_current_status(html):
    """Status of a current-status (diff-mode) page. Returns (status, evidence).

    Declaration beats everything. If the page plainly says what the status is,
    that is the answer regardless of how much protocol text surrounds it.
    """
    text = strip_html(html) if "<" in (html or "") else (html or "")
    if not text:
        return UNKNOWN, None

    # Remove county-scoped lines BEFORE looking for the statewide answer.
    # Pennsylvania publishes:
    #     United States Flag: Full-Staff
    #     Allegheny County Only United States Flags: Half-Staff
    # Both match the same declaration pattern, and whichever the regex reaches
    # first wins — so a single county at half-staff was being reported as the
    # whole state. The county data is still captured by county_exceptions();
    # it just must not answer the statewide question.
    text = COUNTY_LINE_RE.sub(" ", text)

    for idx, pat in enumerate(DECLARATION_RE):
        for m in pat.finditer(text):
            if idx >= GUARDED_FROM:
                # Tier 3 is a bare phrase. Reject it if the preceding words
                # show it is describing flag rules or labelling an archive
                # rather than stating the current status.
                before = text[max(0, m.start() - 70):m.start()]
                if PROSE_BEFORE.search(before):
                    continue
            word = m.group(1).lower()
            return (HALF if word == "half" else FULL,
                    f"declaration: {m.group(0).strip()!r}")

    # NO FALLBACK, deliberately.
    #
    # An earlier version stripped the protocol boilerplate and ran the general
    # classifier on what was left. On Kentucky that turned a correct UNKNOWN
    # into a confident HALF — a false positive, which is strictly worse. These
    # pages are largely *made* of flag-protocol language; any residue-based
    # inference is a coin flip dressed up as an answer.
    #
    # Kentucky renders its status with JavaScript, so the fact simply is not
    # in the HTML. The right answer is to say we don't know.
    #
    # To cover a new state, add its phrasing to DECLARATION_RE after seeing
    # the real page text. Widening the guess is not an acceptable substitute.
    return UNKNOWN, "no explicit status declaration in page text"


# Phrases that indicate the flag is DOWN. Ordered most to least specific.
HALF_SIGNALS = [
    r"\bat\s+half[-\s]?(?:staff|mast)\b",
    r"\bto\s+half[-\s]?(?:staff|mast)\b",
    r"\bhalf[-\s]?(?:staff|mast)\b",
    r"\bflags?\s+(?:are|is|will\s+be|shall\s+be)\s+lowered\b",
    r"\blower(?:ed|ing)?\s+the\s+flags?\b",
    # "Gov. Whitmer Lowers Flags to Honor Detroit Fire Fighter Patrick Trout"
    # is a real half-staff headline that never uses the words "half-staff".
    # Without this the order is read as flag-related but statusless, and
    # dropped — which is exactly how Michigan was missed on a live order.
    r"\blower(?:s|ed|ing)?\s+(?:the\s+)?flags?\b",
    r"\bflags?\s+lowered\b",
]

# Phrases that indicate the flag is UP.
FULL_SIGNALS = [
    r"\bat\s+full[-\s]?staff\b",
    r"\bfull[-\s]?staff\b",
    r"\breturn(?:ed)?\s+to\s+(?:full|the\s+top)\b",
    r"\braised?\s+to\s+(?:full|the\s+top|the\s+peak)\b",
    r"\bno\s+(?:current|active)\s+(?:half[-\s]?staff\s+)?order",
    r"\bflags?\s+(?:are|is)\s+(?:currently\s+)?(?:flying\s+)?full\b",
]

# Authority: who signed it. Presidential language is quite distinctive.
PRESIDENT_SIGNALS = [
    r"\bthe\s+president\s+of\s+the\s+united\s+states\b",
    r"\bpresidential\s+proclamation\b",
    r"\bby\s+order\s+of\s+the\s+president\b",
    r"\bpresident\s+(?:has\s+)?(?:ordered|issued|proclaimed|directed)\b",
    r"\bwhite\s+house\b",
    r"\ball\s+federal\s+(?:buildings|installations)\b",
    # Boilerplate unique to presidential proclamations. Alaska reposts these
    # verbatim onto its state flag page, so this is the line that keeps a
    # federal order from being reported as a gubernatorial one.
    r"\bauthority\s+vested\s+in\s+me\s+by\s+the\s+constitution\s+and\s+the\s+"
    r"laws\s+of\s+the\s+united\s+states\b",
    r"\bat\s+the\s+white\s+house\s+and\s+upon\s+all\s+public\s+buildings\b",
    r"\bunited\s+states\s+embassies,?\s+legations\b",
]
GOVERNOR_SIGNALS = [
    # Real headlines use present tense and multi-word names:
    #   "Governor Ned Lamont Directs Flags Lowered..."
    #   "Gov. Cox orders flags lowered to half-staff"
    # The original pattern required past tense and exactly one name word, so
    # it matched almost no actual press release.
    r"\bgov(?:ernor)?\.?\s+(?:\w+[.'-]?\s+){1,3}"
    r"(?:has\s+|is\s+)?(?:orders?|ordered|directs?|directed|announces?|"
    r"announced|signs?|signed|lowers?|lowered|issues?|issued|proclaims?|"
    r"proclaimed)\b",
    r"\bgovernor'?s?\s+flag\s+order\b",
    r"\bby\s+order\s+of\s+(?:the\s+)?governor\b",
    r"\bgovernor'?s?\s+(?:proclamation|order|executive\s+order)\b",
    r"\bexecutive\s+order\s+(?:no\.?\s*)?[\d-]+\b",
    r"\bthe\s+mayor\s+(?:has\s+)?(?:ordered|directed|issued)\b",  # DC
]

# Rhode Island interleaves translated copies of every release.
TRANSLATION_PREFIX = re.compile(
    r"^\s*(?:SPANISH|PORTUGUESE|SPANISH\s+TRANSLATION|TRADUCCI[OÓ]N)\s*[:\-]",
    re.I,
)

# New York puts the fact in the URL: a listing can be classified without
# opening a single article, which matters because the site sits behind a
# Cloudflare challenge and every extra request is a chance to be blocked.
# Verified against /news/governor-hochul-directs-flags-half-staff-honor-
# retired-sergeant-michael-l-piro (Jan 10 2026).
FLAG_SLUG_RE = re.compile(
    r"half[-_](?:staff|mast)|flags?[-_](?:lowered|to[-_]half)", re.I)

# Roughly half of New York's listing is the Spanish edition of the same
# release, slugged "la-gobernadora-hochul-...". The English and Spanish copies
# have different titles AND different URLs, so nothing else would collapse
# them and every New York order would be counted twice.
TRANSLATED_SLUG_RE = re.compile(
    r"/(?:es|espanol)/|(?:^|/|-)(?:la[-_])?gobernador(?:a)?[-_]"
    r"|media[-_]asta|banderas[-_]", re.I)


def slug_of(url):
    try:
        return urlparse(url or "").path
    except ValueError:
        return url or ""


def is_flag_slug(url):
    """Does the URL itself say this release is a flag order?"""
    return bool(FLAG_SLUG_RE.search(slug_of(url)))

MONTHS = ("january february march april may june july august september "
          "october november december").split()
MONTH_RE = "|".join(MONTHS) + "|" + "|".join(m[:3] for m in MONTHS)

DATE_PATTERNS = [
    re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})(?!\d)"),                   # ISO (may be followed by T10:00:00Z)
    re.compile(rf"\b({MONTH_RE})\.?\s+(\d{{1,2}}),?\s+(20\d\d)\b", re.I),   # Aug 3, 2026
    re.compile(rf"\b(\d{{1,2}})\s+({MONTH_RE})\.?\s+(20\d\d)\b", re.I),     # 3 Aug 2026
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b"),                        # 8/3/2026
]

ITEM_RE = re.compile(r"<(?:item|entry)\b.*?</(?:item|entry)>", re.S | re.I)
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
LINK_TAG_RE = re.compile(
    r"<link[^>]*?(?:href=[\"'](.*?)[\"']|>(.*?)</link>)", re.S | re.I
)
DATE_TAG_RE = re.compile(
    r"<(?:pubDate|published|updated|dc:date)[^>]*>(.*?)</", re.S | re.I
)
BLOCK_RE = re.compile(
    r"<(li|article|tr|h[1-4]|a)\b[^>]*>(.*?)</\1>", re.S | re.I
)
# Headlines only. `div` is deliberately absent from BOTH patterns: finditer
# resumes after the end of a match, so matching an outer <div> consumes every
# headline nested inside it and the parser silently returns nothing. Match
# leaf-ish elements and let them be found individually.
HEADLINE_RE = re.compile(r"<(a|h[1-4])\b[^>]*>(.*?)</\1>", re.S | re.I)
HREF_RE = re.compile(r"href=[\"'](.*?)[\"']", re.I)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def strip_html(s):
    """Remove scripts, tags, and entities. Whitespace-normalized."""
    s = SCRIPT_RE.sub(" ", s or "")
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = TAG_RE.sub(" ", s)
    for ent, ch in (("&amp;", "&"), ("&nbsp;", " "), ("&#39;", "'"),
                    ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"),
                    ("&rsquo;", "'"), ("&ldquo;", '"'), ("&rdquo;", '"'),
                    ("&mdash;", "-"), ("&ndash;", "-")):
        s = s.replace(ent, ch)
    return re.sub(r"\s+", " ", s).strip()


def parse_any_date(text):
    """First parseable date in the text, or None. Never raises."""
    if not text:
        return None
    for pat in DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if pat is DATE_PATTERNS[0]:
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            elif pat is DATE_PATTERNS[1]:
                mo = _month_num(g[0])
                d, y = int(g[1]), int(g[2])
            elif pat is DATE_PATTERNS[2]:
                d = int(g[0])
                mo = _month_num(g[1])
                y = int(g[2])
            else:
                mo, d, y = int(g[0]), int(g[1]), int(g[2])
            return date(y, mo, d)
        except (ValueError, TypeError):
            continue
    # RFC-822, as used in RSS pubDate.
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(20\d\d)", text)
    if m:
        try:
            return date(int(m.group(3)), _month_num(m.group(2)), int(m.group(1)))
        except (ValueError, TypeError):
            pass
    return None


def _month_num(name):
    n = (name or "").lower().rstrip(".")
    for i, m in enumerate(MONTHS, start=1):
        if m.startswith(n[:3]):
            return i
    raise ValueError(name)


def is_translation(title, url=None):
    """A translated copy of another release, by title prefix (Rhode Island)
    or by slug (New York's Spanish edition)."""
    return bool(TRANSLATION_PREFIX.match(title or "")
                or (url and TRANSLATED_SLUG_RE.search(slug_of(url))))


def dedupe_orders(orders):
    """Drop translated duplicates and repeats of the same URL/title.
    Rhode Island and New York double every order without this."""
    seen_urls, seen_titles, out = set(), set(), []
    for o in orders:
        if is_translation(o.get("title"), o.get("url")):
            continue
        u, t = o.get("url"), (o.get("title") or "").lower()
        if u and u in seen_urls:
            continue
        if t and t in seen_titles:
            continue
        if u:
            seen_urls.add(u)
        if t:
            seen_titles.add(t)
        out.append(o)
    return out


# ---------------------------------------------------------------------------
# Status and authority classification
# ---------------------------------------------------------------------------

def classify_status(text):
    """Is the flag half or full? Returns (status, evidence).

    Conservative by construction: ambiguous or contradictory input returns
    UNKNOWN. We would rather tell the user we don't know than be wrong.
    """
    t = strip_html(text) if "<" in (text or "") else (text or "")
    if not t:
        return UNKNOWN, None

    # Negation first. "no current half-staff order" means FULL, even though
    # it contains the substring "half-staff".
    neg = next((m.group(0) for p in NEGATED_HALF
                for m in [re.search(p, t, re.I)] if m), None)
    if neg:
        return FULL, f"negation: {neg!r}"

    half = [m.group(0) for p in HALF_SIGNALS
            for m in [re.search(p, t, re.I)] if m]
    full = [m.group(0) for p in FULL_SIGNALS
            for m in [re.search(p, t, re.I)] if m]

    # "lowered to half-staff, then returned to full staff at noon" contains
    # both. So does a status page listing a past order above a current state.
    # Both signals present = we cannot tell. Say so.
    if half and full:
        return UNKNOWN, f"ambiguous: {half[0]!r} and {full[0]!r} both present"
    if half:
        return HALF, half[0]
    if full:
        return FULL, full[0]
    return UNKNOWN, None


def classify_authority(text):
    """Who ordered it? Returns (authority, evidence).

    Alaska reposts presidential proclamations alongside gubernatorial orders.
    Getting this wrong means showing a federal order as a state one — the
    precise failure this product exists to prevent. Defaults to UNKNOWN.
    """
    t = strip_html(text) if "<" in (text or "") else (text or "")
    if not t:
        return UNKNOWN, None

    pres = next((m.group(0) for p in PRESIDENT_SIGNALS
                 for m in [re.search(p, t, re.I)] if m), None)
    gov = next((m.group(0) for p in GOVERNOR_SIGNALS
                for m in [re.search(p, t, re.I)] if m), None)

    if pres and gov:
        # Common and legitimate: "Governor X directs flags lowered in
        # accordance with the President's proclamation." The governor acted,
        # so it IS a state order — but only when the governor verb is present.
        return GOVERNOR, f"governor acted, referencing federal: {gov!r}"
    if pres:
        return PRESIDENT, pres
    if gov:
        return GOVERNOR, gov
    return UNKNOWN, None


TIME_OF_DAY_RE = re.compile(
    r"(?:sunrise|sunset|dawn|dusk|daybreak|noon|\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)", re.I)


def date_range(text):
    """Best-effort (start, end) for an order. Either may be None."""
    t = strip_html(text) if "<" in (text or "") else (text or "")
    m = re.search(
        rf"(?:from|beginning|effective)\s+(.{{0,60}}?)\s+(?:through|until|to)\s+"
        rf"(.{{0,60}}?)(?:[.;]|$)", t, re.I)
    if m:
        s, e = parse_any_date(m.group(1)), parse_any_date(m.group(2))
        # "From sunrise until sunset on Sunday, October 4, 2026" is ONE day.
        # Returned as (None, Oct 4), the start was filled in by callers with
        # the publication date: South Dakota announced that order on Sept 14,
        # which would have read as three weeks of half-staff.
        if e and not s and TIME_OF_DAY_RE.fullmatch(m.group(1).strip()):
            s = e
        if s or e:
            return s, e
    m = re.search(r"(?:until|through)\s+(?:sunset\s+(?:on\s+)?)?(.{0,40})", t, re.I)
    if m:
        e = parse_any_date(m.group(1))
        if e:
            return parse_any_date(t), e
    single = parse_any_date(t)
    return single, None


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")
WEEKDAY_RE = re.compile(r"\b(" + "|".join(WEEKDAYS) + r")\b", re.I)
WEEKDAY_MAX_LEAD_DAYS = 5
UNTIL_BEFORE_RE = re.compile(
    r"\b(?:until|through|thru)\s+(?:(?:sunset|sunrise|noon)\s+)?(?:on\s+)?$", re.I)


def weekday_window(text, ref):
    """(start, end) for an order that names only weekdays, or (None, None).

    "Armstrong directs flags flown at half-staff Friday" carries no date, so
    the order was dated by its publication (Wednesday) and North Dakota showed
    half-staff two days before the order began. A weekday is resolved to its
    next occurrence on or after ref, the date the order was published.

    Only weekdays AFTER the half-staff phrase count: "On Monday, the governor
    ordered flags to half-staff on Friday" is an order for Friday, and the
    Monday is when it was announced. "...half-staff until sunset Sunday"
    starts at publication and ends Sunday.
    """
    if not ref:
        return None, None
    t = strip_html(text) if "<" in (text or "") else (text or "")
    hits = [m for p in HALF_SIGNALS for m in [re.search(p, t, re.I)] if m]
    if not hits:
        return None, None
    at = min(m.start() for m in hits)
    tail = t[at:at + 300]
    found = list(WEEKDAY_RE.finditer(tail))[:2]
    if not found:
        return None, None
    days, cur = [], ref
    for m in found:
        wd = WEEKDAYS.index(m.group(1).lower())
        cur = cur + timedelta(days=(wd - cur.weekday()) % 7)
        days.append(cur)
    # Orders are announced a few days ahead at most. A weekday 6 days out is
    # far more likely one that already passed ("...were flown at half-staff
    # Friday", published Saturday), and resolving it forward would invent an
    # order a week away.
    if (days[0] - ref).days > WEEKDAY_MAX_LEAD_DAYS:
        return None, None
    if len(days) == 1 and UNTIL_BEFORE_RE.search(tail[:found[0].start()]):
        return ref, days[0]
    return days[0], days[-1]


# ---------------------------------------------------------------------------
# The window an order states, read from the order's own sentence.
# ---------------------------------------------------------------------------
# date_range reads a whole page and takes the first date it finds - which on a
# press release is the release's own dateline. Maine's order for "Friday,
# September 11" started Sept 10, the day it was posted; Iowa's for Sept 18-20
# started Sept 17. And a one-day order ("on Friday", "today", "on September
# 11") came back as a start with no end, so the no-end hold kept it up for
# days. order_window reads the half-staff sentence and the one after it, and
# nothing else, and says so when it finds nothing: only then does a caller
# fall back to the release date.

YEARLESS_DATE_RE = re.compile(
    rf"\b({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!,?\s*20\d\d)", re.I)
# A date right after one of these is a death, a birth or an anniversary being
# remembered - not the day the flag comes down.
NOT_ORDER_DATE_RE = re.compile(
    r"(?:died|passed(?:\s+away)?|death|born|killed|attacks?\s+of|anniversary\s+of|"
    r"since)\s+(?:on\s+)?(?:\w+day,?\s+)?$", re.I)
OPEN_ENDED_RE = re.compile(
    r"until\s+(?:further\s+notice|(?:the\s+)?(?:day\s+of\s+)?(?:(?:his|her|their)\s+)?"
    r"(?:interment|burial|funeral))", re.I)
TODAY_RE = re.compile(r"\b(?:today|tonight)\b", re.I)
RANGE_RE = re.compile(
    r"\b(?:from|beginning|effective|starting)\s+(.{0,90}?)\s+"
    r"(?:through|until|thru|to)\s+(.{0,90})", re.I)
OW_UNTIL_RE = re.compile(r"\b(?:until|through|thru)\s+(.{0,70})", re.I)
ON_DAY_BEFORE_RE = re.compile(
    r"(?:\bon\s+(?:[a-z]+day,?\s+)?|\b[a-z]+day,?\s+)$", re.I)
HISTORIC_DAYS = 366
# The sentence that carries the order. Wider than HALF_SIGNALS, which decide
# half vs full and must stay strict: "flags be lowered from sunrise to sunset
# on Friday, September 11" is Maine's whole order and matches none of them.
ORDER_SENTENCE_RES = [re.compile(p, re.I) for p in HALF_SIGNALS] + [
    re.compile(r"\bflags?\b.{0,60}?\blowered\b", re.I),
    re.compile(r"\bdisplay(?:ed)?\b.{0,60}?\bat\s+half", re.I),
]


def _dates_in(text, ref=None):
    """[(offset, date)] for every date in text, in order. A date with no year
    takes ref's year. Dates far from ref (a 2001 attack, a 1941 raid) are
    history being remembered, not order dates, and are dropped."""
    out = []
    for pat in DATE_PATTERNS:
        for m in pat.finditer(text or ""):
            d = parse_any_date(m.group(0))
            if d:
                out.append((m.start(), d))
    if ref:
        for m in YEARLESS_DATE_RE.finditer(text or ""):
            try:
                d = date(ref.year, _month_num(m.group(1)), int(m.group(2)))
            except (ValueError, TypeError):
                continue
            if not any(abs(a - m.start()) < 3 for a, _ in out):
                out.append((m.start(), d))
        out = [(a, d) for a, d in out if abs((d - ref).days) <= HISTORIC_DAYS]
    else:
        out = [(a, d) for a, d in out if d.year >= 2020]
    return sorted(out)


def _order_dates(text, ref=None):
    """Dates in text that could be the day an order applies to."""
    return [(a, d) for a, d in _dates_in(text, ref)
            if not NOT_ORDER_DATE_RE.search((text or "")[max(0, a - 40):a])]


def release_date(text):
    """The release's own date: the first plausible date near the top."""
    for _, d in _dates_in((text or "")[:1500]):
        return d
    return None


def _window_in(span, published):
    # 1. "from X until/through/to Y" - every occurrence, not only the first:
    #    "from the Governor to all agencies" must not end the search.
    # Dates are found in the whole span, then assigned to the "from" and
    # "until" parts by position. Parsing the regex groups directly cut
    # "attacks of September 11, 2001" to "September 11, 20" at the group's
    # length limit - which then read as a yearless September 11 of THIS year.
    every = _dates_in(span, published)
    orderish = set(_order_dates(span, published))
    for m in RANGE_RE.finditer(span):
        a = [d for x, d in every if m.start(1) <= x < m.end(1) and (x, d) in orderish]
        b = [d for x, d in every if m.start(2) <= x < m.end(2)]
        s = a[0] if a else None
        e = b[0] if b else None
        # "from sunrise to sunset on Friday, September 11" is one day.
        if e and not s and TIME_OF_DAY_RE.fullmatch(m.group(1).strip()):
            return e, e
        if s and e and s <= e:
            return s, e
        if e and not s:
            return None, e
        if s and not e and TIME_OF_DAY_RE.match(m.group(2).strip()) \
                and not WEEKDAY_RE.search(m.group(2)[:40]):
            return s, s        # "from dawn on Sept 18 to dusk"
    # 2. "until/through <date>" with no "from"
    for m in OW_UNTIL_RE.finditer(span):
        b = [(x - m.start(1), d) for x, d in every if m.start(1) <= x < m.end(1)]
        if b and b[0][0] < 40:
            before = [d for x, d in sorted(orderish) if x < m.start()]
            return (before[-1] if before else None), b[0][1]
    # 3. "until further notice", "until the day of interment": open-ended
    if OPEN_ENDED_RE.search(span):
        before = _order_dates(span, published)
        return (before[0][1] if before else published), None
    # 4. explicit days: "on Friday, September 11, 2026". Only a date said to
    #    be a day - after "on" or a weekday. "...in honor of a trooper,
    #    September 21, 2026" is when it was ordered, not a one-day order, and
    #    reading it as one expired live orders on status pages.
    days = sorted({d for a, d in _order_dates(span, published)
                   if ON_DAY_BEFORE_RE.search(span[max(0, a - 30):a])})
    if days and (days[-1] - days[0]).days <= 7:
        return days[0], days[-1]
    # 5. weekdays only: "at half-staff on Friday", "until sunset Sunday"
    if published:
        s, e = weekday_window(span, published)
        if s:
            return s, e
    # 6. "today": the day it was issued
    if published and TODAY_RE.search(span):
        return published, published
    return None


def order_window(text, published=None):
    """(start, end) as stated by the order's own half-staff sentence.

    (None, None) when that sentence states no window - the only case where a
    caller should fall back to the release date. A one-day order returns
    start == end, so the hold for orders with no stated end never applies.

    published is the day the order was issued, used to resolve "Friday" and
    "today". When not given, the release's dateline is used for that and
    ONLY that - never as the start of the order.
    """
    t = strip_html(text) if "<" in (text or "") else (text or "")
    # The release's own dateline is when it was issued, never an order date.
    # It is blanked (offsets kept) so nothing below can read it as one: North
    # Dakota's "Wednesday, September 9, 2026 - 03:26 pm" sits right after a
    # headline with no full stop, and read as a one-day order for Wednesday
    # when the order was for Friday. It still resolves "Friday" and "today".
    dl = _dateline(t)
    if dl and (published is None or dl[0] == published):
        published = dl[0]
        a, b = dl[1]
        t = t[:a] + " " * (b - a) + t[b:]
    # Spans are bounded around each half-staff phrase rather than trusted to
    # sentence punctuation: navigation and headlines carry no full stops, so
    # Maine's order sentence arrived glued to 700+ characters of menu and was
    # skipped as too long.
    spans, seen = [], set()
    for h in sorted({m.start() for p in ORDER_SENTENCE_RES for m in p.finditer(t)}):
        lo = max(_boundary_before(t, h), h - SPAN_BEFORE)
        hi1 = min(_boundary_after(t, h), h + SPAN_AFTER)
        if (lo, hi1) in seen:
            continue
        seen.add((lo, hi1))
        hi2 = min(_boundary_after(t, hi1 + 1), hi1 + SPAN_AFTER)
        spans.append((lo, hi1, hi2))
    # Protocol prose ("flags shall be flown at half-staff upon the death of")
    # is read last, not skipped: a proclamation's own order sentence uses the
    # same words - "shall be flown at half-staff ... until sunset, September
    # 1" - and skipping it lost the Dolly Parton window.
    spans.sort(key=lambda s: bool(BOILERPLATE_RE.search(t[s[0]:s[1]])))
    for lo, hi1, hi2 in spans:
        for span in (t[lo:hi1], t[lo:hi2]):
            w = _window_in(span, published)
            if w:
                return w
    return None, None


SPAN_BEFORE, SPAN_AFTER = 300, 400
DATELINE_REACH = 6000
ORDER_PHRASE_BEFORE_RE = re.compile(
    r"\b(?:on|from|until|through|thru|to|beginning|effective|starting)\b[^.]{0,28}$", re.I)
_SENT_END_RE = re.compile(r"[.!?](?=\s|$)")


def _boundary_before(t, i):
    ends = [m.end() for m in _SENT_END_RE.finditer(t, max(0, i - 2000), i)]
    return ends[-1] if ends else 0


def _boundary_after(t, i):
    m = _SENT_END_RE.search(t, i)
    return m.end() if m else len(t)


def _dateline(t):
    """(date, (start, end)) of the release's own date near the top, with any
    weekday in front of it and any time after it, or None. "Near the top"
    is generous: North Dakota's dateline sits 3,367 characters in, behind
    the site's navigation, and whitehouse.gov's behind 3,000."""
    top = t[:DATELINE_REACH]
    found = [(m.start(), m.end(), parse_any_date(m.group(0)))
             for pat in DATE_PATTERNS for m in pat.finditer(top)]
    # A date inside an order phrase ("on Friday, September 11", "until
    # sunset, September 1") is the order's, even when it is the first date
    # there is - a bare order sentence has no dateline at all.
    found = sorted(f for f in found if f[2] and f[2].year >= 2020
                   and not ORDER_PHRASE_BEFORE_RE.search(top[max(0, f[0] - 40):f[0]]))
    if not found:
        return None
    a, b, d = found[0]
    wd = re.search(r"(?:[A-Z][a-z]+day,?\s*)$", t[:a])
    if wd:
        a = wd.start()
    tm = re.match(r"\s*[-–—|,]?\s*\d{1,2}:\d{2}\s*[ap]\.?m\.?", t[b:], re.I)
    if tm:
        b += tm.end()
    return d, (a, b)

# An index page is evidence of "no current order" only if it actually lists
# press releases. parse_index returns every link-ish headline, so a page that
# renders its list with JavaScript still yields items — the site's own
# navigation — and "items found, none about flags" read as full staff. On
# Sept 15 2026, real listings had 8 to 475 headline-length links to their own
# site; navigation-only pages (MD's old reading, SD, WI, NJ's portal) had 0-4.
MIN_LISTING_HEADLINES = 5
HEADLINE_MIN_WORDS = 7


def listing_evidence(items, base_url):
    """(is_a_listing, evidence) for an index page's parsed items."""
    def host(u):
        return (u or "").lower().removeprefix("www.")
    base = urlparse(base_url or "")
    paths = set()
    for i in items:
        if len((i.get("title") or "").split()) < HEADLINE_MIN_WORDS:
            continue
        u = urlparse(i.get("url") or "")
        if u.netloc and base.netloc and host(u.netloc) != host(base.netloc):
            continue                    # links to other agencies are navigation
        path = u.path.rstrip("/")
        if not path or path == base.path.rstrip("/"):
            continue
        paths.add(path)
    n = len(paths)
    return n >= MIN_LISTING_HEADLINES, f"{n} headline-length links to this site"


def until_noon(text):
    """Memorial Day and some orders are half-staff until noon only."""
    t = strip_html(text) if "<" in (text or "") else (text or "")
    return bool(re.search(r"until\s+noon|noon,?\s+then|half[-\s]?staff\s+until\s+12",
                          t, re.I))


# ---------------------------------------------------------------------------
# Parser 1: feed (13 states)
# ---------------------------------------------------------------------------

def parse_feed(xml, base_url=None):
    """RSS or Atom -> list of {title, url, date, is_flag}."""
    out = []
    for raw in ITEM_RE.findall(xml or "")[:80]:
        tm = TITLE_TAG_RE.search(raw)
        title = strip_html(tm.group(1)) if tm else ""
        if not title:
            continue
        lm = LINK_TAG_RE.search(raw)
        url = strip_html((lm.group(1) or lm.group(2) or "")) if lm else ""
        dm = DATE_TAG_RE.search(raw)
        d = parse_any_date(strip_html(dm.group(1))) if dm else None
        out.append({
            "title": title,
            "url": url or base_url,
            "date": d.isoformat() if d else None,
            "is_flag": bool(FLAG_RE.search(title)) or is_flag_slug(url),
        })
    return out


# ---------------------------------------------------------------------------
# Parser 2: archive (9 states) — dated list of past orders
# ---------------------------------------------------------------------------

def parse_archive(html, base_url=None):
    """Dated HTML list -> flag orders only, newest first where dates exist."""
    orders = []
    for m in BLOCK_RE.finditer(html or ""):
        block = m.group(0)
        text = strip_html(block)
        if not text or len(text) < 10 or not FLAG_RE.search(text):
            continue
        # Skip blocks that are mostly-nested containers; prefer the leaf.
        if block.count("<li") > 1 or block.count("<article") > 1:
            continue
        href = HREF_RE.search(block)
        d = parse_any_date(text)
        orders.append({
            "title": text[:200],
            "url": _abs(href.group(1), base_url) if href else base_url,
            "date": d.isoformat() if d else None,
            "is_flag": True,
        })
    orders = dedupe_orders(orders)
    orders.sort(key=lambda o: o["date"] or "", reverse=True)
    return orders


# ---------------------------------------------------------------------------
# Parser 3: index (18 states) — press release index, headline scan
# ---------------------------------------------------------------------------

def parse_index(html, base_url=None):
    """Press index -> ALL headlines, each marked with is_flag.

    Returns every headline, not just flag ones, to match parse_feed and
    parse_archive. That consistency matters more than it looks: callers use
    "did this page yield any items at all?" to tell a readable page with no
    orders apart from a page they could not read.

    Returning only flag headlines made those two cases identical — an empty
    list. Every index-mode state with no current order was reported UNKNOWN
    instead of FULL, which is 18 of the 51 and included Nebraska sitting at
    "confidence: high" while parsing nothing.
    """
    cands = []
    for m in HEADLINE_RE.finditer(html or ""):
        block = m.group(0)
        text = strip_html(block)
        if not text or len(text) < 12 or len(text) > 300:
            continue
        href = HREF_RE.search(block)
        d = parse_any_date(text)
        url = _abs(href.group(1), base_url) if href else base_url
        cands.append({
            "title": text[:200],
            "url": url,
            "date": d.isoformat() if d else None,
            # New York's headlines are classifiable from the slug alone.
            "is_flag": bool(FLAG_RE.search(text)) or is_flag_slug(url),
        })
    return dedupe_orders(cands)


# ---------------------------------------------------------------------------
# Parser 3b: cards — rendered listings of heading + date + summary
# ---------------------------------------------------------------------------

CARD_HEADING_RE = re.compile(r"<(h[1-4])\b[^>]*>(.*?)</\1>", re.S | re.I)
CARD_BODY_LIMIT = 4000


def parse_cards(html, base_url=None):
    """A listing where each release is a heading followed by its date and a
    summary: Montana's news.mt.gov and South Dakota's press releases, both
    rendered client-side. Returns parse_index-shaped items plus 'summary'.

    The summary usually carries the order's window ("...at half-staff from
    sunrise until sunset on Friday, September 11, 2026"), so an order can be
    dated without opening its page — which on these sites is rendered by
    JavaScript too. The date is read only from the start of the card, so a
    date inside the summary is never mistaken for the publication date.
    """
    import html as _html
    doc = SCRIPT_RE.sub(" ", html or "")
    heads = list(CARD_HEADING_RE.finditer(doc))
    out = []
    for n, m in enumerate(heads):
        title = strip_html(m.group(2))
        if not title or len(title) < 8 or len(title) > 300:
            continue
        stop = heads[n + 1].start() if n + 1 < len(heads) else len(doc)
        body_html = doc[m.end():min(stop, m.end() + CARD_BODY_LIMIT)]
        body = strip_html(body_html)
        href = HREF_RE.search(m.group(0)) or HREF_RE.search(body_html)
        url = _abs(_html.unescape(href.group(1)), base_url) if href else base_url
        d = parse_any_date(body[:40])
        out.append({
            "title": title[:200],
            "url": url,
            "date": d.isoformat() if d else None,
            "summary": body[:1000],
            "is_flag": bool(FLAG_RE.search(title)) or is_flag_slug(url),
        })
    return dedupe_orders(out)


# Labelled status declarations only ("Flag Status: Full-Staff"): the first
# TIER1_END patterns of DECLARATION_RE.
TIER1_END = 8


def declared_values(text):
    """Every value ('half'/'full') a page's labelled status fields state.

    Oklahoma ships a half-staff AND a full-staff widget and hides one with
    JavaScript. Rendered, only the visible one is in the text — but if the
    script did not run, both are, and more than one value means the page
    cannot be read."""
    t = strip_html(text) if "<" in (text or "") else (text or "")
    t = COUNTY_LINE_RE.sub(" ", t)
    return {m.group(1).lower() for pat in DECLARATION_RE[:TIER1_END]
            for m in pat.finditer(t)}


# ---------------------------------------------------------------------------
# Parser 4: diff (9 states) — current-status banner, no history
# ---------------------------------------------------------------------------

def status_fingerprint(html, selector_hint=None):
    """Stable hash of the status-bearing text of a page.

    We hash the TEXT, not the HTML, because government sites churn markup,
    CSRF tokens, and ad slots constantly. Hashing raw HTML produces a change
    alert on every poll, which trains you to ignore alerts — the exact
    failure mode that makes monitoring useless.

    selector_hint narrows to a region when we know one (e.g. "flag status").
    """
    text = strip_html(html)
    if selector_hint:
        i = text.lower().find(selector_hint.lower())
        if i >= 0:
            text = text[max(0, i - 200): i + 1200]
    # Drop volatile numerics: timestamps, view counters, cache-busters.
    text = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\s*(?:AM|PM)?\b", "", text, flags=re.I)
    text = re.sub(r"\b\d{9,}\b", "", text)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def parse_diff(html, previous_hash=None, selector_hint=None):
    """Current-status page -> (status, changed, fingerprint, evidence)."""
    fp = status_fingerprint(html, selector_hint)
    status, evidence = classify_current_status(html)
    start, end = diff_page_dates(html)
    changed = previous_hash is not None and fp != previous_hash
    return {
        "status": status,
        "evidence": evidence,
        "fingerprint": fp,
        "changed": changed,
        "first_seen": previous_hash is None,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "counties": county_exceptions(html),
    }


# ---------------------------------------------------------------------------

def _abs(href, base):
    if not href:
        return base
    if href.startswith("http"):
        return href
    if not base:
        return href
    from urllib.parse import urljoin
    return urljoin(base, href)


def extract_order(text, url=None, title=None):
    """Full record for one order. The output contract for the whole pipeline.

    Anything unproven stays None or UNKNOWN. Callers must treat
    authority == UNKNOWN as unusable for a state-level claim.
    """
    status, s_ev = classify_status(text)
    authority, a_ev = classify_authority(text)
    start, end = order_window(text)
    return {
        "title": (title or "")[:200] or None,
        "url": url,
        "status": status,
        "authority": authority,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "until_noon": until_noon(text),
        "status_evidence": s_ev,
        "authority_evidence": a_ev,
        "usable_as_state_order": (status == HALF and authority == GOVERNOR),
    }


PARSERS = {
    "feed": parse_feed,
    "archive": parse_archive,
    "index": parse_index,
    "diff": parse_diff,
}
