"""API-based live detector.

Instead of scraping the DOM, this reads the JSON returned by TikTok's
internal `webcast.tiktok.com/webcast/feed/?channel_id=42` endpoint — the
same call the /live page makes to populate its left-sidebar "Following"
section.

Why we don't just call the URL ourselves:
    The request needs `msToken` / `X-Bogus` / `X-Gnarly` signing that
    TikTok's webmssdk.js generates on the client. Reproducing the signer
    is brittle (it rotates every few weeks).

What we do instead:
    Subscribe to Playwright's response events. When TikTok itself fires
    the Following-channel call, we capture the parsed response. To force
    a fresh call each polling cycle we just `page.reload()` — the page's
    own JS does the signing dance for us. Cost per cycle: one `/live`
    reload, ~1-2s.

Response shape (relevant slice):
  {
    "status_code": 0,
    "data": [
      {
        "data": {
          "title": "...",
          "status": 2,
          "user_count": 154,
          "like_count": 58167,
          "id_str": "<room_id>",
          "owner": {
            "display_id": "rkbnews4ch",
            "nickname": "RKB毎日放送NEWS",
            "sec_uid": "...",
          }
        }
      },
      ...
    ]
  }
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import Page, Response

log = logging.getLogger(__name__)

LIVE_URL = "https://www.tiktok.com/live"


class FollowingFeedListener:
    """Captures `/webcast/feed/?channel_id=42` responses that TikTok itself
    fires and exposes the latest entries to the scout."""

    def __init__(self) -> None:
        self._latest: list[dict] | None = None
        self._pending: asyncio.Event | None = None

    def attach(self, page: Page) -> None:
        page.on("response", lambda r: asyncio.create_task(self._on_response(r)))

    async def _on_response(self, resp: Response) -> None:
        try:
            if "webcast/feed/" not in resp.url:
                return
            if "channel_id=42" not in resp.url:
                return
            try:
                body = await resp.json()
            except Exception:  # noqa: BLE001
                return
            if not isinstance(body, dict):
                return
            if body.get("status_code") not in (0, None):
                log.debug("ignoring feed response with status_code=%s",
                          body.get("status_code"))
                return
            self._latest = body.get("data") or []
            if self._pending is not None and not self._pending.is_set():
                self._pending.set()
        except Exception as exc:  # noqa: BLE001
            log.debug("response handler swallowed: %s", exc)

    async def fetch(self, page: Page, *, timeout: float = 12.0) -> list[dict] | None:
        """Force a fresh /webcast/feed call by reloading the /live page,
        then wait up to `timeout` seconds for the response handler to
        capture it. Returns the entries, or None on failure."""
        self._pending = asyncio.Event()
        try:
            # If we drifted off /live (e.g. an internal redirect), navigate
            # back. Otherwise a plain reload is enough.
            if "/live" not in page.url.split("?", 1)[0]:
                await page.goto(LIVE_URL, wait_until="domcontentloaded", timeout=30000)
            else:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:  # noqa: BLE001
            log.warning("page reload failed: %s", exc)
            return None

        try:
            await asyncio.wait_for(self._pending.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning(
                "no /webcast/feed channel_id=42 response within %.0fs", timeout
            )
            return None
        return self._latest


def _safe_live_level(owner: dict) -> str | None:
    """badge_list[0].Data.Combine.str — owner's live-streaming level. Optional."""
    badges = owner.get("badge_list") or []
    if not badges:
        return None
    combine = ((badges[0] or {}).get("Data") or {}).get("Combine") or {}
    return combine.get("str")


def status_map(entries: list[dict], targets: list[str]) -> dict[str, dict[str, Any]]:
    """Build {username -> info} for each target.

    Live target: status='live' plus all the metadata we persist per cycle
    (the 8 graphable fields + a few extras for notifications).
    Offline target: status='offline'.
    """
    live: dict[str, dict[str, Any]] = {}
    for entry in entries:
        room = (entry or {}).get("data") or {}
        owner = room.get("owner") or {}
        username = owner.get("display_id")
        if not username:
            continue
        follow_info = owner.get("follow_info") or {}
        hashtag = room.get("hashtag") or {}
        live[username] = {
            "status": "live",
            # Identity / display
            "nickname": owner.get("nickname"),
            "sec_uid": owner.get("sec_uid"),
            "room_id": room.get("id_str"),
            # The 8 graphable fields (persisted to live_samples each cycle)
            "title": room.get("title"),
            "user_count": room.get("user_count"),
            "like_count": room.get("like_count"),
            "hashtag_title": hashtag.get("title"),
            "follower_count": follow_info.get("follower_count"),
            "following_count": follow_info.get("following_count"),
            "live_room_mode": room.get("live_room_mode"),
            "live_level": _safe_live_level(owner),
        }
    return {t: live.get(t, {"status": "offline"}) for t in targets}
