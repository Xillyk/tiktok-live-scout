"""Postgres-backed state. Replaces the old state.json file.

Schema is created on first connect (idempotent CREATE TABLE IF NOT EXISTS),
so no migration tooling is needed. The scout calls `record_check` after every
poll; the web dashboard calls `get_state` to render.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Default credentials and db match docker-compose.yml.
COMPOSE_DB = "tiktok_live_scout"
COMPOSE_USER = "scout"
COMPOSE_PASSWORD = "scout"
COMPOSE_SERVICE = "postgres"
COMPOSE_INTERNAL_PORT = "5432"

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS target_state (
    profile          TEXT NOT NULL DEFAULT 'default',
    username         TEXT NOT NULL,
    status           TEXT NOT NULL,
    last_check       TIMESTAMPTZ,
    last_change      TIMESTAMPTZ,
    live_started_at  TIMESTAMPTZ,
    live_nickname    TEXT,
    live_title       TEXT,
    live_viewer_count INTEGER,
    live_room_id     TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (profile, username)
);

CREATE TABLE IF NOT EXISTS live_events (
    id               BIGSERIAL PRIMARY KEY,
    profile          TEXT NOT NULL DEFAULT 'default',
    username         TEXT NOT NULL,
    event            TEXT NOT NULL,
    at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_seconds INTEGER
);

CREATE INDEX IF NOT EXISTS live_events_username_at_idx
    ON live_events (username, at DESC);
CREATE INDEX IF NOT EXISTS live_events_profile_at_idx
    ON live_events (profile, at DESC);

-- Per-cycle time-series sample of a live stream. One row inserted every
-- cycle that a target is detected live. Used for the dashboard graphs.
CREATE TABLE IF NOT EXISTS live_samples (
    id              BIGSERIAL PRIMARY KEY,
    profile         TEXT NOT NULL,
    username        TEXT NOT NULL,
    at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title           TEXT,
    user_count      INTEGER,
    like_count      BIGINT,
    hashtag_title   TEXT,
    follower_count  BIGINT,
    following_count BIGINT,
    live_room_mode  INTEGER,
    live_level      TEXT
);
CREATE INDEX IF NOT EXISTS live_samples_target_at_idx
    ON live_samples (profile, username, at DESC);

CREATE TABLE IF NOT EXISTS poll_meta (
    profile       TEXT PRIMARY KEY,
    last_poll_at  TIMESTAMPTZ
);

-- Migrations for pre-multi-profile schemas. Each block is idempotent.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='target_state' AND column_name='profile'
    ) THEN
        ALTER TABLE target_state ADD COLUMN profile TEXT NOT NULL DEFAULT 'default';
        ALTER TABLE target_state DROP CONSTRAINT IF EXISTS target_state_pkey;
        ALTER TABLE target_state ADD PRIMARY KEY (profile, username);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='live_events' AND column_name='profile'
    ) THEN
        ALTER TABLE live_events ADD COLUMN profile TEXT NOT NULL DEFAULT 'default';
    END IF;

    -- Live-metadata columns added when the API detector landed.
    ALTER TABLE target_state ADD COLUMN IF NOT EXISTS live_nickname TEXT;
    ALTER TABLE target_state ADD COLUMN IF NOT EXISTS live_title TEXT;
    ALTER TABLE target_state ADD COLUMN IF NOT EXISTS live_viewer_count INTEGER;
    ALTER TABLE target_state ADD COLUMN IF NOT EXISTS live_room_id TEXT;

    -- Old poll_meta had a single id=1 row; migrate it to profile-keyed.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='poll_meta' AND column_name='id'
    ) THEN
        ALTER TABLE poll_meta ADD COLUMN IF NOT EXISTS profile TEXT;
        UPDATE poll_meta SET profile='default' WHERE profile IS NULL;
        ALTER TABLE poll_meta DROP CONSTRAINT IF EXISTS poll_meta_pkey;
        ALTER TABLE poll_meta DROP COLUMN IF EXISTS id;
        ALTER TABLE poll_meta ALTER COLUMN profile SET NOT NULL;
        ALTER TABLE poll_meta ADD PRIMARY KEY (profile);
    END IF;
END $$;
"""

_pool: ConnectionPool | None = None


