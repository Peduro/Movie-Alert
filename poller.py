#!/usr/bin/env python3
"""
Ticket-booking watcher.

Polls a URL (a BookMyShow / District showtimes page, or an internal API
request you grabbed from your browser's DevTools) and sends a Telegram
message the moment a given theatre appears with booking open.

State is tracked in state.json so you get alerted on the *transition*
to "available" instead of on every run.

Everything is driven by config.json (and/or environment variables), so
nothing site-specific is hardcoded -- if BookMyShow/District change their
markup you only edit config, not code.
"""

import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

# Look like a real Chrome on Windows -- BMS rejects obvious bots.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_config():
    cfg = load_json(CONFIG_PATH, default={}) or {}

    # Environment variables override the file (used by GitHub Actions secrets).
    env_map = {
        "TARGET_URL": "target_url",
        "THEATRE": "theatre",
        "MOVIE": "movie",
        "REQUESTED_DATE": "requested_date",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
    }
    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    if os.environ.get("HEADERS_JSON"):
        cfg["headers"] = json.loads(os.environ["HEADERS_JSON"])

    # The BMS date is embedded in the URL, so build the URL from the template
    # and the (possibly overridden) requested_date. Set REQUESTED_DATE=20260717
    # to point everything at the 17th for a live end-to-end test.
    if cfg.get("url_template") and cfg.get("requested_date"):
        cfg["target_url"] = cfg["url_template"].format(date=cfg["requested_date"])

    required = ["target_url", "telegram_bot_token", "telegram_chat_id"]
    detector = cfg.get("detector")
    if detector in ("bms_date", "venue_date", "show_time"):
        required.append("requested_date")
    else:
        required.append("theatre")
    if detector == "venue_date" and not (cfg.get("venue_code") or cfg.get("venue_codes")):
        sys.exit("venue_date detector needs 'venue_code' or 'venue_codes'")
    if detector == "show_time" and not cfg.get("show_time"):
        sys.exit("show_time detector needs 'show_time'")    
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}")
    return cfg


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=30,
    )
    resp.raise_for_status()


def fetch(cfg):
    """
    Fetch the target URL from an India egress when configured.

    BookMyShow blocks non-India / datacenter IPs (e.g. GitHub's US runners),
    so a plain request from CI gets a 403. Two ways to route through India:

    * SCRAPERAPI_KEY  -- routes via ScraperAPI with country_code=in and solves
                         anti-bot. Easiest for CI. Set it as a repo secret.
    * PROXY_URL       -- a standard http(s) proxy string, e.g.
                         "http://user:pass@in-proxy-host:port".

    With neither set, it makes a direct request with browser headers plus a
    cookie warm-up -- enough only when running from an India IP.
    """
    headers = dict(DEFAULT_HEADERS)
    headers.update(cfg.get("headers", {}))

    scraper_key = os.environ.get("SCRAPERAPI_KEY")
    if scraper_key:
        api_url = "https://api.scraperapi.com/?" + urllib.parse.urlencode(
            {"api_key": scraper_key, "country_code": "in", "url": cfg["target_url"]}
        )
        resp = requests.get(api_url, timeout=90)
        resp.raise_for_status()
        return resp.text

    proxy = os.environ.get("PROXY_URL")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    session = requests.Session()
    session.headers.update(headers)

    # Warm-up: hit the homepage first to pick up cookies (helps soft bot checks).
    try:
        session.get("https://in.bookmyshow.com/", timeout=30, proxies=proxies)
    except requests.RequestException:
        pass

    resp = session.get(
        cfg["target_url"],
        timeout=30,
        proxies=proxies,
        headers={"Referer": "https://in.bookmyshow.com/explore/movies-chennai"},
    )
    resp.raise_for_status()
    return resp.text


def is_available_bms_date(page_text, cfg):
    """
    BookMyShow-specific detector for "a given date has opened for booking".

    BMS only renders showtimes for the date currently being displayed, and it
    silently falls back to the nearest available date when you request a date
    that hasn't opened yet. So the requested date (e.g. 20260720) sits at a
    low ~3 count (just the date-strip navigation) until it opens, at which
    point its showtimes render and it becomes the *dominant* date token.

    Rule: open when the requested date is the most-referenced date token on
    the page and it clears a small floor (well above strip-only noise).
    """
    requested = cfg["requested_date"]  # e.g. "20260720"
    floor = cfg.get("min_references", 10)

    tokens = re.findall(r"20\d{6}", page_text)
    if not tokens:
        return False

    counts = Counter(tokens)
    top_date, _ = counts.most_common(1)[0]
    requested_count = counts.get(requested, 0)

    return top_date == requested and requested_count >= floor
def _extract_bracket_span(text, start_idx, open_ch='[', close_ch=']'):
    """
    Given start_idx pointing at open_ch, return (start_idx, end_idx) spanning
    the matching close_ch, using depth counting that's string-aware (so
    brackets inside quoted strings don't throw off the count).
    """
    depth = 0
    i = start_idx
    in_string = False
    escape = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return start_idx, i + 1
        i += 1
    return None

def _find_showtimes_arrays(page_text, event_code):
    """
    Find every ShowTimes array belonging to the given EventCode, anywhere on
    the page.
    """
    arrays = []
    pos = page_text.find(event_code)
    print(f"DEBUG: EventCode text position = {pos}")

    if pos != -1:
       print(page_text[max(0, pos-150):pos+300])

    pattern = re.compile(
        rf'"EventCode"\s*:\s*"{re.escape(event_code)}"'
    )

    for match in pattern.finditer(page_text):
        idx = match.start()

        st_match = re.search(
            r'"ShowTimes"\s*:\s*\[',
            page_text[idx:idx + 5000]
        )

        if not st_match:
            continue

        showtimes_pos = idx + st_match.start()
        bracket_start = page_text.find('[', showtimes_pos)
        span = _extract_bracket_span(page_text, bracket_start)
        if not span:
            continue

        snippet = page_text[span[0]:span[1]]

        try:
            showtimes = json.loads(snippet)
            arrays.append(showtimes)
            print(f"DEBUG: Loaded {len(showtimes)} shows")
        except json.JSONDecodeError:
            continue

    # Temporary debug
    print(f"DEBUG: EventCode={event_code}, arrays_found={len(arrays)}")

    return arrays

