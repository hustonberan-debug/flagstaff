"""canary.py - once a day, look at the site the way a visitor does.

The pipeline can be green while the site is broken, and three times it was,
each found by hand hours later:
  - the VAPID keys in config.js were wiped, so nobody could subscribe
  - git conflict markers were committed into status.json, so it did not parse
  - the service worker kept serving a stale build to everyone who had visited
Every check here reads what is PUBLISHED - halfstaffnow.com, not the repo -
and one of them drives a real browser as a first-time visitor, service
worker and all. Any failure opens (or updates) one GitHub issue; a clean run
closes it.

    python3 canary.py            # check, and file/close the issue in CI
    python3 canary.py --dry-run  # check and print only
    python3 canary.py --no-browser
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SITE = os.environ.get("CANARY_SITE", "https://halfstaffnow.com").rstrip("/")
REPO = os.environ.get("GITHUB_REPOSITORY", "hustonberan-debug/flagstaff")
SITE_FILES = ["index.html", "sw.js", "config.js", "manifest.json"]

STATUS_MAX_AGE = timedelta(hours=2)      # the pipeline publishes every 30 minutes
EXPECTED_STATES = 51                     # 50 states and DC
MIN_COVERED = 35                         # 43 today; the pipeline refuses below 20
STAMP_GRACE = timedelta(minutes=45)      # a site change is stamped on the next run
PROBE_STATE = "OH"                       # the state the browser check picks

ISSUE_TITLE = "Canary: halfstaffnow.com is broken for visitors"
UA = "halfstaffnow-canary/1.0 (+https://halfstaffnow.com)"


def get(url, **kw):
    # Cache-busting query, so we see what is published now, not a CDN copy.
    sep = "&" if "?" in url else "?"
    return requests.get(f"{url}{sep}canary={int(time.time())}",
                        headers={"User-Agent": UA}, timeout=30, **kw)


# --- Checks on published files. Each returns a list of problems. -------------

def check_status(text, now):
    """status.json: parses, is fresh, covers the states it should."""
    if re.search(r"^(<<<<<<<|=======|>>>>>>>)", text or "", re.M):
        return ["status.json contains git conflict markers"]
    try:
        s = json.loads(text)
    except ValueError as e:
        return [f"status.json does not parse ({e})"]
    problems = []
    gen = s.get("generated_at")
    try:
        age = now - datetime.fromisoformat(gen)
        if age > STATUS_MAX_AGE:
            problems.append(f"status.json is {age.total_seconds() / 3600:.1f}h old "
                            f"(generated {gen}) - the pipeline has stopped publishing")
    except (TypeError, ValueError):
        problems.append(f"status.json has no readable generated_at ({gen!r})")
    if s.get("_sample"):
        problems.append("status.json is the sample payload")
    n = len(s.get("states") or {})
    if n != EXPECTED_STATES:
        problems.append(f"status.json has {n} states, expected {EXPECTED_STATES}")
    covered = (s.get("meta") or {}).get("covered")
    if not isinstance(covered, int) or covered < MIN_COVERED:
        problems.append(f"only {covered} states covered (expected at least {MIN_COVERED})")
    return problems


def vapid_key_from_config(text):
    m = re.search(r"VAPID_PUBLIC_KEY\s*:\s*['\"]([^'\"]*)['\"]", text or "")
    return m.group(1).strip() if m else None


def valid_vapid_key(key):
    """A P-256 public key: 65 bytes, uncompressed (0x04), base64url."""
    if not key:
        return False
    try:
        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    except (ValueError, TypeError):
        return False
    return len(raw) == 65 and raw[0] == 4


def check_config(text):
    """config.js: present, with a real push key and an endpoint."""
    problems = []
    key = vapid_key_from_config(text)
    if not key:
        problems.append("config.js has no VAPID_PUBLIC_KEY - nobody can subscribe to alerts")
    elif not valid_vapid_key(key):
        problems.append(f"config.js VAPID_PUBLIC_KEY is not a valid P-256 public key ({key[:12]}...)")
    if not re.search(r"SUBSCRIBE_ENDPOINT\s*:\s*['\"]https://[^'\"]+/subscribe['\"]", text or ""):
        problems.append("config.js has no SUBSCRIBE_ENDPOINT")
    return problems


def endpoint_from_config(text):
    m = re.search(r"SUBSCRIBE_ENDPOINT\s*:\s*['\"](https://[^'\"]+)/subscribe['\"]", text or "")
    return m.group(1) if m else None


def check_worker_key(worker_key, config_key):
    """The Worker answers, and holds the SAME key the site hands to browsers.
    A mismatch makes every new subscription fail at the push service."""
    if not worker_key:
        return ["the Worker did not return a VAPID key from /vapid-public-key"]
    if config_key and worker_key != config_key:
        return ["config.js and the Worker hold DIFFERENT VAPID keys - new subscriptions "
                "will be rejected by the push service"]
    return []


def check_version(stamp, latest, now):
    """version.json names the latest commit that touched the site. `latest` is
    (sha, committed_at) from GitHub; a fresh change gets STAMP_GRACE."""
    if not latest:
        return ["could not look up the latest site commit on GitHub"]
    sha, when = latest
    got = (stamp or {}).get("site_commit")
    if got == sha:
        return []
    if now - when < STAMP_GRACE:
        return []                        # the next pipeline run will stamp it
    return [f"the published site version is {str(got or '')[:7] or 'missing'}, but the latest "
            f"site commit is {sha[:7]} ({when:%Y-%m-%d %H:%M} UTC)"]


def latest_site_commit(token=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    newest = None
    for path in SITE_FILES:
        r = requests.get(f"https://api.github.com/repos/{REPO}/commits",
                         params={"path": path, "sha": "main", "per_page": 1},
                         headers=headers, timeout=30)
        r.raise_for_status()
        c = (r.json() or [None])[0]
        if c and (newest is None or c["commit"]["committer"]["date"] > newest[1]):
            newest = (c["sha"], c["commit"]["committer"]["date"])
    if not newest:
        return None
    return newest[0], datetime.fromisoformat(newest[1].replace("Z", "+00:00"))


VERDICTS = {"half": {"half-staff", "half until noon"}, "full": {"full staff"},
            "unknown": {"unclear", "not covered"}}


def expected_verdicts(status, code):
    """What the page must show for this state, from the published data."""
    if status.get("federal"):
        return VERDICTS["half"]
    st = (status.get("states") or {}).get(code) or {}
    return VERDICTS.get(st.get("effective_status"), set())


# --- The browser ------------------------------------------------------------

def check_browser(status, latest_sha):
    """A first-time visitor in a real browser, then a second visit through the
    service worker. Returns a list of problems."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ["the browser check could not run: Playwright is not installed"]
    problems, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()              # no storage: a first visit
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        try:
            page.goto(SITE + "/", wait_until="load", timeout=45000)
            page.wait_for_function(
                "document.getElementById('verdict') && "
                "document.getElementById('verdict').textContent.trim() !== 'Loading'",
                timeout=20000)
        except Exception as e:
            browser.close()
            return [f"the page did not render an answer ({str(e)[:150]})"]

        text = page.inner_text("main")
        if "Sample data." in text:
            problems.append("the page is showing the SAMPLE DATA banner - status.json "
                            "did not load for a visitor")
        first = page.inner_text("#verdict").strip().lower()
        if first != "choose your state" and not status.get("federal"):
            problems.append(f"a first-time visitor was shown an answer for a state they did "
                            f"not choose ({first!r})")

        def probe(label):
            want = expected_verdicts(status, PROBE_STATE)
            got = page.inner_text("#verdict").strip().lower()
            name = page.inner_text("#stateName").strip().lower()
            if want and got not in want:
                problems.append(f"{label}: the page shows {got!r} for {PROBE_STATE}, but the "
                                f"published data says {sorted(want)}")
            if name != ((status.get("states") or {}).get(PROBE_STATE) or {}).get("state", "").lower():
                problems.append(f"{label}: the page names {name!r}, not the state chosen")
            if latest_sha:
                try:
                    page.wait_for_function(
                        "document.getElementById('buildStamp').textContent.length > 0",
                        timeout=10000)
                except Exception:
                    pass
                foot = page.inner_text("#buildStamp")
                if latest_sha[:7] not in foot:
                    problems.append(f"{label}: the footer shows {foot.strip()!r}, not the latest "
                                    f"site version {latest_sha[:7]} - a stale build is being served")

        # Every step below reports a problem rather than raising: a canary
        # that crashes files no issue. (It did - on the sample-data page,
        # which has no Ohio to choose.)
        try:
            page.select_option("#state", PROBE_STATE, timeout=5000)
        except Exception:
            problems.append(f"a visitor cannot choose {PROBE_STATE} - it is missing from the "
                            f"state list")
            browser.close()
            if errors:
                problems.append(f"JavaScript errors on the page: {'; '.join(errors[:3])}")
            return problems
        page.wait_for_timeout(500)
        probe("first visit")

        # The service worker must register, and a SECOND visit - served
        # through it - must still be the current build. This is the check
        # that would have caught the stale-build bug.
        ready = page.evaluate(
            "() => !!navigator.serviceWorker && Promise.race(["
            "navigator.serviceWorker.ready.then(() => true),"
            "new Promise(r => setTimeout(() => r(false), 15000))])")
        if not ready:
            problems.append("the service worker did not register within 15 seconds - "
                            "alerts cannot work for anyone")
        page.reload(wait_until="load", timeout=45000)
        try:
            page.wait_for_function(
                "document.getElementById('verdict').textContent.trim() !== 'Loading'",
                timeout=20000)
            controlled = page.evaluate("!!(navigator.serviceWorker && navigator.serviceWorker.controller)")
            if not controlled:
                problems.append("the second visit was not served through the service worker")
            probe("second visit (through the service worker)")
        except Exception as e:
            problems.append(f"the second visit did not render an answer ({str(e)[:150]})")
        browser.close()
    if errors:
        problems.append(f"JavaScript errors on the page: {'; '.join(errors[:3])}")
    return problems


