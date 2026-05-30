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
  .profile-section { margin-bottom: 28px; }
  .profile-header { display: flex; align-items: baseline; gap: 12px;
                    margin: 18px 0 10px; padding-bottom: 6px;
                    border-bottom: 1px solid #2a2a2c; }
  .profile-name { font-size: 16px; font-weight: 600; }
  .profile-meta { color: #888; font-size: 12px; }
  .nickname { color: #bbb; font-weight: 400; margin-left: 4px; }
  .ext { font-size: 11px; color: #888; text-decoration: none; margin-left: 4px;
         opacity: 0.7; }
  .ext:hover { opacity: 1; }
  .live-title { color: #ddd; font-size: 13px; margin-top: 2px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                 max-width: 60ch; }
</style>
</head>
<body>
<h1>TikTok Live Scout</h1>

{% if not profiles %}
  <div class="empty">No data yet — start the scout for any profile and refresh.</div>
{% endif %}

{% for prof_name, prof in profiles.items() %}
<div class="profile-section">
  <div class="profile-header">
    <span class="profile-name">{{ prof_name }}</span>
    <span class="scout-pill scout-{{ prof.scout.label }}" title="{{ prof.scout.detail }}">{{ prof.scout.label }}</span>
    <span class="profile-meta">
      {{ prof.targets|length }} target{% if prof.targets|length != 1 %}s{% endif %}
      {% if prof.last_poll_at %} · last poll <time class="ago" datetime="{{ prof.last_poll_at }}">{{ prof.last_poll_at }}</time>{% endif %}
    </span>
  </div>

  {% for name, t in prof.targets.items() %}
  <div class="card">
    <div class="dot {{ t.status }}"></div>
    <div>
      <div class="name">
        <a href="/user/{{ prof_name }}/{{ name }}">@{{ name }}</a>
        <a class="ext" href="https://www.tiktok.com/@{{ name }}/live" target="_blank" title="Open on TikTok">↗</a>
        {% if t.status == "live" and t.live_nickname %}
          <span class="nickname">— {{ t.live_nickname }}</span>
        {% endif %}
      </div>
      {% if t.status == "live" and t.live_title %}
        <div class="live-title">{{ t.live_title }}</div>
      {% endif %}
      <div class="meta">
        last check: {% if t.last_check %}<time class="ago" datetime="{{ t.last_check }}">{{ t.last_check }}</time>{% else %}—{% endif %}
        {% if t.status == "live" and t.live_started_at %}
          · live for {{ t.live_duration }}
        {% endif %}
        {% if t.status == "live" and t.live_viewer_count is not none %}
          · 👀 {{ t.live_viewer_count }} viewers
        {% endif %}
      </div>
      <div class="events">
        {% for ev in t.history[:5] %}
          <span><time class="ago" datetime="{{ ev.at }}">{{ ev.at }}</time> — {{ event_label(ev.event) }}{% if ev.duration_seconds %} ({{ duration(ev.duration_seconds) }}){% endif %}</span>
        {% endfor %}
      </div>
    </div>
    <div class="right">
      <span class="badge {{ t.status }}">{{ t.status }}</span>
    </div>
  </div>
  {% endfor %}
</div>
{% endfor %}

<script>
(function () {
  function rel(date) {
    const now = new Date();
    const diff = Math.round((now - date) / 1000); // seconds
    if (Math.abs(diff) < 5)   return "just now";
    if (diff < 60)            return diff + "s ago";
    if (diff < 3600)          return Math.floor(diff / 60) + "m ago";
    if (diff < 86400)         return Math.floor(diff / 3600) + "h ago";
    if (diff < 86400 * 30)    return Math.floor(diff / 86400) + "d ago";
    return date.toLocaleDateString(undefined,
      {month: "short", day: "numeric", year: "numeric"});
  }
  function abs(date) {
    return date.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit", second: "2-digit",
    });
  }
  function refresh() {
    document.querySelectorAll("time.ago").forEach(function (el) {
      const iso = el.getAttribute("datetime");
      if (!iso) return;
      const d = new Date(iso);
      if (isNaN(d)) return;
      el.textContent = rel(d);
      el.setAttribute("title", abs(d));
    });
  }
  refresh();
  setInterval(refresh, 1000);
})();
</script>

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


_EVENT_LABELS = {
    "live_start": "went LIVE",
    "live_end": "ended live",
    "first_seen_offline": "first sight (offline)",
}


def _event_label(name: str) -> str:
    return _EVENT_LABELS.get(name, name)


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
    profiles = s.get("profiles", {}) or {}
    now = datetime.now(timezone.utc)
    interval = _cfg.poll_interval_seconds if _cfg else 60
    for prof in profiles.values():
        # Per-profile scout pill based on that profile's own last_poll_at.
        prof["scout"] = _scout_status(prof.get("last_poll_at"), interval)
        for t in prof["targets"].values():
            t.setdefault("history", [])
            t["live_duration"] = ""
            started = t.get("live_started_at")
            if t.get("status") == "live" and started:
                try:
                    dt = datetime.fromisoformat(started)
                    t["live_duration"] = _human_duration(int((now - dt).total_seconds()))
                except ValueError:
                    pass
    return {
        "profiles": profiles,
        "last_poll_at": s.get("last_poll_at"),
        "scout": _scout_status(s.get("last_poll_at"), interval),
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    data = _read_state()
    return HTMLResponse(
        PAGE.render(
            event_label=_event_label,
            duration=_human_duration,
            **data,
        )
    )


@app.get("/api/state")
def api_state() -> JSONResponse:
    return JSONResponse(db.get_state())


DETAIL_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>@{{ username }} · TikTok Live Scout</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #111; color: #eee; margin: 0; padding: 24px; }
  h1 { margin: 0 0 8px; font-size: 22px; }
  h1 a { color: #888; text-decoration: none; font-size: 14px; }
  h1 a:hover { color: #fe2c55; }
  .meta { color: #aaa; font-size: 14px; margin-bottom: 18px; }
  .badge { font-size: 11px; padding: 3px 8px; border-radius: 999px;
           background: #333; color: #ddd; text-transform: uppercase; margin-left: 6px; }
  .badge.live { background: #fe2c55; color: #fff; }
  .controls { margin-bottom: 18px; }
  .controls button { background: #1c1c1e; color: #ddd; border: 1px solid #333;
                     padding: 6px 14px; border-radius: 8px; cursor: pointer;
                     margin-right: 6px; font-size: 13px; }
  .controls button.active { background: #fe2c55; color: #fff; border-color: #fe2c55; }
  .chart-card { background: #1c1c1e; border-radius: 12px; padding: 14px 18px 18px;
                margin-bottom: 14px; }
  .chart-title { font-size: 13px; color: #bbb; margin-bottom: 6px;
                 display: flex; align-items: baseline; gap: 10px; }
  .chart-title .current { color: #fff; font-size: 18px; font-weight: 600; }
  .chart-title .delta { font-size: 12px; color: #888; }
  .chart-title .delta.up { color: #2ecc71; }
  .chart-title .delta.down { color: #fe2c55; }
  canvas { width: 100% !important; max-height: 220px; }
  .empty { color: #777; padding: 40px; text-align: center; }
  .section-title { font-size: 13px; color: #888; text-transform: uppercase;
                   letter-spacing: 0.5px; margin: 26px 0 10px; }
  /* FullCalendar dark-mode overrides */
  #calendar { background: #1c1c1e; border-radius: 12px; padding: 14px 18px 18px;
              color: #ddd; }
  .fc { --fc-border-color: #2a2a2c; --fc-page-bg-color: #1c1c1e;
        --fc-neutral-bg-color: #1c1c1e; --fc-list-event-hover-bg-color: #2a2a2c;
        --fc-today-bg-color: rgba(254,44,85,0.08); color: #ddd; }
  .fc .fc-toolbar-title { font-size: 16px !important; }
  .fc .fc-button { background: #2a2a2c !important; border-color: #2a2a2c !important;
                   color: #ddd !important; box-shadow: none !important; }
  .fc .fc-button-active, .fc .fc-button:hover { background: #fe2c55 !important;
                                                  border-color: #fe2c55 !important; }
  .fc .fc-col-header-cell-cushion, .fc .fc-daygrid-day-number,
  .fc .fc-list-day-cushion { color: #aaa; }
  .fc-event { font-size: 11px; padding: 1px 4px; cursor: pointer; }
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<!-- FullCalendar v6 ships CSS inline via JS — no separate <link> needed. -->
<script src="https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.js"></script>
</head>
<body>
<h1>
  <a href="/">← all targets</a>
  &nbsp;@{{ username }}
  {% if t.live_nickname %}<span style="font-weight:400;color:#aaa;">— {{ t.live_nickname }}</span>{% endif %}
  <span class="badge {{ t.status }}">{{ t.status }}</span>
</h1>
<div class="meta">
  profile <code>{{ profile }}</code>
  {% if t.status == "live" and t.live_title %} · {{ t.live_title }}{% endif %}
  {% if t.live_room_id %} · room {{ t.live_room_id }}{% endif %}
  · <a href="https://www.tiktok.com/@{{ username }}/live" target="_blank" style="color:#888;">↗ open on TikTok</a>
</div>

<div class="controls">
  range:
  <button data-range="600">10 min</button>
  <button data-range="3600" class="active">1 hour</button>
  <button data-range="21600">6 hours</button>
  <button data-range="86400">24 hours</button>
</div>

<div id="charts">
  <div class="chart-card">
    <div class="chart-title"><span>👀 Viewers</span><span class="current" id="cur-viewers">—</span><span class="delta" id="delta-viewers"></span></div>
    <canvas id="chart-viewers"></canvas>
  </div>
  <div class="chart-card">
    <div class="chart-title"><span>❤ Likes (total)</span><span class="current" id="cur-likes">—</span><span class="delta" id="delta-likes"></span></div>
    <canvas id="chart-likes"></canvas>
  </div>
  <div class="chart-card">
    <div class="chart-title"><span>👥 Followers</span><span class="current" id="cur-followers">—</span><span class="delta" id="delta-followers"></span></div>
    <canvas id="chart-followers"></canvas>
  </div>
</div>

<div id="empty" class="empty" style="display:none;">No samples in this window yet — wait for the scout to record some while the user is live.</div>

<div class="section-title">Live sessions calendar</div>
<div id="calendar"></div>

<script>
(function () {
  const profile = {{ profile|tojson }};
  const username = {{ username|tojson }};
  let currentRange = 3600;
  const charts = {};

  function fmt(n) {
    if (n === null || n === undefined) return '—';
    return Number(n).toLocaleString();
  }
  function deltaLabel(samples, key) {
    if (samples.length < 2) return '';
    const first = samples[0][key];
    const last  = samples[samples.length - 1][key];
    if (first === null || last === null) return '';
    const d = last - first;
    if (d === 0) return '±0';
    return (d > 0 ? '+' : '') + Number(d).toLocaleString();
  }
  function setDelta(elId, samples, key) {
    const el = document.getElementById(elId);
    const txt = deltaLabel(samples, key);
    el.textContent = txt;
    el.classList.remove('up','down');
    if (txt.startsWith('+')) el.classList.add('up');
    else if (txt.startsWith('-')) el.classList.add('down');
  }
  function lineChart(canvasId, label, color) {
    const ctx = document.getElementById(canvasId).getContext('2d');
    return new Chart(ctx, {
      type: 'line',
      data: { labels: [], datasets: [{ label, data: [], borderColor: color,
              backgroundColor: color + '22', tension: 0.25, fill: true,
              pointRadius: 0, borderWidth: 2 }] },
      options: { responsive: true, animation: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: '#888', maxRotation: 0, autoSkip: true,
                        maxTicksLimit: 6 },
               grid: { color: '#2a2a2c' } },
          y: { ticks: { color: '#888', precision: 0, stepSize: 1,
                        callback: function(v) { return Number.isInteger(v) ? v : null; } },
               grid: { color: '#2a2a2c' },
               beginAtZero: false }
        }
      }
    });
  }
  function updateChart(chart, samples, key) {
    chart.data.labels = samples.map(s => {
      const d = new Date(s.at);
      return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    });
    chart.data.datasets[0].data = samples.map(s => s[key]);
    chart.update('none');
  }

  async function refresh() {
    const url = `/api/samples/${encodeURIComponent(profile)}/${encodeURIComponent(username)}?since=${currentRange}`;
    let samples;
    try { samples = await (await fetch(url)).json(); }
    catch (e) { console.error(e); return; }

    const empty = document.getElementById('empty');
    const chartsEl = document.getElementById('charts');
    if (!samples.length) {
      empty.style.display = 'block';
      chartsEl.style.display = 'none';
      return;
    }
    empty.style.display = 'none';
    chartsEl.style.display = 'block';

    const last = samples[samples.length - 1];
    document.getElementById('cur-viewers').textContent   = fmt(last.user_count);
    document.getElementById('cur-likes').textContent     = fmt(last.like_count);
    document.getElementById('cur-followers').textContent = fmt(last.follower_count);
    setDelta('delta-viewers', samples, 'user_count');
    setDelta('delta-likes',   samples, 'like_count');
    setDelta('delta-followers', samples, 'follower_count');

    updateChart(charts.viewers,   samples, 'user_count');
    updateChart(charts.likes,     samples, 'like_count');
    updateChart(charts.followers, samples, 'follower_count');
  }

  document.querySelectorAll('.controls button').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.controls button').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      currentRange = parseInt(b.dataset.range, 10);
      refresh();
    });
  });

  charts.viewers   = lineChart('chart-viewers',   'viewers',  '#fe2c55');
  charts.likes     = lineChart('chart-likes',     'likes',    '#ff8a00');
  charts.followers = lineChart('chart-followers', 'followers','#3fa9f5');

  refresh();
  setInterval(refresh, 10000);

  // ------------------- FullCalendar -------------------
  function fmtDuration(secs) {
    if (!secs) return '';
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  }
  const calEl = document.getElementById('calendar');
  if (calEl && window.FullCalendar) {
    const calendar = new FullCalendar.Calendar(calEl, {
      initialView: 'dayGridMonth',
      height: 620,
      timeZone: 'local',
      firstDay: 1,
      nowIndicator: true,
      headerToolbar: {
        left: 'prev,next today',
        center: 'title',
        right: 'dayGridMonth,timeGridWeek,timeGridDay,listWeek',
      },
      buttonText: { today: 'today', month: 'month', week: 'week',
                    day: 'day', list: 'list' },
      events: `/api/sessions/${encodeURIComponent(profile)}/${encodeURIComponent(username)}?days=60`,
      eventDisplay: 'block',
      eventTimeFormat: { hour: '2-digit', minute: '2-digit', meridiem: false },
      eventDidMount: (info) => {
        const ep = info.event.extendedProps;
        const parts = [];
        if (ep.ongoing) parts.push('LIVE NOW');
        if (ep.duration_seconds) parts.push('duration ' + fmtDuration(ep.duration_seconds));
        if (ep.viewer_count != null) parts.push(ep.viewer_count + ' viewers');
        if (parts.length) info.el.title = parts.join(' · ');
      },
    });
    calendar.render();
    // Refresh sessions every minute so the ongoing block extends.
    setInterval(() => calendar.refetchEvents(), 60000);
  }
})();
</script>
</body>
</html>
"""
)


def _user_state(profile: str, username: str) -> dict | None:
    """Pull the target's current row from db.get_state()."""
    s = db.get_state()
    prof = (s.get("profiles") or {}).get(profile)
    if not prof:
        return None
    target = (prof.get("targets") or {}).get(username)
    if not target:
        return None
    return target


@app.get("/user/{profile}/{username}", response_class=HTMLResponse)
def user_detail(profile: str, username: str) -> HTMLResponse:
    t = _user_state(profile, username) or {
        "status": "unknown",
        "live_nickname": None,
        "live_title": None,
        "live_room_id": None,
    }
    return HTMLResponse(
        DETAIL_PAGE.render(profile=profile, username=username, t=t)
    )


@app.get("/api/samples/{profile}/{username}")
def api_samples(profile: str, username: str, since: int = 3600) -> JSONResponse:
    """Return live_samples rows for the given target within the last `since`
    seconds. Used by the detail page's graphs."""
    since = max(60, min(since, 7 * 86400))   # clamp 1min .. 7d
    return JSONResponse(db.get_samples(profile, username, since))


@app.get("/api/sessions/{profile}/{username}")
def api_sessions(profile: str, username: str, days: int = 60) -> JSONResponse:
    """Return live sessions in FullCalendar event shape."""
    days = max(1, min(days, 365))
    sessions = db.get_sessions(profile, username, since_seconds=days * 86400)
    events = []
    for s in sessions:
        ongoing = s.get("ongoing")
        title = s.get("title") or "LIVE"
        # FullCalendar requires a non-null end to render as a block on
        # week/day views; for an ongoing session we pin the end to now.
        end = s.get("end") or datetime.now(timezone.utc).isoformat(timespec="seconds")
        events.append(
            {
                "title": ("● " if ongoing else "") + title,
                "start": s["start"],
                "end": end,
                "backgroundColor": "#fe2c55" if ongoing else "rgba(254,44,85,0.55)",
                "borderColor": "#fe2c55",
                "textColor": "#fff",
                "extendedProps": {
                    "ongoing": bool(ongoing),
                    "duration_seconds": s.get("duration_seconds"),
                    "viewer_count": s.get("viewer_count"),
                },
            }
        )
    return JSONResponse(events)


def main() -> None:
    parser = argparse.ArgumentParser(description="TikTok Live Scout — Web UI")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    global _cfg
    _cfg = load_config(args.config)

    uvicorn.run(app, host=_cfg.web.host, port=_cfg.web.port, log_level="info")


if __name__ == "__main__":
    main()
