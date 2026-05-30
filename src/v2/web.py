"""v2 dashboard — reads from the v2_* Postgres tables.

Routes:
  GET /                                – home: targets list with status
  GET /target/{username}                – per-target sessions + calendar
  GET /session/{session_id}             – session detail w/ graphs + feeds
  GET /api/state                        – JSON snapshot of all targets
  GET /api/sessions/{username}          – sessions list (incl. FullCalendar)
  GET /api/snapshots/{session_id}       – time-series for the graphs
  GET /api/events/{session_id}          – recent events for the live feed
  GET /api/top/{session_id}             – top chatters + top gifters

Start:
  .venv/bin/python -m src.v2.web --port 8767
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template

from . import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    yield
    db.close()


app = FastAPI(title="TikTok Live Scout v2", lifespan=lifespan)


# ---------------------------------------------------------------- helpers

def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _human_duration(seconds: int) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _fmt_int(n) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


def _human_bytes(n) -> str:
    if n is None:
        return "—"
    b = float(n)
    if b < 1024:
        return f"{int(b)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        b /= 1024
        if b < 1024:
            return f"{b:.1f} {unit}" if b < 100 else f"{b:.0f} {unit}"
    return f"{b:.0f} PB"


# ---------------------------------------------------------------- templates

_BASE_STYLE = """
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
.section-title { font-size: 13px; color: #888; text-transform: uppercase;
                 letter-spacing: 0.5px; margin: 26px 0 10px; }