# --- Run ----------------------------------------------------------------------

def run_checks(browser=True, token=None, now=None):
    """[(check name, [problems])]."""
    now = now or datetime.now(timezone.utc)
    results, status, config, latest = [], {}, "", None

    try:
        r = get(SITE + "/")
        home = [] if r.ok and "id=\"verdict\"" in r.text else [
            f"halfstaffnow.com returned HTTP {r.status_code}" if not r.ok
            else "halfstaffnow.com loaded, but it is not the Half Staff Now page"]
    except requests.RequestException as e:
        home = [f"halfstaffnow.com did not load ({e})"]
    results.append(("site loads", home))

    try:
        r = get(SITE + "/status.json")
        probs = check_status(r.text, now) if r.ok else [f"status.json HTTP {r.status_code}"]
        if r.ok and not probs:
            status = r.json()
    except requests.RequestException as e:
        probs = [f"status.json did not load ({e})"]
    results.append(("status.json", probs))

    try:
        r = get(SITE + "/config.js")
        config = r.text if r.ok else ""
        probs = check_config(config) if r.ok else [f"config.js HTTP {r.status_code}"]
    except requests.RequestException as e:
        probs = [f"config.js did not load ({e})"]
    results.append(("config.js", probs))

    base = endpoint_from_config(config)
    try:
        if not base:
            probs = ["no Worker address in config.js to check"]
        else:
            r = requests.get(base + "/vapid-public-key", headers={"User-Agent": UA}, timeout=30)
            key = r.json().get("key") if r.ok else None
            probs = ([f"the Worker answered HTTP {r.status_code}"] if not r.ok else
                     check_worker_key(key, vapid_key_from_config(config)))
    except (requests.RequestException, ValueError) as e:
        probs = [f"the Worker did not answer ({e})"]
    results.append(("push Worker", probs))

    try:
        latest = latest_site_commit(token)
        r = get(SITE + "/version.json")
        stamp = r.json() if r.ok else None
        probs = check_version(stamp, latest, now)
    except (requests.RequestException, ValueError) as e:
        probs = [f"could not compare the site version ({e})"]
    results.append(("site version", probs))

    if browser:
        if not status:
            probs = ["skipped: status.json is broken, see above"]
        else:
            try:
                probs = check_browser(status, latest[0] if latest else None)
            except Exception as e:           # never let the canary itself crash
                probs = [f"the browser check itself failed: {type(e).__name__}: {str(e)[:200]}"]
        results.append(("a visitor's browser", probs))
    return results


