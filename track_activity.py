#!/usr/bin/env python3
"""
Letterboxd activity tracker.

For every username in LETTERBOXD_USERNAMES, this script:
  1. Opens https://letterboxd.com/<username>/activity/ with a headless browser
     (the feed is loaded via JS, so a plain HTTP request won't see it).
  2. Extracts each activity entry as a separate block of text, with a stable
     id when the page gives us one (data-activity-id), falling back to a
     content hash when it doesn't.
  3. Compares against state/<username>.json, which remembers which ids were
     already sent on a previous run.
  4. Sends only the new entries to Telegram, oldest-first.
  5. Updates the state file (kept small: last MAX_STATE_IDS ids only).

State is stored in files under state/ so it needs to persist between runs.
In GitHub Actions that means committing the updated files back to the repo
after each run (the workflow file does this).
"""

import json
import os
import re
import sys
import hashlib
import urllib.request
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

STATE_DIR = Path(__file__).parent / "state"
MAX_STATE_IDS = 300  # how many ids to remember per user, to keep files small

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
USERNAMES = [
    u.strip()
    for u in os.environ.get("LETTERBOXD_USERNAMES", "").split(",")
    if u.strip()
]


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def extract_activity_items(page):
    """
    Returns a list of dicts: {"id": str, "text": str}, newest first
    (matches the order Letterboxd renders them in).

    Strategy:
      1. Prefer real DOM nodes with a data-activity-id attribute (this is
         how Letterboxd tags each row) — gives us a stable id and clean text
         per entry.
      2. If that selector ever stops matching (Letterboxd changes markup),
         fall back to grabbing the whole feed's text and splitting it into
         blocks heuristically, hashing each block for an id.
    """
    page.wait_for_selector("#content", timeout=30000)

    # Give the async activity feed a moment to populate.
    try:
        page.wait_for_selector(
            "#activity-table [data-activity-id], .activity-row",
            timeout=15000,
        )
    except Exception:
        pass

    rows = page.query_selector_all("#activity-table [data-activity-id]")
    if not rows:
        rows = page.query_selector_all(".activity-row[data-activity-id]")

    items = []
    if rows:
        for row in rows:
            activity_id = row.get_attribute("data-activity-id")
            text = (row.inner_text() or "").strip()
            text = re.sub(r"\n{2,}", "\n", text)
            if not text:
                continue
            if not activity_id:
                activity_id = hashlib.sha1(text.encode("utf-8")).hexdigest()
            items.append({"id": activity_id, "text": text})
        return items

    # ---- Fallback: no structured rows found, parse the raw feed text ----
    log("  ! data-activity-id rows not found, falling back to text parsing")
    container = page.query_selector("#activity-table") or page.query_selector("#content")
    raw = (container.inner_text() if container else page.inner_text("body")) or ""
    return _split_raw_activity_text(raw)


TIME_RE = re.compile(r"^(now|\d+[smhdwy])$", re.IGNORECASE)


def _split_raw_activity_text(raw: str):
    """
    Heuristic fallback parser for the plain-text activity dump, in case the
    structured selectors above don't match. Each entry ends with a short
    relative-time token (e.g. "20h", "9d", "now"); we fold everything since
    the previous boundary into one entry, and also absorb a leading
    "Title (Year)" poster-caption line that precedes "<name> watched" rows.
    """
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    entries = []
    current = []
    for line in lines:
        current.append(line)
        if TIME_RE.match(line):
            entries.append(current)
            current = []
    if current:
        entries.append(current)

    items = []
    for block in entries:
        text = "\n".join(block)
        activity_id = hashlib.sha1(text.encode("utf-8")).hexdigest()
        items.append({"id": activity_id, "text": text})
    return items


def fetch_activity(playwright, username):
    browser = playwright.chromium.launch()
    try:
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        )
        url = f"https://letterboxd.com/{username}/activity/"
        page.goto(url, wait_until="networkidle", timeout=45000)
        items = extract_activity_items(page)
        return items
    finally:
        browser.close()


# --------------------------------------------------------------------------
# State handling
# --------------------------------------------------------------------------

def state_path(username):
    return STATE_DIR / f"{username}.json"


def load_seen_ids(username):
    path = state_path(username)
    if not path.exists():
        return set(), []
    try:
        data = json.loads(path.read_text())
        ids = data.get("seen_ids", [])
        return set(ids), ids
    except Exception:
        return set(), []


def save_seen_ids(username, ordered_ids):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    trimmed = ordered_ids[:MAX_STATE_IDS]
    state_path(username).write_text(json.dumps({"seen_ids": trimmed}, indent=2))


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram_message(text):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        if resp.status != 200:
            raise RuntimeError(f"Telegram API error: {resp.status} {body}")


def format_message(username, item_text):
    return f"\U0001F3AC {username} on Letterboxd\n\n{item_text}"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def process_user(playwright, username):
    log(f"Checking {username} ...")
    try:
        items = fetch_activity(playwright, username)
    except Exception as e:
        log(f"  ! failed to fetch activity for {username}: {e}")
        return

    if not items:
        log("  no activity items found on page")
        return

    seen_set, seen_ordered = load_seen_ids(username)

    # items[] is newest-first. Collect everything not yet seen.
    new_items = [it for it in items if it["id"] not in seen_set]

    if not seen_ordered:
        # First run ever for this user: don't spam Telegram with the whole
        # history, just record the current state as the baseline.
        log(f"  first run for {username}: recording {len(items)} items as baseline, no messages sent")
        save_seen_ids(username, [it["id"] for it in items])
        return

    if not new_items:
        log("  no new activity")
        return

    log(f"  {len(new_items)} new item(s)")

    # Send oldest-new-first so the Telegram chat reads chronologically.
    for it in reversed(new_items):
        try:
            send_telegram_message(format_message(username, it["text"]))
        except Exception as e:
            log(f"  ! failed to send Telegram message: {e}")

    # New ids go on top (most recent first), followed by previously seen ones.
    updated_ordered = [it["id"] for it in items] + [
        i for i in seen_ordered if i not in {it["id"] for it in items}
    ]
    save_seen_ids(username, updated_ordered)


def main():
    if not TELEGRAM_TOKEN:
        log("ERROR: TELEGRAM_TOKEN secret is not set")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        log("ERROR: TELEGRAM_CHAT_ID secret is not set")
        sys.exit(1)
    if not USERNAMES:
        log("ERROR: LETTERBOXD_USERNAMES secret is not set (comma-separated usernames)")
        sys.exit(1)

    # Mask usernames in the GitHub Actions log output. Each is pulled from a
    # secret, but Actions only auto-masks the exact secret string as a whole
    # (e.g. "bsaif,csaif,ksaif"), not the individual comma-split pieces —
    # this makes each one masked too, so a viewer of the run log with repo
    # access still can't read them off in plain text.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for username in USERNAMES:
            print(f"::add-mask::{username}")

    with sync_playwright() as p:
        for username in USERNAMES:
            process_user(p, username)


if __name__ == "__main__":
    main()
