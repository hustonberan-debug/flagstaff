#!/usr/bin/env python3
"""
read_email.py — ingest flag orders from official notification emails.

WHY THIS EXISTS
  Wyoming, Montana, New Hampshire, Illinois and Minnesota cannot be scraped.
  Their sites are single-page apps that serve nothing to a plain HTTP client,
  or they sit behind bot detection that we correctly refuse to defeat. But
  every one of them publishes flag orders through GovDelivery or a listserv.

  So we stop asking the website and read what the state actually sends. This
  is push, not poll: it arrives faster than any 30-minute cron, it cannot be
  bot-blocked, and it does not break when a site is redesigned. It is also the
  front door — we are a subscriber, exactly as intended.

SETUP (one time)
  1. Make a dedicated Gmail account, e.g. halfstaffnow.alerts@gmail.com.
     Do not use a personal inbox; this one is read by automation.
  2. Enable 2-factor auth on it, then create an App Password:
     myaccount.google.com -> Security -> 2-Step Verification -> App passwords
  3. Subscribe that address to each state's flag notification channel.
  4. Add two GitHub secrets: MAIL_USER and MAIL_PASS (the app password).

TRUST
  A bulletin is used only if (a) its From ADDRESS maps to a state (registry
  expected_sender_domain, SENDER_HINTS, or a GovDelivery account), and (b)
  the receiving server's own Authentication-Results header — the topmost one,
  written by mx.google.com — records dkim=pass for a domain aligned with that
  address. A From line is free text; without (b), anyone could email this
  inbox and set a state to half-staff. The inbox must therefore be Gmail, or
  MAIL_AUTHSERV_ID must name the server that writes that header.

USAGE
    python3 read_email.py            # read inbox, write email-orders.json
    python3 read_email.py --dry-run  # parse and print, write nothing
    python3 read_email.py --days 30  # look further back than the default 14

Writes email-orders.json, which run.py consumes for `email` mode states.
"""

import argparse
import email
import email.utils
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

import parsers as P

OUTPUT = "email-orders.json"
REGISTRY = "registry.json"
IMAP_HOST = os.environ.get("MAIL_HOST", "imap.gmail.com")
DEFAULT_DAYS = 14
MAX_BACKDATE_DAYS = 7   # an order date this far before the email is a citation, not a window
# Code -> name, for scope phrases like "flags in Kansas". Filled from the
# registry at startup; the fallback keeps parse_message usable in tests.
STATE_NAMES = {}
# The receiving server whose Authentication-Results verdict is trusted.
AUTHSERV_ID = os.environ.get("MAIL_AUTHSERV_ID", "mx.google.com")
UNAUTHENTICATED = "sender not authenticated"

# Sender domains that reliably belong to one state. Attribution by sender is
# far safer than guessing from body text, because a Wyoming bulletin can
# easily mention another state in passing.
SENDER_HINTS = {
    # Observed on real bulletins from this project's inbox. These cannot be
    # learned from a signup page — GovDelivery accounts configure their own
    # sending subdomains — so they are recorded only after a message arrived.
    "subscriptions.kentucky.gov": "KY",
    "govsubscriptions.michigan.gov": "MI",
    "wyo.gov": "WY", "wyoming.gov": "WY",
    "mt.gov": "MT", "montana.gov": "MT",
    "nh.gov": "NH",
    "illinois.gov": "IL",
    "state.mn.us": "MN", "mn.gov": "MN",
    "idaho.gov": "ID",
    "governor.ks.gov": "KS", "ks.gov": "KS",
    "state.ma.us": "MA", "mass.gov": "MA",
    "az.gov": "AZ",
    "oregon.gov": "OR",
    "maryland.gov": "MD",
    "michigan.gov": "MI",
    "ky.gov": "KY",
}


def load_previous():
    """The last email-orders.json, so channel history survives between runs."""
    try:
        return json.load(open(OUTPUT)) or {}
    except Exception:
        return {}


