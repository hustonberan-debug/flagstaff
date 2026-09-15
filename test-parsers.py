import parsers as P

ok = bad = 0
def t(label, got, want):
    global ok, bad
    if got == want:
        ok += 1; print(f"  PASS  {label}")
    else:
        bad += 1; print(f"  FAIL  {label}\n        want {want!r}\n        got  {got!r}")

print("\n--- status classification (conservative by design) ---")
t("half", P.classify_status("Flags will be flown at half-staff Monday")[0], P.HALF)
t("half/mast variant", P.classify_status("flags lowered to half mast")[0], P.HALF)
t("full", P.classify_status("Flags are currently at full staff.")[0], P.FULL)
t("no active order = full",
  P.classify_status("There is no current half-staff order in effect.")[0], P.FULL)
t("BOTH signals -> unknown, not a guess",
  P.classify_status("half-staff until noon, then returned to full staff")[0], P.UNKNOWN)
t("empty -> unknown", P.classify_status("")[0], P.UNKNOWN)
t("unrelated -> unknown", P.classify_status("Governor signs budget bill")[0], P.UNKNOWN)

print("\n--- AUTHORITY: the Alaska contamination problem ---")
gov = "Governor Dunleavy has ordered flags lowered to half-staff in honor of Trooper Smith."
pres = ("By order of the President of the United States, the flag shall be flown at "
        "half-staff at all federal buildings until sunset on August 5.")
both = ("Governor Dunleavy has directed that flags be lowered to half-staff in "
        "accordance with the President's proclamation.")
t("governor order", P.classify_authority(gov)[0], P.GOVERNOR)
t("PRESIDENTIAL repost detected", P.classify_authority(pres)[0], P.PRESIDENT)
t("governor acting on federal = state order", P.classify_authority(both)[0], P.GOVERNOR)
t("no signature -> unknown", P.classify_authority("Flags at half-staff today.")[0], P.UNKNOWN)

print("\n--- the money test: a federal repost must NOT count as a state order ---")
t("presidential repost rejected", P.extract_order(pres)["usable_as_state_order"], False)
t("governor order accepted", P.extract_order(gov)["usable_as_state_order"], True)
t("unsigned rejected",
  P.extract_order("Flags at half-staff today.")["usable_as_state_order"], False)

print("\n--- date parsing across the formats state sites actually use ---")
for s, want in [("2026-08-03", "2026-08-03"), ("August 3, 2026", "2026-08-03"),
                ("Aug. 3, 2026", "2026-08-03"), ("3 August 2026", "2026-08-03"),
                ("8/3/2026", "2026-08-03"),
                ("Mon, 03 Aug 2026 14:00:00 GMT", "2026-08-03")]:
    d = P.parse_any_date(s)
    t(f"{s!r}", d.isoformat() if d else None, want)
t("garbage -> None", P.parse_any_date("no date here"), None)
t("bad date -> None", P.parse_any_date("2026-13-45"), None)

print("\n--- date ranges and until-noon ---")
o = P.extract_order("Flags at half-staff from August 3, 2026 through August 7, 2026.")
t("start", o["start_date"], "2026-08-03")
t("end", o["end_date"], "2026-08-07")
t("Memorial Day noon rule",
  P.until_noon("Flags at half-staff until noon, then raised to full."), True)
t("no noon rule", P.until_noon("Flags at half-staff all day."), False)

