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
t("feed: published Wednesday for 'Friday' -> not yet in effect Wednesday",
  o1["state_status"], P.FULL)
on(date(2026, 9, 11))
_, o3, c3 = R.check_state(rec, {"TT": c1}, None)
t("...half on Friday, from the SAME unchanged feed", o3["state_status"], P.HALF)
t("...and it was dated from the weekday, not the dateline",
  (o3["state_order"] or {}).get("end_date"), "2026-09-11")
on(date(2026, 9, 15))
_, o2, c2 = R.check_state(rec, {"TT": c3}, None)
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
thirty = {"reason": "Honoring the Memory of a President", "authority":
          "presidential proclamation", "start_date": "2026-09-01",
          "end_date": "2026-09-30"}
t("30-day order that scrolled off the listing stays in force mid-window",
  bool(R.carry_federal(thirty, date(2026, 9, 20))), True)
t("...and ends when its window does", R.carry_federal(thirty, date(2026, 10, 1)), None)
t("statutory days are never carried", R.carry_federal(
    {"authority": "statute", "start_date": "2026-09-11", "end_date": "2026-09-11"},
    date(2026, 9, 11)), None)

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
GMAIL = "mx.google.com; dkim=pass header.i=@{d} header.s=s1 header.b=abc; spf=pass"
def mail(frm, subj, body, sent="Thu, 10 Sep 2026 14:00:00 -0500", auth=()):
    m = email.message.EmailMessage()
    for a in auth:                          # topmost first, as Gmail prepends
        m["Authentication-Results"] = a
    m["From"], m["Subject"], m["Date"] = frm, subj, sent
    m.set_content(body)
    return m
E.SENDER_HINTS["mooa.dmarc.public.govdelivery.com"] = "MO"
E.SENDER_HINTS["list.ks.gov"] = "KS"
E.GOVDELIVERY_ACCOUNTS["wygov"] = "WY"
MO_FROM = "Missouri OA <missourioa@mooa.dmarc.public.govdelivery.com>"
rec_e, _ = E.parse_message(mail(
    MO_FROM, "Flags to Fly at Half-Staff for Patriot Day",
    "Patriot Day was designated by Public Law 107-89, signed December 18, 2001. "
    "Flags should be flown at half-staff until sunset on September 11, 2026.",
    auth=[GMAIL.format(d="govdelivery.com")]), {"MO"})
t("a law's 2001 signing date is not the order's start",
  (rec_e or {}).get("start_date"), "2026-09-10")
t("Missouri's real From (unquoted comma in the name) still resolves",
  E.sender_address("Missouri OA – Facilities Management, Design & Construction "
                   "<missourioa@mooa.dmarc.public.govdelivery.com>"),
  ("missourioa", "mooa.dmarc.public.govdelivery.com"))
rec_e, _ = E.parse_message(mail(
    "Kansas Governor <govpress@list.ks.gov>",
    "Governor Kelly Directs Flags to Half-Staff Friday", "Flags to half-staff.",
    auth=[GMAIL.format(d="ks.gov")]), {"KS"})
t("email 'half-staff Friday' sent Thursday -> Friday only",
  ((rec_e or {}).get("start_date"), (rec_e or {}).get("end_date")),
  ("2026-09-11", "2026-09-11"))
rec_e, why = E.parse_message(mail(
    "Anyone <x@example.com>", "Illinois Governor Orders Flags to Half-Staff",
    "Flags to half-staff statewide.", auth=[GMAIL.format(d="example.com")]), {"IL"})
t("a state named only in the subject is not attribution", (rec_e, why),
  (None, "no state identified"))

print("\n--- email: DKIM, or it did not come from the state ---")
KS = ("Kansas Governor <govpress@list.ks.gov>",
      "Governor Kelly Directs Flags to Half-Staff", "Flags to half-staff statewide.")
def ks(auth):
    return E.parse_message(mail(*KS, auth=auth), {"KS"})
t("aligned dkim=pass (list.ks.gov signed by ks.gov) accepted",
  (ks([GMAIL.format(d="ks.gov")])[0] or {}).get("state_code"), "KS")
t("no Authentication-Results at all -> rejected",
  ks([])[1].startswith(E.UNAUTHENTICATED), True)
t("forged From, dkim=pass only for the forger's own domain -> rejected",
  ks([GMAIL.format(d="evil.example")])[1].startswith(E.UNAUTHENTICATED), True)
t("dkim=fail -> rejected", ks(["mx.google.com; dkim=fail header.i=@ks.gov"])[1]
  .startswith(E.UNAUTHENTICATED), True)
