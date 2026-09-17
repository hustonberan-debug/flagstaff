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

STATUS = "status.json"
SOURCE_NAME = "Mast"
SOURCE_URL = "https://www.mast.today/{code}"
REQUEST_DELAY_S = 1.0
THEIR_MAX_AGE = timedelta(hours=12)
OUR_MAX_AGE = timedelta(hours=3)
MAX_UNREADABLE = 10
LABEL = "cross-check"
TITLE_PREFIX = "Cross-check: "

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


def our_source(state, status):
    if state.get("reason_source") == "federal":
        return (status.get("federal") or {}).get("source_url") or "statutory calendar"
    return state.get("source_url") or "(no source)"


def issue_title(code, name, d):
    return (f"{TITLE_PREFIX}{name} ({code}) - we say {d['ours']}, "
            f"{SOURCE_NAME} says {d['theirs']}")


def issue_body(code, state, status, theirs, d, their_url):
    ours_detail = (state.get("reason") or state.get("error")
                   or ("no order in effect" if d["ours"] == P.FULL else ""))
    lines = [
        f"**{state.get('state', code)} ({code})** - the daily cross-check found a "
        f"{'conflict' if d['kind'] == 'conflict' else 'possible missed order'}.",
        "",
        "| | Answer | Detail | Source |",
        "|---|---|---|---|",
        f"| **halfstaffnow.com** | **{d['ours']}** | {ours_detail} | {our_source(state, status)} |",
        f"| **{SOURCE_NAME}** | **{d['theirs']}** | {theirs.get('detail') or ''} | {their_url} |",
        "",
        f"- Our status.json generated: {status.get('generated_at')}",
        f"- Our state last checked: {state.get('checked_at')}"
        + (f" (coverage: {state.get('coverage')})" if state.get("coverage") != "covered" else ""),
        f"- {SOURCE_NAME} last checked: {theirs.get('checked'):%Y-%m-%d %H:%M} UTC",
        "",
        f"{SOURCE_NAME} is an alarm, not a source of truth, and never feeds the site. "
        "Check the official source above, then fix the pipeline or close this issue.",
    ]
    return "\n".join(lines)


class GitHub:
    def __init__(self, repo, token):
        self.base = f"https://api.github.com/repos/{repo}"
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})

    def _ok(self, r):
        if r.status_code >= 300:
            raise RuntimeError(f"GitHub API {r.request.method} {r.url}: "
                               f"HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def open_issues(self):
        """Every open issue (not just labelled ones - see existing_by_state)."""
        out, page = [], 1
        while True:
            batch = self._ok(self.s.get(f"{self.base}/issues", params={
                "state": "open", "per_page": 100, "page": page}))
            out += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                return out
            page += 1

    def ensure_label(self):
        r = self.s.post(f"{self.base}/labels", json={
            "name": LABEL, "color": "b60205",
            "description": "Daily cross-check disagreement"})
        if r.status_code not in (201, 422):      # 422: already exists
            print(f"    note: could not create label ({r.status_code}); "
                  f"issues are matched by title, so this is cosmetic")

    def create(self, title, body):
        return self._ok(self.s.post(f"{self.base}/issues", json={
            "title": title, "body": body, "labels": [LABEL]}))


def issue_key(title):
    """'Cross-check: North Dakota (ND)' - stable while the answers change."""
    return title.split(" - ")[0]


def existing_by_state(issues):
    """Open cross-check issues by key, matched on TITLE, not label.

    GitHub silently drops labels on issue creation when the caller lacks push
    access, and this job deliberately has none. Finding issues by label would
    then find nothing, and a disagreement that lasted a week would open seven
    issues instead of one with daily comments.
    """
    return {issue_key(i["title"]): i for i in issues
            if (i.get("title") or "").startswith(TITLE_PREFIX)}

    def comment(self, number, body):
        return self._ok(self.s.post(f"{self.base}/issues/{number}/comments",
                                    json={"body": body}))


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
    args = ap.parse_args()

    before = file_hash(args.status)
    with open(args.status, encoding="utf-8") as f:
        status = json.load(f)
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

    agree = sum(1 for c, t in theirs.items()
                if status["states"][c].get("effective_status") == t["status"])
    print(f"compared {len(theirs)} states: {agree} agree, {len(found)} disagree, "
          f"{len(theirs) - agree - len(found)} where we have no answer and they "
          f"report full, {len(unreadable)} unreadable")
    for code, why in sorted(unreadable.items()):
        print(f"  unreadable {code}: {why}")

    report = [f"## Daily cross-check against {SOURCE_NAME}",
              f"- compared: {len(theirs)}, agree: {agree}, disagree: {len(found)}, "
              f"unreadable: {len(unreadable)}"]
    gh = None
    if found and not args.dry_run:
        gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
        gh.ensure_label()
        existing = existing_by_state(gh.open_issues())
    for code, d in sorted(found.items()):
        s, t = status["states"][code], theirs[code]
        title = issue_title(code, s.get("state", code), d)
        body = issue_body(code, s, status, t, d, t["url"])
        print(f"  DISAGREE {code}: we say {d['ours']}, {SOURCE_NAME} says {d['theirs']} "
              f"({d['kind']})")
        report.append(f"- **{code}**: we say {d['ours']}, {SOURCE_NAME} says {d['theirs']}")
        if args.dry_run:
            continue
        key = issue_key(title)
        try:
            if key in existing:
                gh.comment(existing[key]["number"],
                           f"Still disagreeing on {now:%Y-%m-%d}.\n\n{body}")
                print(f"    commented on #{existing[key]['number']}")
            else:
                i = gh.create(title, body)
                print(f"    opened #{i['number']}")
        except Exception as e:
            failures.append(f"could not file the {code} disagreement: {e}")

    summary(report + [f"- **FAILED:** {f}" for f in failures])

    if file_hash(args.status) != before:
        failures.append("status.json changed during the cross-check - this job must never write it")
    for f in failures:
        print(f"FAIL: {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
