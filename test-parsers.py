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

print(f"\n{'='*52}\n  {ok} passed, {bad} failed\n{'='*52}\n")
raise SystemExit(1 if bad else 0)
