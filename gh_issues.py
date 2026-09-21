#!/usr/bin/env python3
"""
gh_issues.py — filing alarms as GitHub issues.

Shared by cross_check.py (daily) and drift_check.py (weekly) so both file in
the same shape: one issue per state, a daily/weekly comment while the problem
lasts, and no duplicates.
"""

import os
import re
from datetime import datetime

import requests

LABEL = "cross-check"


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
            "description": "Automatic check found something"})
        if r.status_code not in (201, 422):      # 422: already exists
            print(f"    note: could not create label ({r.status_code}); "
                  f"issues are matched by title, so this is cosmetic")

    def create(self, title, body):
        return self._ok(self.s.post(f"{self.base}/issues", json={
            "title": title, "body": body, "labels": [LABEL]}))

    def comment(self, number, body):
        return self._ok(self.s.post(f"{self.base}/issues/{number}/comments",
                                    json={"body": body}))

    def close(self, number, body):
        """Say what changed, then close. An alarm that cannot stand itself
        down leaves a wall of stale issues nobody reads."""
        self.comment(number, body)
        return self._ok(self.s.patch(f"{self.base}/issues/{number}",
                                     json={"state": "closed"}))


def state_code(title):
    """'Cross-check: Iowa (IA) - ...' -> 'IA'."""
    m = re.search(r"\(([A-Z]{2})\)", title or "")
    return m.group(1) if m else None


def days_open(issue, now):
    created = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
    return (now - created).days


def issue_key(title):
    """'Drift: Nebraska (NE)' - stable while the details change."""
    return title.split(" - ")[0]


def existing_by_state(issues, prefix):
    """Open issues of one kind, by key, matched on TITLE, not label.

    GitHub silently drops labels on issue creation when the caller lacks push
    access, and these jobs deliberately have none. Matching by label would
    find nothing, and a problem lasting a month would open a fresh issue
    every run instead of one with comments.
    """
    return {issue_key(i["title"]): i for i in issues
            if (i.get("title") or "").startswith(prefix)}


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def file_all(gh, items, prefix, dry_run, recur_word="Still"):
    """Open or comment on one issue per item [(key_title, body)]. Returns
    (filed, failures)."""
    failures = []
    if not items:
        return [], failures
    filed = []
    existing = {}
    if not dry_run:
        gh.ensure_label()
        existing = existing_by_state(gh.open_issues(), prefix)
    for title, body in items:
        if dry_run:
            continue
        key = issue_key(title)
        try:
            if key in existing:
                gh.comment(existing[key]["number"], f"{recur_word} flagged.\n\n{body}")
                filed.append(f"commented on #{existing[key]['number']}")
            else:
                i = gh.create(title, body)
                filed.append(f"opened #{i['number']}: {i['html_url']}")
        except Exception as e:
            failures.append(f"could not file {key!r}: {e}")
    return filed, failures
