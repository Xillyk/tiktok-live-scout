"""v2 scout — TikTokLive WebSocket → Postgres.

Architecture:
  1. discovery loop  – cheap HTML GET of /@<user>/live, parse SIGI_STATE,
                       only proceed when liveRoom.user.status == 2
  2. session phase    – open v2_sessions row (snapshot from SIGI),
                        connect TikTokLive client,
                        buffer events, flush every FLUSH_INTERVAL_S
                        write v2_snapshots every SNAPSHOT_INTERVAL_S
  3. on disconnect    – close_session with end_reason, backoff, loop
  4. periodic gc      – every GC_INTERVAL_S, drop noisy events > 7d old

CLI:
  .venv/bin/python -m src.v2.scout --target tiktok.arw
  .venv/bin/python -m src.v2.scout --target tiktok.arw --no-jsonl

PHASE 2 — Low-Latency LLS playback (sub-second video):
  TikTok exposes a .sdp URL for every live room (the LLS / WebRTC variant).
  We already surface it via /api/session/<id>/stream → qualities[*].lls, but
  browsers can't play SDP-over-RTP directly. To unlock sub-second latency:
    1. Stand up a small server-side WebRTC bridge (Janus / mediasoup /
       pion + WHEP) that fetches TikTok's SDP offer, builds the peer
       connection, and re-publishes the resulting media stream as a WHEP
       endpoint the browser can consume.
    2. Front it with a route like /api/session/<id>/whep that returns the
       WHEP offer to the page.
    3. The session page picks up a new mode `mode=lls` and uses native
       WHEP playback (or whip-whep-js shim) instead of mpegts.js.
  Keep `.lls` in the stream_info JSON so the day we wire it up requires
  zero scout/db changes.
"""
from __future__ import annotations

# Use macOS Keychain trust so local TLS inspectors (Intego/AV) are honored.
# Must run before any TLS-using import.
import truststore
truststore.inject_into_ssl()

import argparse
import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    CommentEvent,
    ConnectEvent,
    DisconnectEvent,
    FollowEvent,
    GiftEvent,
    JoinEvent,
    LikeEvent,
    LiveEndEvent,
    RoomUserSeqEvent,
    ShareEvent,
    SubscribeEvent,
)

import os

from . import db, notify

log = logging.getLogger("v2.scout")

# Track sessions we've already notified about within this scout process so
# TikTokLive reconnects within the same live don't spam Discord.
_NOTIFIED_START: set[int] = set()
_NOTIFIED_END: set[int] = set()

# Where to point the dashboard links in the Discord embeds.
DASHBOARD_BASE = os.environ.get("V2_DASHBOARD_BASE", "http://127.0.0.1:8767")
# A session that's been running longer than this when the scout connects is
# treated as a "reconnect to an in-progress live" (no live_start notification).
LIVE_START_FRESHNESS_S = 180

# Tunables. Conservative defaults; tweak per ops profile.
DISCOVER_POLL_INTERVAL_S = 60          # how often to re-check an offline target
FLUSH_INTERVAL_S = 5                   # event buffer flush cadence
FLUSH_BATCH_SIZE = 200                 # also flush when buffer hits this size
SNAPSHOT_INTERVAL_S = 5                # v2_snapshots row cadence
GC_INTERVAL_S = 3600                   # noisy-event GC cadence
INITIAL_BACKOFF_S = 5.0
BACKOFF_MULTIPLIER = 1.6
MAX_BACKOFF_S = 300.0

