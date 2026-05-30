"""v2 — Postgres persistence.

Reuses v1's docker-compose port discovery so a single Postgres container
serves both versions side by side. Tables are prefixed `v2_` to keep the
namespaces separate. Schema is created idempotently on first connect.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

# Reuse v1's DSN discovery (Docker-compose port lookup + redaction).
from src.v1.db import discover_dsn  # noqa: F401  (re-export)

load_dotenv()  # populate DATABASE_URL from .env when present

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS v2_targets (
    username   TEXT PRIMARY KEY,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS v2_sessions (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT NOT NULL,
    room_id         TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    end_reason      TEXT,                 -- 'live_end' | 'disconnect' | 'crash'

    title           TEXT,
    cover_url       TEXT,
    follower_count  BIGINT,

    peak_viewers    INTEGER,
    final_viewers   INTEGER,
    total_comments  BIGINT NOT NULL DEFAULT 0,
    total_likes     BIGINT NOT NULL DEFAULT 0,
    total_gifts     BIGINT NOT NULL DEFAULT 0,
    total_diamonds  BIGINT NOT NULL DEFAULT 0,
    total_joins     BIGINT NOT NULL DEFAULT 0,
    total_follows   BIGINT NOT NULL DEFAULT 0,
    total_shares    BIGINT NOT NULL DEFAULT 0,
    total_subs      BIGINT NOT NULL DEFAULT 0,

    UNIQUE (username, room_id)
);
CREATE INDEX IF NOT EXISTS v2_sessions_username_idx
    ON v2_sessions (username, started_at DESC);
CREATE INDEX IF NOT EXISTS v2_sessions_open_idx
    ON v2_sessions (username) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS v2_events (
    id            BIGSERIAL PRIMARY KEY,
    session_id    BIGINT NOT NULL REFERENCES v2_sessions(id) ON DELETE CASCADE,
    at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind          TEXT NOT NULL,         -- 'comment'|'like'|'gift'|'join'|
                                         -- 'follow'|'share'|'subscribe'|'viewer_seq'

    user_id       TEXT,
    unique_id     TEXT,
    nickname      TEXT,

    comment_text         TEXT,
    like_count           INTEGER,
    like_total           BIGINT,
    gift_id              BIGINT,
    gift_name            TEXT,
    gift_diamond_count   INTEGER,
    gift_repeat_count    INTEGER,
    viewer_count         INTEGER,

    extras JSONB
);
CREATE INDEX IF NOT EXISTS v2_events_session_at_idx
    ON v2_events (session_id, at);
CREATE INDEX IF NOT EXISTS v2_events_session_kind_idx
    ON v2_events (session_id, kind, at);
CREATE INDEX IF NOT EXISTS v2_events_unique_id_idx
    ON v2_events (unique_id) WHERE unique_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS v2_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    session_id    BIGINT NOT NULL REFERENCES v2_sessions(id) ON DELETE CASCADE,
    at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    viewer_count  INTEGER,
    like_total    BIGINT,
    comment_total BIGINT,
    gift_total    BIGINT,
    diamond_total BIGINT,
    join_total    BIGINT,
    follow_total  BIGINT,
    share_total   BIGINT
);
CREATE INDEX IF NOT EXISTS v2_snapshots_session_at_idx
    ON v2_snapshots (session_id, at);

-- Migration: streakable gifts emit N events per streak with a final
-- repeat_end=true event. Without this flag we over-count aggregates.
ALTER TABLE v2_events ADD COLUMN IF NOT EXISTS repeat_end BOOLEAN;
CREATE INDEX IF NOT EXISTS v2_events_gift_completed_idx
    ON v2_events (session_id) WHERE kind = 'gift' AND repeat_end = TRUE;
"""

# Retention policy decided for v2:
#   - comment/gift/like/follow/subscribe/share  → kept forever
#   - join/viewer_seq                           → dropped after this many days
NOISY_RETENTION_DAYS = 7

_pool: ConnectionPool | None = None


