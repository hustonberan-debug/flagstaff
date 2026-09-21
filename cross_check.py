#!/usr/bin/env python3
"""
cross_check.py — the daily alarm. Compares status.json with an independent
source and opens a GitHub issue for every state where they disagree.

    python3 cross_check.py --dry-run     # compare and print; touch nothing

WHY
  Every serious bug in this project looked healthy: green checks, clean
  output, plausible numbers. North Dakota showed half-staff for six days and
  nothing flagged it. This asks a second, independently built source the same
  question once a day, so a wrong answer reaches a human in a day instead of
  whenever someone happens to notice.

WHAT IT MUST NEVER DO
  Feed the product. The independent source is an alarm, not an input: this
  script only READS status.json, never writes it or anything the site
  serves, and checks at the end that status.json is byte-identical. The
  workflow that runs it has read-only repository permissions, so it could
  not push a change even by mistake.

WHAT COUNTS AS A DISAGREEMENT
  - both give an answer (half/full) and the answers differ, or
  - the independent source says half-staff and we do not (a missed order,
    even in a state where we report a known gap)
  "We don't know, they say full" is not reported: it claims nothing wrong.

WHAT IT REFUSES TO DO
  Say "all agree" when it could not check. If the independent source cannot
  be read for most states, or its data is stale, or status.json itself is
  stale, the job fails (red, emailed) rather than passing quietly.

SOURCE
  Mast (https://www.mast.today/) — per-state pages that open with a plain
  answer, "Should my flag be at half-staff? Full-staff", and a "Last checked"
  time. robots.txt allows all paths. One request per state, spaced out, once a
  day.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import parsers as P
import run as R
from gh_issues import (GitHub, LABEL, days_open, existing_by_state as _existing_by_state,
                       issue_key, state_code, summary)

STATUS = "status.json"
SOURCE_NAME = "Mast"
SOURCE_URL = "https://www.mast.today/{code}"
REQUEST_DELAY_S = 1.0
THEIR_MAX_AGE = timedelta(hours=12)
OUR_MAX_AGE = timedelta(hours=3)
MAX_UNREADABLE = 10
TITLE_PREFIX = "Cross-check: "
# A disagreement still open after this long is either our bug or a source we
# cannot read. Either way it needs a person, so the daily comment says so.
ESCALATE_AFTER_DAYS = 3

ANSWER_RE = re.compile(r"Should my flag be at half-staff\?\s*(half|full)[-\s]?staff\b"
                       r"(.{0,240})", re.I)
CHECKED_RE = re.compile(r"Last checked\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4},\s+"
                        r"\d{1,2}:\d{2}\s+[AP]M)\s+UTC", re.I)


def read_theirs(html, now):
    """(status, detail, checked_at, problem) from one Mast state page."""
    text = P.strip_html(html or "")
    m = ANSWER_RE.search(text)
    if not m:
        return None, None, None, "no answer found on page"
    status = m.group(1).lower()
    detail = re.split(r"\s+(?:Last checked|Why\?)", m.group(2).strip())[0][:200]
    c = CHECKED_RE.search(text)
    if not c:
        return None, detail, None, "no 'Last checked' time on page"
    checked = datetime.strptime(" ".join(c.group(1).split()), "%b %d, %Y, %I:%M %p"
                                ).replace(tzinfo=timezone.utc)
    if now - checked > THEIR_MAX_AGE:
        return None, detail, checked, f"their data is stale (last checked {checked:%Y-%m-%d %H:%M} UTC)"
    return status, detail, checked, None


def compare(ours, theirs):
    """Disagreement dict, or None. ours: a status.json state; theirs: dict."""
    o = ours.get("effective_status")
    t = theirs.get("status")
    if t not in (P.HALF, P.FULL):
        return None
    if o in (P.HALF, P.FULL) and o != t:
        kind = "conflict"
    elif t == P.HALF and o != P.HALF:
        kind = "missed order"            # they see an order, we have no answer
    else:
        return None
    return {"kind": kind, "ours": o, "theirs": t}


def stale_claim(state):
    """A disagreement-shaped record for a state whose OWN page says half-staff
    with no recent order behind it. The pipeline already withholds that answer;
    this makes sure a human hears about it, because a page like that fools
    every reader of it, independent sources included."""
    s = state.get("stale_half_claim")
    if not s:
        return None
    return {"kind": "stale page", "ours": "withheld (page says half)",
            "theirs": None, "newest": s.get("newest_order_date"),
            "limit": s.get("limit_days")}


def drill(status, code):
    """Flip one state's answer IN MEMORY, to prove the job files a real issue.
    status.json on disk is never touched; main() verifies that."""
    code = (code or "").upper()
    if code not in status["states"]:
        raise SystemExit(f"--drill: unknown state {code!r}")
    s = status["states"][code]
    flipped = P.FULL if s.get("effective_status") == P.HALF else P.HALF
    s.update(effective_status=flipped, reason_source="state",
             reason=f"[DRILL] answer flipped in memory to {flipped}; the real "
                    f"status.json still says {s.get('effective_status')}")
    s["_drill"] = True
    return code


def our_source(state, status):
    if state.get("reason_source") == "federal":
        return (status.get("federal") or {}).get("source_url") or "statutory calendar"
    return state.get("source_url") or "(no source)"


DRILL_PREFIX = "[DRILL] "


def issue_title(code, name, d, is_drill=False):
    if d.get("kind") == "stale page":
        tail = "page says half-staff with no recent order"
    else:
        tail = f"we say {d['ours']}, {SOURCE_NAME} says {d['theirs']}"
    return f"{DRILL_PREFIX if is_drill else ''}{TITLE_PREFIX}{name} ({code}) - {tail}"


def order_line(state):
    """The order behind our answer: title, window, and where it came from.
    This is the fact that decides who is right, so it goes first."""
    o = state.get("state_order") or {}
    title = o.get("title") or o.get("evidence") or state.get("reason")
    start, end = o.get("start_date"), o.get("end_date") or o.get("date")
    if start or end:
        window = f"{start or '?'} to {end or 'no stated end'}"
    else:
        window = "no dated window"
    return title, window, o.get("coverage_reason")


def issue_body(code, state, status, theirs, d, their_url, issue=None, now=None):
    theirs = theirs or {}
    ours_detail = (state.get("reason") or state.get("error")
                   or ("no order in effect" if d["ours"] == P.FULL else ""))
    what = {"conflict": "a conflict", "missed order": "a possible missed order",
            "stale page": "a stale half-staff page"}[d["kind"]]
    title, window, why = order_line(state)
    lines = []
    if state.get("_drill"):
        lines += ["> **DRILL.** This issue was filed on purpose to prove the "
                  "cross-check can file one. Our answer below was flipped in the "
                  "job's memory only; status.json and the live site were not "
                  "changed. Close this issue.", ""]
    if issue is not None and now is not None:
        age = days_open(issue, now)
        if age >= ESCALATE_AFTER_DAYS:
            lines += [f"> **Open {age} days.** A disagreement that lasts this long "
                      f"is one of two things: our pipeline is wrong about "
                      f"{state.get('state', code)}, or its source has become "
                      f"unreadable and we are serving an answer nobody can verify. "
                      f"Both need a person - decide from the evidence below and "
                      f"either fix the pipeline or record the gap in registry.json.",
                      ""]
    # Evidence first: everything needed to decide who is right.
    lines += [
        f"**{state.get('state', code)} ({code})** - {what}, "
        f"{'today' if not now else f'{now:%Y-%m-%d}'}.",
        "",
        f"- **We say {d['ours']}** - {ours_detail or 'no detail'}",
        f"  - order: {title or 'none'}",
        f"  - window: **{window}**" + (f" ({why})" if why else ""),
        f"  - read from: {our_source(state, status)}",
        f"- **{SOURCE_NAME} says {theirs.get('status') or 'unreadable'}** - "
        f"{theirs.get('detail') or 'no detail'}",
        f"  - read from: {their_url}",
        "",
    ]
    if d["kind"] == "stale page":
        lines += [f"Its own status page declares half-staff but shows no order dated "
                  f"in the last {d['limit']} days (newest: "
                  f"{d['newest'] or 'none on the page'}). The pipeline is withholding "
                  "that answer. Every reader of the page would repeat it, so the "
                  "other source is not proof either way - ask the governor's office.",
                  ""]
    lines += [
        f"- Our status.json generated: {status.get('generated_at')}",
        f"- Our state last checked: {state.get('checked_at')}"
        + (f" (coverage: {state.get('coverage')})" if state.get("coverage") != "covered" else ""),
        f"- {SOURCE_NAME} last checked: "
        + (f"{theirs['checked']:%Y-%m-%d %H:%M} UTC" if theirs.get("checked") else "n/a"),
        "",
        f"{SOURCE_NAME} is an alarm, not a source of truth, and never feeds the site. "
        "Check the official source above, then fix the pipeline or close this issue. "
        "This issue closes itself when the two agree again.",
    ]
    return "\n".join(lines)


def resolved_body(code, state, status, theirs, now):
    """What changed, for the comment that closes an issue."""
    title, window, _ = order_line(state)
    if not title:
        # Once an order expires it leaves state_order, so the order that just
        # ended - the thing that changed - is recorded separately.
        expired = state.get("last_expired_order") or {}
        title = expired.get("title")
        window = expired.get("why") or window
    t = (theirs or {}).get("status")
    ours = state.get("effective_status")
    lines = [
        f"**Resolved {now:%Y-%m-%d}** - we and {SOURCE_NAME} no longer disagree about "
        f"{state.get('state', code)}.",
        "",
        f"- **We say {ours}** - {state.get('reason') or state.get('error') or 'no order in effect'}",
        f"- **{SOURCE_NAME} says {t or 'no answer'}**",
        "",
    ]
    if ours == P.FULL and (title or window != "no dated window"):
        lines += [f"Our last order was {title or 'unnamed'}, window **{window}** - "
                  f"it has ended, so the state is back to full staff.", ""]
    elif ours not in (P.HALF, P.FULL):
        lines += [f"We now report no answer for this state "
                  f"(coverage: {state.get('coverage')}), so there is nothing to "
                  f"disagree about: {state.get('error') or ''}", ""]
    lines.append("Closing. It reopens as a new issue if they disagree again.")
    return "\n".join(lines)


def existing_by_state(issues, prefix=TITLE_PREFIX):
    """Open cross-check issues by key, matched on title. See gh_issues."""
    return _existing_by_state(issues, prefix)


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compare and print; open no issues")
    ap.add_argument("--status", default=STATUS)
    ap.add_argument("--drill", metavar="STATE",
                    help="flip this state's answer in memory to prove an issue gets filed")
    args = ap.parse_args()

    before = file_hash(args.status)
    with open(args.status, encoding="utf-8") as f:
        status = json.load(f)
    drilled = drill(status, args.drill) if args.drill else None
    if drilled:
        print(f"DRILL: {drilled} flipped in memory only; {args.status} on disk is untouched")
    now = datetime.now(timezone.utc)
    failures = []

    generated = datetime.fromisoformat(status["generated_at"])
    if now - generated > OUR_MAX_AGE:
        failures.append(f"our status.json is {(now - generated).total_seconds() / 3600:.1f}h "
                        f"old - the pipeline itself has stopped publishing")

    session = requests.Session()
    theirs, unreadable = {}, {}
    for code in sorted(status["states"]):
        url = SOURCE_URL.format(code=code.lower())
        if not R.robots_allows(url, session):
            unreadable[code] = "robots.txt disallows"
            continue
        html, err = R.fetch(url, session)
        if err:
            unreadable[code] = err
        else:
            st, detail, checked, problem = read_theirs(html, now)
            if problem:
                unreadable[code] = problem
            else:
                theirs[code] = {"status": st, "detail": detail, "checked": checked,
                                "url": url}
        time.sleep(REQUEST_DELAY_S)

    if len(unreadable) > MAX_UNREADABLE:
        failures.append(f"{SOURCE_NAME} unreadable for {len(unreadable)} states - "
                        f"could not check, which is not the same as agreeing")

    found = {}
    for code, t in theirs.items():
        d = compare(status["states"][code], t)
        if d:
            found[code] = d
    stale = {}
    for code, s in status["states"].items():
        d = stale_claim(s)
        if d and code not in found:
            stale[code] = d
    if drilled and drilled not in found:
        failures.append(f"drill: flipping {drilled} did not produce a disagreement "
                        f"({SOURCE_NAME} answer: {(theirs.get(drilled) or {}).get('status') or unreadable.get(drilled)})")

    agree = sum(1 for c, t in theirs.items()
                if status["states"][c].get("effective_status") == t["status"])
    print(f"compared {len(theirs)} states: {agree} agree, {len(found)} disagree, "
          f"{len(theirs) - agree - len(found)} where we have no answer and they "
          f"report full, {len(unreadable)} unreadable; {len(stale)} stale half-staff page(s)")
    for code, why in sorted(unreadable.items()):
        print(f"  unreadable {code}: {why}")

    report = [f"## Daily cross-check against {SOURCE_NAME}" + (" (DRILL)" if drilled else ""),
              f"- compared: {len(theirs)}, agree: {agree}, disagree: {len(found)}, "
              f"unreadable: {len(unreadable)}, stale half-staff pages: {len(stale)}"]
    gh, existing = None, {}
    if not args.dry_run:
        # Always read the open issues, even with nothing to report: that is
        # how the ones that have resolved get closed.
        gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
        gh.ensure_label()
        existing = existing_by_state(gh.open_issues())
    for code, d in sorted(list(found.items()) + list(stale.items())):
        s, t = status["states"][code], theirs.get(code) or {}
        their_url = t.get("url") or SOURCE_URL.format(code=code.lower())
        title = issue_title(code, s.get("state", code), d, is_drill=bool(s.get("_drill")))
        body = issue_body(code, s, status, t, d, their_url,
                          issue=existing.get(issue_key(title)), now=now)
        print(f"  {'STALE PAGE' if d['kind'] == 'stale page' else 'DISAGREE'} {code}: "
              f"we say {d['ours']}, {SOURCE_NAME} says {d['theirs'] or t.get('status')} "
              f"({d['kind']})")
        report.append(f"- **{code}** ({d['kind']}): we say {d['ours']}, "
                      f"{SOURCE_NAME} says {d['theirs'] or t.get('status')}")
        if args.dry_run:
            continue
        key = issue_key(title)
        try:
            if key in existing:
                gh.comment(existing[key]["number"],
                           f"Still flagged on {now:%Y-%m-%d}.\n\n{body}")
                print(f"    commented on #{existing[key]['number']}")
            else:
                i = gh.create(title, body)
                print(f"    opened #{i['number']}: {i['html_url']}")
                report.append(f"  - opened [#{i['number']}]({i['html_url']})")
        except Exception as e:
            failures.append(f"could not file the {code} issue: {e}")

    # Close what has resolved. Only for states we actually compared this run:
    # if the other source was unreadable, we do not know that it agrees.
    flagged = set(found) | set(stale)
    for key, issue in sorted(existing.items()):
        code = state_code(issue["title"])
        if not code or code in flagged or code not in theirs:
            continue
        s = status["states"].get(code)
        if not s:
            continue
        try:
            gh.close(issue["number"], resolved_body(code, s, status, theirs[code], now))
            print(f"  RESOLVED {code}: closed #{issue['number']}")
            report.append(f"- **{code}**: resolved, closed #{issue['number']}")
        except Exception as e:
            failures.append(f"could not close the {code} issue: {e}")

    summary(report + [f"- **FAILED:** {f}" for f in failures])

    if file_hash(args.status) != before:
        failures.append("status.json changed during the cross-check - this job must never write it")
    for f in failures:
        print(f"FAIL: {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
