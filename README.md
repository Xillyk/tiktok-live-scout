# tiktok-live-scout

Watches TikTok 24/7 for one or more accounts and fires a Discord notification
the moment a target goes **live** or **ends** their stream.

Detection method:

1. Open `https://www.tiktok.com/live` in a persistent logged-in browser context.
2. Click **See all** in the left sidebar's *Following* section to expand the
   full list of currently-live followed accounts.
3. For each target, check whether `a[href="/@<user>"]` (or `/@<user>/live`)
   exists in that sidebar list — if yes the account is **live**, otherwise
   **offline**.

One `/live` navigation checks all targets per cycle — the *Following* section
on that page lists exclusively followed accounts that are streaming right now,
so absence from the list is a clean offline signal. If the page didn't load
the Following section at all (e.g. transient TikTok hiccup), the cycle is
skipped with a debug dump in `data/debug/`.

## One-click run

Double-click **`tiktok-live-scout.command`** in Finder. It will, in order:

1. Create the Python venv and install dependencies (first run only, ~1 min).
2. Start Docker Desktop if it isn't running, wait for the daemon.
3. `docker compose up -d` and wait for Postgres to report healthy.
4. If no TikTok session cookie is present, open a Chromium login window
   and wait until you've signed in. Close the window when the For You feed
   is loaded.
5. Start the web dashboard in the background (logs to `logs/web.stdout.log`).
6. Open `http://127.0.0.1:8766` in your default browser.
7. Run the scout in the foreground.

`Ctrl+C` in the terminal window stops the scout AND the web dashboard
(Postgres stays running — use `docker compose down` to stop it).

> Tip: keep the `.command` file pinned to the Dock or Desktop. macOS may
> prompt for security approval the first time (right-click → Open).

## Manual run (if you prefer separate processes)

```sh
source .venv/bin/activate
docker compose up -d              # start Postgres if not already
python -m src.scout --login       # only needed once
python -m src.scout               # terminal 1 — the watcher
python -m src.web                 # terminal 2 — http://127.0.0.1:8766
```

The Postgres container publishes on a random free host port — the scout and
web dashboard ask docker for the current port at startup, so collisions with
other Postgres instances are impossible. Check the port with:

```sh
docker compose port postgres 5432
```

## Adding accounts

Edit `config.yaml`:

```yaml
targets:
  - paloyzzzz05
  - someone_else
  - another_one
```

Restart the scout. That's it.

## Where things live

- `config.yaml` — targets, poll interval, paths, web host/port
- `.env` — `DISCORD_WEBHOOK_URL`, `DATABASE_URL`
- `docker-compose.yml` — Postgres 16 on a **dynamic** host port (see below), named volume `pgdata`
- `data/auth/` — persistent Chromium profile (cookies). **Don't commit.**
- `data/debug/` — screenshots/HTML written when detection fails
- `logs/scout.log` — rolling log file (2 MB × 5)

State lives in Postgres now. Two tables:

- `target_state` — current status per username (live / offline / unknown)
- `live_events` — append-only log: `live_start`, `live_end` (with duration), `first_seen_offline`

Quick peek at the DB:

```sh
docker compose exec postgres psql -U scout -d tiktok_live_scout \
  -c "SELECT username, status, last_check FROM target_state;"

docker compose exec postgres psql -U scout -d tiktok_live_scout \
  -c "SELECT username, event, at, duration_seconds FROM live_events ORDER BY at DESC LIMIT 20;"
```

## Troubleshooting

- **"could not connect to Postgres"** — `docker compose up -d` and wait for the
  healthcheck (`docker compose ps` should show `healthy`).
- **"not logged in"** — TikTok cookie expired. Re-run `python -m src.scout --login`.
- **Status stays `unknown`** — either the "View all" button couldn't be found
  or the target isn't in the expanded list even after scrolling. Check
  `data/debug/following_miss_<username>.png` to see what the page looked like.
  Selectors may need tweaking in `src/scout.py::open_view_all`.
- **No Discord ping** — confirm `DISCORD_WEBHOOK_URL` is set; check
  `logs/scout.log` for `discord webhook returned …` lines.

## Run on a schedule (optional, macOS)

The scout is a long-running process — easiest path is to leave it in a
terminal, or wrap it in a `launchd` plist that runs `python -m src.scout`
on login with `KeepAlive: true`. Ping me when you want that and I'll generate
the plist.