# ---------------------------------------------------------------- lifecycle

# Advisory lock key used to serialize concurrent schema bootstraps when
# multiple scout processes start in parallel (the launcher fans out one
# process per target). Arbitrary bigint constant — just needs to be stable.
_INIT_LOCK_KEY = 0x76325F64625F696E  # 'v2_db_in' as ASCII bytes


def init(dsn: str | None = None) -> None:
    """Open the pool and ensure the v2_* schema exists. Idempotent."""
    global _pool
    if _pool is not None:
        return
    if dsn is None:
        dsn = os.environ.get("DATABASE_URL")
        if dsn:
            log.info("v2 db: using DATABASE_URL from env")
        else:
            dsn = discover_dsn()
            log.info("v2 db: auto-discovered DSN via docker compose")
    pool = ConnectionPool(
        dsn, min_size=1, max_size=4,
        kwargs={"row_factory": dict_row}, open=True,
    )
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Without this, concurrent scouts running SCHEMA_SQL deadlock:
            # the IF NOT EXISTS DDLs each take AccessExclusiveLock, and two
            # txns can grab them in different orders. pg_advisory_xact_lock
            # makes later starters wait until the first commits, after which
            # their IF NOT EXISTS statements become no-ops.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_INIT_LOCK_KEY,))
            cur.execute(SCHEMA_SQL)
        conn.commit()
    _pool = pool
    log.info("v2 db: connected, schema ensured")


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _pool_required() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("v2.db.init() must be called before use")
    return _pool


# ---------------------------------------------------------------- writes

def upsert_target(username: str, note: str | None = None) -> None:
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO v2_targets (username, note) VALUES (%s, %s) "
                "ON CONFLICT (username) DO NOTHING",
                (username, note),
            )
        conn.commit()


def open_session(
    username: str,
    room_id: str,
    started_at: datetime,
    *,
    title: str | None = None,
    cover_url: str | None = None,
    follower_count: int | None = None,
) -> int:
    """Open (or reopen) a session row. If (username, room_id) already exists,
    returns the existing id — useful when the scout reconnects mid-session
    after a transient disconnect."""
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO v2_sessions "
                "(username, room_id, started_at, title, cover_url, follower_count) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (username, room_id) DO UPDATE "
                "  SET ended_at = NULL, end_reason = NULL "
                "RETURNING id",
                (username, room_id, started_at, title, cover_url, follower_count),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row["id"])


def close_session(
    session_id: int,
    *,
    end_reason: str,
    final_viewers: int | None = None,
) -> None:
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE v2_sessions SET ended_at=NOW(), end_reason=%s, "
                "  final_viewers = COALESCE(%s, final_viewers) "
                "WHERE id=%s",
                (end_reason, final_viewers, session_id),
            )
        conn.commit()


# Column order mirrors the v2_events DDL so the executemany payload stays tight.
_EVENT_COLS = (
    "session_id", "at", "kind",
    "user_id", "unique_id", "nickname",
    "comment_text",
    "like_count", "like_total",
    "gift_id", "gift_name", "gift_diamond_count", "gift_repeat_count",
    "viewer_count",
    "repeat_end",
    "extras",
)
_EVENT_INSERT_SQL = (
    "INSERT INTO v2_events ("
    + ", ".join(_EVENT_COLS)
    + ") VALUES ("
    + ", ".join(["%s"] * len(_EVENT_COLS))
    + ")"
)


def insert_events(rows: list[tuple]) -> None:
    """Batch insert. Each row must be a tuple matching _EVENT_COLS order.
    The caller is expected to build rows via `event_row(...)` below."""
    if not rows:
        return
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(_EVENT_INSERT_SQL, rows)
        conn.commit()