t("forger's own 'dkim=pass' header BELOW Gmail's verdict is ignored",
  ks(["mx.google.com; dkim=none", GMAIL.format(d="ks.gov")])[1]
  .startswith(E.UNAUTHENTICATED), True)
t("a verdict from any server other than Gmail is ignored",
  ks([GMAIL.format(d="ks.gov").replace("mx.google.com", "mail.evil.example")])[1]
  .startswith(E.UNAUTHENTICATED), True)
t("sound-alike domain (notks.gov) is not Kansas", E.parse_message(mail(
    "Gov <a@notks.gov>", KS[1], KS[2], auth=[GMAIL.format(d="notks.gov")]),
    {"KS"})[1], "no state identified")
t("display name 'list.ks.gov' on someone else's address is not Kansas",
  E.parse_message(mail("list.ks.gov <a@evil.example>", KS[1], KS[2],
                       auth=[GMAIL.format(d="evil.example")]), {"KS"})[1],
  "no state identified")
t("GovDelivery account in the address, signed by govdelivery.com -> WY",
  (E.parse_message(mail("Office of Governor <WYGOV@public.govdelivery.com>",
                        "Governor Orders Flags Lowered to Half-Staff", "Half-staff.",
                        auth=[GMAIL.format(d="govdelivery.com")]), {"WY"})[0]
   or {}).get("state_code"), "WY")
t("state .us suffix keeps its own org domain",
  (E.org_domain("listserv.state.ma.us"), E.org_domain("public.govdelivery.com")),
  ("state.ma.us", "govdelivery.com"))

print("\n--- email channels: silence is only 'no order' while the channel lives ---")
import json as _json, tempfile
_real_orders = R.EMAIL_ORDERS
def email_state(heard, generated="2026-09-15T12:00:00+00:00", skipped=False,
                orders=None, limit=None, seen="2026-06-01"):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    _json.dump({"generated_at": generated, "skipped": skipped, "orders": orders or {},
                "channels_seen": {"TT": seen} if (heard and seen) else {},
                "channels_heard": {"TT": heard} if heard else {}}, f)
    f.close()
    R.EMAIL_ORDERS = f.name
    r = {"state": "Testland", "state_code": "TT", "ingest_mode": "email",
         "channel_max_silence_days": limit}
    _, o, _ = R.check_state(r, {"TT": {"hash": "x", "state_status": P.FULL}}, None)
    R.EMAIL_ORDERS = _real_orders
    os.unlink(f.name)
    return o["state_status"], o["coverage"]
on(date(2026, 9, 15))
t("channel heard 10 days ago, no order -> full", email_state("2026-09-05"),
  (P.FULL, "covered"))
t("channel silent 90 days -> not read as full", email_state("2026-06-17"),
  (P.UNKNOWN, "frozen"))
t("per-state limit can allow a quieter channel",
  email_state("2026-06-17", limit=120), (P.FULL, "covered"))
t("never heard -> not covered", email_state(None), (P.UNKNOWN, "not_covered"))
t("heard only a welcome message, never a flag bulletin -> still pending",
  email_state("2026-09-15", seen=None), (P.UNKNOWN, "not_covered"))
t("flag bulletin long ago, other mail recently -> channel alive, full",
  email_state("2026-09-12", seen="2026-03-01"), (P.FULL, "covered"))
t("inbox not read for 3 days -> stale, last value served",
  email_state("2026-09-05", generated="2026-09-12T12:00:00+00:00"), (P.FULL, "stale"))
t("an order we did read still counts when the inbox is stale",
  email_state("2026-09-14", generated="2026-09-12T12:00:00+00:00", orders={"TT": {
      "subject": "Flags to half-staff", "start_date": "2026-09-14",
      "end_date": "2026-09-16"}})[0], P.HALF)
t("no mail credentials -> last value served, marked stale (never a fresh full)",
  email_state("2026-09-05", skipped=True), (P.FULL, "stale"))

t("channel link: URL pulled out of a detail note (Illinois)", R.channel_url(
    {"notification_channel": {"detail": "https://il.test/opt-in.html -- select "
                              "'Specific Subjects/Agencies' then 'Flag Honors'."}}),
  "https://il.test/opt-in.html")
t("channel link: listserv instructions fall back to the signup page (MA)",
  R.channel_url({"notification_channel": {"detail": "Send a blank email to "
                 "subscribe-bsb@listserv.state.ma.us."},
                 "signup_url": "https://ma.test/flag-status"}),
  "https://ma.test/flag-status")