def report(results, now):
    lines = [f"Checked {now:%Y-%m-%d %H:%M} UTC, as a visitor sees {SITE}.", ""]
    for name, probs in results:
        lines.append(f"- {'**FAIL**' if probs else 'ok'} - {name}")
        lines += [f"  - {p}" for p in probs]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    token = os.environ.get("GITHUB_TOKEN")
    results = run_checks(browser=not args.no_browser, token=token, now=now)
    failed = [n for n, p in results if p]
    body = report(results, now)
    print(body)
    if args.dry_run or not token:
        return 1 if failed else 0

    from gh_issues import GitHub
    gh = GitHub(REPO, token)
    gh.ensure_label()
    open_one = next((i for i in gh.open_issues() if i.get("title") == ISSUE_TITLE), None)
    if failed:
        text = ("The daily canary found the published site broken in a way the "
                "pipeline did not notice (it can be green while visitors see "
                "something wrong).\n\n" + body)
        if open_one:
            gh.comment(open_one["number"], f"Still failing.\n\n{body}")
            print(f"commented on #{open_one['number']}")
        else:
            i = gh.create(ISSUE_TITLE, text)
            print(f"opened #{i['number']}")
        return 1
    if open_one:
        gh.close(open_one["number"], f"**Recovered.** Every check passes.\n\n{body}")
        print(f"closed #{open_one['number']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