def discover_dsn(*, project_dir: Path | None = None, timeout: float = 5.0) -> str:
    """Ask docker-compose for the host port mapped to the postgres container
    and build a DSN. Raises RuntimeError if docker isn't available or the
    service isn't running."""
    if shutil.which("docker") is None:
        raise RuntimeError(
            "docker CLI not found — set DATABASE_URL in .env or run `docker compose up -d`"
        )
    try:
        result = subprocess.run(
            ["docker", "compose", "port", COMPOSE_SERVICE, COMPOSE_INTERNAL_PORT],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(project_dir) if project_dir else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"`docker compose port` timed out after {timeout}s") from exc

    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "could not resolve compose port — is the postgres container running? "
            f"(`docker compose ps` to check). stderr={result.stderr.strip()!r}"
        )

    # Output is like "127.0.0.1:54321" — last line, last colon-segment is port.
    line = result.stdout.strip().splitlines()[-1]
    host_port = line.rsplit(":", 1)[-1].strip()
    if not host_port.isdigit():
        raise RuntimeError(f"unexpected `docker compose port` output: {line!r}")

    return (
        f"postgresql://{COMPOSE_USER}:{COMPOSE_PASSWORD}@127.0.0.1:{host_port}/{COMPOSE_DB}"
    )


def init(dsn: str | None, *, wait_seconds: int = 30) -> None:
    """Open the pool, waiting briefly for Postgres to be ready, then ensure
    schema. Safe to call multiple times — re-runs are no-ops.

    If dsn is None, the port is auto-discovered from docker compose."""
    global _pool
    if _pool is not None:
        return

    if dsn is None:
        dsn = discover_dsn()
        log.info("auto-discovered Postgres at %s", _redact(dsn))

    deadline = time.monotonic() + wait_seconds
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pool = ConnectionPool(
                dsn,
                min_size=1,
                max_size=4,
                kwargs={"row_factory": dict_row},
                open=True,
            )
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(SCHEMA_SQL)
                conn.commit()
            _pool = pool
            log.info("connected to Postgres and ensured schema")
            return
        except (psycopg.OperationalError, psycopg.errors.ConnectionFailure) as exc:
            last_err = exc
            log.info("waiting for Postgres… (%s)", exc.__class__.__name__)
            time.sleep(1.5)
    raise RuntimeError(
        f"could not connect to Postgres at {dsn} within {wait_seconds}s: {last_err}"
    )


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _pool_required() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("db.init() must be called before using the pool")
    return _pool