t("channel link: nothing to link to", R.channel_url({}), None)

print("\n--- weekday-only dates ---")
wed, mon, thu = date(2026, 9, 9), date(2026, 9, 7), date(2026, 9, 10)
t("'half-staff Friday' published Wednesday",
  P.weekday_window("Armstrong directs flags flown at half-staff Friday in memory", wed),
  (date(2026, 9, 11), date(2026, 9, 11)))
t("the announcement's own weekday is ignored",
  P.weekday_window("On Monday, the governor ordered flags to half-staff on Friday", mon),
  (date(2026, 9, 11), date(2026, 9, 11)))
t("'until sunset Sunday' starts at publication",
  P.weekday_window("Flags will fly at half-staff until sunset Sunday", thu),
  (thu, date(2026, 9, 13)))
t("'from sunrise Friday until sunset Sunday'",
  P.weekday_window("Flags at half-staff from sunrise Friday until sunset Sunday", wed),
  (date(2026, 9, 11), date(2026, 9, 13)))
t("a weekday 6 days out is probably past, not future", P.weekday_window(
    "Flags were flown at half-staff Friday", date(2026, 9, 12)), (None, None))
t("no half-staff phrase -> nothing", P.weekday_window("Governor visits Friday", wed),
  (None, None))

print("\n--- navigation-only index pages are not 'no order' ---")
nav = "".join(f'<a href="/{p}">{t_}</a>' for p, t_ in [
    ("services", "Online Services for Residents"), ("agencies", "Agency Directory"),
    ("skip", "Skip to main content"), ("x", "Governor's Office of Community Initiatives"),
    ("y", "Official Site of the State of Testland")])
news = "".join(f'<h3><a href="/news/{n}">Governor Smith Announces New Program Number {n} '
               f'for Rural Families</a></h3>' for n in range(8))
ok_nav, _ = P.listing_evidence(P.parse_index(nav, "https://gov.test/news"), "https://gov.test/news")
ok_news, _ = P.listing_evidence(P.parse_index(news, "https://gov.test/news"), "https://gov.test/news")
t("navigation links are not a listing", ok_nav, False)
t("eight release headlines are", ok_news, True)
t("links to other agencies' sites do not count", P.listing_evidence(
    [{"title": "Governor's Office of Crime Prevention Youth and Victim Services",
      "url": f"https://agency{n}.test/"} for n in range(9)], "https://gov.test/news")[0],
  False)
R.fetch = stub({"https://sd.test/news": "<html>" + nav + "</html>"})
_, o, _ = R.check_state({"state": "Testland", "state_code": "TT", "ingest_mode": "index",
                         "buildable": True, "press_url": "https://sd.test/news"}, {}, None)
t("index page of navigation -> unknown with reason, not full",
  (o["state_status"], "no press listing" in (o["error"] or "")), (P.UNKNOWN, True))

print("\n--- New York: classified from the URL slug, behind Cloudflare ---")
PIRO = ("/news/governor-hochul-directs-flags-half-staff-honor-retired-sergeant-"
        "michael-l-piro")
NY_LIST = f"""<div class="view-content">
<h3><a href="{PIRO}">Governor Hochul Honors Retired Sergeant Michael L. Piro</a></h3>
<h3><a href="/news/la-gobernadora-hochul-ordena-que-las-banderas-ondeen-a-media-asta-en-honor-al-sargento-piro">
La Gobernadora Hochul Rinde Homenaje al Sargento Retirado Michael L. Piro</a></h3>
<h3><a href="/news/governor-hochul-announces-50-million-clean-water-infrastructure-upstate">
Governor Hochul Announces $50 Million for Clean Water Infrastructure Upstate</a></h3>
</div>"""
ny = P.parse_index(NY_LIST, "https://www.governor.ny.gov/news")
t("flag order recognised from the slug, with no flag word in the headline",
  [i["is_flag"] for i in ny if "piro" in (i["url"] or "").lower()
   and "gobernadora" not in (i["url"] or "")], [True])
t("the Spanish copy of that order is dropped",
  any("gobernadora" in (i["url"] or "") for i in ny), False)
t("an unrelated release is not a flag order",
  [i["is_flag"] for i in ny if "clean-water" in (i["url"] or "")], [False])
t("so the listing yields one order, not two",
  sum(1 for i in ny if i["is_flag"]), 1)
t("is_flag_slug: the verified Piro release", P.is_flag_slug(PIRO), True)
t("is_flag_slug: an announcement is not an order",
  P.is_flag_slug("/news/governor-hochul-announces-broadband-expansion"), False)