.empty { color: #777; padding: 32px; text-align: center; }
.card { background: #1c1c1e; border-radius: 12px; padding: 14px 18px;
        margin-bottom: 14px; }
.row { display: flex; align-items: center; gap: 14px; }
.dot { width: 12px; height: 12px; border-radius: 50%; flex: 0 0 auto; }
.dot.live    { background: #fe2c55; box-shadow: 0 0 10px #fe2c55; }
.dot.offline { background: #555; }
.name { font-weight: 600; font-size: 16px; }
.name a { color: #eee; text-decoration: none; }
.name a:hover { color: #fe2c55; }
.muted { color: #888; font-size: 13px; }
.stats { display: flex; flex-wrap: wrap; gap: 18px; margin-top: 6px;
         color: #ccc; font-size: 13px; }
.stats b { color: #fff; font-weight: 600; }
.stats.history { gap: 14px; margin-top: 4px; color: #888; font-size: 12px; }
.stats.history b { color: #ccc; font-weight: 600; }
.stats.history time.ago { color: #ccc; }
.right { margin-left: auto; text-align: right; }

/* Pill-style toggle used in both target + session pages */
.mode-toggle { display: inline-flex; gap: 4px; margin-left: 12px;
               vertical-align: middle; }
.mode-btn { background: #2a2a2c; color: #aaa; border: 1px solid #2a2a2c;
            padding: 3px 10px; border-radius: 12px; cursor: pointer;
            font-size: 11px; font-weight: 500; letter-spacing: 0.2px;
            text-transform: none; }
.mode-btn:hover { color: #ddd; }
.mode-btn.active { background: #fe2c55; color: #fff; border-color: #fe2c55; }

/* Time-range dropdown for charts */
.time-range-control { display: inline-flex; align-items: center; gap: 6px;
                      margin-left: 14px; font-size: 12px; font-weight: 400;
                      color: #888; text-transform: none; letter-spacing: normal; }
.time-range-control select { background: #2a2a2c; color: #ddd;
                             border: 1px solid #2a2a2c; padding: 3px 8px;
                             border-radius: 8px; font-size: 12px;
                             cursor: pointer; }
.time-range-control select:hover { border-color: #555; }
.time-range-control select:focus { outline: none; border-color: #fe2c55; }

/* Click-to-zoom chart modal — used on target + session pages */
.chart-card[data-chart-key] { cursor: pointer; transition: background 0.15s; }
.chart-card[data-chart-key]:hover { background: #232328; }
.chart-modal { position: fixed; inset: 0; z-index: 1000;
               display: none; }
.chart-modal[data-open="1"] { display: block; }
.chart-modal-backdrop { position: absolute; inset: 0;
                        background: rgba(0, 0, 0, 0.78); }
.chart-modal-content { position: absolute; top: 4%; left: 4%;
                       right: 4%; bottom: 4%;
                       background: #1c1c1e;
                       border: 1px solid #2a2a2c;
                       border-radius: 14px;
                       padding: 20px 26px 22px;
                       display: flex; flex-direction: column;
                       box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5); }
.chart-modal-header { display: flex; justify-content: space-between;
                      align-items: center; margin-bottom: 12px; }
.chart-modal-title { color: #fff; font-size: 18px; font-weight: 600;
                     margin: 0; display: flex; gap: 12px; align-items: baseline; }
.chart-modal-title .cur { color: #fe2c55; font-size: 24px;
                          font-variant-numeric: tabular-nums; }
.chart-modal-close { background: none; border: 0; color: #aaa;
                     font-size: 28px; line-height: 1; cursor: pointer;
                     padding: 0 6px; }
.chart-modal-close:hover { color: #fe2c55; }
.chart-modal-body { flex: 1 1 auto; position: relative; min-height: 0; }
.chart-modal-body canvas { width: 100% !important;
                           height: 100% !important;
                           max-height: none !important; }
"""

# Modal markup + JS reused by both target + session pages. The JS expects:
#   - `window._chartsRegistry`: a flat object {key: Chart} populated by each page
#   - `.chart-card[data-chart-key="…"]` elements with a `.chart-title` child
_CHART_MODAL_HTML = """
<div id="chart-modal" class="chart-modal" aria-hidden="true">
  <div class="chart-modal-backdrop" data-close="1"></div>
  <div class="chart-modal-content">
    <div class="chart-modal-header">
      <h3 class="chart-modal-title">
        <span id="chart-modal-title-text">—</span>
        <span class="cur" id="chart-modal-current">—</span>
      </h3>
      <button class="chart-modal-close" data-close="1" aria-label="Close">×</button>
    </div>
    <div class="chart-modal-body"><canvas id="chart-modal-canvas"></canvas></div>
  </div>
</div>
"""

_CHART_MODAL_JS = """
(function () {
  if (!window._chartsRegistry) window._chartsRegistry = {};
  const modalEl = document.getElementById('chart-modal');
  if (!modalEl) return;
  const titleText = document.getElementById('chart-modal-title-text');
  const currentEl = document.getElementById('chart-modal-current');
  const canvas    = document.getElementById('chart-modal-canvas');
  let modalChart = null;
  let activeKey  = null;

  function fmtInt(n) { return (n == null) ? '—' : Number(n).toLocaleString(); }

  function syncFromSource() {
    if (!modalChart || !activeKey) return;
    const src = window._chartsRegistry[activeKey];
    if (!src) return;
    modalChart.data.labels = src.data.labels.slice();
    modalChart.data.datasets[0].data = src.data.datasets[0].data.slice();
    modalChart.update('none');
    const arr = src.data.datasets[0].data;
    currentEl.textContent = arr.length ? fmtInt(arr[arr.length - 1]) : '—';
  }

  function openChartModal(key, title) {
    const src = window._chartsRegistry[key];
    if (!src) return;
    activeKey = key;
    titleText.textContent = title;
    const color = src.data.datasets[0].borderColor;
    if (modalChart) { modalChart.destroy(); modalChart = null; }
    modalChart = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: src.data.labels.slice(),
        datasets: [{
          data: src.data.datasets[0].data.slice(),
          borderColor: color,
          backgroundColor: color + '22',
          tension: 0.25, fill: true, pointRadius: 0, borderWidth: 2.5,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: {
          legend: { display: false },
          tooltip: { mode: 'index', intersect: false,
                     backgroundColor: '#222', borderColor: '#444',
                     borderWidth: 1, titleColor: '#fff', bodyColor: '#ddd' },
        },
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { ticks: { color: '#aaa', maxRotation: 0, autoSkip: true,
                        maxTicksLimit: 14 },
               grid: { color: '#2a2a2c' } },
          y: { ticks: { color: '#aaa', precision: 0, stepSize: 1,
                        callback: function(v) { return Number.isInteger(v) ? v : null; } },
               grid: { color: '#2a2a2c' },
               beginAtZero: false },
        }
      }
    });
    const arr = src.data.datasets[0].data;
    currentEl.textContent = arr.length ? fmtInt(arr[arr.length - 1]) : '—';
    modalEl.setAttribute('data-open', '1');
    modalEl.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
  }

  function closeChartModal() {
    modalEl.removeAttribute('data-open');
    modalEl.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    activeKey = null;
    if (modalChart) { modalChart.destroy(); modalChart = null; }
  }

  modalEl.addEventListener('click', e => {
    if (e.target && e.target.dataset && e.target.dataset.close === '1') closeChartModal();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && modalEl.hasAttribute('data-open')) closeChartModal();
  });

  document.querySelectorAll('.chart-card[data-chart-key]').forEach(card => {
    card.addEventListener('click', () => {
      const key = card.dataset.chartKey;
      const title = (card.querySelector('.chart-title') || {textContent: key}).textContent;
      openChartModal(key, title);
    });
  });

  // Keep the modal chart in sync with underlying refreshes.
  setInterval(syncFromSource, 5000);
})();
"""


HOME_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>TikTok Live Scout · v2</title>
<meta http-equiv="refresh" content="10" id="meta-refresh" />
<style>""" + _BASE_STYLE + """
.watch-select { width: 18px; height: 18px; accent-color: #fe2c55;
                cursor: pointer; flex: 0 0 auto; }
.watch-select-placeholder { width: 18px; height: 18px; flex: 0 0 auto; }
.watch-bar { position: fixed; bottom: 0; left: 0; right: 0;
             background: rgba(28,28,30,0.97); backdrop-filter: blur(8px);
             border-top: 1px solid #2a2a2c; padding: 12px 24px;
             display: none; justify-content: space-between; align-items: center;
             z-index: 100; }
.watch-bar[data-show="1"] { display: flex; }
.watch-bar .left { color: #ddd; font-size: 14px; }
.watch-bar .left b { color: #fe2c55; font-size: 16px;
                     font-variant-numeric: tabular-nums; }
.watch-bar .clear { background: none; color: #aaa; border: 0;
                    cursor: pointer; margin-right: 12px; font-size: 13px; }
.watch-bar .clear:hover { color: #ddd; }
.watch-bar a.go { background: #fe2c55; color: #fff;
                   padding: 8px 18px; border-radius: 999px;
                   text-decoration: none; font-weight: 600; font-size: 14px; }
.watch-bar a.go:hover { background: #e22149; }
.watch-bar a.go.disabled { pointer-events: none; opacity: 0.4; }
body { padding-bottom: 80px; }  /* leave room for the bar */
</style>
</head>
<body>
<h1>TikTok Live Scout <span class="muted" style="font-size:14px;font-weight:400;">· v2</span></h1>
<div class="meta">
  {{ targets|length }} target{% if targets|length != 1 %}s{% endif %} ·
  {{ live_count }} live now ·
  refreshed <time class="ago" datetime="{{ now }}">{{ now }}</time>
</div>

{% if not targets %}
  <div class="empty">No targets yet — add usernames to <code>config_v2.yaml</code> and run the launcher.</div>
{% endif %}

{% for t in targets %}
<div class="card">
  <div class="row">
    {% if t.session_id %}
      <input type="checkbox" class="watch-select" data-username="{{ t.username }}"
             title="select to watch in multi-view" />
    {% else %}
      <span class="watch-select-placeholder"></span>
    {% endif %}
    <div class="dot {{ 'live' if t.session_id else 'offline' }}"></div>
    <div>
      <div class="name">
        <a href="/target/{{ t.username }}">@{{ t.username }}</a>
        {% if t.session_id and t.title %}
          <span class="muted" style="margin-left:8px;font-weight:400;">— {{ t.title }}</span>
        {% endif %}
      </div>
      {% if t.session_id %}
        <div class="stats">
          <span>live for <b>{{ duration(t.started_at_seconds_ago) }}</b></span>
          <span>👀 <b>{{ fmt(t.final_viewers) }}</b> viewers</span>
          <span>❤ <b>{{ fmt(t.total_likes) }}</b></span>
          <span>💬 <b>{{ fmt(t.total_comments) }}</b></span>
          <span>🎁 <b>{{ fmt(t.total_gifts) }}</b> ({{ fmt(t.total_diamonds) }}💎)</span>
        </div>
      {% else %}
        <div class="muted">offline</div>
      {% endif %}
      <div class="stats history">
        <span><b>{{ t.session_count or 0 }}</b> session{% if (t.session_count or 0) != 1 %}s{% endif %}</span>
        {% if t.latest_started_at %}
          <span>last started <time class="ago" datetime="{{ t.latest_started_at_iso }}">{{ t.latest_started_at_iso }}</time></span>
        {% endif %}
        {% if t.latest_ended_at %}
          <span>last ended <time class="ago" datetime="{{ t.latest_ended_at_iso }}">{{ t.latest_ended_at_iso }}</time></span>
        {% elif t.session_id %}
          <span class="muted">(still live)</span>
        {% endif %}
      </div>
      <div class="stats history">
        <span title="rows in v2_events for this target">📊 <b>{{ fmt(t.event_count) }}</b> events</span>
        <span title="rows in v2_snapshots for this target">📸 <b>{{ fmt(t.snapshot_count) }}</b> snapshots</span>
        <span title="proportional share of v2_events + v2_snapshots table size">💾 <b>{{ bytes_fmt(t.approx_bytes) }}</b></span>
      </div>
    </div>
    <div class="right">
      <span class="badge {{ 'live' if t.session_id else '' }}">{{ 'LIVE' if t.session_id else 'offline' }}</span>
    </div>
  </div>
</div>
{% endfor %}

<div class="watch-bar" id="watch-bar" aria-hidden="true">
  <div class="left">
    <b><span id="watch-count">0</span></b> target<span id="watch-plural">s</span> selected
    <span class="muted" style="margin-left:10px;font-size:12px;" id="watch-names"></span>
  </div>
  <div>
    <button class="clear" id="watch-clear">clear</button>
    <a href="#" class="go disabled" id="watch-go">Watch lives →</a>
  </div>
</div>

<script>
(function(){
  function rel(d){var s=Math.round((new Date()-d)/1000); if(s<5)return "just now";
    if(s<60)return s+"s ago"; if(s<3600)return Math.floor(s/60)+"m ago";
    return Math.floor(s/3600)+"h ago"; }
  document.querySelectorAll("time.ago").forEach(function(el){
    var d=new Date(el.getAttribute("datetime")); if(!isNaN(d)){
      el.textContent=rel(d); el.title=d.toLocaleString(); }
  });
})();

(function(){
  // Multi-watch selection persisted in localStorage so the 10s home refresh
  // doesn't wipe in-progress selections. Offline targets are auto-pruned on
  // each render (their checkbox no longer exists).
  const KEY = 'v2.watch.selected';
  const bar      = document.getElementById('watch-bar');
  const countEl  = document.getElementById('watch-count');
  const namesEl  = document.getElementById('watch-names');
  const pluralEl = document.getElementById('watch-plural');
  const goEl     = document.getElementById('watch-go');
  const clearBtn = document.getElementById('watch-clear');
  const meta     = document.getElementById('meta-refresh');
  const raw = (localStorage.getItem(KEY) || '').split(',').filter(Boolean);
  const saved = new Set(raw);

  function persist(){ localStorage.setItem(KEY, [...saved].join(',')); }
  function pruneOffline(){
    const liveBoxes = document.querySelectorAll('.watch-select');
    const liveSet = new Set([...liveBoxes].map(el => el.dataset.username));
    for (const u of [...saved]) if (!liveSet.has(u)) saved.delete(u);
  }
  function render(){
    pruneOffline();
    persist();
    countEl.textContent = saved.size;
    pluralEl.textContent = saved.size === 1 ? '' : 's';
    namesEl.textContent = saved.size
      ? '· ' + [...saved].map(u => '@' + u).join(', ')
      : '';
    bar.dataset.show = saved.size > 0 ? '1' : '0';
    goEl.classList.toggle('disabled', saved.size === 0);
    goEl.href = saved.size > 0
      ? '/watch?targets=' + [...saved].map(encodeURIComponent).join(',')
      : '#';
    // Suspend the meta-refresh while a selection is in progress so the
    // page doesn't reload underneath the user mid-click. Restored on clear.
    if (meta) {
      if (saved.size > 0) meta.setAttribute('http-equiv', '_paused');
      else                meta.setAttribute('http-equiv', 'refresh');
    }
  }

  document.querySelectorAll('.watch-select').forEach(cb => {
    if (saved.has(cb.dataset.username)) cb.checked = true;
    cb.addEventListener('change', () => {
      if (cb.checked) saved.add(cb.dataset.username);
      else            saved.delete(cb.dataset.username);
      render();
    });
  });
  clearBtn.addEventListener('click', () => {
    saved.clear();
    document.querySelectorAll('.watch-select').forEach(cb => cb.checked = false);
    render();
  });
  render();
})();
</script>
</body></html>
"""
)


TARGET_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>@{{ username }} · v2</title>
<style>""" + _BASE_STYLE + """
.live-block { background: #15151a; border-radius: 14px;
              padding: 14px 18px 18px; margin: 14px 0 24px;
              border: 1px solid rgba(254,44,85,0.35); }
.live-block .ttl { font-size: 17px; font-weight: 600; color: #fff;
                   display: flex; align-items: center; gap: 10px; }
.live-block .sub { color: #aaa; font-size: 13px; margin: 2px 0 12px; }

.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
        gap: 10px; margin: 10px 0 16px; }
.kpi { background: #1c1c1e; border-radius: 10px; padding: 10px 12px; }
.kpi .label { color: #888; font-size: 11px; text-transform: uppercase;
              letter-spacing: 0.5px; }
.kpi .val { color: #fff; font-size: 22px; font-weight: 600; margin-top: 2px;
            font-variant-numeric: tabular-nums; }

.chart-grid { display: grid;
              grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
              gap: 10px; }
.chart-card { background: #1c1c1e; border-radius: 10px; padding: 10px 14px 12px; }
.chart-title { font-size: 12px; color: #bbb; margin-bottom: 4px; }
canvas { width: 100% !important; max-height: 170px; }

.feed-list { background: #1c1c1e; border-radius: 10px;
             max-height: 460px; overflow-y: auto; padding: 0; }
.feed-empty { color: #777; padding: 18px; text-align: center; font-size: 13px; }

.gift-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.gift-table th { color: #888; font-weight: 600; text-align: left;
                 padding: 8px 12px; border-bottom: 1px solid #2a2a2c;
                 position: sticky; top: 0; background: #1c1c1e; z-index: 1;
                 font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
.gift-table td { padding: 6px 12px; border-bottom: 1px solid #2a2a2c;
                 color: #ddd; }
.gift-table tr:last-child td { border-bottom: none; }
.gift-table td.t { color: #777; font-variant-numeric: tabular-nums; width: 80px; }
.gift-table td.n { text-align: right; font-variant-numeric: tabular-nums; }
.gift-table td.tot { color: #ffe066; font-weight: 600; }
.gift-table td.u { color: #bbb; font-weight: 600; }
/* Dim interim streak ticks when "every tick" mode is on. */
.gift-table tr.tick td { color: #888; }
.gift-table tr.tick td.u { color: #999; }
.gift-table tr.tick td.tot { color: #c9b246; font-weight: 400; }
.gift-table td.end { color: #fe2c55; font-weight: 600; width: 28px;
                     text-align: center; }

.chat-row { display: flex; gap: 10px; padding: 6px 12px;
            border-bottom: 1px solid #2a2a2c; font-size: 13px; }
.chat-row:last-child { border-bottom: none; }
.chat-time { color: #777; font-variant-numeric: tabular-nums; flex: 0 0 64px; }
.chat-user { color: #bbb; font-weight: 600; flex: 0 0 auto; }
.chat-text { color: #ddd; flex: 1 1 auto; word-break: break-word; }

.session-card { display: grid;
                grid-template-columns: 1fr auto;
                gap: 8px 18px;
                align-items: start; }
.session-head { font-size: 15px; font-weight: 600; }
.session-head a { color: #eee; text-decoration: none; }
.session-head a:hover { color: #fe2c55; }
.session-meta { color: #aaa; font-size: 13px; margin-top: 2px; }
.session-stats { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 6px;
                 color: #ccc; font-size: 13px; }
.session-stats b { color: #fff; font-weight: 600; }
.ongoing-badge { font-size: 10px; padding: 2px 7px; border-radius: 999px;
                 background: #fe2c55; color: #fff; text-transform: uppercase;
                 letter-spacing: 0.5px; }
#calendar { background: #1c1c1e; border-radius: 12px; padding: 14px 18px 18px;
            color: #ddd; }
.fc { --fc-border-color: #2a2a2c; --fc-page-bg-color: #1c1c1e;
      --fc-neutral-bg-color: #1c1c1e; --fc-list-event-hover-bg-color: #2a2a2c;
      --fc-today-bg-color: rgba(254,44,85,0.08); color: #ddd; }
.fc .fc-toolbar-title { font-size: 16px !important; }
.fc .fc-button { background: #2a2a2c !important; border-color: #2a2a2c !important;
                 color: #ddd !important; box-shadow: none !important; }
.fc .fc-button-active, .fc .fc-button:hover {
    background: #fe2c55 !important; border-color: #fe2c55 !important; }
.fc .fc-col-header-cell-cushion, .fc .fc-daygrid-day-number,
.fc .fc-list-day-cushion { color: #aaa; }
.fc-event { font-size: 11px; padding: 1px 4px; cursor: pointer; }
</style>
<script src="https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.js"></script>
</head>
<body>
<h1>
  <a href="/">← all targets</a>
  &nbsp;@{{ username }}
  <span class="badge {{ 'live' if open_session else '' }}">
    {{ 'LIVE' if open_session else 'offline' }}
  </span>
</h1>
<div class="meta">
  {{ sessions|length }} session{% if sessions|length != 1 %}s{% endif %} in the last 60 days
  {% if open_session %} · <a href="/session/{{ open_session.id }}" style="color:#fe2c55;">currently live — open session →</a>{% endif %}
</div>

<div class="section-title">Live sessions calendar</div>
<div id="calendar"></div>

<div class="section-title">Sessions ({{ sessions|length }})</div>
{% if not sessions %}
  <div class="empty">No sessions yet — they show up after the scout sees this target go live.</div>
{% endif %}
{% for s in sessions %}
<div class="card session-card">
  <div>
    <div class="session-head">
      <a href="/session/{{ s.id }}">{{ s.started_str }}</a>
      {% if not s.ended_at %}<span class="ongoing-badge">LIVE NOW</span>{% endif %}
      {% if s.title %}<span class="muted" style="font-weight:400;">— {{ s.title }}</span>{% endif %}
    </div>
    <div class="session-meta">
      duration {{ duration(s.duration_seconds) }} · room {{ s.room_id }}
      {% if s.end_reason %} · ended ({{ s.end_reason }}){% endif %}
    </div>
    <div class="session-stats">
      <span>👀 peak <b>{{ fmt(s.peak_viewers) }}</b></span>
      <span>❤ <b>{{ fmt(s.total_likes) }}</b></span>
      <span>💬 <b>{{ fmt(s.total_comments) }}</b></span>
      <span>🎁 <b>{{ fmt(s.total_gifts) }}</b> ({{ fmt(s.total_diamonds) }}💎)</span>
      <span>🚪 <b>{{ fmt(s.total_joins) }}</b></span>
      <span>➕ <b>{{ fmt(s.total_follows) }}</b></span>
      <span>↗ <b>{{ fmt(s.total_shares) }}</b></span>
    </div>
  </div>
</div>
{% endfor %}

<script>
(function () {
  const calEl = document.getElementById('calendar');
  if (calEl && window.FullCalendar) {
    const cal = new FullCalendar.Calendar(calEl, {
      initialView: 'dayGridMonth',
      height: 520,
      timeZone: 'local',
      firstDay: 1,
      nowIndicator: true,
      headerToolbar: {
        left: 'prev,next today',
        center: 'title',
        right: 'dayGridMonth,timeGridWeek,listWeek',
      },
      events: '/api/sessions/{{ username }}/calendar',
      eventDisplay: 'block',
      eventClick: (info) => {
        const id = info.event.extendedProps.session_id;
        if (id) location.href = `/session/${id}`;
      },
      eventTimeFormat: { hour: '2-digit', minute: '2-digit', meridiem: false },
      displayEventTime: true,
      displayEventEnd: true,
    });
    cal.render();
  }
})();
</script>

</body></html>
"""
)


SESSION_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>{{ session.username }} · session {{ session.id }}</title>
<style>""" + _BASE_STYLE + """
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 10px; margin: 14px 0; }
.kpi { background: #1c1c1e; border-radius: 10px; padding: 12px 14px; }
.kpi .label { color: #888; font-size: 12px; text-transform: uppercase;
              letter-spacing: 0.5px; }
.kpi .val { color: #fff; font-size: 22px; font-weight: 600; margin-top: 2px; }

/* Inline FLV/HLS live-video player */
.video-card { background: #000; border-radius: 12px; overflow: hidden;
              margin: 14px 0; border: 1px solid #2a2a2c; }
.video-toolbar { display: flex; align-items: center; gap: 10px;
                 padding: 8px 12px; background: #15151a;
                 border-bottom: 1px solid #2a2a2c; }
.video-toolbar .live-pill {
    background: #fe2c55; color: #fff; padding: 2px 8px;
    border-radius: 999px; font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px; }
.video-toolbar .video-help { color: #777; font-size: 11px;
                              margin-left: auto; font-variant-numeric: tabular-nums; }
.video-toolbar .quality-group { display: inline-flex; gap: 4px; }
.quality-pill { background: #2a2a2c; color: #aaa; border: 1px solid #2a2a2c;
                padding: 3px 10px; border-radius: 12px; cursor: pointer;
                font-size: 11px; font-weight: 600; letter-spacing: 0.4px;
                text-transform: uppercase; }
.quality-pill:hover { color: #ddd; }
.quality-pill.active { background: #fe2c55; color: #fff; border-color: #fe2c55; }
#live-video { width: 100%; max-height: 540px; display: block;
              background: #000; aspect-ratio: 16 / 9; }
.video-error { color: #aaa; padding: 24px 16px; text-align: center;
               font-size: 13px; }
.chart-grid { display: grid;
              grid-template-columns: repeat(3, minmax(0, 1fr));
              gap: 12px; }
@media (max-width: 1000px) {
  .chart-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 640px) {
  .chart-grid { grid-template-columns: 1fr; }
}
.chart-card { background: #1c1c1e; border-radius: 12px; padding: 12px 16px; }
.chart-title { font-size: 13px; color: #bbb; margin-bottom: 4px; }
canvas { width: 100% !important; max-height: 180px; }
.feeds { display: grid; grid-template-columns: 1fr 1fr; gap: 14px;
         margin-top: 14px; }
@media (max-width: 800px) { .feeds { grid-template-columns: 1fr; } }
.feed { background: #1c1c1e; border-radius: 12px; padding: 12px 16px;
        max-height: 360px; overflow-y: auto; }
.feed h2 { font-size: 13px; color: #888; text-transform: uppercase;
           letter-spacing: 0.5px; margin: 0 0 10px; }
.feed-row { display: flex; gap: 10px; padding: 6px 0;
            border-bottom: 1px solid #2a2a2c; font-size: 13px; }
.feed-row:last-child { border-bottom: none; }
.feed-row.tick { opacity: 0.55; }
.feed-row.tick .feed-text { color: #aaa; }
.feed-row .end-mark { color: #fe2c55; font-weight: 600; width: 14px; flex: 0 0 14px; }
.feed-time { color: #777; font-variant-numeric: tabular-nums; flex: 0 0 56px; }
.feed-user { color: #bbb; font-weight: 600; }
.feed-text { color: #ddd; }
.toprow { display: flex; justify-content: space-between; gap: 8px;
          padding: 5px 0; font-size: 13px; color: #ccc; }
.toprow b { color: #fff; }

/* User-filter search on the comments feed */
.user-search { display: inline-flex; align-items: center; gap: 4px;
               margin-left: 12px; font-weight: 400; text-transform: none;
               letter-spacing: normal; }
.user-search input { background: #2a2a2c; color: #ddd;
                     border: 1px solid #2a2a2c; padding: 4px 10px;
                     border-radius: 999px; font-size: 12px;
                     width: 200px; outline: none; }
.user-search input:focus { border-color: #fe2c55; }
.user-search input::placeholder { color: #666; }
.user-search button { background: transparent; color: #888; border: 0;
                      cursor: pointer; font-size: 16px; line-height: 1;
                      padding: 0 4px; opacity: 0; transition: opacity 0.1s; }
.user-search.has-value button { opacity: 1; }
.user-search button:hover { color: #fe2c55; }
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.js"></script>
</head>
<body>
<h1>
  <a href="/target/{{ session.username }}">← @{{ session.username }}</a>
  &nbsp;session #{{ session.id }}
  <span class="badge {{ 'live' if not session.ended_at else '' }}">
    {{ 'LIVE' if not session.ended_at else (session.end_reason or 'ended') }}
  </span>
</h1>
<div class="meta">
  {% if session.title %}<b>{{ session.title }}</b> · {% endif %}
  started {{ session.started_str }}
  · duration {{ duration(session.duration_seconds) }}
  · room {{ session.room_id }}
</div>

{% if not session.ended_at %}
<div class="video-card" id="video-card">
  <div class="video-toolbar">
    <span class="live-pill">● live</span>
    <span class="quality-group" id="quality-group">
      <button class="quality-pill"            data-q="ld">LD</button>
      <button class="quality-pill active"     data-q="hd">HD</button>
    </span>
    <span class="video-help" id="video-help">FLV · ~3-5s delay</span>
  </div>
  <video id="live-video" controls muted playsinline autoplay></video>
</div>
{% endif %}

<div class="kpis">
  <div class="kpi"><div class="label">👥 mTotal</div><div class="val" id="kpi-mtotal">—</div></div>
  <div class="kpi"><div class="label">peak viewers</div><div class="val" id="kpi-peak">{{ fmt(session.peak_viewers) }}</div></div>
  <div class="kpi"><div class="label">❤ likes</div><div class="val" id="kpi-like">{{ fmt(session.total_likes) }}</div></div>
  <div class="kpi"><div class="label">💬 comments</div><div class="val" id="kpi-comment">{{ fmt(session.total_comments) }}</div></div>
  <div class="kpi"><div class="label">🎁 gifts</div><div class="val" id="kpi-gift">{{ fmt(session.total_gifts) }}</div></div>
  <div class="kpi"><div class="label">💎 diamonds</div><div class="val" id="kpi-diamond">{{ fmt(session.total_diamonds) }}</div></div>
  <div class="kpi"><div class="label">🚪 joins</div><div class="val" id="kpi-join">{{ fmt(session.total_joins) }}</div></div>
  <div class="kpi"><div class="label">➕ follows</div><div class="val" id="kpi-follow">{{ fmt(session.total_follows) }}</div></div>
  <div class="kpi"><div class="label">↗ shares</div><div class="val" id="kpi-share">{{ fmt(session.total_shares) }}</div></div>
</div>

<div class="section-title">
  Time series (5s snapshots)
  <span class="muted" style="font-weight:400;font-size:12px;margin-left:8px;">
    click any chart to zoom
  </span>
  <span class="time-range-control">
    range:
    <select id="chart-range">
      <option value="0" selected>all</option>
      <option value="1800">last 30 min</option>
      <option value="3600">last 1 h</option>
      <option value="10800">last 3 h</option>
      <option value="21600">last 6 h</option>
      <option value="43200">last 12 h</option>
      <option value="86400">last 24 h</option>
    </select>
  </span>
</div>
<div class="chart-grid">
  <div class="chart-card" data-chart-key="contributors"><div class="chart-title">👥 contributors (mTotal)</div><canvas id="c-contributors"></canvas></div>
  <div class="chart-card" data-chart-key="like"><div class="chart-title">❤ likes (cumulative)</div><canvas id="c-like"></canvas></div>
  <div class="chart-card" data-chart-key="comment"><div class="chart-title">💬 comments (cumulative)</div><canvas id="c-comment"></canvas></div>
  <div class="chart-card" data-chart-key="gift"><div class="chart-title">🎁 gifts (cumulative)</div><canvas id="c-gift"></canvas></div>
  <div class="chart-card" data-chart-key="diamond"><div class="chart-title">💎 diamonds (cumulative)</div><canvas id="c-diamond"></canvas></div>
  <div class="chart-card" data-chart-key="join"><div class="chart-title">🚪 joins (cumulative)</div><canvas id="c-join"></canvas></div>
</div>

<div class="feeds">
  <div class="feed">
    <h2>
      recent comments <span class="muted" id="comments-count"></span>
      <span class="user-search">
        <input id="comments-user-search" type="text"
               placeholder="filter by @handle or nickname…"
               autocomplete="off" spellcheck="false">
        <button id="comments-user-clear" type="button" title="clear filter">×</button>
      </span>
    </h2>
    <div id="feed-comments"><div class="empty" style="padding:8px;">no comments yet</div></div>
  </div>
  <div class="feed">
    <h2>
      recent gifts <span class="muted" id="s-gift-count"></span>
      <span class="mode-toggle">
        <button class="mode-btn active" id="s-gift-mode-completed"
                title="streak completions only — real diamond spend">streak completions</button>
        <button class="mode-btn" id="s-gift-mode-all"
                title="every WebSocket frame including streak ticks">every tick</button>
      </span>
    </h2>
    <div id="feed-gifts"><div class="empty" style="padding:8px;">no gifts yet</div></div>
  </div>
</div>

<div class="feeds">
  <div class="feed">
    <h2>top chatters</h2>
    <div id="top-chatters"><div class="empty" style="padding:8px;">no data</div></div>
  </div>
  <div class="feed">
    <h2>top gifters (by diamonds)</h2>
    <div id="top-gifters"><div class="empty" style="padding:8px;">no data</div></div>
  </div>
</div>

<script>
(function(){
  const SESSION = {{ session.id }};
  const palette = { viewer:'#fe2c55', contributors:'#4dd0e1',
                    like:'#ff8a00', comment:'#3fa9f5',
                    gift:'#b56dff', diamond:'#ffe066', join:'#2ecc71' };
  function makeChart(id, color){
    return new Chart(document.getElementById(id).getContext('2d'),{
      type:'line',
      data:{labels:[], datasets:[{data:[], borderColor:color,
            backgroundColor:color+'22', tension:0.25, fill:true,
            pointRadius:0, borderWidth:2}]},
      options:{ responsive:true, animation:false,
        plugins:{ legend:{ display:false } },
        scales:{
          x:{ ticks:{ color:'#888', maxRotation:0, autoSkip:true, maxTicksLimit:6 },
              grid:{ color:'#2a2a2c' } },
          y:{ ticks:{ color:'#888', precision:0, stepSize:1,
                       callback: function(v){ return Number.isInteger(v) ? v : null; } },
              grid:{ color:'#2a2a2c' }, beginAtZero:false }
        } }
    });
  }
  const charts = {
    contributors: makeChart('c-contributors', palette.contributors),
    like:         makeChart('c-like',         palette.like),
    comment:      makeChart('c-comment',      palette.comment),
    gift:         makeChart('c-gift',         palette.gift),
    diamond:      makeChart('c-diamond',      palette.diamond),
    join:         makeChart('c-join',         palette.join),
  };
  // Expose to the shared chart-modal script.
  window._chartsRegistry = Object.assign(window._chartsRegistry || {}, charts);
  function fmtTime(iso){ const d=new Date(iso);
    return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}); }

  // Time-range filter for all charts. 0 = show all.
  let chartRangeSec = 0;
  function filterByRange(rows){
    if (!chartRangeSec || chartRangeSec <= 0) return rows;
    const cutoff = Date.now() - chartRangeSec * 1000;
    return rows.filter(r => new Date(r.at).getTime() >= cutoff);
  }
  const rangeSel = document.getElementById('chart-range');
  if (rangeSel) {
    rangeSel.addEventListener('change', e => {
      chartRangeSec = parseInt(e.target.value, 10) || 0;
      loadCharts();
    });
  }

  async function loadCharts(){
    // Cumulative metrics via 5s snapshots, but viewer count via the raw
    // viewer_seq stream (real TikTok push cadence, captures dips too).
    const snapsP  = fetch('/api/snapshots/'+SESSION).then(r => r.json());
    const viewerP = fetch('/api/viewer_series/'+SESSION).then(r => r.json());
    let [rows, viewer] = await Promise.all([snapsP, viewerP]);
    rows = filterByRange(rows);
    viewer = filterByRange(viewer);

    const labels = rows.map(r => fmtTime(r.at));
    function setSnap(key, col){
      charts[key].data.labels = labels;
      charts[key].data.datasets[0].data = rows.map(r => r[col]);
      charts[key].update('none');
    }
    setSnap('like','like_total');
    setSnap('comment','comment_total');
    setSnap('gift','gift_total');
    setSnap('diamond','diamond_total');
    setSnap('join','join_total');

    // Viewer chart removed from this page — current value is in the KPI tile.
    const vlabels = viewer.map(r => fmtTime(r.at));
    charts.contributors.data.labels = vlabels;
    charts.contributors.data.datasets[0].data = viewer.map(r => r.m_total);
    charts.contributors.update('none');
  }

  // 'completed' shows only streak ends; 'all' shows every WebSocket frame.
  let giftMode = 'completed';
  function updateGiftMode(mode) {
    giftMode = mode;
    document.getElementById('s-gift-mode-completed').classList.toggle('active', mode === 'completed');
    document.getElementById('s-gift-mode-all').classList.toggle('active', mode === 'all');
    loadFeed('gift', 'feed-gifts');
  }
  document.getElementById('s-gift-mode-completed').addEventListener(
    'click', () => updateGiftMode('completed'));
  document.getElementById('s-gift-mode-all').addEventListener(
    'click', () => updateGiftMode('all'));

  // User-filter for the comments feed. Empty string = no filter.
  let commentUserFilter = '';
  (function setupCommentSearch(){
    const inp   = document.getElementById('comments-user-search');
    const btn   = document.getElementById('comments-user-clear');
    const wrap  = inp ? inp.parentElement : null;
    if (!inp || !btn || !wrap) return;
    let debounce = null;
    function apply() {
      commentUserFilter = inp.value.trim();
      wrap.classList.toggle('has-value', commentUserFilter.length > 0);
      loadFeed('comment', 'feed-comments');
    }
    inp.addEventListener('input', () => {
      clearTimeout(debounce);
      debounce = setTimeout(apply, 220);   // debounce keystrokes
    });
    inp.addEventListener('keydown', e => {
      if (e.key === 'Escape') { inp.value = ''; apply(); }
    });
    btn.addEventListener('click', () => { inp.value = ''; apply(); inp.focus(); });
  })();

  async function loadFeed(kind, mountId, params){
    // For gifts we obey giftMode; for comments we just pull every event
    // (and apply the user filter if one is set).
    let qs;
    if (params != null) qs = params;
    else if (kind === 'gift') qs = (giftMode === 'completed') ? '&completed=1' : '';
    else if (kind === 'comment' && commentUserFilter) {
      qs = '&user=' + encodeURIComponent(commentUserFilter);
    } else qs = '';
    const r = await fetch('/api/events/'+SESSION+'?kind='+kind+'&limit=200'+qs);
    const rows = await r.json();
    const mount = document.getElementById(mountId);

    if (kind === 'gift') {
      const ends = rows.filter(r => r.repeat_end === true).length;
      const label = giftMode === 'completed'
        ? '(' + rows.length + ' streak' + (rows.length === 1 ? '' : 's') + ')'
        : '(' + rows.length + ' events · ' + ends + ' streak ends)';
      document.getElementById('s-gift-count').textContent = label;
    }
    if (kind === 'comment') {
      const lbl = commentUserFilter
        ? '(' + rows.length + ' match' + (rows.length === 1 ? '' : 'es') + ')'
        : '(' + rows.length + ')';
      const el = document.getElementById('comments-count');
      if (el) el.textContent = lbl;
    }

    if (!rows.length) {
      const empty = (kind === 'comment' && commentUserFilter)
        ? `no comments from "${escapeHtml(commentUserFilter)}" in this session`
        : 'no '+kind+'s yet';
      mount.innerHTML = '<div class="empty" style="padding:8px;">' + empty + '</div>';
      return;
    }
    mount.innerHTML = rows.map(r => {
      const t = fmtTime(r.at);
      const ul = userLabel(r.unique_id, r.nickname, r.user_id);
      if (kind === 'comment') {
        return `<div class="feed-row"><span class="feed-time">${t}</span>`+
               `<span class="feed-user">${ul}</span>`+
               `<span class="feed-text">${escapeHtml(r.comment_text||'')}</span></div>`;
      } else if (kind === 'gift') {
        const gname = r.gift_name || 'gift';
        const dia = r.gift_diamond_count || 0;
        const rep = r.gift_repeat_count || 1;
        const total = dia * rep;
        const isEnd = r.repeat_end === true;
        const detail = rep > 1
          ? `→ ${escapeHtml(gname)} × ${rep} (💎${dia} each = <b style="color:#ffe066;">${total}💎</b>)`
          : `→ ${escapeHtml(gname)} (💎${dia})`;
        const cls = (giftMode === 'all' && !isEnd) ? ' tick' : '';
        const mark = isEnd ? '✓' : '';
        return `<div class="feed-row${cls}">`+
               `<span class="end-mark">${mark}</span>`+
               `<span class="feed-time">${t}</span>`+
               `<span class="feed-user">${ul}</span>`+
               `<span class="feed-text">${detail}</span></div>`;
      }
      return '';
    }).join('');
  }

  async function loadTops(){
    const r = await fetch('/api/top/'+SESSION);
    const j = await r.json();
    document.getElementById('top-chatters').innerHTML = j.chatters.length
      ? j.chatters.map(c => `<div class="toprow"><span>${userLabel(c.unique_id, c.nickname)}</span><b>${c.comments} msgs</b></div>`).join('')
      : '<div class="empty" style="padding:8px;">no data</div>';
    document.getElementById('top-gifters').innerHTML = j.gifters.length
      ? j.gifters.map(g => `<div class="toprow"><span>${userLabel(g.unique_id, g.nickname)}</span><b>💎${g.diamonds} (${g.gifts} gifts)</b></div>`).join('')
      : '<div class="empty" style="padding:8px;">no data</div>';
  }
  function escapeHtml(s){return String(s).replace(/[&<>"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));}
  // Render an audience member as "Nickname @handle" (or just one when only
  // one is known). The @handle is muted so the human-readable nickname
  // leads. Falls back to the numeric user_id if both are missing.
  function userLabel(unique_id, nickname, user_id) {
    const nn  = (nickname || '').trim();
    const uid = (unique_id || '').trim();
    if (nn && uid && nn.toLowerCase() !== uid.toLowerCase()) {
      return '<b>' + escapeHtml(nn) + '</b> '
           + '<span class="muted">@' + escapeHtml(uid) + '</span>';
    }
    if (nn || uid) return '<b>' + escapeHtml(nn || uid) + '</b>';
    if (user_id)  return '<span class="muted">id ' + escapeHtml(String(user_id)) + '</span>';
    return '<span class="muted">unknown</span>';
  }

  function fmtInt(n) { return (n == null) ? '—' : Number(n).toLocaleString(); }
  async function loadKpis(){
    try {
      const s = await (await fetch('/api/session/' + SESSION)).json();
      const setVal = (id, v) => {
        const el = document.getElementById(id);
        if (el) el.textContent = fmtInt(v);
      };
      setVal('kpi-mtotal',  s.latest_m_total);
      setVal('kpi-peak',    s.peak_viewers);
      setVal('kpi-like',    s.total_likes);
      setVal('kpi-comment', s.total_comments);
      setVal('kpi-gift',    s.total_gifts);
      setVal('kpi-diamond', s.total_diamonds);
      setVal('kpi-join',    s.total_joins);
      setVal('kpi-follow',  s.total_follows);
      setVal('kpi-share',   s.total_shares);
    } catch (e) { console.error(e); }
  }

  function refresh(){
    loadCharts(); loadKpis();
    loadFeed('comment','feed-comments'); loadFeed('gift','feed-gifts'); loadTops();
  }
  refresh();
  setInterval(refresh, 5000);
})();
</script>

{% if not session.ended_at %}
<script>
// FLV live-video player. Pulls stream URLs from /api/session/<id>/stream
// (which resolves them anonymously via webcast/room/info). Uses mpegts.js
// (the maintained fork of flv.js) for ~3-5s latency in any modern browser.
//
// PHASE 2 TODO — Low-Latency LLS via WebRTC:
//   /api/session/<id>/stream already returns the `.lls` URL for each quality
//   (TikTok's WebRTC SDP endpoint, sub-second latency). To play it we'd need
//   a server-side WebRTC bridge (Janus / mediasoup / pion-RTP-to-WHEP)
//   that re-offers the stream as a WHEP / WebRTC endpoint our browser can
//   consume. Until that exists, this player sticks to FLV.
(function () {
  const SESSION = {{ session.id }};
  const videoEl = document.getElementById('live-video');
  const card    = document.getElementById('video-card');
  const helpEl  = document.getElementById('video-help');
  const qualityGroup = document.getElementById('quality-group');
  if (!videoEl || !card) return;

  let player = null;
  let currentQuality = 'hd';
  let lastInfo = null;

  function destroyPlayer() {
    if (player) {
      try { player.pause(); } catch (e) {}
      try { player.unload(); } catch (e) {}
      try { player.detachMediaElement(); } catch (e) {}
      try { player.destroy(); } catch (e) {}
      player = null;
    }
  }

  function showError(msg) {
    destroyPlayer();
    card.innerHTML = '<div class="video-error">📺 ' + msg + '</div>';
  }

  async function fetchStream() {
    try {
      const r = await fetch('/api/session/' + SESSION + '/stream');
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        return { error: j.error || ('http ' + r.status) };
      }
      return await r.json();
    } catch (e) {
      return { error: String(e) };
    }
  }

  function pickUrl(info, quality) {
    if (!info || !info.qualities) return null;
    const q = info.qualities[quality] || info.qualities[info.default_quality]
            || Object.values(info.qualities)[0];
    return q ? (q.flv || info.flv_url || null) : (info.flv_url || null);
  }

  function loadAt(quality) {
    if (!lastInfo) return;
    const url = pickUrl(lastInfo, quality);
    if (!url) { showError('no FLV URL available for this stream'); return; }

    destroyPlayer();

    if (typeof mpegts === 'undefined' || !mpegts.isSupported()) {
      // Fallback to HLS via native <video> (Safari) or just give up.
      const q = (lastInfo.qualities || {})[quality] || {};
      const hls = q.hls || lastInfo.hls_url;
      if (hls) {
        videoEl.src = hls;
        helpEl.textContent = 'HLS · ~4-8s delay';
        videoEl.play().catch(() => {});
        return;
      }
      showError('FLV player unsupported in this browser');
      return;
    }

    player = mpegts.createPlayer(
      { type: 'flv', isLive: true, url: url },
      { enableWorker: true, lazyLoad: false,
        liveBufferLatencyChasing: true,
        liveBufferLatencyMaxLatency: 2.0,
        liveBufferLatencyMinRemain: 0.5 }
    );
    player.attachMediaElement(videoEl);
    player.load();
    player.play().catch(() => { /* autoplay blocked is fine — user can click */ });
    helpEl.textContent = 'FLV · ' + quality.toUpperCase()
                       + ' · ~3-5s delay'
                       + (lastInfo.stream_size && lastInfo.stream_size.width
                          ? '  · ' + lastInfo.stream_size.width + '×'
                            + lastInfo.stream_size.height : '');
  }

  qualityGroup.querySelectorAll('.quality-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      qualityGroup.querySelectorAll('.quality-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentQuality = btn.dataset.q;
      loadAt(currentQuality);
    });
  });

  // Initial setup
  (async () => {
    const info = await fetchStream();
    if (info.error) {
      showError('stream unavailable: ' + info.error);
      return;
    }
    lastInfo = info;
    if (info.default_quality && info.default_quality !== 'hd') {
      currentQuality = info.default_quality;
      qualityGroup.querySelectorAll('.quality-pill').forEach(b => {
        b.classList.toggle('active', b.dataset.q === currentQuality);
      });
    }
    loadAt(currentQuality);
  })();

  // If the player stalls on a stream-URL expiry, re-resolve from backend.
  videoEl.addEventListener('error', async () => {
    lastInfo = await fetchStream();
    if (lastInfo && !lastInfo.error) loadAt(currentQuality);
  });
})();
</script>
{% endif %}

""" + _CHART_MODAL_HTML + """
<script>""" + _CHART_MODAL_JS + """</script>
</body></html>
"""
)


WATCH_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Watch · {{ live_count }} live · v2</title>
<style>""" + _BASE_STYLE + """
.layout-toolbar { display: flex; gap: 8px; margin: 0 0 14px;
                  align-items: center; flex-wrap: wrap; }
.layout-toolbar .mode-btn { background: #2a2a2c; color: #aaa;
                            border: 1px solid #2a2a2c; padding: 4px 10px;
                            border-radius: 14px; cursor: pointer;
                            font-size: 11px; font-weight: 500; }
.layout-toolbar .mode-btn.active {
    background: #fe2c55; color: #fff; border-color: #fe2c55; }
.layout-toolbar .hint { color: #777; font-size: 12px; margin-left: 6px; }

.watch-grid { display: grid;
              grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
              gap: 12px; align-items: start; }
.watch-grid.cols-1 { grid-template-columns: 1fr; }
.watch-grid.cols-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.watch-grid.cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }

.tile { background: #1c1c1e; border-radius: 12px; overflow: hidden;
        display: flex; flex-direction: column;
        border: 1px solid #2a2a2c; transition: border-color 0.15s; }
.tile.focused { border-color: #fe2c55;
                box-shadow: 0 0 0 1px #fe2c55, 0 6px 20px rgba(254,44,85,0.18); }
.tile.offline { opacity: 0.75; }

.tile-head { display: flex; align-items: center; gap: 8px;
             padding: 8px 12px; background: #15151a;
             border-bottom: 1px solid #2a2a2c; }
.tile-head .pill { background: #fe2c55; color: #fff; font-size: 10px;
                   padding: 2px 7px; border-radius: 999px;
                   text-transform: uppercase; font-weight: 600;
                   letter-spacing: 0.4px; }
.tile-head .pill.off { background: #555; }
.tile-head .name { font-weight: 600; color: #fff; flex: 1 1 auto;
                   white-space: nowrap; overflow: hidden;
                   text-overflow: ellipsis; font-size: 14px; }
.tile-head .name a { color: #fff; text-decoration: none; }
.tile-head .name a:hover { color: #fe2c55; }
.tile-head .audio-state { color: #888; font-size: 11px; flex: 0 0 auto; }
.tile-head .audio-state.on { color: #fe2c55; }
.tile-head .qpill { background: #2a2a2c; color: #aaa; border: 0;
                    padding: 3px 8px; border-radius: 10px;
                    font-size: 10px; font-weight: 600; cursor: pointer;
                    letter-spacing: 0.3px; }
.tile-head .qpill.active { background: #fe2c55; color: #fff; }

.tile-video { position: relative; height: var(--tile-vh, 40vh); background: #000;
              cursor: pointer; }
.tile-video video { width: 100%; height: 100%; display: block;
                    background: #000; }
.tile-video .err { position: absolute; inset: 0;
                   display: flex; align-items: center; justify-content: center;
                   color: #aaa; font-size: 12px; padding: 12px;
                   text-align: center; }
.tile-video .focus-hint { position: absolute; bottom: 8px; right: 10px;
                          color: rgba(255,255,255,0.7); font-size: 11px;
                          background: rgba(0,0,0,0.45); padding: 2px 8px;
                          border-radius: 999px; pointer-events: none;
                          opacity: 0; transition: opacity 0.15s; }
.tile-video:hover .focus-hint { opacity: 1; }
.tile.focused .tile-video .focus-hint { opacity: 0; }

.tile-kpis { display: grid;
             grid-template-columns: repeat(4, 1fr);
             gap: 1px; background: #2a2a2c; }
.tile-kpis .k { background: #1c1c1e; padding: 6px 8px;
                font-variant-numeric: tabular-nums; text-align: center; }
.tile-kpis .lbl { color: #888; font-size: 10px;
                  text-transform: uppercase; letter-spacing: 0.4px; }
.tile-kpis .v { color: #fff; font-weight: 600; font-size: 14px;
                margin-top: 1px; }

.tile-gifts-head { display: flex; align-items: center; gap: 6px;
                   width: 100%; padding: 6px 12px; background: #181821;
                   color: #ccc; font-size: 12px; font-weight: 600;
                   border: 0; border-bottom: 1px solid #2a2a2c;
                   cursor: pointer; text-align: left; }
.tile-gifts-head:hover { background: #1f1f29; }
.tile-gifts-head .caret { display: inline-block; color: #888;
                          font-size: 10px; transition: transform 0.15s;
                          width: 10px; }
.tile-gifts-head.open .caret { transform: rotate(90deg); }
.tile-gifts-head .lbl { flex: 1 1 auto; }
.tile-gifts-head .count { color: #888; font-weight: 500; }
.tile-gifts { padding: 6px 12px 8px;
              max-height: 180px; min-height: 180px; overflow-y: auto;
              font-size: 12px; background: #181821;
              border-bottom: 1px solid #2a2a2c; }
.tile-gifts.collapsed { display: none; }
.tile-gifts .row { padding: 3px 0; color: #ccc;
                   border-bottom: 1px solid #23232a; display: flex;
                   gap: 6px; align-items: baseline; }
.tile-gifts .row:last-child { border-bottom: none; }
.tile-gifts .u { color: #bbb; font-weight: 600; flex: 0 0 auto;
                 max-width: 110px; white-space: nowrap; overflow: hidden;
                 text-overflow: ellipsis; }
.tile-gifts .t { color: #ddd; flex: 1 1 auto;
                 white-space: nowrap; overflow: hidden;
                 text-overflow: ellipsis; }
.tile-gifts .dia { color: #ffe066; font-weight: 600; flex: 0 0 auto; }
.tile-gifts .empty { color: #777; padding: 8px 0; }

.tile-feed { padding: 6px 12px 10px;
             max-height: 300px; min-height: 300px; overflow-y: auto;
             font-size: 12px; background: #1c1c1e; }
.tile-feed .row { padding: 3px 0; color: #ccc;
                  border-bottom: 1px solid #2a2a2c; display: flex;
                  gap: 6px; align-items: baseline; }
.tile-feed .row:last-child { border-bottom: none; }
.tile-feed .u { color: #bbb; font-weight: 600; flex: 0 0 auto;
                max-width: 110px; white-space: nowrap; overflow: hidden;
                text-overflow: ellipsis; }
.tile-feed .t { color: #ddd; flex: 1 1 auto;
                white-space: nowrap; overflow: hidden;
                text-overflow: ellipsis; }
.tile-feed .empty { color: #777; padding: 8px 0; }
</style>
<script src="https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.js"></script>
</head>
<body>
<h1>
  <a href="/">← all targets</a>
  &nbsp;Watch lives
  <span class="muted" style="font-size:14px;font-weight:400;">
    · {{ live_count }} live{% if count > live_count %} · {{ count - live_count }} offline{% endif %}
  </span>
</h1>

<div class="layout-toolbar">
  <span class="muted" style="font-size:12px;">layout</span>
  <button class="mode-btn active" data-cols="auto">auto</button>
  <button class="mode-btn"        data-cols="1">1 col</button>
  <button class="mode-btn"        data-cols="2">2 col</button>
  <button class="mode-btn"        data-cols="3">3 col</button>
  <span class="muted" style="font-size:12px;margin-left:12px;">height</span>
  <input type="range" id="height-slider" min="20" max="90" step="5" value="40"
         style="vertical-align:middle;">
  <span class="muted" style="font-size:12px;" id="height-val">40%</span>
  <span class="hint">click a tile to unmute / switch audio</span>
</div>

{% if not tiles %}
  <div class="empty">No targets selected. Pick lives on the <a href="/" style="color:#fe2c55;">home page</a>.</div>
{% endif %}

<div class="watch-grid" id="grid">
  {% for t in tiles %}
  <div class="tile {{ '' if t.live else 'offline' }}"
       data-username="{{ t.username }}"
       data-session-id="{{ t.session_id or '' }}"
       data-live="{{ '1' if t.live else '0' }}">
    <div class="tile-head">
      <span class="pill {{ '' if t.live else 'off' }}">{{ 'LIVE' if t.live else 'offline' }}</span>
      <span class="name">
        {% if t.live %}
          <a href="/session/{{ t.session_id }}" target="_blank" title="open session detail">@{{ t.username }}</a>
        {% else %}
          <a href="/target/{{ t.username }}" target="_blank">@{{ t.username }}</a>
        {% endif %}
      </span>
      {% if t.live %}
        <span class="audio-state" data-role="audio-state">🔇 muted</span>
        <button class="qpill"         data-q="ld" title="low quality">LD</button>
        <button class="qpill active"  data-q="hd" title="HD quality">HD</button>
      {% endif %}
    </div>
    <div class="tile-video">
      {% if t.live %}
        <video muted autoplay playsinline></video>
        <div class="focus-hint">click to focus audio</div>
      {% else %}
        <div class="err">{% if t.missing %}unknown target @{{ t.username }}{% else %}target is offline{% endif %}</div>
      {% endif %}
    </div>
    <div class="tile-kpis">
      <div class="k"><div class="lbl">👥 mTotal</div><div class="v" data-kpi="mtotal">—</div></div>
      <div class="k"><div class="lbl">❤ likes</div><div class="v" data-kpi="like">—</div></div>
      <div class="k"><div class="lbl">💬 chats</div><div class="v" data-kpi="comment">—</div></div>
      <div class="k"><div class="lbl">💎 diamonds</div><div class="v" data-kpi="diamond">—</div></div>
    </div>
    <button type="button" class="tile-gifts-head" data-acc="gifts">
      <span class="caret">▸</span>
      <span class="lbl">🎁 gifts</span>
      <span class="count" data-gift-count>—</span>
    </button>
    <div class="tile-gifts collapsed">
      <div class="empty">{% if t.live %}awaiting gifts…{% else %}—{% endif %}</div>
    </div>
    <div class="tile-feed">
      <div class="empty">{% if t.live %}awaiting comments…{% else %}—{% endif %}</div>
    </div>
  </div>
  {% endfor %}
</div>

<script>
(function(){
  const grid = document.getElementById('grid');
  if (!grid) return;

  // ---- layout switcher
  document.querySelectorAll('.layout-toolbar .mode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.layout-toolbar .mode-btn').forEach(b =>
        b.classList.remove('active'));
      btn.classList.add('active');
      grid.classList.remove('cols-1','cols-2','cols-3');
      const c = btn.dataset.cols;
      if (c !== 'auto') grid.classList.add('cols-' + c);
    });
  });

  // ---- gift accordion toggle
  document.querySelectorAll('.tile-gifts-head').forEach(btn => {
    btn.addEventListener('click', () => {
      btn.classList.toggle('open');
      const body = btn.nextElementSibling;
      if (body && body.classList.contains('tile-gifts')) {
        body.classList.toggle('collapsed');
      }
    });
  });

  // ---- tile-height slider
  const hSlider = document.getElementById('height-slider');
  const hLabel  = document.getElementById('height-val');
  if (hSlider) {
    const apply = () => {
      const v = hSlider.value;
      document.documentElement.style.setProperty('--tile-vh', v + 'vh');
      if (hLabel) hLabel.textContent = v + '%';
    };
    hSlider.addEventListener('input', apply);
    apply();
  }

  function fmtInt(n){ return (n == null) ? '—' : Number(n).toLocaleString(); }
  function escapeHtml(s){ return String(s).replace(/[&<>"]/g, m =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m])); }
  function userLabel(uniq, nick) {
    const n = (nick||'').trim(); const u = (uniq||'').trim();
    if (n && u && n.toLowerCase() !== u.toLowerCase()) return escapeHtml(n);
    return escapeHtml(n || u || 'unknown');
  }

  // ---- per-tile state (only live tiles get a player)
  const tiles = [];
  document.querySelectorAll('.tile[data-live="1"]').forEach(el => {
    tiles.push({
      el,
      username:  el.dataset.username,
      sessionId: parseInt(el.dataset.sessionId, 10),
      video:     el.querySelector('video'),
      player:    null,
      quality:   'hd',
      info:      null,
    });
  });

  // ---- audio focus: at most one tile unmuted
  let focused = null;
  function setFocus(tile) {
    focused = tile;
    tiles.forEach(t => {
      const isFocus = t === tile;
      t.el.classList.toggle('focused', isFocus);
      if (t.video) t.video.muted = !isFocus;
      const lbl = t.el.querySelector('[data-role="audio-state"]');
      if (lbl) {
        lbl.textContent = isFocus ? '🔊 audio' : '🔇 muted';
        lbl.classList.toggle('on', isFocus);
      }
    });
  }
  tiles.forEach(t => {
    t.el.querySelector('.tile-video').addEventListener('click', () => setFocus(t));
  });

  // ---- player setup
  async function fetchStream(sid) {
    try {
      const r = await fetch('/api/session/' + sid + '/stream');
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        return { error: j.error || ('http ' + r.status) };
      }
      return await r.json();
    } catch (e) { return { error: String(e) }; }
  }
  function pickUrl(info, quality) {
    if (!info || !info.qualities) return info && info.flv_url || null;
    const q = info.qualities[quality]
           || info.qualities[info.default_quality]
           || Object.values(info.qualities)[0];
    return q ? (q.flv || info.flv_url || null) : (info.flv_url || null);
  }
  function showErr(tile, msg) {
    const v = tile.el.querySelector('.tile-video');
    v.innerHTML = '<div class="err">' + escapeHtml(msg) + '</div>';
  }
  function destroyPlayer(tile) {
    if (!tile.player) return;
    try { tile.player.pause(); } catch(e){}
    try { tile.player.unload(); } catch(e){}
    try { tile.player.detachMediaElement(); } catch(e){}
    try { tile.player.destroy(); } catch(e){}
    tile.player = null;
  }
  function loadPlayer(tile, quality) {
    const url = pickUrl(tile.info, quality);
    if (!url) { showErr(tile, 'no FLV URL available'); return; }
    destroyPlayer(tile);
    if (typeof mpegts === 'undefined' || !mpegts.isSupported()) {
      // Safari fallback via HLS in native <video>
      const q = (tile.info.qualities || {})[quality] || {};
      const hls = q.hls || tile.info.hls_url;
      if (hls) { tile.video.src = hls; tile.video.play().catch(()=>{}); return; }
      showErr(tile, 'FLV unsupported in this browser');
      return;
    }
    tile.player = mpegts.createPlayer(
      { type: 'flv', isLive: true, url: url },
      { enableWorker: true, lazyLoad: false,
        liveBufferLatencyChasing: true,
        liveBufferLatencyMaxLatency: 2.0,
        liveBufferLatencyMinRemain: 0.5 }
    );
    tile.player.attachMediaElement(tile.video);
    tile.player.load();
    tile.player.play().catch(()=>{});
    tile.quality = quality;
  }
  async function initPlayer(tile) {
    tile.info = await fetchStream(tile.sessionId);
    if (!tile.info || tile.info.error) {
      showErr(tile, 'stream unavailable: ' + (tile.info && tile.info.error || 'unknown'));
      return;
    }
    if (tile.info.default_quality && tile.info.default_quality !== 'hd') {
      tile.quality = tile.info.default_quality;
      tile.el.querySelectorAll('.qpill').forEach(b =>
        b.classList.toggle('active', b.dataset.q === tile.quality));
    }
    loadPlayer(tile, tile.quality);
    tile.el.querySelectorAll('.qpill').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        tile.el.querySelectorAll('.qpill').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        loadPlayer(tile, btn.dataset.q);
      });
    });
    tile.video.addEventListener('error', async () => {
      // Stream URLs expire — re-resolve and reload.
      tile.info = await fetchStream(tile.sessionId);
      if (tile.info && !tile.info.error) loadPlayer(tile, tile.quality);
    });
  }
  tiles.forEach(initPlayer);

  // ---- KPI + comment refresh (every 5s)
  async function refreshKpis(tile) {
    try {
      const s = await (await fetch('/api/session/' + tile.sessionId)).json();
      if (s && !s.error) {
        const set = (k, v) => {
          const el = tile.el.querySelector('[data-kpi="' + k + '"]');
          if (el) el.textContent = fmtInt(v);
        };
        set('mtotal',  s.latest_m_total);
        set('like',    s.total_likes);
        set('comment', s.total_comments);
        set('diamond', s.total_diamonds);
      }
    } catch(e) { /* ignore */ }
  }
  async function refreshGifts(tile) {
    try {
      const rows = await (await fetch(
        '/api/events/' + tile.sessionId + '?kind=gift&completed=1&limit=100')).json();
      const feed = tile.el.querySelector('.tile-gifts');
      const countEl = tile.el.querySelector('[data-gift-count]');
      if (countEl) countEl.textContent = Array.isArray(rows) ? '(' + rows.length + ')' : '—';
      if (!Array.isArray(rows) || !rows.length) {
        feed.innerHTML = '<div class="empty">no gifts yet</div>';
        return;
      }
      feed.innerHTML = rows.map(r => {
        const name = escapeHtml(r.gift_name || 'gift');
        const dia  = r.gift_diamond_count || 0;
        const rep  = r.gift_repeat_count  || 1;
        const total = dia * rep;
        const detail = rep > 1
          ? name + ' × ' + rep
          : name;
        return '<div class="row">'+
          '<span class="u">' + userLabel(r.unique_id, r.nickname) + '</span>'+
          '<span class="t">' + detail + '</span>'+
          '<span class="dia">💎' + total + '</span>'+
          '</div>';
      }).join('');
    } catch(e) { /* ignore */ }
  }
  async function refreshComments(tile) {
    try {
      const rows = await (await fetch(
        '/api/events/' + tile.sessionId + '?kind=comment&limit=200')).json();
      const feed = tile.el.querySelector('.tile-feed');
      if (!Array.isArray(rows) || !rows.length) {
        feed.innerHTML = '<div class="empty">no comments yet</div>';
        return;
      }
      feed.innerHTML = rows.map(r =>
        '<div class="row">'+
        '<span class="u">' + userLabel(r.unique_id, r.nickname) + '</span>'+
        '<span class="t">' + escapeHtml(r.comment_text || '') + '</span>'+
        '</div>'
      ).join('');
    } catch(e) { /* ignore */ }
  }
  function refreshAll() {
    tiles.forEach(t => { refreshKpis(t); refreshGifts(t); refreshComments(t); });
  }
  refreshAll();
  setInterval(refreshAll, 5000);

  // ---- default focus = first live tile
  if (tiles.length) setFocus(tiles[0]);
})();
</script>
</body></html>
"""
)


# ---------------------------------------------------------------- route handlers

def _decorate_target(t: dict) -> dict:
    started = t.get("started_at")
    if started and isinstance(started, datetime):
        t["started_at_seconds_ago"] = int((datetime.now(timezone.utc) - started).total_seconds())
    # ISO-format the lifetime timestamps so the home-page <time class="ago">
    # script can parse them client-side.
    for k in ("latest_started_at", "latest_ended_at"):
        v = t.get(k)
        t[f"{k}_iso"] = _iso(v) if isinstance(v, datetime) else None
    return t


def _decorate_session(s: dict) -> dict:
    started = s.get("started_at")
    ended = s.get("ended_at")
    now = datetime.now(timezone.utc)
    if isinstance(started, datetime):
        s["started_str"] = started.astimezone().strftime("%Y-%m-%d %H:%M")
        end = ended if isinstance(ended, datetime) else now
        s["duration_seconds"] = max(0, int((end - started).total_seconds()))
        s["started_at_seconds_ago"] = int((now - started).total_seconds())
    return s


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    rows = [_decorate_target(dict(t)) for t in db.list_targets()]
    storage = db.get_storage_stats()
    for r in rows:
        s = storage.get(r["username"], {})
        r["event_count"] = s.get("event_count", 0)
        r["snapshot_count"] = s.get("snapshot_count", 0)
        r["approx_bytes"] = s.get("approx_bytes", 0)
    live_count = sum(1 for t in rows if t.get("session_id"))
    html = HOME_PAGE.render(
        targets=rows, live_count=live_count,
        now=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        duration=_human_duration, fmt=_fmt_int, bytes_fmt=_human_bytes,
    )
    return HTMLResponse(html)


@app.get("/target/{username}", response_class=HTMLResponse)
def target(username: str) -> HTMLResponse:
    sessions = [_decorate_session(dict(s)) for s in db.list_sessions(username)]
    open_session = next((s for s in sessions if s.get("ended_at") is None), None)
    started_at_ms = 0
    if open_session and isinstance(open_session.get("started_at"), datetime):
        started_at_ms = int(open_session["started_at"].timestamp() * 1000)
    html = TARGET_PAGE.render(
        username=username, sessions=sessions, open_session=open_session,
        started_at_ms=started_at_ms,
        duration=_human_duration, fmt=_fmt_int,
    )
    return HTMLResponse(html)


@app.get("/session/{session_id}", response_class=HTMLResponse)
def session(session_id: int) -> HTMLResponse:
    s = db.get_session(session_id)
    if not s:
        return HTMLResponse("session not found", status_code=404)
    s = _decorate_session(dict(s))
    html = SESSION_PAGE.render(
        session=s, duration=_human_duration, fmt=_fmt_int,
    )
    return HTMLResponse(html)


@app.get("/watch", response_class=HTMLResponse)
def watch(targets: str = "") -> HTMLResponse:
    """Multi-live watch grid. `targets` = comma-separated usernames. Each tile
    resolves to the target's currently-open session; offline / unknown targets
    render as a greyed placeholder so the URL stays shareable across restarts.
    """
    requested = []
    seen = set()
    for raw in targets.split(","):
        u = raw.strip().lstrip("@")
        if u and u not in seen:
            seen.add(u)
            requested.append(u)
    by_user = {t["username"]: dict(t) for t in db.list_targets()}
    tiles = []
    for u in requested:
        t = by_user.get(u)
        if not t:
            tiles.append({"username": u, "live": False, "missing": True,
                          "session_id": None, "title": None})
            continue
        tiles.append({
            "username":   u,
            "live":       bool(t.get("session_id")),
            "session_id": t.get("session_id"),
            "title":      t.get("title"),
            "missing":    False,
        })
    live_count = sum(1 for x in tiles if x["live"])
    html = WATCH_PAGE.render(
        tiles=tiles, count=len(tiles), live_count=live_count,
        fmt=_fmt_int,
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------- JSON APIs

@app.get("/api/state")
def api_state() -> JSONResponse:
    rows = [_decorate_target(dict(t)) for t in db.list_targets()]
    # Serialize datetimes
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, datetime):
                r[k] = _iso(v)
    return JSONResponse({"targets": rows, "now": _iso(datetime.now(timezone.utc))})


@app.get("/api/sessions/{username}")
def api_sessions(
    username: str,
    as_: str | None = Query(default=None, alias="as"),
    days: int = 60,
) -> JSONResponse:
    """Default mode: raw session rows. ?as=calendar mode: FullCalendar events."""
    raw_sessions = [_decorate_session(dict(s)) for s in db.list_sessions(username, days)]
    if as_ != "calendar":
        for s in raw_sessions:
            for k, v in list(s.items()):
                if isinstance(v, datetime):
                    s[k] = _iso(v)
        return JSONResponse(raw_sessions)
    events = []
    for s in raw_sessions:
        end = s.get("ended_at") or datetime.now(timezone.utc)
        events.append({
            "title": ("● " if not s.get("ended_at") else "") + (s.get("title") or "LIVE"),
            "start": _iso(s.get("started_at")),
            "end": _iso(end),
            "backgroundColor": "#fe2c55" if not s.get("ended_at") else "rgba(254,44,85,0.55)",
            "borderColor": "#fe2c55",
            "textColor": "#fff",
            "extendedProps": {
                "session_id": s.get("id"),
                "duration_seconds": s.get("duration_seconds"),
                "peak_viewers": s.get("peak_viewers"),
                "ongoing": s.get("ended_at") is None,
            },
        })
    return JSONResponse(events)


@app.get("/api/sessions/{username}/calendar")
def api_sessions_calendar(username: str, days: int = 60) -> JSONResponse:
    """Alias for ?as=calendar so the URL works in browsers that don't
    pass query strings well."""
    raw_sessions = [_decorate_session(dict(s)) for s in db.list_sessions(username, days)]
    events = []
    for s in raw_sessions:
        end = s.get("ended_at") or datetime.now(timezone.utc)
        events.append({
            "title": ("● " if not s.get("ended_at") else "") + (s.get("title") or "LIVE"),
            "start": _iso(s.get("started_at")),
            "end": _iso(end),
            "backgroundColor": "#fe2c55" if not s.get("ended_at") else "rgba(254,44,85,0.55)",
            "borderColor": "#fe2c55",
            "textColor": "#fff",
            "extendedProps": {
                "session_id": s.get("id"),
                "duration_seconds": s.get("duration_seconds"),
                "peak_viewers": s.get("peak_viewers"),
                "ongoing": s.get("ended_at") is None,
            },
        })
    return JSONResponse(events)


@app.get("/api/session/{session_id}/stream")
async def api_session_stream(session_id: int) -> JSONResponse:
    """Resolve the live FLV / HLS playback URLs for a session by calling
    `webcast/room/info/?room_id=…`. Used by the in-page video player.

    Returns 410 if the session has ended (no live stream to play).
    Returns 502 if TikTok's room/info call fails.

    NOTE: also returns `lls` URLs (TikTok's WebRTC low-latency .sdp endpoints)
    in the qualities map. Browsers can't play them today without bridging
    through a server-side WebRTC peer; see the LLS phase plan in scout.py."""
    import json as _json
    from curl_cffi.requests import AsyncSession  # local import; only this route uses it

    s = db.get_session(session_id)
    if not s:
        return JSONResponse({"error": "session not found"}, status_code=404)
    if s.get("ended_at"):
        return JSONResponse({"error": "session has ended"}, status_code=410)
    room_id = s.get("room_id")
    if not room_id:
        return JSONResponse({"error": "session has no room_id"}, status_code=404)

    url = (f"https://webcast.tiktok.com/webcast/room/info/"
           f"?room_id={room_id}&aid=1988&app_language=en")
    try:
        async with AsyncSession() as session:
            r = await session.get(url, impersonate="chrome", timeout=20)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"upstream fetch failed: {exc!r}"},
                            status_code=502)
    if r.status_code != 200:
        return JSONResponse({"error": f"upstream http {r.status_code}"},
                            status_code=502)
    try:
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"upstream non-JSON: {exc!r}"},
                            status_code=502)

    room = data.get("data") or {}
    if room.get("status") != 2:
        return JSONResponse({"error": "room not live", "status": room.get("status")},
                            status_code=410)

    su = room.get("stream_url") or {}
    qualities: dict[str, dict[str, str | None]] = {}
    sd_str = ((su.get("live_core_sdk_data") or {}).get("pull_data") or {}).get("stream_data")
    if sd_str:
        try:
            sd_obj = _json.loads(sd_str)
            for q, info in (sd_obj.get("data") or {}).items():
                main = info.get("main") or {}
                qualities[q] = {
                    "flv": main.get("flv") or None,
                    "hls": main.get("hls") or None,
                    "lls": main.get("lls") or None,   # WebRTC .sdp — phase 2
                }
        except Exception:  # noqa: BLE001
            pass

    default = "hd" if "hd" in qualities else (
        "ld" if "ld" in qualities else (next(iter(qualities), None))
    )
    return JSONResponse({
        "session_id": session_id,
        "room_id": room_id,
        "default_quality": default,
        "qualities": qualities,
        # Fallback (legacy direct fields) for clients that don't parse the SDK ladder.
        "hls_url": su.get("hls_pull_url"),
        "flv_url": (su.get("flv_pull_url") or {}).get("HD1")
                   if isinstance(su.get("flv_pull_url"), dict) else None,
        "stream_size": {
            "width":  su.get("stream_size_width"),
            "height": su.get("stream_size_height"),
        },
    })


@app.get("/api/session/{session_id}")
def api_session(session_id: int) -> JSONResponse:
    """Latest aggregates for a single session — used by the session page's
    KPI tiles + duration ticker for live updates. Includes `latest_viewer`
    and `latest_m_total` derived from the most recent RoomUserSeqEvent so
    the page can show "now" values alongside the aggregate counters."""
    s = db.get_session(session_id)
    if not s:
        return JSONResponse({"error": "session not found"}, status_code=404)
    d = _decorate_session(dict(s))
    latest_seq = db.get_latest_viewer_seq(session_id)
    d["latest_viewer"]  = latest_seq.get("viewer_count")
    d["latest_m_total"] = latest_seq.get("m_total")
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = _iso(v)
    return JSONResponse(d)


@app.get("/api/viewer_series/{session_id}")
def api_viewer_series(session_id: int) -> JSONResponse:
    """Raw viewer-count time series at WebSocket push resolution
    (~3-5s/sample). Falls back to snapshots if viewer_seq events have been
    GC'd (older than 7 days)."""
    rows = db.get_viewer_series(session_id)
    if not rows:
        # Fallback for old sessions whose noisy viewer_seq events were GC'd
        # — m_total isn't recoverable, so it's None for these rows.
        rows = [
            {"at": r["at"], "viewer_count": r["viewer_count"], "m_total": None}
            for r in db.get_snapshots(session_id)
            if r.get("viewer_count") is not None
        ]
    out = []
    for r in rows:
        d = dict(r)
        d["at"] = _iso(d["at"])
        out.append(d)
    return JSONResponse(out)


@app.get("/api/snapshots/{session_id}")
def api_snapshots(session_id: int) -> JSONResponse:
    rows = []
    for r in db.get_snapshots(session_id):
        d = dict(r)
        d["at"] = _iso(d["at"])
        rows.append(d)
    return JSONResponse(rows)


@app.get("/api/events/{session_id}")
def api_events(
    session_id: int,
    kind: str | None = None,
    limit: int = 50,
    completed: int = 0,
    user: str | None = None,
) -> JSONResponse:
    """`completed=1` filters out interim streak ticks (gift events where
    repeat_end is not TRUE). Default 0 returns every event row.
    `user=<text>` does a case-insensitive substring match on unique_id or
    nickname (leading '@' is allowed and stripped)."""
    kinds = [kind] if kind else None
    limit = max(1, min(limit, 2000))
    rows = []
    for r in db.get_recent_events(
        session_id, kinds=kinds, limit=limit,
        completed_only=bool(completed),
        user_query=user,
    ):
        d = dict(r)
        d["at"] = _iso(d["at"])
        rows.append(d)
    return JSONResponse(rows)


@app.get("/api/top/{session_id}")
def api_top(session_id: int) -> JSONResponse:
    return JSONResponse({
        "chatters": [dict(r) for r in db.get_top_chatters(session_id, 10)],
        "gifters":  [dict(r) for r in db.get_top_gifters(session_id, 10)],
    })


# ---------------------------------------------------------------- entrypoint

def main() -> None:
    ap = argparse.ArgumentParser(description="v2 dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
