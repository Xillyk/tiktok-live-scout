"""Discord webhook sender. Best-effort: failures are logged, never raised."""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


async def send(webhook_url: str | None, content: str, embed: dict[str, Any] | None = None) -> None:
    if not webhook_url:
        return
    payload: dict[str, Any] = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook_url, json=payload)
            if r.status_code >= 300:
                log.warning("discord webhook returned %s: %s", r.status_code, r.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("discord webhook failed: %s", exc)


def live_start_embed(
    username: str,
    started_at: str,
    *,
    nickname: str | None = None,
    title: str | None = None,
    viewer_count: int | None = None,
) -> dict[str, Any]:
    embed: dict[str, Any] = {
        "title": f"{nickname or username} is LIVE"
        + (f" — {title}" if title else ""),
        "url": f"https://www.tiktok.com/@{username}/live",
        "color": 0xFE2C55,
        "fields": [
            {"name": "Username", "value": f"@{username}", "inline": True},
            {"name": "Started", "value": started_at, "inline": True},
        ],
    }
    if viewer_count is not None:
        embed["fields"].append(
            {"name": "Viewers", "value": str(viewer_count), "inline": True}
        )
    return embed


def live_end_embed(username: str, ended_at: str, duration_seconds: int | None) -> dict[str, Any]:
    fields = [{"name": "Ended", "value": ended_at, "inline": True}]
    if duration_seconds is not None:
        h, rem = divmod(duration_seconds, 3600)
        m, s = divmod(rem, 60)
        fields.append(
            {"name": "Duration", "value": f"{h:d}h {m:02d}m {s:02d}s", "inline": True}
        )
    return {
        "title": f"@{username} live ended",
        "url": f"https://www.tiktok.com/@{username}",
        "color": 0x808080,
        "fields": fields,
    }