def event_row(
    session_id: int, at: datetime, kind: str, payload: dict[str, Any],
) -> tuple:
    """Build a row tuple for `insert_events` from the v2.scout payload dict.
    Any payload key we don't have a column for is dropped into `extras`."""
    column_keys = {
        "user_id", "unique_id", "nickname",
        "comment", "comment_text",
        "count", "like_count",
        "total", "like_total",
        "gift_id", "gift_name",
        "diamond_count", "gift_diamond_count",
        "repeat_count", "gift_repeat_count",
        "viewer_count",
        "repeat_end",
    }
    # Pull known fields with their alternate names.
    user_id = payload.get("user_id")
    unique_id = payload.get("unique_id")
    nickname = payload.get("nickname")
    comment_text = payload.get("comment_text") or payload.get("comment")
    like_count = payload.get("like_count") or payload.get("count")
    like_total = payload.get("like_total") or payload.get("total")
    gift_id = payload.get("gift_id")
    gift_name = payload.get("gift_name")
    gift_diamond_count = payload.get("gift_diamond_count") or payload.get("diamond_count")
    gift_repeat_count = payload.get("gift_repeat_count") or payload.get("repeat_count")
    viewer_count = payload.get("viewer_count")
    repeat_end = payload.get("repeat_end")

    # Anything else goes to extras for forensic value.
    extras = {k: v for k, v in payload.items() if k not in column_keys}
    return (
        session_id, at, kind,
        user_id, unique_id, nickname,
        comment_text,
        like_count, like_total,
        gift_id, gift_name, gift_diamond_count, gift_repeat_count,
        viewer_count,
        repeat_end,
        Json(extras) if extras else None,
    )


def bump_aggregates(session_id: int, deltas: dict[str, int]) -> None:
    """Add deltas to the v2_sessions counters. `deltas` keys must be a subset
    of {total_comments, total_likes, total_gifts, total_diamonds, total_joins,
    total_follows, total_shares, total_subs, peak_viewers, final_viewers}.

    peak_viewers uses GREATEST(); final_viewers is a straight overwrite if
    provided."""
    if not deltas:
        return
    counter_cols = (
        "total_comments", "total_likes", "total_gifts", "total_diamonds",
        "total_joins", "total_follows", "total_shares", "total_subs",
    )
    sets: list[str] = []
    args: list[Any] = []
    for c in counter_cols:
        if c in deltas:
            sets.append(f"{c} = {c} + %s")
            args.append(int(deltas[c]))
    if "peak_viewers" in deltas:
        sets.append("peak_viewers = GREATEST(COALESCE(peak_viewers, 0), %s)")
        args.append(int(deltas["peak_viewers"]))
    if "final_viewers" in deltas:
        sets.append("final_viewers = %s")
        args.append(int(deltas["final_viewers"]))
    if not sets:
        return
    args.append(session_id)
    sql = f"UPDATE v2_sessions SET {', '.join(sets)} WHERE id = %s"
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
        conn.commit()


def write_snapshot(session_id: int) -> None:
    """Append a v2_snapshots row whose totals are read from v2_sessions and
    whose viewer_count is the latest viewer_seq event for the session."""
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO v2_snapshots "
                "(session_id, viewer_count, like_total, comment_total, "
                " gift_total, diamond_total, join_total, follow_total, share_total) "
                "SELECT s.id, "
                "       (SELECT viewer_count FROM v2_events "
                "         WHERE session_id = s.id AND kind = 'viewer_seq' "
                "         ORDER BY at DESC LIMIT 1), "
                "       s.total_likes, s.total_comments, "
                "       s.total_gifts, s.total_diamonds, "
                "       s.total_joins, s.total_follows, s.total_shares "
                "FROM v2_sessions s WHERE s.id = %s",
                (session_id,),
            )
        conn.commit()


# ---------------------------------------------------------------- maintenance