SIGI_RE = re.compile(
    r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', re.DOTALL,
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------- discovery

async def discover(username: str) -> dict | None:
    """One HTTP GET to /@user/live. Parse SIGI_STATE. Return a descriptor
    with room_id/title/cover_url/started_at/follower_count if the target's
    user.status == 2, else None."""
    url = f"https://www.tiktok.com/@{username}/live"
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
        ) as c:
            r = await c.get(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("discover: HTTP error for @%s: %r", username, exc)
        return None
    if r.status_code != 200:
        log.info("discover: @%s returned http=%d", username, r.status_code)
        return None
    m = SIGI_RE.search(r.text)
    if not m:
        log.info("discover: SIGI_STATE not present in @%s HTML", username)
        return None
    try:
        sigi = json.loads(m.group(1))
    except json.JSONDecodeError:
        log.warning("discover: SIGI_STATE not parseable for @%s", username)
        return None
    info = (sigi.get("LiveRoom") or {}).get("liveRoomUserInfo") or {}
    user = info.get("user") or {}
    if user.get("status") != 2:
        return None
    live = info.get("liveRoom") or {}
    stats = info.get("stats") or {}
    started_at = None
    if live.get("startTime"):
        try:
            started_at = datetime.fromtimestamp(live["startTime"], tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            pass
    return {
        "room_id": user.get("roomId") or live.get("roomId") or str(live.get("id") or ""),
        "title": live.get("title"),
        "cover_url": live.get("coverUrl"),
        "started_at": started_at or datetime.now(timezone.utc),
        "follower_count": stats.get("followerCount"),
        "nickname": user.get("nickname"),
    }


# ---------------------------------------------------------------- session

def _user_fields(user) -> dict:
    if user is None:
        return {}
    return {
        "user_id": (str(getattr(user, "id", "") or "") or None),
        "unique_id": getattr(user, "unique_id", None),
        "nickname": getattr(user, "nickname", None),
    }


def _open_viewer_seq_log(username: str):
    """Per-target raw-dump file for RoomUserSeqEvent diagnostics. Lets us
    inspect every candidate field TikTok pushes (total_user vs total vs
    viewer_count vs total_pv_for_anchor) so we know which one really is
    the concurrent viewer count."""
    path = Path("logs/v2/viewer_seq") / f"{username.replace('.', '_')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a", buffering=1, encoding="utf-8")


async def run_session(
    username: str,
    room_info: dict,
    jsonl_sink: Callable[[dict], None] | None,
) -> str:
    """Connect TikTokLive, persist all events to Postgres for this session.
    Returns the end_reason ('live_end' | 'disconnect' | 'crash:<class>')."""
    session_id = db.open_session(
        username,
        room_info["room_id"],
        room_info["started_at"],
        title=room_info.get("title"),
        cover_url=room_info.get("cover_url"),
        follower_count=room_info.get("follower_count"),
    )
    log.info("session %d opened for @%s (room_id=%s, title=%r)",
             session_id, username, room_info["room_id"], room_info.get("title"))

    # Discord live_start notification — fire once per session, and only when
    # the session is "fresh" so a scout restart mid-stream doesn't re-notify.
    if session_id not in _NOTIFIED_START:
        started = room_info.get("started_at")
        age = (datetime.now(timezone.utc) - started).total_seconds() if started else 0
        if age <= LIVE_START_FRESHNESS_S:
            _NOTIFIED_START.add(session_id)
            asyncio.create_task(notify.send(
                embed=notify.live_start_embed(
                    username=username,
                    nickname=room_info.get("nickname"),
                    title=room_info.get("title"),
                    follower_count=room_info.get("follower_count"),
                    viewer_count=None,   # not known yet until first viewer_seq
                    cover_url=room_info.get("cover_url"),
                    started_at=room_info["started_at"].astimezone().strftime("%Y-%m-%d %H:%M")
                                if started else None,
                    session_id=session_id,
                    dashboard_base=DASHBOARD_BASE,
                ),
            ))
            log.info("→ discord live_start sent for @%s (session %d)",
                     username, session_id)
        else:
            log.info("scout reconnecting to in-progress session %d (age %.0fs) — "
                     "skipping live_start notification", session_id, age)
            _NOTIFIED_START.add(session_id)   # mark so we don't keep checking

    buffer: list[tuple] = []
    deltas: dict[str, int] = {}
    latest_viewer: dict[str, int | None] = {"vc": None}

    def _inc(key: str, n: int = 1) -> None:
        if n:
            deltas[key] = deltas.get(key, 0) + n

    def _max(key: str, val: int) -> None:
        cur = deltas.get(key, 0)
        if val > cur:
            deltas[key] = val

    def _set(key: str, val: int) -> None:
        deltas[key] = val

    def queue(kind: str, payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        buffer.append(db.event_row(session_id, now, kind, payload))
        if jsonl_sink:
            jsonl_sink({
                "at": now.isoformat(timespec="seconds"),
                "target": username,
                "event": kind,
                "payload": payload,
            })

    async def flush() -> None:
        if not buffer and not deltas:
            return
        b, d = buffer.copy(), dict(deltas)
        buffer.clear()
        deltas.clear()
        try:
            db.insert_events(b)
            db.bump_aggregates(session_id, d)
        except Exception as exc:  # noqa: BLE001
            log.exception("flush failed: %r", exc)

    async def flush_loop() -> None:
        try:
            while True:
                await asyncio.sleep(FLUSH_INTERVAL_S)
                await flush()
                # Opportunistic big-batch flush is implicit since we drain
                # everything every FLUSH_INTERVAL_S. If buffer grows huge
                # mid-interval (>2*FLUSH_BATCH_SIZE), kick a flush early.
                if len(buffer) >= FLUSH_BATCH_SIZE:
                    await flush()
        except asyncio.CancelledError:
            return

    async def snapshot_loop() -> None:
        try:
            while True:
                await asyncio.sleep(SNAPSHOT_INTERVAL_S)
                try:
                    db.write_snapshot(session_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("snapshot failed: %r", exc)
        except asyncio.CancelledError:
            return

    client = TikTokLiveClient(unique_id=f"@{username}")
    reason = "unknown"

    @client.on(ConnectEvent)
    async def _connect(_: ConnectEvent):
        log.info("✅ WS connected (@%s session=%d)", username, session_id)

    @client.on(DisconnectEvent)
    async def _disconnect(_: DisconnectEvent):
        nonlocal reason
        reason = "disconnect"

    @client.on(LiveEndEvent)
    async def _end(_: LiveEndEvent):
        nonlocal reason
        reason = "live_end"

    @client.on(CommentEvent)
    async def _comment(ev: CommentEvent):
        queue("comment", {**_user_fields(getattr(ev, "user", None)),
                          "comment": getattr(ev, "comment", None)})
        _inc("total_comments")

    @client.on(LikeEvent)
    async def _like(ev: LikeEvent):
        count = getattr(ev, "count", None)
        queue("like", {**_user_fields(getattr(ev, "user", None)),
                       "count": count,
                       "total": getattr(ev, "total", None)})
        _inc("total_likes", int(count) if count else 0)

    @client.on(GiftEvent)
    async def _gift(ev: GiftEvent):
        gift = getattr(ev, "gift", None)
        diamond_per = int(getattr(gift, "diamond_count", 0) or 0)
        repeat_count = int(getattr(ev, "repeat_count", 1) or 1)
        # TikTokLive's documented "streak finished" flag — set only on the
        # last frame of a streakable gift's streak (e.g. Rose). It is
        # ALWAYS False for non-streakable / one-shot gifts like Love Glasses,
        # which is why naïvely filtering on it under-counts large gifts.
        repeat_end = bool(getattr(ev, "repeat_end", False))
        # `streaking` is True ONLY during in-progress streak ticks. For
        # non-streakable gifts it's always False (no streak to be in), so
        # `not streaking` correctly identifies "this gift event is a real
        # purchase to count" for BOTH non-streakable and streak-end frames.
        streaking = bool(getattr(ev, "streaking", False))
        # A gift counts as a real diamond purchase when either:
        #   - the streak just ended (repeat_end), OR
        #   - we're not currently streaking (non-streakable, or streak ended).
        complete = repeat_end or not streaking
        queue("gift", {
            **_user_fields(getattr(ev, "user", None)),
            "gift_id": getattr(gift, "id", None),
            "gift_name": getattr(gift, "name", None),
            "diamond_count": diamond_per,
            "repeat_count": repeat_count,
            "repeat_end": complete,         # treat column as "counted purchase"
            "tt_repeat_end": repeat_end,    # → extras: raw TikTokLive flag
            "tt_streaking": streaking,      # → extras: raw TikTokLive flag
        })
        if complete:
            _inc("total_gifts")
            _inc("total_diamonds", diamond_per * repeat_count)

    @client.on(JoinEvent)
    async def _join(ev: JoinEvent):
        queue("join", _user_fields(getattr(ev, "user", None)))
        _inc("total_joins")

    @client.on(FollowEvent)
    async def _follow(ev: FollowEvent):
        queue("follow", _user_fields(getattr(ev, "user", None)))
        _inc("total_follows")

    @client.on(ShareEvent)
    async def _share(ev: ShareEvent):
        queue("share", _user_fields(getattr(ev, "user", None)))
        _inc("total_shares")

    @client.on(SubscribeEvent)
    async def _sub(ev: SubscribeEvent):
        queue("subscribe", _user_fields(getattr(ev, "user", None)))
        _inc("total_subs")

    seq_log_f = _open_viewer_seq_log(username)

    @client.on(RoomUserSeqEvent)
    async def _seq(ev: RoomUserSeqEvent):
        # Pull every candidate "viewer count" field TikTokLive may surface.
        candidates = {
            "total_user":          getattr(ev, "total_user", None),
            "total":               getattr(ev, "total", None),
            "viewer_count":        getattr(ev, "viewer_count", None),
            "total_pv_for_anchor": getattr(ev, "total_pv_for_anchor", None),
            "anchor_id":           getattr(ev, "anchor_id", None),
        }
        ranks = getattr(ev, "ranks_list", None) or getattr(ev, "ranks", None) or []
        # mTotal = TikTok's "total contributors known to the room" (leaderboard
        # size; may be larger than the top-N we receive in mContributors).
        try:
            _raw_dict_for_mt = ev.to_dict() if hasattr(ev, "to_dict") else {}
        except Exception:  # noqa: BLE001
            _raw_dict_for_mt = {}
        m_total_raw = _raw_dict_for_mt.get("mTotal")
        try:
            m_total = int(m_total_raw) if m_total_raw is not None else None
        except (TypeError, ValueError):
            m_total = None
        # We currently treat total_user as the truth (with fallbacks). Log
        # everything so we can verify against the TikTok UI's viewer counter.
        vc = candidates["total_user"] or candidates["total"] or candidates["viewer_count"]

        # Diagnostic dump — every push gets a JSONL row for later inspection.
        try:
            raw = ev.to_dict() if hasattr(ev, "to_dict") else {}
        except Exception:
            raw = {}
        seq_log_f.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "username": username,
            "session_id": session_id,
            "selected_vc": int(vc) if vc is not None else None,
            "candidates": {k: (int(v) if isinstance(v, (int, float)) else v)
                           for k, v in candidates.items()},
            "ranks_count": len(ranks),
            "raw_keys": sorted(raw.keys())[:50],
            "raw": raw,
        }, default=str, ensure_ascii=False) + "\n")

        queue("viewer_seq", {
            "viewer_count": vc,
            "ranks_count": len(ranks),
            "m_total": m_total,   # → lands in v2_events.extras JSONB
        })
        if vc is not None:
            latest_viewer["vc"] = int(vc)
            _max("peak_viewers", int(vc))
            _set("final_viewers", int(vc))

    flush_task = asyncio.create_task(flush_loop())
    snap_task = asyncio.create_task(snapshot_loop())
    try:
        await client.connect()
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001
        reason = f"crash:{exc.__class__.__name__}"
        log.warning("TikTokLive session crashed: %r", exc)
    finally:
        flush_task.cancel()
        snap_task.cancel()
        # Wait for tasks to actually stop so we don't double-write during
        # the final flush.
        for t in (flush_task, snap_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await flush()  # final drain
        try:
            db.close_session(
                session_id,
                end_reason=reason,
                final_viewers=latest_viewer["vc"],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("close_session failed: %r", exc)
        # Discord live_end notification — only on a clean live_end (the host
        # ended the stream). Disconnects / crashes don't fire because they
        # often resolve to a reconnect rather than the stream actually ending.
        if reason == "live_end" and session_id not in _NOTIFIED_END:
            _NOTIFIED_END.add(session_id)
            try:
                final = db.get_session(session_id) or {}
                started = final.get("started_at")
                ended   = final.get("ended_at")
                dur = None
                if started and ended:
                    dur = int((ended - started).total_seconds())
                asyncio.create_task(notify.send(
                    embed=notify.live_end_embed(
                        username=username,
                        nickname=room_info.get("nickname"),
                        end_reason=reason,
                        duration_seconds=dur,
                        peak_viewers=final.get("peak_viewers"),
                        total_likes=final.get("total_likes"),
                        total_comments=final.get("total_comments"),
                        total_gifts=final.get("total_gifts"),
                        total_diamonds=final.get("total_diamonds"),
                        total_follows=final.get("total_follows"),
                        session_id=session_id,
                        dashboard_base=DASHBOARD_BASE,
                    ),
                ))
                log.info("→ discord live_end sent for @%s (session %d)",
                         username, session_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("notify live_end failed: %r", exc)
        try:
            seq_log_f.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("session %d closed (reason=%s)", session_id, reason)
    return reason


# ---------------------------------------------------------------- main loop

async def scout_target(username: str, jsonl_path: Path | None) -> None:
    db.upsert_target(username)

    jsonl_f = None
    jsonl_sink: Callable[[dict], None] | None = None
    if jsonl_path:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        jsonl_f = open(jsonl_path, "a", buffering=1, encoding="utf-8")
        def _write(row: dict) -> None:
            jsonl_f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        jsonl_sink = _write
        log.info("audit JSONL: %s", jsonl_path)

    last_gc_at = 0.0
    backoff = INITIAL_BACKOFF_S
    try:
        while True:
            # Discovery
            room_info = await discover(username)
            if room_info is None:
                log.info("@%s not live; rechecking in %ds", username, DISCOVER_POLL_INTERVAL_S)
                await asyncio.sleep(DISCOVER_POLL_INTERVAL_S)
                continue

            log.info("@%s LIVE — title=%r, room_id=%s, started_at=%s",
                     username, room_info.get("title"),
                     room_info.get("room_id"),
                     room_info.get("started_at"))
            try:
                reason = await run_session(username, room_info, jsonl_sink)
            except KeyboardInterrupt:
                return

            # Periodic GC
            now = time.monotonic()
            if now - last_gc_at > GC_INTERVAL_S:
                try:
                    dropped = db.gc()
                    if any(dropped.values()):
                        log.info("gc dropped: %s", dropped)
                except Exception as exc:  # noqa: BLE001
                    log.warning("gc failed: %r", exc)
                last_gc_at = now

            # Backoff before re-entering discovery
            if reason == "live_end":
                # natural end — likely back online soon
                backoff = INITIAL_BACKOFF_S
                wait = INITIAL_BACKOFF_S
            else:
                wait = min(backoff + random.uniform(0, backoff * 0.25), MAX_BACKOFF_S)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_S)
            log.info("backing off %.1fs (reason=%s)", wait, reason)
            await asyncio.sleep(wait)
    finally:
        if jsonl_f:
            jsonl_f.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    ap = argparse.ArgumentParser(description="v2 single-target scout (DB-backed)")
    ap.add_argument("--target", required=True, help="TikTok username (no @)")
    ap.add_argument("--jsonl", default=None,
                    help="Audit JSONL path "
                         "(default: data/v2/events_<target>.jsonl)")
    ap.add_argument("--no-jsonl", action="store_true",
                    help="Skip the audit JSONL — DB only")
    args = ap.parse_args()

    jsonl_path = None
    if not args.no_jsonl:
        jsonl_path = (
            Path(args.jsonl) if args.jsonl
            else Path("data/v2") / f"events_{args.target.replace('.', '_')}.jsonl"
        )

    db.init()
    try:
        asyncio.run(scout_target(args.target, jsonl_path))
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        db.close()


if __name__ == "__main__":
    main()
