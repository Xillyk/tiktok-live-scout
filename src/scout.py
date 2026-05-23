"""TikTok Live Scout.

Polling loop that checks the left-sidebar "Following accounts" on the TikTok
homepage to detect when a target is live, and fires Discord + log notifications
on each live_start / live_end transition.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from playwright.async_api import (
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

from . import db, detector_api, notify
from .config import Config, load as load_config

log = logging.getLogger("scout")

HOME_URL = "https://www.tiktok.com/foryou"
LIVE_URL = "https://www.tiktok.com/live"


async def is_logged_in(page: Page, *, wait_timeout: int = 10000) -> bool:
    """Wait for the homepage to hydrate, then return True iff the
    `data-e2e="nav-profile"` link's href is `/@<username>` (not `/@`).
    The login modal being visible takes precedence as a negative signal.

    Why this signal: TikTok's SPA bootstraps with a placeholder href of `/@`
    on the profile nav link, then swaps it for the real username once the
    auth state hydrates. The login modal element is added to the DOM only
    for anonymous visitors."""
    try:
        # Wait for the auth state to actually hydrate — either the modal
        # appears OR the nav-profile gets a real username.
        await page.wait_for_function(
            """
            () => {
                const modal = document.querySelector('[data-e2e="login-modal"]');
                if (modal && modal.offsetParent !== null) return true;
                const nav = document.querySelector('[data-e2e="nav-profile"]');
                if (nav) {
                    const href = nav.getAttribute('href') || '';
                    if (href.startsWith('/@') && href.length > 2) return true;
                }
                return false;
            }
            """,
            timeout=wait_timeout,
        )
    except PWTimeout:
        return False

    modal_visible = await page.evaluate(
        """() => {
            const m = document.querySelector('[data-e2e="login-modal"]');
            return m !== null && m.offsetParent !== null;
        }"""
    )
    if modal_visible:
        return False

    href = await page.evaluate(
        """() => {
            const n = document.querySelector('[data-e2e="nav-profile"]');
            return n ? (n.getAttribute('href') || '') : '';
        }"""
    )
    return href.startswith("/@") and len(href) > 2


async def is_logged_out(page: Page) -> bool:
    """Positive logged-out signal: the interest-picker 'login-modal' overlay
    is shown only to anonymous visitors."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const m = document.querySelector('[data-e2e="login-modal"]');
                    return m !== null && m.offsetParent !== null;
                }"""
            )
        )
    except Exception:  # noqa: BLE001
        return False


async def detect_via_live_page(
    page: Page, usernames: list[str], debug_dir: Path
) -> dict[str, str]:
    """PRIMARY flow. Navigates to https://www.tiktok.com/live and reads the
    'Following' section in the left sidebar — TikTok populates it exclusively
    with followed accounts that are CURRENTLY LIVE. Clicks 'See all' if
    present to make sure the full list is expanded.

    Returns a dict {username -> 'live' | 'offline' | 'unknown'}. A target
    found in the sidebar means LIVE; logged-in but not found means OFFLINE;
    anything else (nav failed, no 'Following' section visible) is UNKNOWN."""
    result: dict[str, str] = {u: "unknown" for u in usernames}

    try:
        await page.goto(LIVE_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:  # noqa: BLE001
        log.warning("nav to /live failed: %s", exc)
        return result

    await page.wait_for_timeout(3000)

    if await is_logged_out(page):
        log.error(
            "session expired on /live — re-run `python -m src.scout --login`"
        )
        await dump_debug(page, debug_dir, "logged_out_live")
        return result

    # Expand the full Following-live list if the "See all" toggle is present.
    await _click_see_all_live(page)

    # Sanity check: the Following heading should exist on the /live page.
    has_following = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('h1, h2, h3, h4, p, div, span'))
            .some(n => n.children.length === 0 &&
                  /^(Following|กำลังติดตาม)$/i.test((n.textContent || '').trim()))
        """
    )
    if not has_following:
        log.warning("no Following section on /live — leaving all unknown")
        await dump_debug(page, debug_dir, "live_no_following")
        return result

    # For each target: present in the left sidebar with non-zero size = live.
    for username in usernames:
        is_live = await page.evaluate(
            """
            (u) => {
                const selectors = [`a[href="/@${u}"]`, `a[href="/@${u}/live"]`];
                for (const sel of selectors) {
                    for (const a of document.querySelectorAll(sel)) {
                        const r = a.getBoundingClientRect();
                        // Sidebar lives in the left ~260px column.
                        if (r.left < 320 && r.width > 0 && r.height > 0) {
                            return true;
                        }
                    }
                }
                return false;
            }
            """,
            username,
        )
        result[username] = "live" if is_live else "offline"

    return result


