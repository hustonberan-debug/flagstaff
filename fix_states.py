#!/usr/bin/env python3
"""
fix_states.py — apply corrections found by inspecting real page text.

Run once, in the folder with registry.json:
    python3 fix_states.py

WHY EACH CHANGE
  OH  Its page is a current-status banner ("Flag Status Full Staff"), not a
      press index. It was assigned `index` mode, so the pipeline hunted for
      flag headlines on a page that has none. Same data, wrong reader.

  TN  registry had www.tn.gov/news — Tennessee's general state news portal,
      not the governor's newsroom. Candidates are tried in order and the
      first one that actually returns flag content wins.

  MD  Left alone. Its press page is JavaScript-rendered: 196KB of HTML
      collapses to 4.6KB of text with no listings in it. No parser fix can
      read content that is not in the response. Needs a different source.

  MN  Left alone, deliberately. mn.gov serves a Radware bot-detection
      captcha. That is an explicit "no automated access" signal and we
      respect it. Minnesota publishes via GovDelivery — use the subscription
      channel, which is the front door and more reliable than scraping.
"""

import json
import sys

# Notification channels actually subscribed to, with the GovDelivery account
# slug. The slug matters: GovDelivery puts it in the sending subdomain and in
# the List-* headers, so recording it here is what lets read_email.py
# attribute an incoming bulletin to a state without a hand-maintained domain
# list.
#
# expected_sender_domain is filled in ONLY where a real message has been seen
# arriving from it. It cannot be read off a signup page, and a wrong value
# does not fail safely — it files another state's order under this flag.
CHANNELS = {
    "KS": ("KSOG",  None),
    "WY": ("WYGOV", None),
    "KY": ("KYGOV", "subscriptions.kentucky.gov"),
    "MI": ("MIEOG", "govsubscriptions.michigan.gov"),
    "MO": ("MOOA",  "mooa.dmarc.public.govdelivery.com"),
    "ID": ("IDITS", None),
}

CHANGES = {
    "OH": {
        "ingest_mode": "diff",
        "flag_page_url": "https://governor.ohio.gov/flag-honors",
        "_note": "current-status banner, not a press index",
    },
    "UT": {
        # Its flag page states the answer outright in the first sentence:
        #   "...are currently at Half Staff"
        # It was assigned `feed` mode, so the pipeline read an RSS feed and
        # never looked at the page that has the answer.
        "ingest_mode": "diff",
        "flag_page_url": "https://governor.utah.gov/flag-status",
        "_note": "declarative status page, was wrongly on feed mode",
    },
    "VA": {
        # Virginia publishes a dated current-status page:
        #   "August 14, 2026  National Flag: Half Staff  State Flag: Half Staff"
        # It was marked not_covered because the harvest never found it.
        "ingest_mode": "diff",
        "flag_page_url": "https://www.governor.virginia.gov/constituent-services/flag-information/",
        "_note": "dedicated dated flag-status page",
    },
}

# Oregon: the registry points at a category-filtered newsroom search whose
# most recent listing was 17 days stale while an order was live. That is not a
# parser bug — the source genuinely does not contain the current order. Point
# at the unfiltered newsroom instead and let the parser find flag headlines.
OR_CANDIDATES = [
    "https://apps.oregon.gov/oregon-newsroom/OR/GOV/Posts",
    "https://www.oregon.gov/gov/pages/newsroom.aspx",
    "https://apps.oregon.gov/oregon-newsroom/OR/GOV/Posts/Search?org=GOV",
]

# Tried in order; the first that returns real flag content is kept.
TN_CANDIDATES = [
    "https://www.tn.gov/governor/news.html",
    "https://www.tn.gov/governor/news",
    "https://www.tn.gov/content/tn/governor/news.html",
]

try:
    reg = json.load(open("registry.json"))
except FileNotFoundError:
    sys.exit("registry.json not found - run this in the folder that has it.")

