"""v2 — Discord webhook sender. Best-effort: failures are logged, never raised.

Reads `DISCORD_WEBHOOK_URL` from .env (or the process environment). If unset,
`send()` becomes a silent no-op so the scout works fine without Discord.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()  # populate os.environ from .env

log = logging.getLogger(__name__)


def webhook_url() -> str | None:
    """Re-read on every call so .env edits don't require a restart in dev."""
    return os.getenv("DISCORD_WEBHOOK_URL") or None


async def send(content: str | None = None,
               embed: dict[str, Any] | None = None) -> None:
    url = webhook_url()
    if not url:
        return
    payload: dict[str, Any] = {}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]
    if not payload:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload)
            if r.status_code >= 300:
                log.warning("discord webhook returned %s: %s",
                            r.status_code, r.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("discord webhook failed: %r", exc)


# ---------------------------------------------------------------- embeds

def live_start_embed(
    username: str,
    *,
    nickname: str | None = None,
    title: str | None = None,
    follower_count: int | None = None,
    viewer_count: int | None = None,
    cover_url: str | None = None,
    started_at: str | None = None,
    session_id: int | None = None,
    dashboard_base: str | None = None,
) -> dict[str, Any]:
    fields: list[dict[str, Any]] = [
        {"name": "@handle", "value": f"`@{username}`", "inline": True},
    ]
    if follower_count is not None:
        fields.append({"name": "👥 Followers",
                       "value": f"{follower_count:,}", "inline": True})
    if viewer_count is not None:
        fields.append({"name": "👀 Viewers now",
                       "value": f"{viewer_count:,}", "inline": True})
    if started_at:
        fields.append({"name": "Started", "value": started_at, "inline": True})
    if session_id is not None and dashboard_base:
        fields.append({
            "name": "Dashboard",
            "value": f"[session #{session_id}]({dashboard_base.rstrip('/')}/session/{session_id})",
            "inline": True,
        })
    embed: dict[str, Any] = {
        "title": f"🔴 {nickname or '@' + username} is LIVE",
        "url": f"https://www.tiktok.com/@{username}/live",
        "color": 0xFE2C55,
        "fields": fields,
    }
    if title:
        embed["description"] = f"📺 **{title}**"
    if cover_url:
        embed["thumbnail"] = {"url": cover_url}
    return embed


def live_end_embed(
    username: str,
    *,
    nickname: str | None = None,
    end_reason: str | None = None,
    duration_seconds: int | None = None,
    peak_viewers: int | None = None,
    total_likes: int | None = None,
    total_comments: int | None = None,
    total_gifts: int | None = None,
    total_diamonds: int | None = None,
    total_follows: int | None = None,
    session_id: int | None = None,
    dashboard_base: str | None = None,
) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    if duration_seconds is not None:
        h, rem = divmod(int(duration_seconds), 3600)
        m, s = divmod(rem, 60)
        fields.append({"name": "Duration",
                       "value": f"{h:d}h {m:02d}m {s:02d}s", "inline": True})
    if peak_viewers is not None:
        fields.append({"name": "👀 Peak", "value": f"{peak_viewers:,}", "inline": True})
    if total_likes is not None:
        fields.append({"name": "❤ Likes", "value": f"{total_likes:,}", "inline": True})
    if total_comments is not None:
        fields.append({"name": "💬 Comments", "value": f"{total_comments:,}", "inline": True})
    if total_gifts is not None:
        fields.append({"name": "🎁 Gifts", "value": f"{total_gifts:,}", "inline": True})
    if total_diamonds is not None:
        fields.append({"name": "💎 Diamonds",
                       "value": f"{total_diamonds:,}", "inline": True})
    if total_follows is not None:
        fields.append({"name": "➕ Follows",
                       "value": f"{total_follows:,}", "inline": True})
    if session_id is not None and dashboard_base:
        fields.append({
            "name": "Recap",
            "value": f"[session #{session_id}]({dashboard_base.rstrip('/')}/session/{session_id})",
            "inline": True,
        })
    return {
        "title": f"⏹ {nickname or '@' + username} live ended",
        "url": f"https://www.tiktok.com/@{username}",
        "color": 0x808080,
        "description": f"end reason: `{end_reason or 'unknown'}`",
        "fields": fields,
    }