async def _click_see_all_live(page: Page) -> bool:
    """Click 'See all' (or Thai 'ดูทั้งหมด') near the Following heading on
    the /live page. Safe no-op if the button doesn't exist."""
    try:
        clicked = await page.evaluate(
            """
            () => {
                const heading = Array.from(document.querySelectorAll('h1, h2, h3, h4, p, div, span'))
                    .find(n => n.children.length === 0 &&
                              /^(Following|กำลังติดตาม)$/i.test((n.textContent || '').trim()));
                if (!heading) return false;

                // Walk up looking for a 'See all' / 'ดูทั้งหมด' control.
                const labelRe = /^(see all|see more|view all|view more|ดูทั้งหมด)$/i;
                let cur = heading.parentElement;
                for (let i = 0; i < 6 && cur && cur !== document.body; i++) {
                    const cand = Array.from(cur.querySelectorAll('a, button, span, div, p'))
                        .find(n => n.children.length === 0 &&
                              labelRe.test((n.textContent || '').trim()));
                    if (cand) {
                        cand.scrollIntoView({block: 'center'});
                        cand.click();
                        return true;
                    }
                    cur = cur.parentElement;
                }
                return false;
            }
            """
        )
    except Exception:  # noqa: BLE001
        clicked = False

    if clicked:
        await page.wait_for_timeout(1500)
    return bool(clicked)


LIVE_BADGE_JS = """
(el) => {
    // 1) LIVE text badge anywhere in the link subtree.
    const hasLiveBadge = Array.from(el.querySelectorAll('span, div, p'))
        .some(n =>
            n.children.length === 0 &&
            (n.textContent || '').trim().toUpperCase() === 'LIVE'
        );
    if (hasLiveBadge) return true;

    // 2) Class hint: TikTok component class names often include 'Live'.
    if (el.querySelector(
        '[class*="LiveBadge"], [class*="live-badge"], ' +
        '[class*="LiveAvatar"], [class*="live-avatar"], ' +
        '[class*="LiveBorder"]'
    )) return true;

    return false;
}
"""


async def open_view_all(page: Page) -> bool:
    """Click the 'View all' button next to the Following accounts section.
    Returns True if a click happened, False if we couldn't find the button
    (e.g. the user follows so few accounts the button isn't shown)."""
    clicked = await page.evaluate(
        """
        () => {
            // Find the heading that introduces the Following section.
            const headingRe = /following accounts|กำลังติดตาม|ติดตามอยู่/i;
            const heading = Array.from(document.querySelectorAll('h1, h2, h3, h4, p, div, span'))
                .find(n => n.children.length === 0 && headingRe.test((n.textContent || '')));
            if (!heading) return false;

            // Walk up a few levels and look for a 'view all' / 'see all' control
            // (button, link, or clickable span/div).
            const labelRe = /^(view all|see all|view more|see more|ดูทั้งหมด)$/i;
            let cur = heading;
            for (let i = 0; i < 6 && cur; i++) {
                const cand = Array.from(cur.querySelectorAll('a, button, span, div, p'))
                    .find(n => n.children.length === 0 && labelRe.test((n.textContent || '').trim()));
                if (cand) {
                    cand.scrollIntoView({block: 'center'});
                    cand.click();
                    return true;
                }
                cur = cur.parentElement;
            }
            return false;
        }
        """
    )
    if clicked:
        # Let the modal/panel render.
        await page.wait_for_timeout(1500)
    return bool(clicked)


async def detect_via_following_list(page: Page, username: str) -> str:
    """Click 'View all' on the Following accounts section (if present), then
    locate the target. The list is virtualized, so we scroll the section
    incrementally until either the target appears or no new rows load."""
    selector = f'a[href="/@{username}"]'

    # Tap the View-all expander if it exists (some layouts collapse the list).
    await open_view_all(page)

    # Quick check before scrolling — target may be in the initial chunk.
    try:
        await page.wait_for_selector(selector, timeout=3000, state="attached")
    except PWTimeout:
        scroll_result = await _scroll_following_to_find(page, f"/@{username}")
        log.debug("scroll-to-find result for @%s: %s", username, scroll_result)
        if not scroll_result.get("ok"):
            return "unknown"

    links = await page.query_selector_all(selector)
    if not links:
        return "unknown"

    for link in links:
        try:
            if await link.evaluate(LIVE_BADGE_JS):
                return "live"
        except Exception:  # noqa: BLE001
            continue
    return "offline"