def backfill_gift_streaks(gap_threshold_seconds: int = 15) -> dict[str, int]:
    """One-time backfill for gift events that pre-date the repeat_end column.
    Groups consecutive events with the same (session_id, user_id, gift_id)
    into streaks split on gaps > `gap_threshold_seconds`, marks the LAST
    row of each streak as repeat_end=TRUE, then recomputes
    v2_sessions.total_gifts / total_diamonds from those streak-end rows.

    Returns counts: {marked, sessions_recalculated}."""
    pool = _pool_required()
    out = {"marked": 0, "sessions_recalculated": 0}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Mark one row (the latest in each streak) as repeat_end=true.
            cur.execute(
                """
                WITH events AS (
                    SELECT id, session_id, user_id, gift_id, at,
                           EXTRACT(EPOCH FROM (at - LAG(at) OVER (
                               PARTITION BY session_id, user_id, gift_id
                               ORDER BY at
                           ))) AS gap_sec
                    FROM v2_events WHERE kind = 'gift'
                ),
                streaked AS (
                    SELECT id, session_id, user_id, gift_id,
                           SUM(CASE WHEN gap_sec IS NULL OR gap_sec > %s
                                    THEN 1 ELSE 0 END)
                               OVER (PARTITION BY session_id, user_id, gift_id
                                     ORDER BY at) AS streak_seq
                    FROM events
                ),
                streak_ends AS (
                    SELECT MAX(id) AS streak_end_id
                    FROM streaked
                    GROUP BY session_id, user_id, gift_id, streak_seq
                )
                UPDATE v2_events SET repeat_end = TRUE
                WHERE id IN (SELECT streak_end_id FROM streak_ends)
                  AND COALESCE(repeat_end, FALSE) IS NOT TRUE
                """,
                (gap_threshold_seconds,),
            )
            out["marked"] = cur.rowcount

            # Reset gift aggregates and recompute from repeat_end rows only.
            cur.execute("UPDATE v2_sessions SET total_gifts = 0, total_diamonds = 0")
            cur.execute(
                """
                UPDATE v2_sessions s
                SET total_gifts = agg.gifts,
                    total_diamonds = agg.diamonds
                FROM (
                    SELECT session_id,
                           COUNT(*)::BIGINT AS gifts,
                           SUM(COALESCE(gift_repeat_count, 1) *
                               COALESCE(gift_diamond_count, 0))::BIGINT AS diamonds
                    FROM v2_events
                    WHERE kind = 'gift' AND repeat_end = TRUE
                    GROUP BY session_id
                ) agg
                WHERE s.id = agg.session_id
                """
            )
            out["sessions_recalculated"] = cur.rowcount
        conn.commit()
    return out


def gc(retention_days: int = NOISY_RETENTION_DAYS) -> dict[str, int]:
    """Drop high-volume noise events older than `retention_days`. Returns
    a {kind: deleted_rows} map for logging."""
    pool = _pool_required()
    deleted: dict[str, int] = {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for kind in ("join", "viewer_seq"):
                cur.execute(
                    "DELETE FROM v2_events "
                    "WHERE kind = %s "
                    "  AND at < NOW() - make_interval(days => %s)",
                    (kind, retention_days),
                )
                deleted[kind] = cur.rowcount
        conn.commit()
    return deleted


# ---------------------------------------------------------------- reads (for dashboard)

def list_targets() -> list[dict[str, Any]]:
    """All registered targets with: (a) their currently-open session (if any),
    and (b) lifetime session stats — session_count, latest_started_at,
    latest_ended_at — derived from v2_sessions."""
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.username, t.added_at, t.enabled, t.note, "
                "       s.id AS session_id, s.room_id, s.started_at, s.title, "
                "       s.cover_url, s.follower_count, s.peak_viewers, "
                "       s.final_viewers, s.total_likes, s.total_comments, "
                "       s.total_gifts, s.total_diamonds, s.total_joins, "
                "       s.total_follows, s.total_shares, "
                "       agg.session_count, agg.latest_started_at, "
                "       agg.latest_ended_at "
                "FROM v2_targets t "
                "LEFT JOIN LATERAL ( "
                "  SELECT * FROM v2_sessions "
                "  WHERE username = t.username AND ended_at IS NULL "
                "  ORDER BY started_at DESC LIMIT 1 "
                ") s ON true "
                "LEFT JOIN LATERAL ( "
                "  SELECT COUNT(*)::INT     AS session_count, "
                "         MAX(started_at)   AS latest_started_at, "
                "         MAX(ended_at)     AS latest_ended_at "
                "  FROM v2_sessions WHERE username = t.username "
                ") agg ON true "
                "ORDER BY (s.id IS NOT NULL) DESC, "
                "         agg.latest_started_at DESC NULLS LAST, "
                "         t.username"
            )
            return list(cur.fetchall())


