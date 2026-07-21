# Movie-Alert

Get a **Telegram** ping the moment a specific **movie + theatre + date (+ showtime)**
opens for booking on BookMyShow. Runs on **GitHub Actions**, triggered every few
minutes by **cron-job.org** — nothing to keep running on your own machine.

## How it works

1. **cron-job.org** triggers the workflow on a schedule (GitHub's own scheduler
   is unreliable, so we trigger it externally).
2. `poller.py` fetches the BMS page through **ScraperAPI** (an India IP — BMS
   blocks foreign/datacenter IPs and returns 403 otherwise).
3. It checks whether your target is open and, on the `False → True` flip, sends
   one Telegram message. Last-seen state lives in `state.json`.

## 1. Telegram bot

- Message **@BotFather** → `/newbot` → copy the **token**.
- Send your new bot any message, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id` — that's
  your **chat id**.
- **Never paste your bot token anywhere public** (chats, issues, screenshots). If
  you ever do, revoke it immediately via BotFather (`/mybots` → your bot → **API
  Token** → **Revoke current token**) and use the new one.

## 2. ScraperAPI key

Sign up at **scraperapi.com** (free tier) and copy your API key.

## 3. Add repo secrets

**Settings → Secrets and variables → Actions:**

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SCRAPERAPI_KEY`

## 4. Configure `config.json`

Pick a `detector`. The page URL is built from `url_template` + `requested_date`.
After editing, reset `state.json` to `{"available": false}`.

### `venue_date` — a specific theatre opens for a specific date (most precise)
```json
{
  "detector": "venue_date",
  "movie": "The Odyssey (IMAX 2D)",
  "requested_date": "20260722",
  "venue_code": "INPR",
  "venue_label": "INOX: LUXE Phoenix Market City, Velachery",
  "url_template": "https://in.bookmyshow.com/movies/chennai/the-odyssey/buytickets/ET00480917/{date}"
}
```
Use `"venue_codes": ["PVPZ","INPR"]` to fire when *any* of several theatres open.

### `bms_date` — a date opens at *any* theatre (date-dominance on the page)
```json
{ "detector": "bms_date", "requested_date": "20260722",
  "url_template": ".../buytickets/ET00480917/{date}", "min_references": 10 }
```

### `show_time` — a specific showtime + screen type gets added to an already-open date

Use this when the venue/date is already bookable, but a specific showtime
(e.g. an early-morning show added later due to demand) hasn't appeared yet.
Checks that `show_time` and `show_attribute` occur near each other on the
page (within `proximity_window` characters, default 400) — not just anywhere
on the page — so an unrelated showtime elsewhere with a matching time or
screen type doesn't trigger a false alert.

```json
{
  "detector": "show_time",
  "movie": "Jana Nayagan",
  "requested_date": "20260723",
  "venue_code": "RSSC",
  "venue_label": "Rohini Silver Screens: Koyambedu",
  "show_time": "09:00 AM",
  "show_attribute": "RGB ATMOS",
  "proximity_window": 400,
  "url_template": "https://in.bookmyshow.com/movies/chennai/jana-nayagan/buytickets/ET00430817/{date}"
}
```

- `show_time` — required. The exact time string as it renders on the page (e.g. `"09:00 AM"`).
- `show_attribute` — optional but recommended. Narrows to a specific screen/format
  (e.g. `"RGB ATMOS"`, `"4K RGB"`, `"2K/DOLBY 7.1"`) so a same-time show on a
  different screen doesn't cause a false positive.
- `proximity_window` — optional, default `400`. How many characters around each
  `show_time` match to search for `show_attribute`. Lower it (e.g. `150–200`) if
  you get false positives; raise it if a true positive isn't being detected.

**Note:** even with proximity matching, this is a heuristic on rendered
text, not a guaranteed "same show entry" match. Treat alerts as "very
likely open, go check the app," not an absolute guarantee.

**Finding the values:** read `<city>/<slug>/<ETcode>/<date>` from the movie's
"Book tickets" URL. For `venue_code`, open a date where the theatre *is* open and
read its cinema link `.../cinemas/<city>/<venue-slug>/buytickets/<CODE>/<date>` —
the `<CODE>` (e.g. `PVPZ`, `INPR`, `RSSC`) is the value.

## 5. Schedule it with cron-job.org

**a. GitHub token** — *Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate*. Scope it to this repo, permission
**Actions: Read and write**. Copy the token.

**b. cron-job.org** — create a cronjob:

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/<you>/<repo>/actions/workflows/booking-watch.yml/dispatches` |
| Schedule | every 2–3 minutes (see note below) |
| Method | `POST` |
| Header | `Accept: application/vnd.github+json` |
| Header | `Authorization: Bearer <your-token>` |
| Header | `X-GitHub-Api-Version: 2022-11-28` |
| Body | `{"ref":"main"}` |

Save, then **Run now**. GitHub returns `204`; a run appears under **Actions**
that you didn't trigger manually — that confirms it's wired up correctly.
From then on it fires on your chosen schedule. Keep the token only in
cron-job.org, never in the repo or in chat.

**On frequency / free-tier limits:** GitHub Actions free tier gives ~2,000
minutes/month. Each run takes ~10–20 seconds.
- Every 10 min ≈ 18 hours/month — well within free.
- Every 1 min ≈ 180 hours/month — **exceeds** the free cap.
- Every 2–3 min is a safe, fast middle ground.

Note GitHub Actions dispatch can queue for a few seconds to a couple minutes
under load, so sub-minute real-world detection isn't guaranteed regardless
of cron interval.

## Geo-block (why ScraperAPI)

BMS only serves India and blocks datacenter IPs, so GitHub's US runners get a
**403** on a direct request. `SCRAPERAPI_KEY` routes through an India IP and fixes
it. Alternatives: set a `PROXY_URL` secret (India proxy), or run from a machine in
India. It's IP/geo-based — headers alone won't get past it.

## Reuse it (fork)

Fork the repo, **enable Actions** on the fork (off by default), add your own
secrets, edit `config.json`, reset `state.json`, and set up your own cron-job.org
trigger. Each fork is independent with its own Telegram chat. No branches needed —
which detector runs is just the `detector` field in `config.json`.

## Files

| File | Purpose |
|------|---------|
| `poller.py` | Fetch page, detect availability (`venue_date` / `bms_date` / `show_time` / generic), send Telegram alert |
| `config.json` | Your movie / date / theatre / showtime target |
| `.github/workflows/booking-watch.yml` | The runner (dispatched by cron-job.org) |
| `requirements.txt` | Python deps (`requests`) |
| `state.json` | Auto-managed; tracks last-seen availability |

## What this does *not* do

This only **notifies** you the moment your target shows as bookable — it does
not select seats or complete a purchase. Once alerted, you still need to open
the app/site and book manually, as fast as you can. For high-demand releases,
have the app open and logged in beforehand so you're not fumbling at the
moment it drops.