changed = []
for r in reg:
    code = r.get("state_code")

    if code in CHANGES:
        for k, v in CHANGES[code].items():
            if k.startswith("_"):
                continue
            if r.get(k) != v:
                r[k] = v
        r["buildable"] = True
        r["confidence"] = "medium"
        changed.append(f"{code}: mode -> {r['ingest_mode']}")

    if code == "TN":
        r["press_url"] = TN_CANDIDATES[0]
        r["url_candidates"] = TN_CANDIDATES
        r["ingest_mode"] = "index"
        changed.append("TN: press_url -> " + TN_CANDIDATES[0])

    if code == "OK":
        # Oklahoma serves flag-status-half.html and flag-status-full.html.
        # Both always exist; each always says its own name. Reading either
        # directly gives a constant. The signal is which one the governor's
        # page links to, so we read the hub and follow the link.
        r["ingest_mode"] = "toggle"
        r["toggle_hub_url"] = "https://oklahoma.gov/governor.html"
        r["url_candidates"] = ["https://oklahoma.gov/governor.html",
                               "https://oklahoma.gov/governor/newsroom.html"]
        r["confidence"] = "medium"
        changed.append("OK: mode -> toggle (two-page status, read the hub link)")

    if code == "LA":
        # /page/flag-status-half-staff is a permanent page, not a status. It
        # reads "Half Staff" every day of the year. Louisiana works like
        # Oklahoma: two pages, and the menu link is the actual signal.
        r["ingest_mode"] = "toggle"
        r["toggle_hub_url"] = "https://gov.louisiana.gov/"
        r["url_candidates"] = ["https://gov.louisiana.gov/",
                               "https://gov.louisiana.gov/index.cfm/newsroom"]
        r["confidence"] = "medium"
        changed.append("LA: mode -> toggle (URL encoded the status, always read half)")

    if code in CHANNELS:
        slug, domain = CHANNELS[code]
        r["notification_channel"] = {
            "type": "govdelivery",
            "detail": f"https://public.govdelivery.com/accounts/{slug}/subscriber/new",
            "account": slug,
        }
        if domain:
            r["expected_sender_domain"] = domain
        changed.append(f"{code}: GovDelivery {slug}"
                       + (f" (sends from {domain})" if domain else ""))

    if code == "KS":
        # 403s even with full browser headers. Try alternates before falling
        # back to the GovDelivery channel.
        r["url_candidates"] = [
            "https://www.governor.ks.gov/newsroom/kansas-flag-honors",
            "https://governor.kansas.gov/newsroom/kansas-flag-honors/",
            "https://www.governor.ks.gov/newsroom/kansas-flag-honors/",
        ]
        r["notification_channel"] = {
            "type": "govdelivery",
            "detail": "https://public.govdelivery.com/accounts/KSOG/subscriber/new"}
        changed.append("KS: alternate URLs + GovDelivery channel recorded")

    if code == "OR":
        r["press_url"] = OR_CANDIDATES[0]
        r["url_candidates"] = OR_CANDIDATES
        r["ingest_mode"] = "index"
        r["confidence"] = "medium"
        changed.append("OR: press_url -> unfiltered newsroom (filtered search was stale)")

    if code == "MN":
        # Minnesota publishes an official, flag-SPECIFIC RSS feed. Validated:
        # RSS 2.0, 10 dated items, descriptions carry full order text.
        # This is the front door — no scraping, no captcha, no signup. The
        # earlier Radware block was on a different path.
        # The literal space in the feed name must stay percent-encoded.
        r["ingest_mode"] = "feed"
        r["rss_url"] = ("https://mn.gov/governor/rest/rss/Flags%20Half%20Staff"
                        "?id=1055-63312&detailPage=/governor/newsroom/"
                        "flag-status/index.jsp")
        r["press_url"] = "https://mn.gov/governor/newsroom/flag-status/"
        r["buildable"] = True
        r["confidence"] = "high"
        r.pop("blocked_reason", None)
        r["notification_channel"] = {
            "type": "govdelivery",
            "detail": "https://mn.gov/governor/connect/flag-status/"}
        changed.append("MN: official flag-specific RSS feed -> feed mode")

    if code == "MD":
        r["confidence"] = "low"
        r["blocked_reason"] = ("Press page is JavaScript-rendered; listings "
                               "are absent from the HTML response. Needs a "
                               "feed, an API, or a browser-based fetch.")
        changed.append("MD: flagged JS-rendered")

json.dump(reg, open("registry.json", "w"), indent=2)

print("Applied:")
for c in changed:
    print("  " + c)

buildable = sum(1 for r in reg if r.get("buildable"))
print(f"\nBuildable: {buildable}/{len(reg)}")
print("\nNext: python3 run.py   (the cache auto-clears on parser version change)")