async def _scroll_following_to_find(page: Page, target_href: str) -> dict:
    """Scroll the Following accounts list container in steps, waiting between
    scrolls for new (virtualized) rows to hydrate. Returns
    {ok: bool, reason: str, rows_loaded: int}."""
    try:
        return await page.evaluate(
            """
            async (targetHref) => {
                // Locate the 'Following accounts' heading.
                const heading = Array.from(document.querySelectorAll('h1, h2, h3, h4, p, div, span'))
                    .find(n => n.children.length === 0 &&
                          /following accounts|กำลังติดตาม/i.test((n.textContent || '').trim()));
                if (!heading) return {ok: false, reason: 'no heading'};

                // Walk up to find a scrollable ancestor.
                let container = heading.parentElement;
                while (container && container !== document.body) {
                    const cs = getComputedStyle(container);
                    if (container.scrollHeight > container.clientHeight + 20 &&
                        /auto|scroll/.test(cs.overflowY)) {
                        break;
                    }
                    container = container.parentElement;
                }
                // No scrollable ancestor — try `window` as a last resort
                // (the whole sidebar might scroll with the page).
                const scrollEl = (container && container !== document.body)
                    ? container
                    : (document.scrollingElement || document.documentElement);

                const countLinks = () =>
                    document.querySelectorAll('a[href^="/@"]').length;

                let prevCount = countLinks();
                let prevTop = -1;
                const sel = `a[href="${targetHref}"]`;

                for (let i = 0; i < 80; i++) {
                    if (document.querySelector(sel)) {
                        return {ok: true, iter: i, rows_loaded: countLinks()};
                    }
                    prevTop = scrollEl.scrollTop;
                    scrollEl.scrollTop = scrollEl.scrollHeight;
                    await new Promise(r => setTimeout(r, 450));
                    const curTop = scrollEl.scrollTop;
                    const curCount = countLinks();

                    // Stop if we can't scroll further AND no new rows hydrated.
                    if (curTop === prevTop && curCount === prevCount) {
                        return {
                            ok: !!document.querySelector(sel),
                            reason: 'reached bottom',
                            iter: i,
                            rows_loaded: curCount,
                        };
                    }
                    prevCount = curCount;
                }
                return {
                    ok: !!document.querySelector(sel),
                    reason: 'max iter',
                    rows_loaded: countLinks(),
                };
            }
            """,
            target_href,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"evaluate failed: {exc}"}


async def dump_debug(page: Page, debug_dir: Path, label: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(debug_dir / f"{label}.png"), full_page=True)
        html = await page.content()
        (debug_dir / f"{label}.html").write_text(html)
    except Exception as exc:  # noqa: BLE001
        log.debug("debug dump failed: %s", exc)


async def cycle(
    page: Page,
    cfg: Config,
    profile,
    feed_listener: detector_api.FollowingFeedListener,
) -> None:
    """One polling cycle for the given profile.

    Primary detection is the webcast/feed API. We trigger a fresh fetch by
    reloading /live (so TikTok's own signed call fires) and the response
    listener captures the parsed JSON."""
    entries = await feed_listener.fetch(page)
    if entries is None:
        log.warning("[%s] webcast/feed unreachable — skipping cycle", profile.name)
        return

    statuses = detector_api.status_map(entries, profile.targets)

    for username in profile.targets:
        info = statuses[username]
        status = info["status"]

        event = db.record_check(profile.name, username, status, meta=info)

        # Time-series sample for the detail-page graphs: one row per cycle
        # while the target is live.
        if status == "live":
            try:
                db.record_sample(profile.name, username, info)
            except Exception as exc:  # noqa: BLE001
                log.warning("record_sample failed for @%s: %s", username, exc)
        nick = info.get("nickname")
        viewers = info.get("user_count")
        suffix = ""
        if event and event["event"] in ("live_start", "live_end"):
            suffix = " (transition!)"
        elif status == "live" and viewers is not None:
            suffix = f" — {nick or ''} ({viewers} viewers)".rstrip()
        log.info(
            "[%s] @%s status=%s%s",
            profile.name,
            username,
            status,
            suffix,
        )

        if event and event["event"] == "live_start":
            await notify.send(
                cfg.discord_webhook_url,
                f"[{profile.name}] @{username} is now LIVE → "
                f"https://www.tiktok.com/@{username}/live",
                notify.live_start_embed(
                    username,
                    event["at"],
                    nickname=info.get("nickname"),
                    title=info.get("title"),
                    viewer_count=info.get("user_count"),
                ),
            )
        elif event and event["event"] == "live_end":
            await notify.send(
                cfg.discord_webhook_url,
                f"[{profile.name}] @{username} ended their live",
                notify.live_end_embed(
                    username, event["at"], event.get("duration_seconds")
                ),
            )