t("Spanish slug detected as a translation",
  P.is_translation("La Gobernadora Hochul Ordena...",
                   "/news/la-gobernadora-hochul-ordena-banderas-media-asta"), True)
t("English slug is not a translation",
  P.is_translation("Governor Hochul Directs Flags", PIRO), False)

NY_URL = "https://ny.test/news"
NYREC = {"state": "Testland", "state_code": "TT", "ingest_mode": "index",
         "buildable": True, "press_url": NY_URL, "listing_pages": 3}
def ny_page(n, extra=""):
    """A page of the real listing's shape: many long release headlines."""
    return extra + "".join(
        f'<h3><a href="/news/governor-hochul-announces-initiative-number-{n}{k}-'
        f'for-new-yorkers">Governor Hochul Announces Statewide Initiative '
        f'Number {n}{k} for New Yorkers</a></h3>' for k in range(6))
page2 = ny_page(2, f'<h3><a href="{PIRO}">Governor Hochul Honors Retired '
                    f'Sergeant Michael L. Piro</a></h3>')
calls.clear()
on(date(2026, 9, 16))
R.fetch = stub({NY_URL: ny_page(1),
                f"{NY_URL}?page=1": page2,
                f"{NY_URL}?page=2": ny_page(3),
                "https://ny.test" + PIRO: "<p>Governor Hochul today directed that "
                "flags be flown at half-staff from sunrise to sunset on Friday, "
                "September 18, 2026.</p>"})
_, o, c_ny = R.check_state(NYREC, {}, None)
t("listing pages are capped at 3", o.get("listing_pages_read"), 3)
t("no page beyond the cap is requested", f"{NY_URL}?page=3" in calls, False)
calls.clear()
_, o1p, _ = R.check_state(dict(NYREC, listing_pages=1), {}, None)
t("listing_pages=1 asks for exactly one page (NY's setting)",
  (o1p.get("listing_pages_read"), f"{NY_URL}?page=1" in calls), (1, False))
t("an order for Friday is not in effect on Wednesday", o["state_status"], P.FULL)
on(date(2026, 9, 18))
_, o, c_ny = R.check_state(NYREC, {"TT": c_ny}, None)
t("an order found on page 2 is used on its day", o["state_status"], P.HALF)
on(date(2026, 9, 19))
_, o, _ = R.check_state(NYREC, {"TT": c_ny}, None)
t("...and expires on its own date", o["state_status"], P.FULL)

R.fetch = stub({NY_URL: page2})     # listing loads, the article 403s
on(date(2026, 9, 16))
_, o, _ = R.check_state(NYREC, {}, None)
t("order we can see but cannot open -> unknown, never full staff",
  (o["state_status"], "could not be read" in (o["error"] or "")), (P.UNKNOWN, True))

print("\n--- Cloudflare 403: stale for a few days, then an honest gap ---")
R.fetch = stub({})                  # everything 403s
_, o, _ = R.check_state(NYREC, {}, None)
t("403 with nothing cached -> not covered, no answer",
  (o["state_status"], o["coverage"]), (P.UNKNOWN, "not_covered"))
cached = {"hash": "h", "state_status": P.FULL, "state_order": None,
          "last_parsed": "2026-09-15T12:00:00+00:00",
          "source": R.source_sig(NYREC, NY_URL)}
_, o, c2 = R.check_state(NYREC, {"TT": cached}, None)
t("403 one day after a good read -> stale, last value served",
  (o["state_status"], o["coverage"]), (P.FULL, "stale"))
t("failures are counted", c2["consecutive_errors"], 1)
on(date(2026, 9, 22))
_, o, _ = R.check_state(NYREC, {"TT": dict(cached, consecutive_errors=40)}, None)
t("a week of 403s is a coverage gap, not 'full staff'",
  (o["state_status"], o["coverage"]), (P.UNKNOWN, "not_covered"))
t("...and says how long it has been dark",
  "unreadable since 2026-09-15" in (o["error"] or ""), True)

print("\n--- 'from sunrise until sunset on <date>' is one day ---")
t("single-day window", P.date_range(
    "ordered that flags be flown at half-staff at the State Capitol from sunrise "
    "until sunset on Sunday, October 4, 2026."), (date(2026, 10, 4), date(2026, 10, 4)))
t("a real range is unchanged", P.date_range(
    "Flags at half-staff from August 3, 2026 through August 7, 2026."),
  (date(2026, 8, 3), date(2026, 8, 7)))