def is_available_show_time(page_text, cfg):
    event_code = cfg.get("event_code")
    if not event_code:
        print("DEBUG: event_code not configured")
        return False

    date = cfg["requested_date"]
    show_time = cfg["show_time"]
    show_attribute = cfg.get("show_attribute")

    print(f"DEBUG: Looking for {date} | {show_time} | {show_attribute}")

    for showtimes in _find_showtimes_arrays(page_text, event_code):
        for show in showtimes:
            print(
                f"DEBUG: {show.get('ShowDateCode')} | "
                f"{show.get('ShowTime')} | "
                f"{show.get('Attributes')}"
            )

            if show.get("ShowDateCode") != date:
                continue

            if show.get("ShowTime") != show_time:
                continue

            if show_attribute and show.get("Attributes") != show_attribute:
                continue

            print("DEBUG: MATCH FOUND")
            return True

    print("DEBUG: No matching show found")
    return False
    
def is_available_venue_date(page_text, cfg):
    """
    Theatre-specific detector: is a given venue bookable on a given date?

    BMS renders a per-venue booking link like
        /cinemas/chennai/<slug>/buytickets/<venueCode>/<date>
    only when that venue has live shows for that exact date. Because the date
    is baked into the link, it can't be confused with the silent fallback
    (a fallback page carries /<code>/<fallbackDate>, not /<code>/<ourDate>).

    Set venue_code (one) or venue_codes (list). With a list, it's open when
    ANY of them is bookable for the date.
    """
    date = cfg["requested_date"]
    codes = cfg.get("venue_codes") or [cfg["venue_code"]]
    return any("/{}/{}".format(code, date) in page_text for code in codes)


def is_available(page_text, cfg):
    detector = cfg.get("detector")
    if detector == "show_time":
        return is_available_show_time(page_text, cfg)
    if detector == "venue_date":
        return is_available_venue_date(page_text, cfg)
    if detector == "bms_date":
        return is_available_bms_date(page_text, cfg)
    return is_available_generic(page_text, cfg)


def is_available_generic(page_text, cfg):
    """
    Booking is considered OPEN for the target theatre when the theatre name
    is present AND at least one 'booking is live' signal is present.

    Matching is case-insensitive and ignores extra whitespace so small
    formatting differences don't cause misses.
    """
    haystack = re.sub(r"\s+", " ", page_text).lower()

    theatre = re.sub(r"\s+", " ", cfg["theatre"]).lower().strip()
    if theatre not in haystack:
        return False

    # If the movie name is configured, require it too (avoids false hits when
    # the theatre is listed for other movies).
    movie = cfg.get("movie")
    if movie:
        if re.sub(r"\s+", " ", movie).lower().strip() not in haystack:
            return False

    # Signals that booking is actually live rather than "coming soon".
    open_signals = cfg.get(
        "open_signals",
        ["book tickets", "book now", '"showtimes"', "showtime", "select seats"],
    )
    # Signals that it's NOT yet open -- if present near-exclusively, treat as closed.
    closed_signals = cfg.get("closed_signals", ["notify me", "coming soon"])

    has_open = any(s.lower() in haystack for s in open_signals)
    only_closed = any(s.lower() in haystack for s in closed_signals) and not has_open

    return has_open and not only_closed


def main():
    cfg = load_config()
    state = load_json(STATE_PATH, default={"available": False}) or {"available": False}

    target_desc = cfg.get("theatre") or cfg.get("requested_date", "target")
    label = f"{cfg.get('movie', 'movie')} @ {target_desc}"

    try:
        page = fetch(cfg)
    except requests.RequestException as exc:
        # Transient network/blocking errors shouldn't crash the workflow.
        print(f"[{label}] fetch failed: {exc}")
        return 0
        
    print("========== PAGE START ==========")
    print(page[:5000])
    print("========== PAGE END ==========")
    
    available = is_available(page, cfg)
    print(f"[{label}] available={available} (was {state.get('available')})")

    if available and not state.get("available"):
        if cfg.get("detector") in ("bms_date", "venue_date", "show_time"):
            rd = cfg["requested_date"]
            pretty = f"{rd[6:8]}-{rd[4:6]}-{rd[0:4]}"
            venue = cfg.get("venue_label") or cfg.get("venue_code") or ""
            venue_line = f"Theatre: {venue}\n" if venue else ""
            msg = (
                f"🎬 Booking just OPENED!\n\n"
                f"{cfg.get('movie', 'Movie')}\n"
                f"{venue_line}"
                f"Date: {pretty}\n\n"
                f"Book here: {cfg['target_url']}"
            )
        else:
            msg = (
                f"🎬 Booking is OPEN!\n\n"
                f"{cfg.get('movie', 'Movie')}\n"
                f"Theatre: {cfg['theatre']}\n\n"
                f"Book here: {cfg['target_url']}"
            )
        send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], msg)
        print(f"[{label}] notification sent")

    # Persist current state so we don't re-alert every run.
    if available != state.get("available"):
        state["available"] = available
        state["checked_at"] = int(time.time())
        save_json(STATE_PATH, state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