async def run_login(page: Page) -> int:
    """Headed login flow. Polls until positive login is detected, or until
    the user closes the browser window. Returns 0 on success, 1 if the
    window was closed before login completed."""
    log.info("Log in via the browser window. I'll wait until you're signed in…")
    while True:
        if page.is_closed():
            log.error(
                "browser window was closed before login completed — session NOT saved"
            )
            return 1
        try:
            if await is_logged_in(page):
                break
        except Exception:  # noqa: BLE001
            # Page might be mid-navigation; that's fine.
            pass
        await asyncio.sleep(3)
    # Give the persistent context a beat to flush cookies to disk.
    await asyncio.sleep(2)
    log.info("Login detected. Session saved to the user_data_dir.")
    return 0


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg)

    if not cfg.profiles:
        log.error("no profiles configured in %s", args.config)
        return 1

    # Pick the profile to run. If only one is defined and no --profile is
    # given, use it implicitly.
    if args.profile:
        try:
            profile = cfg.profile(args.profile)
        except KeyError as exc:
            log.error("%s", exc)
            return 1
    elif len(cfg.profiles) == 1:
        profile = cfg.profiles[0]
    else:
        log.error(
            "multiple profiles configured (%s) — pass --profile <name>",
            ", ".join(p.name for p in cfg.profiles),
        )
        return 1

    if not profile.targets:
        log.error("profile %r has no targets configured", profile.name)
        return 1

    profile.user_data_dir.mkdir(parents=True, exist_ok=True)
    cfg.debug_dump_dir.mkdir(parents=True, exist_ok=True)

    if not (args.login or args.check_login):
        db.init(cfg.database_url)

    # --login is the only mode that runs headed.
    headless = cfg.headless and not args.login

    log.info(
        "starting scout: profile=%s targets=%s interval=%ss headless=%s",
        profile.name,
        [f"@{t}" for t in profile.targets],
        cfg.poll_interval_seconds,
        headless,
    )

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile.user_data_dir),
            headless=headless,
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Always land on the homepage at startup — that's where the
        # nav-profile positive-login signal hydrates. The polling loop's
        # cycle() then drives navigation to /live for actual detection.
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:  # noqa: BLE001
            log.warning("initial navigation failed: %s", exc)

        if args.login:
            try:
                rc = await run_login(page)
            finally:
                try:
                    await ctx.close()
                except Exception as exc:  # noqa: BLE001
                    log.debug("ctx.close raised: %s", exc)
            return rc

        if args.check_login:
            # Give the SPA a beat to hydrate before sampling.
            await page.wait_for_timeout(2000)
            logged_in = await is_logged_in(page)
            try:
                await ctx.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("ctx.close raised: %s", exc)
            print("logged_in" if logged_in else "logged_out")
            return 0 if logged_in else 2

        if not await is_logged_in(page):
            log.error(
                "not logged in. run `python -m src.scout --login` first."
            )
            try:
                db.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("db.close raised: %s", exc)
            try:
                await ctx.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("ctx.close raised: %s", exc)
            return 1

        # Hook the response listener BEFORE the first /live navigation so
        # we don't miss the initial Following-channel call.
        feed_listener = detector_api.FollowingFeedListener()
        feed_listener.attach(page)
        try:
            await page.goto(LIVE_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not navigate to /live: %s", exc)

        try:
            while True:
                try:
                    await cycle(page, cfg, profile, feed_listener)
                except Exception:  # noqa: BLE001
                    log.exception("cycle failed")
                    await dump_debug(page, cfg.debug_dump_dir, "cycle_error")
                await asyncio.sleep(cfg.poll_interval_seconds)
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("shutting down…")
        finally:
            # Close the DB pool first so its finalizer doesn't run during
            # interpreter shutdown (Python 3.14 can't join threads there).
            try:
                db.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("db.close raised: %s", exc)
            # Playwright's driver may already be gone on Ctrl+C — swallow.
            try:
                await ctx.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("ctx.close raised: %s", exc)
    return 0


def setup_logging(cfg: Config) -> None:
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        cfg.log_file, maxBytes=2_000_000, backupCount=5
    )
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    root.handlers = [fh, sh]


def main() -> None:
    parser = argparse.ArgumentParser(description="TikTok Live Scout")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--profile",
        default=None,
        help="profile name from config.yaml (required when >1 profile)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="open a headed browser and wait for you to sign in, then exit",
    )
    parser.add_argument(
        "--check-login",
        action="store_true",
        help="exit 0 if logged in, 2 if logged out (headless; no scout polling)",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