print("\n--- rendered pages: Montana and South Dakota cards, Oklahoma visible text ---")
MT_CARDS = """<div class="articles-wrapper">
<div class="p-3 article rounded"><div class="category"><a href="https://news.mt.gov/Governors-Office/index">Governor's Office</a></div>
<div class="articleBody"><h2 class="title"><a class="stretched-link" href="https://news.mt.gov/Governors-Office/Governor-Gianforte-Joins-Montanans-to-Remember-911">Governor Gianforte Joins Montanans to Remember 9/11</a></h2>
<span class="date small text-muted">9/11/2026</span><p class="summary">BOZEMAN, Mont. - Governor Greg Gianforte today joined veterans to remember September 11, 2001.</p></div></div><hr>
<div class="p-3 article rounded"><div class="articleBody"><h2 class="title"><a href="https://news.mt.gov/Governors-Office/Governor-Gianforte-Honors-Montana-Veterans">Governor Gianforte Honors Montana Veterans</a></h2>
<span class="date">9/10/2026</span><p class="summary">BELGRADE, Mont. - Governor Greg Gianforte recognized eleven veterans.</p></div></div></div>"""
mt = P.parse_cards(MT_CARDS, "https://news.mt.gov/Governors-Office")
t("Montana: one card per release", [c["title"] for c in mt],
  ["Governor Gianforte Joins Montanans to Remember 9/11",
   "Governor Gianforte Honors Montana Veterans"])
t("Montana: date read from the card, not from '2001' in the summary",
  mt[0]["date"], "2026-09-11")
t("Montana: link is the headline's", mt[0]["url"],
  "https://news.mt.gov/Governors-Office/Governor-Gianforte-Joins-Montanans-to-Remember-911")

def sd_card(title, day, summary, n):
    return (f'<div class="newsitem"><h2>{title}</h2><div class="newsdate"><span '
            f'id="rptGovNews_lblDate_{n}">{day}</span></div><div class="newsarticle">'
            f'{summary} <a class="nobreak" href="https://news.sd.gov/news?id=kb&amp;'
            f'sys_id={n}">Read more . . . </a></div></div>')
SD_CARDS = "".join([
    sd_card("Flags at Half-Staff at State Capitol in Honor of Lars Herseth", "09/14/2026",
            "Today, Governor Larry Rhoden ordered that flags be flown at half-staff at "
            "the State Capitol from sunrise until sunset on Sunday, October 4, 2026.", 0),
    sd_card("Patriots and Unity", "09/11/2026", "25 years ago, the United States...", 1),
    sd_card("Flags at Half-Staff Statewide in Honor of Patriot Day", "09/10/2026",
            "Today, Governor Larry Rhoden ordered that flags be flown at half-staff "
            "statewide from sunrise until sunset on Friday, September 11, 2026.", 2),
    sd_card("Fairing Well", "09/04/2026", "Summer is winding down.", 3),
    sd_card("Gov. Rhoden Receives Disaster Declaration", "09/02/2026",
            "Gov. Larry Rhoden announced a declaration.", 4),
])
sd = P.parse_cards(SD_CARDS, "https://governor.sd.gov/news/press-releases.aspx")
t("South Dakota: title is not a link, 'Read more' is, entities unescaped",
  sd[2]["url"], "https://news.sd.gov/news?id=kb&sys_id=2")
t("South Dakota: flag cards found", [c["title"] for c in sd if c["is_flag"]],
  ["Flags at Half-Staff at State Capitol in Honor of Lars Herseth",
   "Flags at Half-Staff Statewide in Honor of Patriot Day"])

import shutil
from datetime import timedelta as _td
_today = date.today()
_rdir, _real_rdir = tempfile.mkdtemp(), R.RENDER_DIR
R.RENDER_DIR = _rdir
def snapshot(code, url, html=None, text=None, age_h=0.5, error=None, good=True):
    from datetime import datetime as _dt, timezone as _tz
    at = (_dt.now(_tz.utc) - _td(hours=age_h)).isoformat(timespec="seconds")
    snap = {"url": url, "attempted_at": at, "error": error}
    if good:
        snap.update(rendered_at=at, html=html, text=text)
    with open(os.path.join(_rdir, f"{code}.json"), "w") as f:
        _json.dump(snap, f)

SD_URL = "https://governor.sd.gov/news/press-releases.aspx"
sdrec = {"state": "Testland", "state_code": "TT", "ingest_mode": "cards", "render": True,
         "buildable": True, "press_url": SD_URL}