def list_sessions(username: str, since_days: int = 60) -> list[dict[str, Any]]:
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, room_id, started_at, ended_at, end_reason, "
                "       title, cover_url, follower_count, peak_viewers, final_viewers, "
                "       total_comments, total_likes, total_gifts, total_diamonds, "
                "       total_joins, total_follows, total_shares, total_subs "
                "FROM v2_sessions "
                "WHERE username = %s "
                "  AND (started_at >= NOW() - make_interval(days => %s) "
                "       OR ended_at IS NULL) "
                "ORDER BY started_at DESC",
                (username, since_days),
            )
            return list(cur.fetchall())


def get_session(session_id: int) -> dict[str, Any] | None:
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM v2_sessions WHERE id = %s", (session_id,))
            return cur.fetchone()


def get_latest_viewer_seq(session_id: int) -> dict[str, Any]:
    """Last RoomUserSeqEvent we received for this session — used by the
    session page's KPI tiles to show current viewer + mTotal."""
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT viewer_count, "
                "       NULLIF(extras->>'m_total','')::int AS m_total "
                "FROM v2_events "
                "WHERE session_id = %s AND kind = 'viewer_seq' "
                "  AND viewer_count IS NOT NULL "
                "ORDER BY at DESC LIMIT 1",
                (session_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else {}


def get_viewer_series(session_id: int) -> list[dict[str, Any]]:
    """Every RoomUserSeqEvent row (kind='viewer_seq') for the session, in
    push-order. Use this for the live viewer-count chart so you see the raw
    TikTok cadence (~3-5s) instead of the 5s snapshot aggregate.

    Also returns m_total = TikTok's mTotal field (total contributors known
    to the room, i.e. leaderboard size — pulled out of extras JSONB)."""
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT at, viewer_count, "
                "       NULLIF(extras->>'m_total','')::int AS m_total "
                "FROM v2_events "
                "WHERE session_id = %s "
                "  AND kind = 'viewer_seq' "
                "  AND viewer_count IS NOT NULL "
                "ORDER BY at",
                (session_id,),
            )
            return list(cur.fetchall())


def get_snapshots(session_id: int) -> list[dict[str, Any]]:
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT at, viewer_count, like_total, comment_total, "
                "       gift_total, diamond_total, join_total, follow_total, share_total "
                "FROM v2_snapshots WHERE session_id = %s ORDER BY at",
                (session_id,),
            )
            return list(cur.fetchall())


def get_recent_events(
    session_id: int,
    kinds: list[str] | None = None,
    limit: int = 50,
    completed_only: bool = False,
    user_query: str | None = None,
) -> list[dict[str, Any]]:
    """`completed_only=True` filters out interim streak ticks (only rows
    where repeat_end=TRUE) — useful when listing gifts as 'real' purchases
    instead of every WebSocket frame.

    `user_query` (optional) does a case-insensitive substring match against
    unique_id OR nickname. Stripped of a leading '@' so users can paste
    either form."""
    pool = _pool_required()
    sql = ("SELECT at, kind, user_id, unique_id, nickname, comment_text, "
           "       like_count, like_total, gift_id, gift_name, "
           "       gift_diamond_count, gift_repeat_count, viewer_count, "
           "       repeat_end "
           "FROM v2_events WHERE session_id = %s ")
    params: list[Any] = [session_id]
    if kinds:
        placeholders = ",".join(["%s"] * len(kinds))
        sql += f"AND kind IN ({placeholders}) "
        params.extend(kinds)
    if completed_only:
        sql += "AND repeat_end = TRUE "
    if user_query:
        q = user_query.strip().lstrip("@")
        if q:
            sql += "AND (unique_id ILIKE %s OR nickname ILIKE %s) "
            params.extend([f"%{q}%", f"%{q}%"])
    sql += "ORDER BY at DESC LIMIT %s"
    params.append(limit)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def get_top_chatters(session_id: int, limit: int = 10) -> list[dict[str, Any]]:
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT unique_id, nickname, COUNT(*) AS comments "
                "FROM v2_events "
                "WHERE session_id = %s AND kind = 'comment' AND unique_id IS NOT NULL "
                "GROUP BY unique_id, nickname "
                "ORDER BY comments DESC LIMIT %s",
                (session_id, limit),
            )
            return list(cur.fetchall())


