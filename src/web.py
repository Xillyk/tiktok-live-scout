"""Tiny FastAPI dashboard. Reads from Postgres — no shared memory with the scout."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template

from . import db
from .config import load as load_config

_cfg = None  # populated in main()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    db.init(_cfg.database_url)
    yield
    db.close()


app = FastAPI(title="TikTok Live Scout", lifespan=lifespan)


PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>TikTok Live Scout</title>
<meta http-equiv="refresh" content="10" />
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #111; color: #eee; margin: 0; padding: 24px; }
  h1 { margin: 0 0 6px; font-size: 22px; display: flex; align-items: center; gap: 12px; }
  .scout-pill { font-size: 11px; padding: 4px 10px; border-radius: 999px;
                text-transform: uppercase; font-weight: 600; letter-spacing: 0.4px; }
  .scout-active     { background: #1f7a3a; color: #d8ffd8; }
  .scout-stalled    { background: #8a6a00; color: #fff3c4; }
  .scout-terminated { background: #6e1f1f; color: #ffd0d0; }
  .scout-never      { background: #444; color: #ddd; }
  .last { color: #888; font-size: 13px; margin-bottom: 24px; }
  .card { background: #1c1c1e; border-radius: 12px; padding: 16px 18px;
          margin-bottom: 14px; display: flex; align-items: center;
          gap: 18px; }
  .dot { width: 14px; height: 14px; border-radius: 50%; flex: 0 0 auto; }
  .live   { background: #fe2c55; box-shadow: 0 0 12px #fe2c55; }
  .offline{ background: #555; }
  .unknown{ background: #c0a020; }
  .name { font-weight: 600; font-size: 17px; }
  .name a { color: #eee; text-decoration: none; }
  .name a:hover { text-decoration: underline; }
  .meta { color: #999; font-size: 13px; margin-top: 2px; }
  .right { margin-left: auto; text-align: right; }
  .badge { font-size: 11px; padding: 3px 8px; border-radius: 999px;
           background: #333; color: #ddd; text-transform: uppercase; }
  .badge.live   { background: #fe2c55; color: #fff; }
  .events { margin-top: 6px; font-size: 12px; color: #888; }
  .events span { margin-right: 10px; }
  .empty { color: #777; padding: 24px; text-align: center; }
</style>
</head>
<body>
<h1>
  TikTok Live Scout
  <span class="scout-pill scout-{{ scout.label }}" title="{{ scout.detail }}">{{ scout.label }}</span>
</h1>
<div class="last">
  Last poll: {{ last_poll_at or "never" }}
  {% if scout.age_seconds is not none %}· {{ scout.age_human }} ago{% endif %}
  · auto-refresh 10s
</div>

{% if not targets %}
  <div class="empty">No targets yet. Add usernames under <code>targets:</code> in config.yaml.</div>
{% endif %}

{% for name, t in targets.items() %}
<div class="card">
  <div class="dot {{ t.status }}"></div>
  <div>
    <div class="name"><a href="https://www.tiktok.com/@{{ name }}" target="_blank">@{{ name }}</a></div>
    <div class="meta">
      last check: {{ t.last_check or "—" }}
      {% if t.status == "live" and t.live_started_at %}
        · live for {{ t.live_duration }}
      {% endif %}
    </div>
    <div class="events">
      {% for ev in t.history[:5] %}
        <span>{{ ev.at }} — {{ ev.event }}{% if ev.duration_seconds %} ({{ ev.duration_seconds }}s){% endif %}</span>
      {% endfor %}
    </div>
  </div>
  <div class="right">
    <span class="badge {{ t.status }}">{{ t.status }}</span>
  </div>
</div>
{% endfor %}

</body>
</html>
"""
)


def _human_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _scout_status(last_poll_at: str | None, interval_seconds: int) -> dict:
    """Derive scout liveness from the last_poll_at heartbeat the scout writes
    after every cycle. Label maps to a CSS class on the page."""
    if not last_poll_at:
        return {
            "label": "never",
            "detail": "scout has not run yet",
            "age_seconds": None,
            "age_human": "",
        }
    try:
        last = datetime.fromisoformat(last_poll_at)
    except ValueError:
        return {
            "label": "never",
            "detail": "unparseable last_poll_at",
            "age_seconds": None,
            "age_human": "",
        }
    age = int((datetime.now(timezone.utc) - last).total_seconds())
    # 2× interval = "active with one missed cycle ok"
    # 5× interval = scout is probably dead
    if age <= interval_seconds * 2:
        label = "active"
    elif age <= interval_seconds * 5:
        label = "stalled"
    else:
        label = "terminated"
    return {
        "label": label,
        "detail": f"last poll {age}s ago (poll interval {interval_seconds}s)",
        "age_seconds": age,
        "age_human": _human_duration(age),
    }


def _read_state() -> dict:
    s = db.get_state()
    targets = s.get("targets", {}) or {}
    now = datetime.now(timezone.utc)
    for t in targets.values():
        t.setdefault("history", [])
        t["live_duration"] = ""
        started = t.get("live_started_at")
        if t.get("status") == "live" and started:
            try:
                dt = datetime.fromisoformat(started)
                t["live_duration"] = _human_duration(int((now - dt).total_seconds()))
            except ValueError:
                pass
    interval = _cfg.poll_interval_seconds if _cfg else 60
    return {
        "targets": targets,
        "last_poll_at": s.get("last_poll_at"),
        "scout": _scout_status(s.get("last_poll_at"), interval),
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    data = _read_state()
    return HTMLResponse(PAGE.render(**data))


@app.get("/api/state")
def api_state() -> JSONResponse:
    return JSONResponse(db.get_state())


def main() -> None:
    parser = argparse.ArgumentParser(description="TikTok Live Scout — Web UI")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    global _cfg
    _cfg = load_config(args.config)

    uvicorn.run(app, host=_cfg.web.host, port=_cfg.web.port, log_level="info")


if __name__ == "__main__":
    main()