R.fetch = stub({})                  # nothing may be fetched: summaries carry the dates
snapshot("TT", SD_URL, html=SD_CARDS)
on(date(2026, 9, 11))
_, o, _ = R.check_state(sdrec, {}, None)
t("SD statewide Patriot Day order: half on Sept 11", o["state_status"], P.HALF)
t("...dated from the card summary, no article opened", calls.get(
    "https://news.sd.gov/news?id=kb&sys_id=2"), None)
on(date(2026, 9, 20))
_, o, _ = R.check_state(sdrec, {}, None)
t("SD Sept 20: full, and the Oct 4 Capitol-only order does not count",
  o["state_status"], P.FULL)
on(date(2026, 10, 4))
_, o, _ = R.check_state(sdrec, {}, None)
t("SD Oct 4: Capitol-only order is not a statewide half-staff day",
  (o["state_status"], len(o.get("limited_orders") or [])), (P.FULL, 1))
t("'checked' is when the browser read the page", o["checked_at"] == o["rendered_at"], True)

OK_URL = "https://oklahoma.gov/governor.html"
okrec = {"state": "Testland", "state_code": "TT", "ingest_mode": "diff", "render": True,
         "render_text": "visible", "buildable": True, "flag_page_url": OK_URL}
recent = _today.strftime("%m/%d/%Y")
on(_today)
snapshot("TT", OK_URL, html="<div class='flag-status'>Flag Status : Half Staff</div>"
         "<a href='/governor/flag-status-full.html'>Flag Status: Full-Staff</a>",
         text=f"Governor J. Kevin Stitt\nFlag Status: Full-Staff\nNews {recent}")
_, o, _ = R.check_state(okrec, {}, None)
t("Oklahoma: only the VISIBLE label is read (hidden half widget ignored)",
  o["state_status"], P.FULL)
snapshot("TT", OK_URL, text=f"Flag Status : Half Staff\nFlag Status: Full-Staff\nNews {recent}")
_, o, _ = R.check_state(okrec, {}, None)
t("Oklahoma: both labels visible (script did not run) -> unknown",
  (o["state_status"], "both" in (o["error"] or "")), (P.UNKNOWN, True))

snapshot("TT", SD_URL, html=SD_CARDS, age_h=1)
os.unlink(os.path.join(_rdir, "TT.json"))
_, o, _ = R.check_state(sdrec, {}, None)
t("browser step never ran -> not covered, says why",
  (o["coverage"], "no rendered snapshot" in o["error"]), ("not_covered", True))
snapshot("TT", SD_URL, html=SD_CARDS, age_h=13)
same_source = {"hash": "h", "state_status": P.FULL, "last_parsed": _today.isoformat(),
               "source": R.source_sig(sdrec, SD_URL)}
_, o, _ = R.check_state(sdrec, {"TT": same_source}, None)
t("snapshot 13h old -> stale, not a fresh answer",
  (o["coverage"], "13h old" in o["error"]), ("stale", True))
old_source = dict(same_source, source="index|" + SD_URL + "|fetch")
_, o, _ = R.check_state(sdrec, {"TT": old_source}, None)
t("an answer cached from a DIFFERENT source is never carried (SD's nav-page read)",
  (o["state_status"], o["coverage"]), (P.UNKNOWN, "not_covered"))
snapshot("TT", SD_URL, age_h=0.2, error="TimeoutError: page.goto", good=False)
_, o, _ = R.check_state(sdrec, {}, None)
t("failed render with no good snapshot -> error names the failure",
  "TimeoutError" in (o["error"] or ""), True)

import render_fetch as RF
RF.OUT_DIR = _rdir
mtrec = {"state_code": "TT", "ingest_mode": "cards", "render": True,
         "press_url": "https://news.mt.gov/Governors-Office"}
snapshot("TT", mtrec["press_url"], html=MT_CARDS, age_h=1)
t("render cache: a 1h-old snapshot is reused, no browser", RF.due(mtrec), None)
snapshot("TT", mtrec["press_url"], html=MT_CARDS, age_h=4)
t("render cache: a 4h-old snapshot is re-rendered", bool(RF.due(mtrec)), True)
t("render cache: per-state max age respected",
  RF.due(dict(mtrec, render_max_age_hours=6)), None)
snapshot("TT", "https://other.test/", html=MT_CARDS, age_h=0.1)
t("render cache: URL change forces a render", RF.due(mtrec), "URL changed")
R.RENDER_DIR = _real_rdir
shutil.rmtree(_rdir, ignore_errors=True)

