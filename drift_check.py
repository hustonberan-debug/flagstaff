#!/usr/bin/env python3
"""
drift_check.py — the weekly alarm: sources that have gone quiet.

    python3 drift_check.py --dry-run

A source that stops updating looks exactly like a state with no orders. Both
are silence. Arizona's half-staff page sat frozen from January 2025 still
announcing the Jimmy Carter order, and South Carolina was being read from a
feed whose newest item was from January 2020 — neither was caught by anything
automatic. Both were found by hand, months later.

So once a week, three kinds of silence are reported as GitHub issues, one per
state, in the same shape as the daily cross-check:

  PAGE     its text has not changed in 90 days (we watched it not change)
  DATES    the newest date on the page or feed is over 90 days old
  CHANNEL  its notification channel has not delivered in 90 days

Reads cache.json, status.json and email-orders.json. Writes nothing the site
serves; the workflow running it has read-only repository permissions.

The PAGE signal needs history: fingerprints have only been timestamped since
2026-09-17, so it reports "watched since" until 90 days of history exist.
DATES works immediately, which is what would have caught Arizona and South
Carolina on day one.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

import run as R
from gh_issues import GitHub, file_all, summary

QUIET_DAYS = 90
TITLE_PREFIX = "Drift: "


# Three outcomes, not two. "Nobody ever wrote a date down", "somebody wrote
# something we cannot read" and "it was N days ago" are different facts, and
# collapsing the first two into None meant a channel that had NEVER delivered
# was silently skipped by every drift check - the one source most worth
# flagging read as the one least in need of attention.
ABSENT, UNREADABLE = "absent", "unreadable"


def age_of(stamp, today):
    """(kind, days) where kind is ABSENT, UNREADABLE, or "ok"."""
    if not stamp:
        return ABSENT, None
    try:
        return "ok", (today - date.fromisoformat(str(stamp)[:10])).days
    except ValueError:
        return UNREADABLE, None


def days_since(stamp, today):
    """Days since `stamp`, or None when there is no readable one. Callers that
    need to tell the two apart use age_of()."""
    return age_of(stamp, today)[1]


def find_drift(registry, cache, status, mail, today, quiet_days=QUIET_DAYS):
    """[{code, kind, days, detail, ...}] for every source that has gone quiet."""
    out = []
    states = status.get("states", {})
    heard = {**(mail.get("channels_seen") or {}), **(mail.get("channels_heard") or {})}
    for rec in registry:
        code = rec["state_code"]
        s = states.get(code) or {}
        entry = cache.get(code) or {}
        if rec.get("ingest_mode") == "email":
            # Silence from a channel that has spoken before is "no order" -
            # until it has been silent so long that the channel itself is the
            # more likely explanation.
            kind, n = age_of(heard.get(code), today)
            if kind == ABSENT:
                # Never delivered anything. run.py reports this state as
                # "subscription pending"; the drift job used to skip it
                # entirely, which is why a channel that never worked could
                # sit unnoticed for as long as one that stopped working.
                out.append({"code": code, "state": rec.get("state", code),
                            "kind": "NEVER", "days": 0,
                            "detail": "this channel has never delivered anything - "
                                      "the subscription may not have been confirmed",
                            "source": R.channel_url(rec) or "(channel)"})
            elif kind == UNREADABLE:
                out.append({"code": code, "state": rec.get("state", code),
                            "kind": "BAD STAMP", "days": 0,
                            "detail": f"last-heard date {heard.get(code)!r} cannot be "
                                      f"read, so we cannot tell if this channel is alive",
                            "source": R.channel_url(rec) or "(channel)"})
            elif n >= quiet_days:
                out.append({"code": code, "state": rec.get("state", code),
                            "kind": "CHANNEL", "days": n,
                            "detail": f"last delivered {heard.get(code)}",
                            "source": R.channel_url(rec) or "(channel)"})
            continue
        if not rec.get("buildable"):
            continue                       # already a declared gap
        url = R.pick_url(rec)
        stamp = entry.get("hash_changed_at")
        kind, n = age_of(stamp, today)
        if kind == UNREADABLE:
            out.append({"code": code, "state": rec.get("state", code),
                        "kind": "BAD STAMP", "days": 0,
                        "detail": f"page-change date {stamp!r} cannot be read",
                        "source": url})
        if kind == ABSENT and entry.get("last_parsed"):
            # We have checked this page but have no record of it ever
            # changing. That is not the same as "it changed recently", which
            # is how a missing stamp used to read - the source could have
            # been frozen the whole time and nothing would have said so.
            out.append({"code": code, "state": rec.get("state", code),
                        "kind": "NO RECORD", "days": 0,
                        "detail": "no record of this page ever changing - "
                                  "cannot tell a quiet source from a fresh one",
                        "source": url})
        if n is not None and n >= quiet_days:
            out.append({"code": code, "state": rec.get("state", code),
                        "kind": "PAGE", "days": n,
                        "detail": f"text unchanged since {str(entry['hash_changed_at'])[:10]}",
                        "source": url})
        # Old dates only mean drift when we have NOT seen the page change.
        # Florida's flag page is alive - its widget flipped the day this was
        # written - but it lists six memos a year, so its newest dated order
        # is routinely months old. That is a quiet state, not a dead source.
        age = s.get("source_age_days")
        if isinstance(age, int) and age >= quiet_days and (n is None or n >= quiet_days):
            # n is None here means we do not know when the page last moved,
            # so this alarm says "old dates and no change history", not
            # "old dates on a page we know is frozen".
            out.append({"code": code, "state": rec.get("state", code),
                        "kind": "DATES", "days": age,
                        "detail": (f"newest date on the source is "
                                   f"{s.get('source_last_modified') or 'unknown'}"
                                   + ("" if n is not None
                                      else "; no record of the page changing")),
                        "source": url})
    return sorted(out, key=lambda d: (-d["days"], d["code"]))


def issue_title(d):
    what = {"PAGE": "page has not changed", "DATES": "source dates are old",
            "CHANNEL": "channel has gone quiet",
            "NEVER": "channel has never delivered",
            "NO RECORD": "no record of this page ever changing",
            "BAD STAMP": "a date in our own records cannot be read",
            }[d["kind"]]
    return f"{TITLE_PREFIX}{d['state']} ({d['code']}) - {what}"


def issue_body(d, status, quiet_days=QUIET_DAYS):
    s = (status.get("states") or {}).get(d["code"]) or {}
    why = {
        "PAGE": "We have watched this page not change for "
                f"{d['days']} days. A page that stops being updated reports the "
                "day it froze, and looks exactly like a state with no orders.",
        "DATES": f"The newest date anywhere on this source is {d['days']} days old. "
                 "Arizona's page sat frozen for a year this way, still announcing a "
                 "January 2025 order; South Carolina's feed had not published since "
                 "2020.",
        "CHANNEL": f"This state is covered by an email channel that has not "
                   f"delivered anything for {d['days']} days. Its silence is being "
                   "read as 'no order' - but a dead subscription is silent too.",
        "NEVER": "This state is covered by an email channel that has never "
                 "delivered anything at all. Until it does, the subscription is "
                 "unproven: a signup that was never confirmed, a confirmation that "
                 "went to spam and a working list with nothing to say all look the "
                 "same from here. Check the inbox, or re-subscribe.",
        "NO RECORD": "We have been checking this page but have never recorded it "
                     "changing, so we cannot tell a live source from one that was "
                     "already frozen when we started watching. This resolves itself "
                     "the first time the page moves.",
        "BAD STAMP": "One of our own recorded dates cannot be parsed. This is a bug "
                     "in what we wrote, not in the source - but while it stands, the "
                     "drift check cannot tell whether this source is alive.",
    }[d["kind"]]
    return "\n".join([
        f"**{d['state']} ({d['code']})** - {d['kind'].lower()} drift, {d['days']} days "
        f"(limit {quiet_days}).",
        "",
        why,
        "",
        f"- Source: {d['source']}",
        f"- Detail: {d['detail']}",
        f"- What the site says now: **{s.get('effective_status', 'unknown')}** "
        f"(coverage: {s.get('coverage', 'unknown')})",
        f"- status.json generated: {status.get('generated_at')}",
        "",
        "Check whether the source still publishes there. If it moved, update "
        "registry.json; if it is dead, record that in blocked_reason so the state "
        "shows an honest gap rather than a stale answer.",
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today", help="ISO date to treat as today")
    args = ap.parse_args()

    today = (date.fromisoformat(args.today) if args.today
             else datetime.now(timezone.utc).date())
    registry = R.load_json(R.REGISTRY, [])
    cache = R.load_json(R.CACHE, {})
    status = R.load_json(R.OUTPUT, {})
    mail = R.load_json(R.EMAIL_ORDERS, {})
    failures = []
    if not registry or not status.get("states"):
        failures.append("registry.json or status.json unreadable - could not check")
    if not cache:
        failures.append("cache.json unreadable - page fingerprints could not be checked")

    drift = find_drift(registry, cache, status, mail, today)
    tracked = sum(1 for c, e in cache.items()
                  if isinstance(e, dict) and e.get("hash_changed_at"))
    quiet = [d for d in drift if d["kind"] in ("PAGE", "DATES", "CHANNEL")]
    unknown = [d for d in drift if d not in quiet]
    print(f"{len(quiet)} source(s) quiet for {QUIET_DAYS}+ days, "
          f"{len(unknown)} we cannot tell about "
          f"({tracked} pages have fingerprint history)")
    for d in drift:
        print(f"  {d['kind']:8} {d['code']}  {d['days']:4}d  {d['detail']}")

    items = [(issue_title(d), issue_body(d, status)) for d in drift]
    gh = None if args.dry_run else GitHub(os.environ["GITHUB_REPOSITORY"],
                                          os.environ["GITHUB_TOKEN"])
    filed, errs = file_all(gh, items, TITLE_PREFIX, args.dry_run,
                           recur_word="Still quiet, still")
    failures += errs
    for f in filed:
        print(f"  {f}")

    summary([f"## Weekly drift check",
             f"- quiet sources: {len(drift)}; pages with fingerprint history: {tracked}"]
            + [f"- **{d['code']}** {d['kind']}: {d['days']}d - {d['detail']}" for d in drift]
            + [f"- **FAILED:** {f}" for f in failures])
    for f in failures:
        print(f"FAIL: {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