print("\n--- feed parser (RSS + Atom) ---")
rss = """<rss><channel>
<item><title><![CDATA[Governor Orders Flags to Half-Staff]]></title>
<link>https://g.gov/n/1</link><pubDate>Mon, 03 Aug 2026 14:00:00 GMT</pubDate></item>
<item><title>Governor Announces Broadband Grant</title>
<link>https://g.gov/n/2</link><pubDate>Fri, 31 Jul 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""
f = P.parse_feed(rss)
t("2 items", len(f), 2)
t("1 flagged", sum(1 for i in f if i["is_flag"]), 1)
t("date parsed", f[0]["date"], "2026-08-03")
t("CDATA stripped", "CDATA" in f[0]["title"], False)
atom = """<feed><entry><title>Gov. lowers flags to half-mast</title>
<link href="https://g.gov/a/9"/><updated>2026-06-11T10:00:00Z</updated></entry></feed>"""
a = P.parse_feed(atom)
t("atom link", a[0]["url"], "https://g.gov/a/9")
t("atom date", a[0]["date"], "2026-06-11")

print("\n--- Rhode Island: translated duplicates must not double-count ---")
ri = """<rss><channel>
<item><title>Governor Orders Flags to Half-Staff</title><link>https://ri.gov/1</link>
<pubDate>2026-08-03</pubDate></item>
<item><title>SPANISH TRANSLATION: Governor Orders Flags to Half-Staff</title>
<link>https://ri.gov/1-es</link><pubDate>2026-08-03</pubDate></item>
</channel></rss>"""
items = [i for i in P.parse_feed(ri) if i["is_flag"]]
t("raw feed has both", len(items), 2)
t("after dedupe: one order", len(P.dedupe_orders(items)), 1)
t("translation detector", P.is_translation("SPANISH TRANSLATION: Flags lowered"), True)
t("normal title kept", P.is_translation("Governor Orders Flags"), False)

print("\n--- archive parser ---")
arch = """<ul>
<li><a href="/o/1">August 3, 2026 - Flags to Half-Staff for Fallen Trooper</a></li>
<li><a href="/o/2">July 4, 2026 - Governor Celebrates Independence Day</a></li>
<li><a href="/o/3">May 15, 2026 - Half-Staff for Peace Officers Memorial Day</a></li>
</ul>"""
ar = P.parse_archive(arch, "https://g.gov")
t("2 flag orders, non-flag skipped", len(ar), 2)
t("newest first", ar[0]["date"], "2026-08-03")
t("relative url resolved", ar[0]["url"], "https://g.gov/o/1")

print("\n--- index parser ---")
idx = """<div>
<h3><a href="/news/1">Governor Orders Flags to Half-Staff for Fallen Firefighter</a></h3>
<h3><a href="/news/2">Governor Signs Transportation Bill</a></h3>
<a href="/news/3">Flags lowered statewide in remembrance</a>
<a href="/">Home</a></div>"""
ix = P.parse_index(idx, "https://g.gov")
# parse_index returns ALL headlines with an is_flag marker, matching
# parse_feed and parse_archive. Callers need "page had items but none were
# flags" (-> full staff) to look different from "page yielded nothing"
# (-> unknown); returning only flag items made those identical.
t("3 headlines returned", len(ix), 3)
t("2 of them flagged", sum(1 for c in ix if c["is_flag"]), 2)
t("short nav link ignored", any("Home" == c["title"] for c in ix), False)
t("empty page yields nothing", len(P.parse_index("<html></html>")), 0)

print("\n--- diff parser: the 9 states with no history ---")
p1 = "<html><body><h1>Flag Status</h1><p>Flags are at full staff.</p></body></html>"
p2 = "<html><body><h1>Flag Status</h1><p>Flags are at half-staff.</p></body></html>"
d1 = P.parse_diff(p1)
t("first poll: full", d1["status"], P.FULL)
t("first poll flagged as first_seen", d1["first_seen"], True)
t("first poll not 'changed'", d1["changed"], False)
d2 = P.parse_diff(p2, previous_hash=d1["fingerprint"])
t("second poll: half", d2["status"], P.HALF)
t("CHANGE DETECTED", d2["changed"], True)
d3 = P.parse_diff(p2, previous_hash=d2["fingerprint"])
t("no change on repeat", d3["changed"], False)

print("\n--- fingerprint must ignore cosmetic churn (or alerts become noise) ---")
# Realistic churn: same content, but rotating attribute values, a changing
# timestamp, and injected script. The status sentence is identical.
base = ('<html><body><div class="a" data-cache="1119283">Flags are at full staff.'
        '</div><span>Updated 3:42 PM</span></body></html>')
churn = ('<html><body><div class="a" data-cache="9912837">Flags are at full staff.'
         '</div><span>Updated 9:07 AM</span><script>var t=1</script></body></html>')
t("timestamp/attr/script churn ignored",
  P.status_fingerprint(base), P.status_fingerprint(churn))
t("real content change detected",
  P.status_fingerprint(base) == P.status_fingerprint(
      base.replace("full staff", "half-staff")), False)

print("\n--- robustness: nothing may raise ---")
for junk in ["", None, "<html>", "<<<>>>", "\x00\xff", "a" * 50000,
             "<item><title>", "not html at all"]:
    try:
        P.parse_feed(junk); P.parse_archive(junk); P.parse_index(junk)
        P.parse_diff(junk); P.classify_status(junk); P.classify_authority(junk)
        P.parse_any_date(junk); P.extract_order(junk or "")
    except Exception as e:
        bad += 1; print(f"  FAIL  raised on {junk!r:20.20}: {type(e).__name__}: {e}")
        break
else:
    ok += 1; print("  PASS  all malformed inputs handled")

print("\n--- county regex must not backtrack (this hung the whole suite) ---")
import time
t0 = time.time()
P.county_exceptions("a" * 50000)
P.parse_diff("a" * 50000)
t("50k-letter word handled in under 2s", time.time() - t0 < 2, True)
t("Pennsylvania county line still parsed",
  P.county_exceptions("United States Flag: Full-Staff Allegheny County Only "
                      "United States Flags: Half-Staff"),
  [{"county": "Allegheny", "status": P.HALF}])

# The pipeline tests below stub the network: fetch() and today() are
# replaced, so nothing here touches a real site or the real clock.
from datetime import date
import email.message
import run as R
import read_email as E

_real_fetch, _real_today = R.fetch, R.today
calls = {}
def stub(pages):
    def f(url, session=None):
        calls[url] = calls.get(url, 0) + 1
        v = pages.get(url)
        return (v, None) if isinstance(v, str) else (None, v or "HTTP 404")
    return f
def on(d):
    R.today = lambda: d

print("\n--- a verdict is a function of (page, DATE): North Dakota, Sept 2026 ---")
nd = {"state_status": P.HALF, "state_order": {
    "title": "x", "url": "u", "start_date": "2026-09-09", "end_date": None,
    "date": "2026-09-09"}}
t("carried order live on its date", R.revalidate(nd, date(2026, 9, 9))[0], P.HALF)
t("carried order expired 6 days later", R.revalidate(nd, date(2026, 9, 15))[0], P.FULL)
t("statutory day not carried into the next day",
  R.revalidate({"state_status": P.HALF, "state_order": {
      "start_date": "2026-09-11", "end_date": "2026-09-11"}}, date(2026, 9, 12))[0],
  P.FULL)

FEED = "https://nd.test/rss"
ART = "https://nd.test/news/flags"
nd_feed = f"""<rss><channel><item>
<title>Governor directs flags flown at half-staff Friday in memory of 9/11 victims</title>
<link>{ART}</link><pubDate>Wed, 09 Sep 2026 15:00:00 GMT</pubDate></item>
<item><title>Governor announces broadband grants</title><link>https://nd.test/n/2</link>
<pubDate>Tue, 08 Sep 2026 15:00:00 GMT</pubDate></item></channel></rss>"""
R.fetch = stub({FEED: nd_feed, ART: "<p>Flags will be flown at half-staff.</p>"})
rec = {"state": "Testland", "state_code": "TT", "ingest_mode": "feed",
       "buildable": True, "rss_url": FEED}
on(date(2026, 9, 9))
_, o1, c1 = R.check_state(rec, {}, None)
t("feed: half on the order date", o1["state_status"], P.HALF)
on(date(2026, 9, 15))
_, o2, c2 = R.check_state(rec, {"TT": c1}, None)
t("SAME unchanged feed six days later: full, not the cached half",
  o2["state_status"], P.FULL)
t("page text did not move", o2["content_changed"], False)
t("order page fetched once, then read from the per-URL cache", calls.get(ART), 1)

print("\n--- freshness: frozen sources say so on EVERY run, not just the first ---")
old = ("<rss><channel><item><title>Governor signs budget</title><link>https://x/1</link>"
       "<pubDate>Mon, 06 Jan 2020 10:00:00 GMT</pubDate></item></channel></rss>")
R.fetch = stub({"https://sc.test/feed": old})
sc = {"state": "Testland", "state_code": "TT", "ingest_mode": "feed",
      "buildable": True, "rss_url": "https://sc.test/feed"}
_, o1, c1 = R.check_state(sc, {}, None)
_, o2, _ = R.check_state(sc, {"TT": c1}, None)
t("feed last updated 2020 -> frozen, not full", (o1["coverage"], o1["state_status"]),
  ("frozen", P.UNKNOWN))
t("...and still frozen, with its reason, on the next run",
  (o2["coverage"], bool(o2["error"])), ("frozen", True))

R.fetch = stub({"https://al.test/flag": "<p>Last updated: September 1, 2026. "
                "The flag may be flown at half-staff by order of the governor.</p>"})
al = {"state": "Testland", "state_code": "TT", "ingest_mode": "diff",
      "buildable": True, "flag_page_url": "https://al.test/flag"}
_, o, _ = R.check_state(al, {}, None)
t("diff page with no declaration: unknown AND says why",
  (o["state_status"], bool(o["error"])), (P.UNKNOWN, True))

print("\n--- status pages: real layouts from Sept 15 2026 ---")
idaho = ("Flag Status USA Flag Status Flag at full staff Idaho Flag Status Flag at full "
         "staff The U.S. President and Idaho Governor have the authority to order U.S. "
         "and State of Idaho flags to be flown at half-staff within the State of Idaho "
         "for certain occasions.")
t("Idaho: label says full -> full (was read as half from protocol prose)",
  P.classify_current_status(idaho)[0], P.FULL)
t("Colorado label layout", P.classify_current_status(
    "Flag Status USA Flag Status: Flag at Half Staff Colorado Flag Status: Flag at Half Staff")[0],
  P.HALF)
t("Alaska declaration still read", P.classify_current_status(
    "Governor Dunleavy has directed the United States flag to be flown at half staff")[0],
  P.HALF)
fl = ("<p>Flag Status: Half Staff</p><p>RE: Flags at Half-Staff in Honor of Patriot Day. "
      "I hereby direct the flags of the United States and the State of Florida to be "
      "flown at half-staff at all local and state buildings from sunrise to sunset on "
      "Friday, September 11, 2026.</p><p>Flags at Half-Staff in Honor of City "
      "Commissioner Chris Jones May 28, 2026</p>")
R.fetch = stub({"https://fl.test/flag": fl})
flrec = {"state": "Testland", "state_code": "TT", "ingest_mode": "diff",
         "buildable": True, "flag_page_url": "https://fl.test/flag"}
on(date(2026, 9, 11))
_, o, _ = R.check_state(flrec, {}, None)
t("Florida on Sept 11: half", o["state_status"], P.HALF)
on(date(2026, 9, 15))
_, o, _ = R.check_state(flrec, {}, None)
t("Florida on Sept 15: widget still says half, listed order ended -> unknown, not half",
  (o["state_status"], "ended 2026-09-11" in (o["error"] or "")), (P.UNKNOWN, True))

print("\n--- federal: flag text past character 6,000 (Patriot Day 2026 was at 10,310) ---")
LIST = "https://wh.test/proclamations/"
PROC = "https://www.whitehouse.gov/presidential-actions/2026/09/honoring-the-memory-of-x/"
NAV = "https://www.whitehouse.gov/presidential-actions/executive-orders/"
listing = (f'<a href="{PROC}">Honoring the Memory of X</a>'
           f'<a href="{NAV}">Executive Orders</a>')
long_body = ("<p>" + "Site navigation item. " * 450 + "</p><p>NOW, THEREFORE, I do "
             "hereby order that the flag of the United States shall be flown at "
             "half-staff at the White House and upon all public buildings until "
             "sunset, September 20, 2026.</p>")
import os
os.environ["FEDERAL_PROCLAMATION_URL"] = LIST
on(date(2026, 9, 15))
R.fetch = stub({LIST: listing, PROC: long_body})
cache = {}
fed, ferr = R.federal_proclamation(None, cache)
t("long proclamation detected", (fed or {}).get("reason"), "Honoring the Memory of X")
t("no error when checked cleanly", ferr, None)
t("end-date-only order stays active mid-window",
  (fed or {}).get("coverage_reason", "").startswith("window"), True)
calls.clear()
R.fetch = stub({LIST: listing})          # the proclamation page errors
fed, ferr = R.federal_proclamation(None, {})
t("unreadable proclamation -> 'could not determine', not 'no order'",
  (fed, bool(ferr)), (None, True))
R.fetch = stub({})
fed, ferr = R.federal_proclamation(None, {})
t("unreachable listing -> error, not silence", (fed, bool(ferr)), (None, True))
t("nav links do not use up the article budget", NAV in calls, False)
c2 = {}
R.fetch = stub({LIST: listing})
R.federal_proclamation(None, c2)
t("a failed article fetch is not cached as empty forever",
  PROC in c2.get(R.FEDERAL_ARTICLE_CACHE, {}), False)
del os.environ["FEDERAL_PROCLAMATION_URL"]

print("\n--- change tracking: the ANSWER changed, not the page ---")
res = {"NV": {"effective_status": P.FULL, "checked_at": "T2", "state_order": None},
       "ND": {"effective_status": P.FULL, "checked_at": "T2", "state_order": None},
       "AL": {"effective_status": P.UNKNOWN, "checked_at": "T2", "state_order": None}}
prev = {"NV": {"last_known_status": P.FULL, "last_status_change_at": "T0"},
        "ND": {"last_known_status": P.HALF, "last_status_change_at": "T0"},
        "AL": {"last_known_status": P.FULL}}
nc = {}
R.track_changes(res, prev, nc)
t("page edit, same answer: not a change (Nevada: 479 of these)", res["NV"]["changed"], False)
t("...keeps its 'unchanged since' stamp", res["NV"]["last_changed_at"], "T0")
t("half -> full is a change", res["ND"]["changed"], True)
t("full -> unknown is NOT announced as a change", res["AL"]["changed"], False)
t("unknown does not overwrite the last known answer", nc["AL"]["last_known_status"], P.FULL)
t("last national day before Sept 15 2026 is Patriot Day",
  R.last_national_day(date(2026, 9, 15)), "2026-09-11")
res = {"NJ": {"effective_status": P.FULL, "checked_at": "T2", "state_order": None}}
R.track_changes(res, {"NJ": {"state_status": P.FULL,
                             "last_changed_at": "2026-08-26T18:00:00+00:00"}}, {},
                legacy_floor="2026-09-12T00:00:00+00:00")
t("old page-edit stamp cannot claim 'unchanged' across Patriot Day",
  res["NJ"]["last_changed_at"], "2026-09-12T00:00:00+00:00")

print("\n--- email: dates and attribution ---")
def mail(frm, subj, body, sent="Thu, 10 Sep 2026 14:00:00 -0500"):
    m = email.message.EmailMessage()
    m["From"], m["Subject"], m["Date"] = frm, subj, sent
    m.set_content(body)
    return m
E.SENDER_HINTS["mooa.dmarc.public.govdelivery.com"] = "MO"
rec_e, _ = E.parse_message(mail(
    "Missouri OA <missourioa@mooa.dmarc.public.govdelivery.com>",
    "Flags to Fly at Half-Staff for Patriot Day",
    "Patriot Day was designated by Public Law 107-89, signed December 18, 2001. "
    "Flags should be flown at half-staff on Friday."), {"MO"})
t("a law's 2001 signing date is not the order's start",
  (rec_e or {}).get("start_date"), "2026-09-10")
rec_e, why = E.parse_message(mail(
    "Anyone <x@example.com>", "Illinois Governor Orders Flags to Half-Staff",
    "Flags to half-staff statewide."), {"IL"})
t("a state named only in the subject is not attribution", (rec_e, why),
  (None, "no state identified"))

R.fetch, R.today = _real_fetch, _real_today

print(f"\n{'='*52}\n  {ok} passed, {bad} failed\n{'='*52}\n")
raise SystemExit(1 if bad else 0)
