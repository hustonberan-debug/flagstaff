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
from datetime import date, datetime

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

# Index of the first context-guarded (Tier 3) pattern.
GUARDED_FROM = 11

# If any of these appear just before a Tier 3 match, the sentence is
# describing the rules rather than stating today's status.
PROSE_BEFORE = re.compile(
    r"\b(?:may|should|shall|when|whenever|if|authorized|proclaim|code|"
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
COUNTY_LINE_RE = re.compile(
    r"(?<![-\w])[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+count(?:y|ies)\s+only"
    r"[^:]{0,60}:\s*(?:half|full)[-\s]?staff", re.I)

COUNTY_SCOPED_RE = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+count(?:y|ies)\s+only[^:]{0,60}:\s*"
    r"(half|full)[-\s]?staff", re.I)

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


def order_scope(text):
    """'statewide' or 'limited', plus the phrase that decided it."""
    t = strip_html(text) if "<" in (text or "") else (text or "")
    m = LIMITED_SCOPE_RE.search(t)
    return ("limited", m.group(0).strip()) if m else ("statewide", None)


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


def page_last_modified(html, headers=None):
    """Best available freshness signal for a page, or None."""
    if headers:
        for k in ("Last-Modified", "last-modified"):
            if headers.get(k):
                d = parse_any_date(headers[k])
                if d:
                    return d
    for pat in LASTMOD_PATTERNS:
        m = pat.search(html or "")
        if m:
            d = parse_any_date(m.group(1))
            if d:
                return d
    # Fall back to the newest date mentioned anywhere in the visible text.
    text = strip_html(html)
    best = None
    for pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            d = parse_any_date(m.group(0))
            if d and (best is None or d > best):
                best = d
    return best


def parse_toggle(html, base_url=None):
    """Two-static-page states (Oklahoma).

    The site serves flag-status-half.html and flag-status-full.html. Both
    always exist and each always says its own name. The status is encoded in
    WHICH ONE the site links to, so we read a hub page and look at the link,
    not at the destination.

    Returns (status, evidence).
    """
    text = html or ""
    hrefs = re.findall(r'href=[\"\'"]([^\"\'"]*flag[-_]?status[^\"\'"]*)', text, re.I)
    half = [h for h in hrefs if re.search(r"[-_]half", h, re.I)]
    full = [h for h in hrefs if re.search(r"[-_]full", h, re.I)]

    # Exactly one of the two should be linked. Both or neither means the page
    # is a directory listing rather than a status indicator, and we say so
    # instead of picking one.
    if half and not full:
        return HALF, f"site links to {half[0]}"
    if full and not half:
        return FULL, f"site links to {full[0]}"
    if half and full:
        return UNKNOWN, "both half and full pages linked - not a status signal"
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


def is_translation(title):
    return bool(TRANSLATION_PREFIX.match(title or ""))


def dedupe_orders(orders):
    """Drop translated duplicates and repeats of the same URL/title.
    Rhode Island doubles every order without this."""
    seen_urls, seen_titles, out = set(), set(), []
    for o in orders:
        if is_translation(o.get("title")):
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


def date_range(text):
    """Best-effort (start, end) for an order. Either may be None."""
    t = strip_html(text) if "<" in (text or "") else (text or "")
    m = re.search(
        rf"(?:from|beginning|effective)\s+(.{{0,60}}?)\s+(?:through|until|to)\s+"
        rf"(.{{0,60}}?)(?:[.;]|$)", t, re.I)
    if m:
        s, e = parse_any_date(m.group(1)), parse_any_date(m.group(2))
        if s or e:
            return s, e
    m = re.search(r"(?:until|through)\s+(?:sunset\s+(?:on\s+)?)?(.{0,40})", t, re.I)
    if m:
        e = parse_any_date(m.group(1))
        if e:
            return parse_any_date(t), e
    single = parse_any_date(t)
    return single, None


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
            "is_flag": bool(FLAG_RE.search(title)),
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
    """Press index -> candidate flag orders from headline text."""
    cands = []
    for m in HEADLINE_RE.finditer(html or ""):
        block = m.group(0)
        text = strip_html(block)
        if not text or len(text) < 12 or len(text) > 300:
            continue
        if not FLAG_RE.search(text):
            continue
        href = HREF_RE.search(block)
        d = parse_any_date(text)
        cands.append({
            "title": text[:200],
            "url": _abs(href.group(1), base_url) if href else base_url,
            "date": d.isoformat() if d else None,
            "is_flag": True,
        })
    return dedupe_orders(cands)


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
    start, end = date_range(text)
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
