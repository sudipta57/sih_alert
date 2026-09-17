#!/usr/bin/env python3
"""
sih_watch.py - email alerts when an SIH problem statement's idea count crosses
milestones (50, 75, 100, ... in steps of 25).

Zero third-party dependencies (stdlib + system curl).

Usage:
  python3 sih_watch.py              # normal run (cron / GitHub Actions)
  python3 sih_watch.py --dry-run    # fetch + parse + show decision, no mail, no state write
  python3 sih_watch.py --test-mail  # send one test email and exit
  python3 sih_watch.py --html page.html --dry-run   # parse a saved copy offline
"""
import argparse
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
URL = "https://sih.gov.in/sih2026PS"


# ---------------------------------------------------------------- config
def load_dotenv(path: Path) -> None:
    """Minimal .env loader. Real env vars win over the file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v and not os.environ.get(k.strip()):
            os.environ[k.strip()] = v


def cfg(name: str, default=None, required=False):
    val = (os.environ.get(name) or "").strip() or default   # blank == unset
    if required and not val:
        sys.exit(f"[config] missing required env var: {name}")
    return val


# ---------------------------------------------------------------- fetch
def fetch_html() -> str:
    # The portal sits behind a WAF that tends to block python-requests;
    # curl with browser headers gets through (same approach as public scrapers).
    cmd = [
        "curl", "-sS", "-L", "--compressed", "--max-time", "90",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", "Referer: https://sih.gov.in/",
        URL,
    ]
    last = None
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.decode(errors="replace").strip())
            text = r.stdout.decode("utf-8", errors="replace")
            if "dataTablePS" not in text:
                raise RuntimeError("page has no #dataTablePS (blocked or layout changed)")
            return text
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < 2:
                time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"fetch failed after 3 attempts: {last}")


# ---------------------------------------------------------------- parse
TAG_RE = re.compile(r"<[^>]+>")
COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def parse_count(html: str, ps_id: str):
    """Return (submitted, cap) for ps_id. Columns: ... | PS Number | Submitted | Theme | ...
    Finds the <td> whose whole text is the PS id, then reads the next <td>."""
    ps_cell = re.compile(r"<td[^>]*>\s*" + re.escape(ps_id) + r"\s*</td>", re.I)
    next_td = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
    for m in ps_cell.finditer(html):
        n = next_td.search(html, m.end())
        if not n:
            continue
        text = TAG_RE.sub("", n.group(1)).strip()
        c = COUNT_RE.search(text)
        if c:
            return int(c.group(1)), int(c.group(2))
    raise ValueError(f"{ps_id} not found in page (or count cell format changed)")


# ---------------------------------------------------------------- state
def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- decide
def milestone_for(count: int, start: int, step: int, cap: int) -> int:
    """Highest milestone <= count (0 if below start). The cap always counts as a milestone."""
    if count >= cap:
        return cap
    if count < start:
        return 0
    return start + ((count - start) // step) * step


# ---------------------------------------------------------------- mail
def send_mail(subject: str, body: str) -> None:
    host = cfg("SMTP_HOST", "smtp.gmail.com")
    port = int(cfg("SMTP_PORT", "465"))
    user = cfg("SMTP_USER", required=True)
    pwd = cfg("SMTP_PASS", required=True)
    to = [a.strip() for a in cfg("MAIL_TO", required=True).split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.send_message(msg)


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-mail", action="store_true")
    ap.add_argument("--html", help="parse a saved HTML file instead of fetching")
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    ps_id = cfg("PS_ID", "SIH26166").upper()
    start = int(cfg("START_AT", "50"))
    step = int(cfg("STEP", "25"))
    fail_alert_after = int(cfg("FAIL_ALERT_AFTER", "6"))
    state_path = HERE / cfg("STATE_FILE", "state.json")
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")

    if args.test_mail:
        send_mail(f"[SIH watch] test mail for {ps_id}",
                  f"If you can read this, alerts for {ps_id} will reach you.\nSent {now}")
        print(f"[{now}] test mail sent")
        return 0

    state = load_state(state_path)
    st = state.setdefault(ps_id, {"last_notified": 0, "fail_streak": 0})

    # --- fetch + parse, with failure tracking so the watcher never dies silently
    try:
        html = Path(args.html).read_text(errors="replace") if args.html else fetch_html()
        count, cap = parse_count(html, ps_id)
    except Exception as e:  # noqa: BLE001
        st["fail_streak"] = st.get("fail_streak", 0) + 1
        print(f"[{now}] ERROR ({st['fail_streak']} in a row): {e}")
        if not args.dry_run:
            if st["fail_streak"] == fail_alert_after:  # alert once per outage
                send_mail(f"[SIH watch] {ps_id}: watcher failing",
                          f"{st['fail_streak']} consecutive failures.\nLast error: {e}\n"
                          f"Check {URL} manually until this is fixed.\n{now}")
            save_state(state_path, state)
        return 1

    if st.get("fail_streak", 0) >= fail_alert_after and not args.dry_run:
        send_mail(f"[SIH watch] {ps_id}: watcher recovered",
                  f"Fetching works again. Current count: {count}/{cap}\n{now}")
    st["fail_streak"] = 0

    prev_count = st.get("last_count")
    last = st.get("last_notified", 0)
    hit = milestone_for(count, start, step, cap)
    print(f"[{now}] {ps_id}: {count}/{cap}  (prev={prev_count}, last_notified={last}, milestone={hit})")

    if hit > last:
        skipped = [m for m in range(max(start, last + step), hit, step) if m > last]
        subject = (f"[SIH watch] {ps_id} is FULL: {count}/{cap}" if hit >= cap
                   else f"[SIH watch] {ps_id} crossed {hit}: now {count}/{cap}")
        body = (f"{ps_id} now has {count} of {cap} idea submissions.\n"
                f"Milestone crossed: {hit}\n"
                + (f"(Also passed since last alert: {', '.join(map(str, skipped))})\n" if skipped else "")
                + f"Previous check: {prev_count}\n"
                  f"Next alert at: {'none (cap reached)' if hit >= cap else min(hit + step, cap)}\n\n"
                  f"Source: {URL}\nChecked: {now}\n")
        if args.dry_run:
            print(f"[dry-run] WOULD SEND: {subject}")
        else:
            send_mail(subject, body)
            st["last_notified"] = hit
            print(f"[{now}] mail sent: {subject}")
    else:
        nxt = start if last < start else min(last + step, cap)
        print(f"[{now}] no alert (next at {nxt})")

    if not args.dry_run:
        st["last_count"] = count
        st["last_checked"] = now
        save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
