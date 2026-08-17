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

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

# Sender domains that reliably belong to one state. Attribution by sender is
# far safer than guessing from body text, because a Wyoming bulletin can
# easily mention another state in passing.
SENDER_HINTS = {
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


def load_seen():
    """States whose channel has ever delivered a parseable bulletin."""
    try:
        return json.load(open(OUTPUT)).get("channels_seen", {}) or {}
    except Exception:
        return {}


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


def state_from(sender, subject, body, allowed):
    """(code, how) or (None, reason).

    Sender domain first — it is an identity, not a mention. Falling back to
    "which state name appears in the text" would let a bulletin that merely
    references another state be filed under the wrong one.
    """
    s = (sender or "").lower()
    for domain, code in SENDER_HINTS.items():
        if domain in s and (not allowed or code in allowed):
            return code, f"sender domain {domain}"

    # Fall back to a state named in the SUBJECT only, and only if exactly one
    # candidate matches. Ambiguity means we do not know.
    subj = (subject or "").lower()
    hits = [c for c, name in STATES.items()
            if name.lower() in subj and (not allowed or c in allowed)]
    if len(hits) == 1:
        return hits[0], "state named in subject"
    if len(hits) > 1:
        return None, f"subject names {len(hits)} states - ambiguous"
    return None, "no state identified"


def parse_message(msg, allowed):
    subject = decoded(msg.get("Subject"))
    sender = decoded(msg.get("From"))
    body = body_text(msg)
    blob = f"{subject}\n{body}"

    if not P.FLAG_RE.search(blob):
        return None, "not flag-related"

    code, how = state_from(sender, subject, body, allowed)
    if not code:
        return None, how

    # Status and authority from the subject where possible: headlines are
    # unambiguous by construction, bodies routinely mention both half and full.
    status, ev = P.classify_status(subject)
    if status == P.UNKNOWN:
        status, ev = P.classify_status(body[:600])
    if status != P.HALF:
        return None, f"no half-staff order detected ({status})"

    scope, scope_ev = P.order_scope(blob)
    if scope == "limited":
        return None, f"limited scope: {scope_ev}"

    authority, a_ev = P.classify_authority(blob)
    start, end = P.date_range(blob)

    try:
        sent = email.utils.parsedate_to_datetime(msg.get("Date"))
        sent_date = sent.date().isoformat()
    except Exception:
        sent_date = None

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
    }, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = ap.parse_args()

    user = os.environ.get("MAIL_USER")
    password = os.environ.get("MAIL_PASS")
    if not user or not password:
        print("MAIL_USER / MAIL_PASS not set - skipping email ingest.")
        if not args.dry_run:
            json.dump({"generated_at": None, "orders": {}, "skipped": True},
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
    except Exception:
        allowed = set()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%d-%b-%Y")

    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST)
        M.login(user, password)
        M.select("INBOX")
        typ, data = M.search(None, f'(SINCE "{since}")')
    except Exception as e:
        print(f"IMAP failed: {type(e).__name__}: {e}")
        sys.exit(1)

    ids = (data[0] or b"").split()
    print(f"{len(ids)} message(s) since {since}\n")

    orders, rejected = {}, []
    # Every channel we have actually heard from, ever. Silence from a channel
    # that has spoken before means "no order". Silence from one that never has
    # means "we do not know whether the subscription works". Those must not be
    # the same value.
    seen = load_seen()
    unattributed = {}
    for mid in ids:
        try:
            typ, raw = M.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(raw[0][1])
        except Exception:
            continue
        rec, why = parse_message(msg, allowed)
        if not rec:
            if why not in ("not flag-related",):
                rejected.append(why)
                if "no state identified" in why:
                    frm = decoded(msg.get("From"))
                    dom = frm.split("@")[-1].strip("> ").lower() if "@" in frm else frm
                    unattributed[dom] = unattributed.get(dom, 0) + 1
            continue
        seen[rec["state_code"]] = rec.get("sent_date") or seen.get(rec["state_code"])
        code = rec["state_code"]
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
        for dom, n in sorted(unattributed.items(), key=lambda x: -x[1]):
            print(f"    {n:3d}  from {dom}")
        print("  -> add these to expected_sender_domain in registry.json")

    if args.dry_run:
        print("\n(dry run - nothing written)")
        return

    json.dump({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "orders": orders,
        "channels_seen": seen,
        "unattributed_senders": unattributed,
    }, open(OUTPUT, "w"), indent=2)
    print(f"\nWrote {OUTPUT}  ({len(seen)} channel(s) have ever delivered)")


if __name__ == "__main__":
    main()
