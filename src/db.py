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
    username         TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    last_check       TIMESTAMPTZ,
    last_change      TIMESTAMPTZ,
    live_started_at  TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS live_events (
    id               BIGSERIAL PRIMARY KEY,
    username         TEXT NOT NULL,
    event            TEXT NOT NULL,
    at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_seconds INTEGER
);

CREATE INDEX IF NOT EXISTS live_events_username_at_idx
    ON live_events (username, at DESC);

CREATE TABLE IF NOT EXISTS poll_meta (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_poll_at  TIMESTAMPTZ
);

INSERT INTO poll_meta (id, last_poll_at) VALUES (1, NULL)
    ON CONFLICT (id) DO NOTHING;
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


def record_check(username: str, status: str) -> dict[str, Any] | None:
    """Atomically update target_state. Returns a transition event dict
    {"event": ..., "at": iso, "duration_seconds": int?} when the status
    actually changed, otherwise None.

    Mirrors the old state.record_check semantics:
      * unknown after a known state -> no transition, last_check still bumps
      * known status equal to prior -> no transition, last_check still bumps
      * live <-> offline -> transition event written to live_events
    """
    pool = _pool_required()
    now = datetime.now(timezone.utc)
    event: dict[str, Any] | None = None

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, live_started_at FROM target_state "
                "WHERE username = %s FOR UPDATE",
                (username,),
            )
            row = cur.fetchone()

            if row is None:
                live_started = now if status == "live" else None
                cur.execute(
                    "INSERT INTO target_state "
                    "(username, status, last_check, last_change, live_started_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (username, status, now, now, live_started),
                )
                if status == "live":
                    cur.execute(
                        "INSERT INTO live_events (username, event, at) "
                        "VALUES (%s, 'live_start', %s)",
                        (username, now),
                    )
                    event = {"event": "live_start", "at": now.isoformat(timespec="seconds")}
                elif status == "offline":
                    cur.execute(
                        "INSERT INTO live_events (username, event, at) "
                        "VALUES (%s, 'first_seen_offline', %s)",
                        (username, now),
                    )
            else:
                prev = row["status"]
                started = row["live_started_at"]

                if status == "unknown" or status == prev:
                    cur.execute(
                        "UPDATE target_state SET last_check = %s, updated_at = NOW() "
                        "WHERE username = %s",
                        (now, username),
                    )
                elif status == "live":
                    cur.execute(
                        "UPDATE target_state SET status=%s, last_check=%s, "
                        "last_change=%s, live_started_at=%s, updated_at=NOW() "
                        "WHERE username=%s",
                        (status, now, now, now, username),
                    )
                    cur.execute(
                        "INSERT INTO live_events (username, event, at) "
                        "VALUES (%s, 'live_start', %s)",
                        (username, now),
                    )
                    event = {"event": "live_start", "at": now.isoformat(timespec="seconds")}
                elif status == "offline":
                    duration = (
                        int((now - started).total_seconds()) if started else None
                    )
                    cur.execute(
                        "UPDATE target_state SET status=%s, last_check=%s, "
                        "last_change=%s, live_started_at=NULL, updated_at=NOW() "
                        "WHERE username=%s",
                        (status, now, now, username),
                    )
                    cur.execute(
                        "INSERT INTO live_events (username, event, at, duration_seconds) "
                        "VALUES (%s, 'live_end', %s, %s)",
                        (username, now, duration),
                    )
                    event = {
                        "event": "live_end",
                        "at": now.isoformat(timespec="seconds"),
                    }
                    if duration is not None:
                        event["duration_seconds"] = duration

            cur.execute(
                "UPDATE poll_meta SET last_poll_at = %s WHERE id = 1", (now,)
            )
        conn.commit()
    return event


def get_state() -> dict[str, Any]:
    """Snapshot for the web dashboard: targets keyed by username + last poll."""
    pool = _pool_required()
    targets: dict[str, Any] = {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, status, last_check, last_change, live_started_at "
                "FROM target_state ORDER BY username"
            )
            for r in cur.fetchall():
                targets[r["username"]] = {
                    "status": r["status"],
                    "last_check": _iso(r["last_check"]),
                    "last_change": _iso(r["last_change"]),
                    "live_started_at": _iso(r["live_started_at"]),
                    "history": [],
                }

            if targets:
                cur.execute(
                    "SELECT username, event, at, duration_seconds FROM live_events "
                    "WHERE username = ANY(%s) "
                    "ORDER BY at DESC LIMIT 200",
                    (list(targets.keys()),),
                )
                for r in cur.fetchall():
                    targets[r["username"]]["history"].append(
                        {
                            "event": r["event"],
                            "at": _iso(r["at"]),
                            "duration_seconds": r["duration_seconds"],
                        }
                    )

            cur.execute("SELECT last_poll_at FROM poll_meta WHERE id = 1")
            row = cur.fetchone()
            last_poll = _iso(row["last_poll_at"]) if row else None

    return {"targets": targets, "last_poll_at": last_poll}


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