# Senders that are never a state bulletin. Google's security mail trips the
# flag regex on stray wording in its body and then clutters the unattributed
# report, which is where genuinely unmapped states need to be visible.
IGNORE_SENDERS = ("accounts.google.com", "no-reply@google.com",
                  "mail-noreply@google.com", "googlemail.com")


def decoded(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def body_text(msg):
    """Plain text of a message, preferring text/plain over stripped HTML."""
    if not msg.is_multipart():
        try:
            raw = msg.get_payload(decode=True) or b""
            text = raw.decode(msg.get_content_charset() or "utf-8", "replace")
        except Exception:
            text = str(msg.get_payload())
        return P.strip_html(text) if "<" in text else text

    plain, html = None, None
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_filename():
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html is None:
            html = text
    # Take whichever part actually carries the content, not whichever came
    # first. Many bulletins ship a stub text/plain part ("View this in your
    # browser") with the real order only in the HTML alternative — preferring
    # plain unconditionally silently threw the order away.
    plain = (plain or "").strip()
    html_text = P.strip_html(html or "").strip()
    return plain if len(plain) >= len(html_text) else html_text


# GovDelivery embeds the account slug in the sending subdomain and in its
# List-* headers: MOOA mail arrives from mooa.dmarc.public.govdelivery.com.
# Since the registry already records each state's account, that turns into a
# general attribution rule instead of a hand-maintained domain list.
GOVDELIVERY_ACCOUNTS = {}


def load_govdelivery_accounts(reg):
    """account slug -> state code, from notification_channel URLs."""
    for r in reg:
        nc = r.get("notification_channel") or {}
        detail = nc.get("detail") or ""
        m = re.search(r"/accounts/([A-Za-z0-9_-]+)/", detail)
        if m:
            GOVDELIVERY_ACCOUNTS[m.group(1).lower()] = r["state_code"]
    return GOVDELIVERY_ACCOUNTS


def sender_address(sender):
    """(local part, domain) of the From address, lowercased. The display name
    is ignored: "mt.gov <anyone@example.com>" is not a Montana address.

    The bracketed address is taken first: parseaddr gives up on a display
    name with an unquoted comma, and Missouri's is "Missouri OA - Facilities
    Management, Design & Construction <missourioa@...>" — its bulletins
    silently disappeared."""
    m = re.search(r"<\s*([^<>\s@]+@[^<>\s@]+)\s*>\s*$", sender or "")
    addr = (m.group(1) if m else email.utils.parseaddr(sender or "")[1]).lower()
    if "@" not in addr:
        return "", ""
    local, _, dom = addr.rpartition("@")
    return local, dom.strip(".")


def org_domain(d):
    """Organizational domain, for DMARC-style relaxed alignment.

    Approximates the public suffix list for the suffixes state mail uses:
    two labels (ks.gov, govdelivery.com), or three under a state .us suffix
    (state.mn.us, state.ma.us)."""
    labels = (d or "").lower().strip(".").split(".")
    if len(labels) >= 3 and labels[-1] == "us" and len(labels[-2]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def dkim_pass_domains(msg):
    """(domains, problem): domains with dkim=pass in the receiving server's
    own verdict, or a reason there is no usable verdict.

    Only the TOPMOST Authentication-Results header counts, and only if the
    receiving server wrote it (authserv-id mx.google.com). Gmail prepends its
    header on arrival; anything below it arrived with the message, and a
    forger can write "dkim=pass" into a header as easily as into a From line.
    """
    hdrs = msg.get_all("Authentication-Results") or []
    if not hdrs:
        return set(), "no Authentication-Results header"
    top = " ".join(str(hdrs[0]).split())
    if not re.match(re.escape(AUTHSERV_ID) + r"\s*;", top, re.I):
        return set(), f"top Authentication-Results is not from {AUTHSERV_ID}"
    out = set()
    for m in re.finditer(r"\bdkim\s*=\s*pass\b([^;]*)", top, re.I):
        props = m.group(1)
        d = (re.search(r"\bheader\.d\s*=\s*([a-z0-9.-]+)", props, re.I)
             or re.search(r"\bheader\.i\s*=\s*[^@\s;]*@([a-z0-9.-]+)", props, re.I))
        if d:
            out.add(d.group(1).lower().strip("."))
    return out, None if out else "no dkim=pass"


def dkim_aligned(msg, from_domain):
    """(True, domain) if a dkim=pass signature aligns with the From domain,
    else (False, why). Relaxed alignment: same organizational domain, so a
    list.ks.gov bulletin signed by ks.gov passes, and GovDelivery mail signed
    by govdelivery.com passes for public.govdelivery.com senders — which
    then leaves attribution to the GovDelivery account in the address,
    something only that account can send as."""
    passed, problem = dkim_pass_domains(msg)
    want = org_domain(from_domain)
    for d in passed:
        if org_domain(d) == want:
            return True, d
    return False, problem or f"dkim=pass only for {', '.join(sorted(passed))}, not {from_domain}"


def state_from(sender, allowed):
    """(code, how) or (None, reason), from the From ADDRESS alone.

    Matching used to search the whole From header plus List-*, Reply-To and
    Return-Path, all of which the sender writes — a display name of
    "wyo.gov" was enough. Only the address counts now, and only once
    authenticated_state has checked DKIM for its domain.
    """
    local, dom = sender_address(sender)
    if not dom:
        return None, "no sender address"
    for hint, code in SENDER_HINTS.items():
        if (dom == hint or dom.endswith("." + hint)) and (not allowed or code in allowed):
            return code, f"sender domain {hint}"

    # GovDelivery account slug, as the local part (WYGOV@public.govdelivery
    # .com) or the sending subdomain (mooa.dmarc.public.govdelivery.com).
    if org_domain(dom) == "govdelivery.com":
        for slug, code in GOVDELIVERY_ACCOUNTS.items():
            if slug and (local == slug or dom.split(".")[0] == slug) and \
                    (not allowed or code in allowed):
                return code, f"govdelivery account {slug.upper()}"

    # There used to be a fallback here: file the message under the one state
    # named in its subject. A subject is text anyone can write, so any email
    # reaching this inbox with "Illinois ... flags to half-staff" in the
    # subject would have set Illinois to half-staff on the live site. Unknown
    # senders now land in the unattributed report instead, where a human adds
    # the real sender domain to registry.json once.
    return None, "no state identified"


def sent_day(msg):
    try:
        return email.utils.parsedate_to_datetime(msg.get("Date")).date()
    except Exception:
        return None


def authenticated_state(msg, allowed):
    """(code, how) for a message from a known state channel whose sender
    domain passed DKIM, else (None, why). Flag-related or not: any message
    that passes is proof the channel is still reaching this inbox."""
    sender = decoded(msg.get("From"))
    code, how = state_from(sender, allowed)
    if not code:
        return None, how
    ok, detail = dkim_aligned(msg, sender_address(sender)[1])
    if not ok:
        return None, f"{UNAUTHENTICATED} for {code}: {detail}"
    return code, f"{how}, dkim=pass {detail}"


def parse_message(msg, allowed):
    subject = decoded(msg.get("Subject"))
    sender = decoded(msg.get("From"))
    low = sender.lower()
    if any(x in low for x in IGNORE_SENDERS):
        return None, "not flag-related"
    body = body_text(msg)
    blob = f"{subject}\n{body}"

    if not P.FLAG_RE.search(blob):
        return None, "not flag-related"

    code, how = authenticated_state(msg, allowed)
    if not code:
        return None, how

    # Status and authority from the subject where possible: headlines are
    # unambiguous by construction, bodies routinely mention both half and full.
    status, ev = P.classify_status(subject)
    if status == P.UNKNOWN:
        status, ev = P.classify_status(body[:600])
    if status != P.HALF:
        return None, f"no half-staff order detected ({status})"

    # Same rule as an order document: statewide only if the bulletin says so.
    # A GovDelivery bulletin can carry a city order as easily as a press page
    # can - Nebraska's Yutan order went out as a press release, and the next
    # one may arrive by email.
    scope, scope_ev = P.order_scope(blob, STATE_NAMES.get(code))
    if scope == "limited":
        return None, f"limited scope: {scope_ev}"

    authority, a_ev = P.classify_authority(blob)
    sent = sent_day(msg)
    # The order's own sentence, with "Friday" and "today" resolved against the
    # day the bulletin was sent - never the send date as the start.
    start, end = P.order_window(blob, sent)
    sent_date = sent.isoformat() if sent else None

    # An order cannot start or end well before the email announcing it. A
    # Missouri Patriot Day bulletin cites the law "signed December 18, 2001";
    # that date was taken as the order's start. "The order's date" and "a date
    # mentioned in the order" are different facts.
    if sent:
        floor = sent - timedelta(days=MAX_BACKDATE_DAYS)
        if start and start < floor:
            start = None
        if end and end < floor:
            end = None
        # "Flags to half-staff Friday": resolve the weekday against the day
        # the bulletin was sent, rather than treating the send date as the
        # order's first day.
        if not end:
            ws, we = P.weekday_window(blob, sent)
            if we:
                start, end = ws, we

    return {
        "state_code": code,
        "identified_by": how,
        "status": P.HALF,
        "authority": authority,
        "subject": subject[:200],
        "from": sender[:120],
        "sent_date": sent_date,
        "start_date": start.isoformat() if start else sent_date,
        "end_date": end.isoformat() if end else None,
        "until_noon": P.until_noon(blob),
        "evidence": ev,
        "scope": scope,
        "scope_evidence": scope_ev,
    }, None


AUTH_FAILURE_RE = re.compile(
    r"AUTHENTICATIONFAILED|Invalid credentials|Application-specific password"
    r"|Web login required|Username and Password not accepted|LOGIN failed", re.I)


def is_auth_failure(e):
    """Did the server reject our credentials (as opposed to not answering)?"""
    return isinstance(e, imaplib.IMAP4.error) and bool(AUTH_FAILURE_RE.search(str(e)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = ap.parse_args()

    previous = load_previous()
    user = os.environ.get("MAIL_USER")
    password = os.environ.get("MAIL_PASS")
    if not user or not password:
        print("MAIL_USER / MAIL_PASS not set - skipping email ingest.")
        if not args.dry_run:
            # Keep what we knew. Writing an empty file erased every channel's
            # history, so fixing the credentials later restarted every email
            # state at "subscription pending".
            json.dump(dict(previous, generated_at=None, skipped=True),
                      open(OUTPUT, "w"), indent=2)
        return

    try:
        reg = json.load(open(REGISTRY))
        allowed = {r["state_code"] for r in reg
                   if r.get("ingest_mode") == "email" or r.get("notification_channel")}
        # A sender domain read off a real bulletin beats any built-in guess.
        # GovDelivery accounts configure their own sending subdomains, so the
        # signup page never tells you what mail will actually arrive from.
        for r in reg:
            d = (r.get("expected_sender_domain") or "").strip().lower()
            if d:
                SENDER_HINTS[d] = r["state_code"]
        load_govdelivery_accounts(reg)
        STATE_NAMES.update({r["state_code"]: r.get("state") for r in reg})
    except Exception:
        allowed = set()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%d-%b-%Y")

    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST)
        M.login(user, password)
        M.select("INBOX")
        typ, data = M.search(None, f'(SINCE "{since}")')
    except Exception as e:
        # Two failures, two meanings. A rejected login does not fix itself:
        # the app password was revoked (any Google password change or
        # security event does it) and every email state will go stale while
        # the job stays green. A network error usually clears by the next
        # run. The workflow fails the job on the first immediately and on
        # the second only once it has lasted a day, so a blip does not page
        # anyone. Recorded in email-orders.json because the step's own exit
        # code is hidden by continue-on-error.
        kind = "auth" if is_auth_failure(e) else "network"
        print(f"IMAP failed ({kind}): {type(e).__name__}: {e}")
        if not args.dry_run:
            json.dump(dict(previous, ingest_error={
                "kind": kind,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "detail": f"{type(e).__name__}: {e}"[:300]}),
                open(OUTPUT, "w"), indent=2)
        sys.exit(2 if kind == "auth" else 1)

    ids = (data[0] or b"").split()
    print(f"{len(ids)} message(s) since {since}\n")

    orders, rejected = {}, []
    # channels_seen: the last day each channel delivered a flag ORDER.
    # channels_heard: the last day each channel delivered ANY authenticated
    # message. A channel that has spoken before and is quiet today means "no
    # order" — but only while it is still speaking at all. run.py stops
    # reading silence as full staff once channels_heard goes stale.
    seen = dict(previous.get("channels_seen") or {})
    heard = dict(previous.get("channels_heard") or {})
    unattributed, unauthenticated = {}, {}

    def note(bucket, key, msg):
        entry = bucket.setdefault(key, {"count": 0, "subjects": []})
        entry["count"] += 1
        subj = decoded(msg.get("Subject"))[:70]
        if subj and subj not in entry["subjects"]:
            entry["subjects"].append(subj)

    for mid in ids:
        try:
            typ, raw = M.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(raw[0][1])
        except Exception:
            continue
        who, _ = authenticated_state(msg, allowed)
        day = sent_day(msg)
        if who and day:
            heard[who] = max(heard.get(who) or "", day.isoformat())
        rec, why = parse_message(msg, allowed)
        if not rec:
            if why not in ("not flag-related",):
                rejected.append(why)
                if "no state identified" in why or "no sender address" in why:
                    note(unattributed, sender_address(decoded(msg.get("From")))[1]
                         or decoded(msg.get("From")), msg)
                elif why.startswith(UNAUTHENTICATED):
                    # A flag bulletin from a known state sender that failed
                    # DKIM: either a forgery, or a real channel we are now
                    # dropping. Both need a human to look.
                    note(unauthenticated, why.split(":")[0].split()[-1], msg)
            continue
        code = rec["state_code"]
        seen[code] = max(seen.get(code) or "", rec.get("sent_date") or "") or None
        # Keep the most recent order per state.
        if code not in orders or (rec.get("sent_date") or "") > (orders[code].get("sent_date") or ""):
            orders[code] = rec
            print(f"  {code}  {rec['sent_date']}  {rec['subject'][:60]}")
            print(f"       via {rec['identified_by']}, ends {rec['end_date'] or 'unstated'}")

    try:
        M.logout()
    except Exception:
        pass

    print(f"\n  {len(orders)} state order(s) found")
    if rejected:
        print(f"  {len(rejected)} flag-ish message(s) rejected:")
        for r in sorted(set(rejected)):
            print(f"    - {r}")

    if unattributed:
        print("\n  Flag mail we could not attribute to a state:")
        for dom, e in sorted(unattributed.items(), key=lambda x: -x[1]["count"]):
            print(f"    {e['count']:3d}  {dom}")
            for sj in e["subjects"][:2]:
                print(f"         \"{sj}\"")
        print("  -> add these to expected_sender_domain in registry.json")

    if unauthenticated:
        print("\n  Flag mail from a known state sender that FAILED DKIM (dropped):")
        for code, e in sorted(unauthenticated.items()):
            print(f"    {e['count']:3d}  {code}  \"{(e['subjects'] or [''])[0]}\"")
        print("  -> a forgery, or a real channel whose signing domain does not "
              "align with its From domain")

    if args.dry_run:
        print("\n(dry run - nothing written)")
        return

    json.dump({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "orders": orders,
        "channels_seen": seen,
        "channels_heard": heard,
        "unattributed_senders": unattributed,
        "unauthenticated_senders": unauthenticated,
    }, open(OUTPUT, "w"), indent=2)
    print(f"\nWrote {OUTPUT}  ({len(seen)} channel(s) have ever delivered an "
          f"order, {len(heard)} heard from)")


if __name__ == "__main__":
    main()