print("\n--- daily cross-check (an alarm, never an input) ---")
import cross_check as X
from datetime import datetime as _dtm, timezone as _tzn
_now = _dtm(2026, 9, 16, 22, 0, tzinfo=_tzn.utc)
MAST_FULL = ("<h1>Should my flag be at half-staff?</h1><p>Full-staff</p><p>No active "
             "half-staff order applies to Texas.</p><p>Last checked Sep 16, 2026, 9:18 PM "
             "UTC.</p><a>Why?</a>")
MAST_HALF = ("Should my flag be at half-staff? Half-staff Governor orders flags lowered "
             "in honor of a fallen officer. Last checked Sep 16, 2026, 9:40 PM UTC.")
t("their page: full", X.read_theirs(MAST_FULL, _now)[0], P.FULL)
t("their page: half, with the reason", X.read_theirs(MAST_HALF, _now)[:2],
  (P.HALF, "Governor orders flags lowered in honor of a fallen officer."))
t("their page without an answer is unreadable, not 'full'",
  X.read_theirs("<p>Site maintenance</p>", _now)[0], None)
t("their stale data is unreadable, not compared",
  X.read_theirs(MAST_FULL, _now + _td(days=2))[3].startswith("their data is stale"), True)

nd_ours = {"state": "North Dakota", "effective_status": P.HALF, "reason_source": "state",
           "reason": "Armstrong directs flags flown at half-staff Friday",
           "source_url": "https://www.governor.nd.gov/rss/news",
           "checked_at": "2026-09-15T12:00:00+00:00", "coverage": "covered"}
mast_full = {"status": P.FULL, "detail": "No active half-staff order applies to North Dakota.",
             "checked": _now, "url": "https://www.mast.today/nd"}
d = X.compare(nd_ours, mast_full)
t("North Dakota, Sept 15: we say half, they say full -> flagged", d,
  {"kind": "conflict", "ours": P.HALF, "theirs": P.FULL})
body = X.issue_body("ND", nd_ours, {"generated_at": "2026-09-15T12:00:00+00:00"},
                    mast_full, d, mast_full["url"])
t("issue names both answers and both source URLs",
  all(s in body for s in ("**half**", "**full**", "https://www.governor.nd.gov/rss/news",
                          "https://www.mast.today/nd")), True)
t("issue title names the state and both answers",
  X.issue_title("ND", "North Dakota", d),
  "Cross-check: North Dakota (ND) - we say half, Mast says full")
t("we have no answer, they say half -> flagged as a missed order",
  X.compare({"effective_status": P.UNKNOWN}, {"status": P.HALF})["kind"], "missed order")
t("we have no answer, they say full -> not flagged (claims nothing wrong)",
  X.compare({"effective_status": P.UNKNOWN}, {"status": P.FULL}), None)
t("agreement -> nothing", X.compare({"effective_status": P.FULL}, {"status": P.FULL}), None)
issues = [
    {"number": 7, "title": "Cross-check: North Dakota (ND) - we say half, Mast says full",
     "labels": []},                                     # label silently dropped
    {"number": 8, "title": "Improve the footer", "labels": []},
]
t("an open issue is found by title even when GitHub dropped its label",
  {k: v["number"] for k, v in X.existing_by_state(issues).items()},
  {"Cross-check: North Dakota (ND)": 7})
t("so tomorrow's run comments on #7 instead of opening a duplicate",
  X.issue_key(X.issue_title("ND", "North Dakota", {"ours": P.HALF, "theirs": P.FULL}))
  in X.existing_by_state(issues), True)
t("...even if the answers have changed since the issue was opened",
  X.issue_key(X.issue_title("ND", "North Dakota", {"ours": P.UNKNOWN, "theirs": P.HALF}))
  in X.existing_by_state(issues), True)
t("the cross-check source never writes the site's data",
  any(w in open("cross_check.py", encoding="utf-8").read()
      for w in ('open(args.status, "w"', "open(STATUS, \"w\"", "json.dump(")), False)

print("\n--- stale half-staff pages: no recent order behind the claim ---")
t("dates next to half-staff wording are order dates",
  P.order_dates("<p>Flags at Half-Staff in Honor of City Commissioner Chris Jones "
                "May 28, 2026</p>"), [date(2026, 5, 28)])
t("a page-chrome date beside the widget is not an order date",
  P.order_dates("Last updated: September 15, 2026 | Flag Status: Half-Staff"), [])
t("protocol boilerplate dates do not count",
  P.order_dates("The flag may be flown at half-staff upon the death of a president, "
                "per the law of June 22, 1942."), [])
t("Delaware/Texas/Pennsylvania-style widget: no dated order at all",
  P.order_dates("Flag Status - HALF STAFF  About the Governor  Contact"), [])

