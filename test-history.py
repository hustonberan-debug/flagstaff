#!/usr/bin/env python3
"""
test-history.py — replay history.jsonl against two invariants.

    python3 test-history.py                 # self-test, then replay history
    python3 test-history.py --now 2026-09-16T00:00:00+00:00

  1. WINDOW  A state is never shown half-staff outside the stated window of
             the order it cites, give or take a day (UTC vs. US time zones,
             and a 30-minute polling cadence).
  2. FLAP    A state never records more than two status changes in one UTC
             day. Every history row was a push notification to subscribers.

Both rules exist because the worst bugs in this project looked healthy.
North Dakota showed half-staff from Sept 9 to Sept 15 2026 for a one-day
order; Nevada logged 479 "changes" in a month, each one a notification. The
pipeline ran green throughout.

History rows written before BASELINE were produced by the old change
tracking. Their violations are printed as the historical record; only
violations at or after BASELINE fail the run.

Rows record the order's window since BASELINE. For older rows the window is
derived from the order title with the pipeline's own parsers — explicit
dates, weekday names ("half-staff Friday"), and statutory days by name. An
order whose title states no window cannot be checked by rule 1, and the
count of those is printed so it is clear how much the rule cannot see.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

import parsers as P

HISTORY = "history.jsonl"
CALENDAR = "statutory-calendar.json"
BASELINE = "2026-09-15T21:26:00+00:00"   # first run with answer-change tracking
TOLERANCE = timedelta(days=1)
MAX_CHANGES_PER_DAY = 2


def ts(s):
    return datetime.fromisoformat(s)


def day_start(d):
    return datetime.combine(d, time(0), tzinfo=timezone.utc)


def statutory_names(path=CALENDAR):
    try:
        years = json.load(open(path)).get("years", {})
    except (OSError, ValueError):
        return {}
    out = {}
    for y, obs in years.items():
        for o in obs:
            if o.get("active") and o.get("name"):
                out[(y, o["name"].lower())] = date.fromisoformat(o["date"])
    return out


def stated_window(row, statutory):
    """(start, end, how) for the order a row cites, or (None, None, why)."""
    if row.get("end_date"):
        return (date.fromisoformat(row["start_date"]) if row.get("start_date")
                else None, date.fromisoformat(row["end_date"]), "recorded")
    reason = row.get("reason") or ""
    when = ts(row["at"]).date()
    hit = statutory.get((str(when.year), reason.lower()))
    if hit:
        return hit, hit, "statutory day"
    s, e = P.date_range(reason)
    if e:
        return s, e, "dates in title"
    ws, we = P.weekday_window(reason, when)
    if we:
        return ws, we, "weekday in title"
    return None, None, "no window stated" if reason else "no reason recorded"


def half_periods(rows, now):
    """Per state: [{state, reason, start, end, first_row}] for each unbroken
    run of half-staff citing the same reason."""
    by_state = defaultdict(list)
    for r in rows:
        by_state[r["state"]].append(r)
    out = []
    for state, rs in by_state.items():
        rs.sort(key=lambda r: r["at"])
        cur = None
        for r in rs:
            if r["status"] == P.HALF:
                if cur and cur["reason"] == (r.get("reason") or ""):
                    continue                     # repeat row, same order
                if cur:
                    cur["end"] = ts(r["at"])
                    out.append(cur)
                cur = {"state": state, "reason": r.get("reason") or "",
                       "start": ts(r["at"]), "end": None, "first_row": r}
            elif cur:
                cur["end"] = ts(r["at"])
                out.append(cur)
                cur = None
        if cur:
            cur["end"] = now
            cur["ongoing"] = True
            out.append(cur)
    return out


NATIONAL_MIN_STATES = 10


def legacy_national_reasons(rows, base):
    """Reasons that were national orders in rows written before BASELINE.

    The old logger wrote a row only when a state's own page changed. When a
    national order ended and every state dropped back to full, nothing was
    logged — so its history shows those states "still half" for days they
    were actually shown full. Checked against status.json snapshots: after
    Patriot Day 2026, 10 of 17 such cases were this artifact and 7 were
    real. History alone cannot tell them apart, so they are not judged.
    A reason cited by many states on the same day was a national order.
    """
    states = defaultdict(set)
    for r in rows:
        if r["status"] == P.HALF and r.get("reason") and ts(r["at"]) < base:
            states[(r["reason"], ts(r["at"]).date())].add(r["state"])
    return {reason for (reason, _), s in states.items()
            if len(s) >= NATIONAL_MIN_STATES}


def window_violations(rows, now, statutory, base=None):
    found, unverifiable, national = [], 0, 0
    legacy_national = legacy_national_reasons(rows, base) if base else set()
    for p in half_periods(rows, now):
        if p["start"] < (base or p["start"]) and p["reason"] in legacy_national:
            national += 1
            continue
        ws, we, how = stated_window(p["first_row"], statutory)
        if not we:
            unverifiable += 1
            continue
        allowed_from = day_start(ws or p["start"].date()) - TOLERANCE
        allowed_to = day_start(we + timedelta(days=1)) + TOLERANCE
        if p["end"] > allowed_to:
            found.append(dict(p, kind="late", window=(ws, we), how=how,
                              over=p["end"] - day_start(we + timedelta(days=1)),
                              at=allowed_to))
        if ws and p["start"] < allowed_from:
            found.append(dict(p, kind="early", window=(ws, we), how=how,
                              over=day_start(ws) - p["start"], at=p["start"]))
    return found, unverifiable, national


def flap_violations(rows):
    per_day = defaultdict(list)
    for r in rows:
        per_day[(r["state"], ts(r["at"]).date())].append(r)
    return sorted(((s, d, len(rs)) for (s, d), rs in per_day.items()
                   if len(rs) > MAX_CHANGES_PER_DAY), key=lambda x: (x[1], x[0]))


def fmt_td(td):
    h = td.total_seconds() / 3600
    return f"{h / 24:.1f}d" if h >= 24 else f"{h:.0f}h"


# ---------------------------------------------------------------------------
# Self-test: the checker must catch the two bugs it exists for.
# ---------------------------------------------------------------------------

def self_test():
    ok = bad = 0

    def t(label, got, want):
        nonlocal ok, bad
        if got == want:
            ok += 1
        else:
            bad += 1
            print(f"  FAIL  {label}\n        want {want!r}\n        got  {got!r}")

    stat = {("2026", "patriot day"): date(2026, 9, 11)}
    nd = [{"at": "2026-09-09T21:04:11+00:00", "state": "ND", "status": "half",
           "reason": "Armstrong directs flags flown at half-staff Friday in memory "
                     "of 9/11 victims"},
          {"at": "2026-09-15T18:20:55+00:00", "state": "ND", "status": "full",
           "reason": None}]
    v, _, _ = window_violations(nd, ts("2026-09-16T00:00:00+00:00"), stat)
    t("North Dakota: late by days", [x["kind"] for x in v], ["late", "early"])
    t("North Dakota: window read from 'Friday'", v[0]["window"],
      (date(2026, 9, 11), date(2026, 9, 11)))

    good = [{"at": "2026-09-11T04:00:00+00:00", "state": "NE", "status": "half",
             "reason": "Patriot Day"},
            {"at": "2026-09-12T05:00:00+00:00", "state": "NE", "status": "full",
             "reason": None}]
    t("a correct one-day order passes",
      window_violations(good, ts("2026-09-16T00:00:00+00:00"), stat)[0], [])
    t("an order that starts on its own day is not early",
      window_violations(good[:1] + [dict(good[1], at="2026-09-11T23:00:00+00:00")],
                        ts("2026-09-16T00:00:00+00:00"), stat)[0], [])

    stuck = [dict(good[0])]
    t("still half with no row ending it counts as ongoing",
      [x["kind"] for x in window_violations(
          stuck, ts("2026-09-15T00:00:00+00:00"), stat)[0]], ["late"])

    nv = [{"at": f"2026-09-01T{h:02d}:00:00+00:00", "state": "NV",
           "status": "half", "reason": None} for h in (5, 10, 15, 21, 23)]
    t("Nevada: five notifications in a day", flap_violations(nv),
      [("NV", date(2026, 9, 1), 5)])
    t("Memorial Day (down at dawn, up at noon) is two changes, allowed",
      flap_violations(nv[:2]), [])

    undated = [{"at": "2026-09-01T00:00:00+00:00", "state": "FL", "status": "half",
                "reason": "State order"}]
    t("an order with no stated window is counted, not guessed",
      window_violations(undated, ts("2026-09-20T00:00:00+00:00"), stat), ([], 1, 0))

    # Before BASELINE, a national order's end was never logged.
    old = [{"at": "2026-09-11T01:00:00+00:00", "state": s, "status": "half",
            "reason": "Patriot Day"} for s in "AL AK AZ AR CA CO CT DE FL GA".split()]
    base = ts("2026-09-15T21:26:00+00:00")
    t("legacy national order with no logged end is not judged",
      window_violations(old, ts("2026-09-16T00:00:00+00:00"), stat, base), ([], 0, 10))
    new = [dict(r, at="2026-09-20T01:00:00+00:00") for r in old]
    stat2 = {("2026", "patriot day"): date(2026, 9, 20)}
    t("...but the same pattern after BASELINE is judged",
      len(window_violations(new, ts("2026-09-25T00:00:00+00:00"), stat2, base)[0]), 10)

    rec = [{"at": "2026-09-20T12:00:00+00:00", "state": "TX", "status": "half",
            "reason": "Governor orders flags", "start_date": "2026-09-20",
            "end_date": "2026-09-22"}]
    t("a recorded window is used as-is",
      window_violations(rec, ts("2026-09-23T12:00:00+00:00"), stat)[0], [])
    return ok, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="ISO time to treat as now (default: current)")
    ap.add_argument("--history", default=HISTORY)
    args = ap.parse_args()

    ok, bad = self_test()
    print(f"self-test: {ok} passed, {bad} failed")
    if bad:
        return 1

    now = ts(args.now) if args.now else datetime.now(timezone.utc)
    rows = [json.loads(l) for l in open(args.history) if l.strip()]
    base = ts(BASELINE)
    statutory = statutory_names()
    print(f"\nreplaying {len(rows)} rows, {rows[0]['at'][:10]} to {rows[-1]['at'][:10]}"
          f" (now = {now.isoformat(timespec='minutes')})")

    wins, unverifiable, national = window_violations(rows, now, statutory, base)
    flaps = flap_violations(rows)
    new_wins = [w for w in wins if w["at"] >= base]
    new_flaps = flap_violations([r for r in rows if ts(r["at"]) >= base])

    print(f"\nRULE 1 - half-staff outside the order's stated window: "
          f"{len(wins)} violation(s)")
    print(f"      not checkable: {unverifiable} period(s) cite no stated window; "
          f"{national} are pre-baseline national-order periods whose end was "
          f"never logged")
    for w in sorted(wins, key=lambda w: w["start"]):
        ws, we = w["window"]
        tag = "NEW " if w in new_wins else "    "
        span = f"{w['start']:%m-%d %H:%M} -> " + (
            "still half" if w.get("ongoing") else f"{w['end']:%m-%d %H:%M}")
        print(f"  {tag}{w['state']} {w['kind']:5} by {fmt_td(w['over']):>5}  "
              f"window {ws or '?'}..{we} ({w['how']})  shown {span}  "
              f"{w['reason'][:60]!r}")

    print(f"\nRULE 2 - more than {MAX_CHANGES_PER_DAY} changes in one day: "
          f"{len(flaps)} state-day(s)")
    by_state = defaultdict(list)
    for s, d, n in flaps:
        by_state[s].append((d, n))
    for s, days in sorted(by_state.items(), key=lambda x: -sum(n for _, n in x[1])):
        worst = max(days, key=lambda x: x[1])
        print(f"      {s}: {len(days)} day(s), {sum(n for _, n in days)} rows on those "
              f"days, worst {worst[0]} with {worst[1]}")

    if new_wins or new_flaps:
        print(f"\nFAIL: {len(new_wins)} window and {len(new_flaps)} flap violation(s) "
              f"since {BASELINE}")
        for s, d, n in new_flaps:
            print(f"  NEW {s} {d}: {n} changes")
        return 1
    print(f"\nOK: no violations since {BASELINE}; everything above is the "
          f"historical record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