def get_top_gifters(session_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Aggregate diamonds per sender using only streak-end rows (repeat_end=
    TRUE). Without this filter the SUM also includes every interim WebSocket
    tick of a streak, which inflates results several-fold for streakable
    gifts like Rose."""
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT unique_id, nickname, "
                "       SUM(COALESCE(gift_diamond_count, 0) * "
                "           COALESCE(gift_repeat_count, 1))::BIGINT AS diamonds, "
                "       COUNT(*) AS gifts "
                "FROM v2_events "
                "WHERE session_id = %s AND kind = 'gift' "
                "  AND unique_id IS NOT NULL "
                "  AND repeat_end = TRUE "
                "GROUP BY unique_id, nickname "
                "ORDER BY diamonds DESC NULLS LAST LIMIT %s",
                (session_id, limit),
            )
            return list(cur.fetchall())


def get_storage_stats() -> dict[str, dict[str, Any]]:
    """Per-username storage footprint: event_count, snapshot_count, and an
    approximate byte share of the v2_events + v2_snapshots tables.

    Byte share = (this user's rows / total rows) × pg_total_relation_size.
    It's a proportional estimate, not a true per-row sum — `pg_column_size`
    over every row would be too expensive to run on every home-page refresh.
    """
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH events_per_user AS (
                    SELECT s.username, COUNT(e.id)::BIGINT AS c
                    FROM v2_sessions s
                    LEFT JOIN v2_events e ON e.session_id = s.id
                    GROUP BY s.username
                ),
                snaps_per_user AS (
                    SELECT s.username, COUNT(sn.id)::BIGINT AS c
                    FROM v2_sessions s
                    LEFT JOIN v2_snapshots sn ON sn.session_id = s.id
                    GROUP BY s.username
                ),
                totals AS (
                    SELECT
                        GREATEST((SELECT COUNT(*) FROM v2_events), 1)    AS te,
                        pg_total_relation_size('v2_events')              AS be,
                        GREATEST((SELECT COUNT(*) FROM v2_snapshots), 1) AS ts,
                        pg_total_relation_size('v2_snapshots')           AS bs
                )
                SELECT
                    t.username,
                    COALESCE(eu.c, 0) AS event_count,
                    COALESCE(su.c, 0) AS snapshot_count,
                    (COALESCE(eu.c, 0)::FLOAT8 / NULLIF(tot.te, 0)) * tot.be
                    + (COALESCE(su.c, 0)::FLOAT8 / NULLIF(tot.ts, 0)) * tot.bs
                    AS approx_bytes
                FROM v2_targets t
                LEFT JOIN events_per_user eu ON eu.username = t.username
                LEFT JOIN snaps_per_user  su ON su.username = t.username
                CROSS JOIN totals tot
                """,
            )
            return {r["username"]: dict(r) for r in cur.fetchall()}


def open_sessions_for(username: str) -> list[dict[str, Any]]:
    pool = _pool_required()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, room_id, started_at, title, cover_url, "
                "  follower_count, peak_viewers, final_viewers, total_likes, "
                "  total_comments, total_gifts, total_diamonds "
                "FROM v2_sessions "
                "WHERE username=%s AND ended_at IS NULL",
                (username,),
            )
            return list(cur.fetchall())