DE_URL = "https://de.test/flag-status"
derec = {"state": "Testland", "state_code": "TT", "ingest_mode": "diff",
         "buildable": True, "flag_page_url": DE_URL}
fresh = f"Page reviewed {_today:%B %d, %Y}."   # keeps the freshness alarm quiet
on(_today)
R.fetch = stub({DE_URL: f"<p>Flag Status - HALF STAFF</p><p>{fresh}</p>"})
_, o, _ = R.check_state(derec, {}, None)
t("widget says half, page shows no order -> withheld as stale, not half",
  (o["state_status"], bool(o.get("stale_half_claim")),
   "no dated order on the page" in (o["error"] or "")), (P.UNKNOWN, True, True))
old_order = (_today - _td(days=60)).strftime("%B %d, %Y").replace(" 0", " ")
R.fetch = stub({DE_URL: f"<p>Flag Status - HALF STAFF</p><p>Flags at half-staff in honor "
                        f"of a trooper, ordered {old_order}.</p><p>{fresh}</p>"})
_, o, _ = R.check_state(derec, {}, None)
t("...and when the newest order on the page is 60 days old",
  (o["state_status"], (o.get("stale_half_claim") or {}).get("newest_order_date")),
  (P.UNKNOWN, (_today - _td(days=60)).isoformat()))
new_order = (_today - _td(days=2)).strftime("%B %d, %Y").replace(" 0", " ")
R.fetch = stub({DE_URL: f"<p>Flag Status - HALF STAFF</p><p>Governor directs flags to "
                        f"half-staff in honor of a trooper, {new_order}.</p><p>{fresh}</p>"})
_, o, _ = R.check_state(derec, {}, None)
t("an order dated 2 days ago backs the claim -> half",
  (o["state_status"], o.get("stale_half_claim")), (P.HALF, None))
R.fetch = stub({DE_URL: f"<p>Flag Status - FULL STAFF</p><p>{fresh}</p>"})
_, o, _ = R.check_state(derec, {}, None)
t("a full-staff widget needs no order behind it", o["state_status"], P.FULL)

sc = X.stale_claim({"stale_half_claim": {"newest_order_date": None, "limit_days": 30}})
t("cross-check files stale half-staff pages even though no other source can disagree",
  sc["kind"], "stale page")
t("stale-page issue title", X.issue_title("DE", "Delaware", sc),
  "Cross-check: Delaware (DE) - page says half-staff with no recent order")
sbody = X.issue_body("DE", {"state": "Delaware", "source_url": DE_URL,
                            "error": "page declares half-staff but shows no order"},
                     {"generated_at": "x"}, {}, sc, "https://www.mast.today/de")
t("stale-page issue names both URLs", DE_URL in sbody and "mast.today/de" in sbody, True)

st_mem = {"states": {"ND": {"state": "North Dakota", "effective_status": P.FULL}}}
X.drill(st_mem, "nd")
t("drill flips the answer in memory", st_mem["states"]["ND"]["effective_status"], P.HALF)
dd = X.compare(st_mem["states"]["ND"], {"status": P.FULL})
dtitle = X.issue_title("ND", "North Dakota", dd, is_drill=True)
t("drill issue is marked in the title", dtitle.startswith("[DRILL] Cross-check: "), True)
t("a drill issue can never be mistaken for a real thread (dedupe ignores it)",
  X.existing_by_state([{"number": 1, "title": dtitle}]), {})
t("drill body says the live site was not changed",
  "were not changed" in X.issue_body("ND", st_mem["states"]["ND"], {"generated_at": "x"},
                                     {"status": P.FULL, "checked": _now}, dd, "u"), True)

print("\n--- freshness and future dates ---")
from datetime import timedelta as _td
_today = date.today()
_d = lambda n: (_today + _td(days=n)).strftime("%B %d, %Y").replace(" 0", " ")
t("a far-future date (fiscal year ends in 2099) is not an update date",
  P.page_last_modified(f"<p>Posted {_d(-45)}. The fiscal year ends September 30, 2099.</p>"),
  _today - _td(days=45))
t("an upcoming date 10 days out means the page was written about now (Ohio)",
  P.page_last_modified(f"<p>Proclamation declaring a day of service on {_d(10)}. "
                       f"Policy adopted September 11, 2001.</p>"), _today)

R.fetch, R.today = _real_fetch, _real_today

print(f"\n{'='*52}\n  {ok} passed, {bad} failed\n{'='*52}\n")
raise SystemExit(1 if bad else 0)