def record_check(
    profile: str,
    username: str,
    status: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Atomically update target_state for (profile, username). Returns a
    transition event dict when the status actually changed.

    `meta` is optional and only used when status == 'live' — it can carry
    nickname / title / viewer_count / room_id from the API detector. On
    live_end transitions those columns are cleared."""
    pool = _pool_required()
    now = datetime.now(timezone.utc)
    event: dict[str, Any] | None = None

    nickname = (meta or {}).get("nickname")
    title = (meta or {}).get("title")
    viewer_count = (meta or {}).get("user_count")
    room_id = (meta or {}).get("room_id")

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, live_started_at FROM target_state "
                "WHERE profile = %s AND username = %s FOR UPDATE",
                (profile, username),
            )
            row = cur.fetchone()

            if row is None:
                live_started = now if status == "live" else None
                cur.execute(
                    "INSERT INTO target_state "
                    "(profile, username, status, last_check, last_change, "
                    " live_started_at, live_nickname, live_title, "
                    " live_viewer_count, live_room_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (profile, username, status, now, now, live_started,
                     nickname if status == "live" else None,
                     title if status == "live" else None,
                     viewer_count if status == "live" else None,
                     room_id if status == "live" else None),
                )
                if status == "live":
                    cur.execute(
                        "INSERT INTO live_events (profile, username, event, at) "
                        "VALUES (%s, %s, 'live_start', %s)",
                        (profile, username, now),
                    )
                    event = {"event": "live_start", "at": now.isoformat(timespec="seconds")}
                elif status == "offline":
                    cur.execute(
                        "INSERT INTO live_events (profile, username, event, at) "
                        "VALUES (%s, %s, 'first_seen_offline', %s)",
                        (profile, username, now),
                    )
            else:
                prev = row["status"]
                started = row["live_started_at"]

                if status == "unknown":
                    cur.execute(
                        "UPDATE target_state SET last_check = %s, updated_at = NOW() "
                        "WHERE profile = %s AND username = %s",
                        (now, profile, username),
                    )
                elif status == "live" and prev == "live":
                    # Still live — refresh metadata (viewer_count moves).
                    cur.execute(
                        "UPDATE target_state SET last_check=%s, "
                        "live_nickname=%s, live_title=%s, "
                        "live_viewer_count=%s, live_room_id=%s, "
                        "updated_at=NOW() "
                        "WHERE profile=%s AND username=%s",
                        (now, nickname, title, viewer_count, room_id,
                         profile, username),
                    )
                elif status == prev:
                    cur.execute(
                        "UPDATE target_state SET last_check = %s, updated_at = NOW() "
                        "WHERE profile = %s AND username = %s",
                        (now, profile, username),
                    )
                elif status == "live":
                    cur.execute(
                        "UPDATE target_state SET status=%s, last_check=%s, "
                        "last_change=%s, live_started_at=%s, "
                        "live_nickname=%s, live_title=%s, "
                        "live_viewer_count=%s, live_room_id=%s, "
                        "updated_at=NOW() "
                        "WHERE profile=%s AND username=%s",
                        (status, now, now, now,
                         nickname, title, viewer_count, room_id,
                         profile, username),
                    )
                    cur.execute(
                        "INSERT INTO live_events (profile, username, event, at) "
                        "VALUES (%s, %s, 'live_start', %s)",
                        (profile, username, now),
                    )
                    event = {"event": "live_start", "at": now.isoformat(timespec="seconds")}
                elif status == "offline":
                    duration = (
                        int((now - started).total_seconds()) if started else None
                    )
                    cur.execute(
                        "UPDATE target_state SET status=%s, last_check=%s, "
                        "last_change=%s, live_started_at=NULL, "
                        "live_nickname=NULL, live_title=NULL, "
                        "live_viewer_count=NULL, live_room_id=NULL, "
                        "updated_at=NOW() "
                        "WHERE profile=%s AND username=%s",
                        (status, now, now, profile, username),
                    )
                    cur.execute(
                        "INSERT INTO live_events "
                        "(profile, username, event, at, duration_seconds) "
                        "VALUES (%s, %s, 'live_end', %s, %s)",
                        (profile, username, now, duration),
                    )
                    event = {
                        "event": "live_end",
                        "at": now.isoformat(timespec="seconds"),
                    }
                    if duration is not None:
                        event["duration_seconds"] = duration

            cur.execute(
                "INSERT INTO poll_meta (profile, last_poll_at) VALUES (%s, %s) "
                "ON CONFLICT (profile) DO UPDATE SET last_poll_at = EXCLUDED.last_poll_at",
                (profile, now),
            )
        conn.commit()
    return event


def get_state() -> dict[str, Any]:
    """Snapshot for the web dashboard. Shape:

    {
      "profiles": {
        "<profile_name>": {
          "targets": {"<username>": {status, last_check, ..., history: [...]}},
          "last_poll_at": "iso",
        },
        ...
      },
      "last_poll_at": "iso" | None,   # max across profiles, for the global pill
    }
    """
    pool = _pool_required()
    profiles: dict[str, dict[str, Any]] = {}

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT profile, username, status, last_check, last_change, "
                "live_started_at, live_nickname, live_title, live_viewer_count, "
                "live_room_id "
                "FROM target_state ORDER BY profile, username"
            )
            for r in cur.fetchall():
                prof = profiles.setdefault(
                    r["profile"], {"targets": {}, "last_poll_at": None}
                )
                prof["targets"][r["username"]] = {
                    "status": r["status"],
                    "last_check": _iso(r["last_check"]),
                    "last_change": _iso(r["last_change"]),
                    "live_started_at": _iso(r["live_started_at"]),
                    "live_nickname": r["live_nickname"],
                    "live_title": r["live_title"],
                    "live_viewer_count": r["live_viewer_count"],
                    "live_room_id": r["live_room_id"],
                    "history": [],
                }

            if profiles:
                cur.execute(
                    "SELECT profile, username, event, at, duration_seconds "
                    "FROM live_events ORDER BY at DESC LIMIT 500"
                )
                for r in cur.fetchall():
                    prof = profiles.get(r["profile"])
                    if not prof:
                        continue
                    target = prof["targets"].get(r["username"])
                    if not target:
                        continue
                    target["history"].append(
                        {
                            "event": r["event"],
                            "at": _iso(r["at"]),
                            "duration_seconds": r["duration_seconds"],
                        }
                    )

            cur.execute("SELECT profile, last_poll_at FROM poll_meta")
            for r in cur.fetchall():
                if r["profile"] in profiles:
                    profiles[r["profile"]]["last_poll_at"] = _iso(r["last_poll_at"])

    overall_last = max(
        (p["last_poll_at"] for p in profiles.values() if p["last_poll_at"]),
        default=None,
    )
    return {"profiles": profiles, "last_poll_at": overall_last}


def record_sample(profile: str, username: str, meta: dict[str, Any]) -> None:
    """Insert one time-series sample of the target's live state.

    Called from the scout each polling cycle that the target is detected
    live. `meta` is the dict returned by detector_api.status_map with all
    8 graphable fields populated."""
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO live_samples "
                "(profile, username, title, user_count, like_count, "
                " hashtag_title, follower_count, following_count, "
                " live_room_mode, live_level) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    profile,
                    username,
                    meta.get("title"),
                    meta.get("user_count"),
                    meta.get("like_count"),
                    meta.get("hashtag_title"),
                    meta.get("follower_count"),
                    meta.get("following_count"),
                    meta.get("live_room_mode"),
                    meta.get("live_level"),
                ),
            )
        conn.commit()


def get_sessions(
    profile: str, username: str, since_seconds: int = 60 * 86400
) -> list[dict[str, Any]]:
    """Return live sessions for (profile, username), oldest first.

    Completed sessions are pulled from the `live_events` log (each
    `live_end` row carries its duration so we can derive the start).
    An ongoing session (if the target is currently live) is appended
    from `target_state`."""
    pool = _pool_required()
    sessions: list[dict[str, Any]] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT (at - make_interval(secs => duration_seconds)) "
                "         AS started_at, "
                "       at AS ended_at, "
                "       duration_seconds "
                "FROM live_events "
                "WHERE profile=%s AND username=%s "
                "  AND event='live_end' "
                "  AND duration_seconds IS NOT NULL "
                "  AND at >= NOW() - make_interval(secs => %s) "
                "ORDER BY at ASC",
                (profile, username, since_seconds),
            )
            for r in cur.fetchall():
                sessions.append(
                    {
                        "start": _iso(r["started_at"]),
                        "end": _iso(r["ended_at"]),
                        "duration_seconds": r["duration_seconds"],
                        "ongoing": False,
                    }
                )

            cur.execute(
                "SELECT live_started_at, live_title, live_viewer_count "
                "FROM target_state "
                "WHERE profile=%s AND username=%s AND status='live' "
                "  AND live_started_at IS NOT NULL",
                (profile, username),
            )
            row = cur.fetchone()
            if row:
                sessions.append(
                    {
                        "start": _iso(row["live_started_at"]),
                        "end": None,
                        "duration_seconds": None,
                        "title": row["live_title"],
                        "viewer_count": row["live_viewer_count"],
                        "ongoing": True,
                    }
                )

    return sessions


def get_samples(
    profile: str, username: str, since_seconds: int
) -> list[dict[str, Any]]:
    """Return samples for (profile, username) within the last
    `since_seconds`, oldest first."""
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT at, title, user_count, like_count, hashtag_title, "
                "follower_count, following_count, live_room_mode, live_level "
                "FROM live_samples "
                "WHERE profile=%s AND username=%s "
                "  AND at >= NOW() - make_interval(secs => %s) "
                "ORDER BY at ASC",
                (profile, username, since_seconds),
            )
            return [
                {
                    "at": _iso(r["at"]),
                    "title": r["title"],
                    "user_count": r["user_count"],
                    "like_count": r["like_count"],
                    "hashtag_title": r["hashtag_title"],
                    "follower_count": r["follower_count"],
                    "following_count": r["following_count"],
                    "live_room_mode": r["live_room_mode"],
                    "live_level": r["live_level"],
                }
                for r in cur.fetchall()
            ]


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _redact(dsn: str) -> str:
    """Strip the password from a DSN for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse

        u = urlparse(dsn)
        if u.password:
            netloc = f"{u.username}:***@{u.hostname}:{u.port}"
            return urlunparse((u.scheme, netloc, u.path, "", "", ""))
    except Exception:  # noqa: BLE001
        pass
    return dsn
