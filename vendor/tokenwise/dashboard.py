"""Self-contained HTML savings dashboard, generated from the JSONL ledger.

No build step, no CDN, no storage APIs: one file you can open, email, or drop
behind a static host. Charts are hand-rolled SVG so the whole thing stays
dependency-free.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

_TEMPLATE = r"""<!doctype html>
<html lang="en" data-theme="__THEME__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  .viz-root {
    color-scheme: light;
    --surface-1: #fcfcfb;
    --plane: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --grid: #e1e0d9;
    --axis: #c3c2b7;
    --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6;
    --series-2: #eb6834;
    --series-3: #1baf7a;
    --good: #006300;
    --seq-250: #86b6ef;
    --seq-450: #2a78d6;
    --seq-600: #184f95;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark;
      --surface-1: #1a1a19; --plane: #0d0d0d;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
      --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
      --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
      --good: #0ca30c;
      --seq-250: #184f95; --seq-450: #3987e5; --seq-600: #86b6ef;
    }
  }
  :root[data-theme="dark"] .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19; --plane: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
    --good: #0ca30c;
    --seq-250: #184f95; --seq-450: #3987e5; --seq-600: #86b6ef;
  }

  * { box-sizing: border-box; }
  body { margin: 0; background: var(--plane); color: var(--text-primary);
         font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 32px 20px 64px; }
  header { display: flex; align-items: baseline; justify-content: space-between;
           gap: 16px; flex-wrap: wrap; margin-bottom: 4px; }
  h1 { font-size: 20px; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
  .sub { color: var(--text-secondary); font-size: 13px; margin: 0 0 24px; }
  .controls { display: flex; gap: 8px; align-items: center; }
  button { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 8px;
           border: 1px solid var(--border); background: var(--surface-1);
           color: var(--text-secondary); cursor: pointer; }
  button:hover { color: var(--text-primary); }
  button[aria-pressed="true"] { color: var(--text-primary); font-weight: 600; }

  .tiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
           margin-bottom: 24px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
          padding: 16px 18px; }
  .tile .label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
  .tile .value { font-size: 28px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.1; }
  .tile .foot { font-size: 12px; color: var(--text-muted); margin-top: 6px; }
  .tile .foot.good { color: var(--good); }

  .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
          padding: 18px 18px 12px; margin-bottom: 20px; }
  .card h2 { font-size: 14px; font-weight: 600; margin: 0 0 2px; }
  .card .note { font-size: 12px; color: var(--text-secondary); margin: 0 0 14px; }
  .legend { display: flex; gap: 16px; font-size: 12px; color: var(--text-secondary);
            margin: 0 0 10px; flex-wrap: wrap; }
  .legend i { width: 10px; height: 10px; border-radius: 3px; display: inline-block;
              margin-right: 6px; vertical-align: -1px; }
  svg { display: block; width: 100%; overflow: visible; }
  .tick { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
  .dlabel { fill: var(--text-secondary); font-size: 11px; font-variant-numeric: tabular-nums; }
  .cat { fill: var(--text-primary); font-size: 12px; }

  .tt { position: fixed; pointer-events: none; opacity: 0; transition: opacity .09s;
        background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
        padding: 8px 10px; font-size: 12px; color: var(--text-primary);
        box-shadow: 0 6px 20px rgba(0,0,0,.12); z-index: 20; min-width: 120px; }
  .tt b { font-weight: 600; }
  .tt .row { display: flex; justify-content: space-between; gap: 14px;
             color: var(--text-secondary); margin-top: 3px; }
  .tt .row span:last-child { color: var(--text-primary); font-variant-numeric: tabular-nums; }

  table { width: 100%; border-collapse: collapse; font-size: 13px;
          font-variant-numeric: tabular-nums; }
  th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--grid); }
  th:first-child, td:first-child { text-align: left; font-variant-numeric: normal; }
  th { color: var(--text-secondary); font-weight: 500; font-size: 12px; }
  [hidden] { display: none !important; }
  footer { color: var(--text-muted); font-size: 12px; margin-top: 28px; }
</style>
</head>
<body class="viz-root">
<div class="wrap">
  <header>
    <div>
      <h1>__TITLE__</h1>
    </div>
    <div class="controls">
      <button id="viewToggle" aria-pressed="false">Table view</button>
      <button id="themeToggle">Theme</button>
    </div>
  </header>
  <p class="sub" id="window"></p>

  <div class="tiles" id="tiles"></div>

  <div class="card" id="card-time">
    <h2>Spend over time</h2>
    <p class="note">What you paid, against what the same traffic would have cost unoptimized on the baseline model.</p>
    <div class="legend">
      <span><i style="background:var(--series-2)"></i>Baseline (unoptimized)</span>
      <span><i style="background:var(--series-1)"></i>Actual</span>
    </div>
    <div id="chart-time"></div>
  </div>

  <div class="card" id="card-strategy">
    <h2>Savings by strategy</h2>
    <p class="note">Dollars attributed to each optimizer. A negative bar is real: a cache write or an escalation that did not pay off.</p>
    <div id="chart-strategy"></div>
  </div>

  <div class="card" id="card-model">
    <h2>Spend by model</h2>
    <p class="note">Where the money actually went after routing.</p>
    <div id="chart-model"></div>
  </div>

  <div id="tables" hidden>
    <div class="card">
      <h2>Daily</h2>
      <table id="t-days"></table>
    </div>
    <div class="card">
      <h2>By strategy</h2>
      <table id="t-strat"></table>
    </div>
    <div class="card">
      <h2>By model</h2>
      <table id="t-model"></table>
    </div>
  </div>

  <footer id="foot"></footer>
</div>
<div class="tt" id="tt"></div>

<script>
const DATA = __DATA__;
const tt = document.getElementById('tt');

const money = (x) => {
  const a = Math.abs(x);
  const s = a >= 1000 ? '$' + x.toFixed(0) : a >= 1 ? '$' + x.toFixed(2)
          : a >= 0.01 ? '$' + x.toFixed(4) : '$' + x.toFixed(6);
  return s.replace('$-', '-$');
};
const num = (n) => n.toLocaleString();
// Axis ticks share one precision, chosen from the scale's top -- mixed
// precision down a tick column ("$0.8271" over "$0.000000") is unreadable.
const axisFmt = (max) => {
  const d = max >= 10 ? 0 : max >= 1 ? 2 : max >= 0.1 ? 3 : max >= 0.01 ? 4 : 5;
  return (v) => '$' + v.toFixed(d);
};
const el = (tag, attrs) => {
  const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
};
function showTip(evt, html) {
  tt.innerHTML = html;
  tt.style.opacity = 1;
  const r = tt.getBoundingClientRect();
  let x = evt.clientX + 14, y = evt.clientY - r.height - 10;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
  if (y < 8) y = evt.clientY + 16;
  tt.style.left = x + 'px'; tt.style.top = y + 'px';
}
const hideTip = () => { tt.style.opacity = 0; };

/* ---------- stat tiles ---------- */
function tiles() {
  const s = DATA.summary;
  const items = [
    { label: 'Saved', value: money(s.saved),
      foot: s.saved_pct.toFixed(1) + '% below baseline', good: s.saved > 0 },
    { label: 'Actual spend', value: money(s.actual_cost),
      foot: 'baseline would be ' + money(s.baseline_cost) },
    { label: 'Requests', value: num(s.requests),
      foot: money(s.requests ? s.actual_cost / s.requests : 0) + ' each' },
    { label: 'Cache hit rate', value: (s.cache_hit_rate * 100).toFixed(1) + '%',
      foot: 'requests answered without an API call' },
  ];
  document.getElementById('tiles').innerHTML = items.map(i =>
    `<div class="tile"><div class="label">${i.label}</div>
     <div class="value">${i.value}</div>
     <div class="foot ${i.good ? 'good' : ''}">${i.foot}</div></div>`).join('');
}

/* ---------- line chart ---------- */
function lineChart(mount, days) {
  const W = 900, H = 260, P = { t: 14, r: 86, b: 28, l: 64 };
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
                          'aria-label': 'Baseline and actual spend per day' });
  const n = days.length;
  const maxY = Math.max(...days.map(d => Math.max(d.baseline, d.actual)), 1e-9) * 1.15;
  const x = i => P.l + (n === 1 ? (W - P.l - P.r) / 2 : i * (W - P.l - P.r) / (n - 1));
  const y = v => H - P.b - (v / maxY) * (H - P.t - P.b);

  const fmt = axisFmt(maxY);
  for (let k = 0; k <= 4; k++) {
    const v = maxY * k / 4;
    svg.appendChild(el('line', { x1: P.l, x2: W - P.r, y1: y(v), y2: y(v),
      stroke: 'var(--grid)', 'stroke-width': 1 }));
    const t = el('text', { x: P.l - 10, y: y(v) + 4, class: 'tick', 'text-anchor': 'end' });
    t.textContent = fmt(v); svg.appendChild(t);
  }
  svg.appendChild(el('line', { x1: P.l, x2: W - P.r, y1: H - P.b, y2: H - P.b,
    stroke: 'var(--axis)', 'stroke-width': 1 }));

  const path = (key) => days.map((d, i) => (i ? 'L' : 'M') + x(i) + ' ' + y(d[key])).join(' ');
  svg.appendChild(el('path', { d: path('baseline'), fill: 'none', stroke: 'var(--series-2)',
    'stroke-width': 2, 'stroke-dasharray': '5 4', 'stroke-linecap': 'round' }));
  svg.appendChild(el('path', { d: path('actual'), fill: 'none', stroke: 'var(--series-1)',
    'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));

  days.forEach((d, i) => {
    ['baseline', 'actual'].forEach((k, si) => {
      svg.appendChild(el('circle', { cx: x(i), cy: y(d[k]), r: 4.5,
        fill: si ? 'var(--series-1)' : 'var(--series-2)',
        stroke: 'var(--surface-1)', 'stroke-width': 2 }));
    });
  });

  const step = Math.ceil(n / 8);
  days.forEach((d, i) => {
    if (i % step && i !== n - 1) return;
    const t = el('text', { x: x(i), y: H - P.b + 18, class: 'tick', 'text-anchor': 'middle' });
    t.textContent = d.day.slice(5); svg.appendChild(t);
  });

  // direct label on the last point of each series
  if (n) {
    const last = days[n - 1];
    const spots = [['baseline', 'var(--series-2)'], ['actual', 'var(--series-1)']]
      .map(([k, c]) => ({ k, c, y: y(last[k]) }))
      .sort((a, b) => a.y - b.y);
    // Nudge apart when the two series end at nearly the same value.
    const gap = spots[1].y - spots[0].y;
    if (gap < 14) { spots[0].y -= (14 - gap) / 2; spots[1].y += (14 - gap) / 2; }
    spots.forEach(sp => {
      const t = el('text', { x: x(n - 1) + 10, y: sp.y + 4, class: 'dlabel', fill: sp.c });
      t.textContent = money(last[sp.k]); svg.appendChild(t);
    });
  }

  const cross = el('line', { y1: P.t, y2: H - P.b, stroke: 'var(--axis)',
    'stroke-width': 1, opacity: 0 });
  svg.appendChild(cross);
  const hit = el('rect', { x: P.l, y: P.t, width: W - P.l - P.r, height: H - P.t - P.b,
    fill: 'transparent' });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (e) => {
    const box = svg.getBoundingClientRect();
    const px = (e.clientX - box.left) / box.width * W;
    let i = 0, best = Infinity;
    days.forEach((_, k) => { const d = Math.abs(x(k) - px); if (d < best) { best = d; i = k; } });
    cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i));
    cross.setAttribute('opacity', 1);
    const d = days[i];
    showTip(e, `<b>${d.day}</b>
      <div class="row"><span>Baseline</span><span>${money(d.baseline)}</span></div>
      <div class="row"><span>Actual</span><span>${money(d.actual)}</span></div>
      <div class="row"><span>Saved</span><span>${money(d.baseline - d.actual)}</span></div>
      <div class="row"><span>Requests</span><span>${num(d.requests)}</span></div>`);
  });
  hit.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); hideTip(); });
  mount.appendChild(svg);
}

/* ---------- horizontal bars ---------- */
function barChart(mount, items, opts) {
  opts = opts || {};
  const rowH = 34, W = 900, L = opts.labelWidth || 190, R = 92;
  const H = Math.max(items.length * rowH + 10, 60);
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
                          'aria-label': opts.label || 'bar chart' });
  const vals = items.map(i => i.value);
  const maxV = Math.max(...vals.map(Math.abs), 1e-12);
  const barFmt = axisFmt(maxV);   // one precision down the whole label column
  const zero = vals.some(v => v < 0) ? L + (W - L - R) * 0.18 : L;
  const scale = v => (v / maxV) * (W - L - R - (zero - L));

  items.forEach((it, i) => {
    const cy = i * rowH + 10, h = 18;
    const w = scale(it.value);
    const x0 = w >= 0 ? zero : zero + w;
    const width = Math.max(Math.abs(w), 2);
    const rounded = 4;
    const g = el('g', {});
    // rounded only on the data end, square against the baseline
    const d = w >= 0
      ? `M${x0} ${cy} H${x0 + width - rounded} a${rounded} ${rounded} 0 0 1 ${rounded} ${rounded}
         V${cy + h - rounded} a${rounded} ${rounded} 0 0 1 -${rounded} ${rounded} H${x0} Z`
      : `M${x0 + width} ${cy} H${x0 + rounded} a${rounded} ${rounded} 0 0 0 -${rounded} ${rounded}
         V${cy + h - rounded} a${rounded} ${rounded} 0 0 0 ${rounded} ${rounded} H${x0 + width} Z`;
    g.appendChild(el('path', { d, fill: it.color || 'var(--seq-450)' }));

    const lab = el('text', { x: L - 14, y: cy + 13, class: 'cat', 'text-anchor': 'end' });
    lab.textContent = it.label; g.appendChild(lab);

    const val = el('text', { y: cy + 13, class: 'dlabel',
      x: w >= 0 ? x0 + width + 8 : x0 - 8,
      'text-anchor': w >= 0 ? 'start' : 'end' });
    val.textContent = it.display || barFmt(it.value); g.appendChild(val);

    const hit = el('rect', { x: 0, y: cy - 6, width: W, height: rowH - 2, fill: 'transparent' });
    hit.addEventListener('mousemove', (e) => showTip(e, it.tip ||
      `<b>${it.label}</b><div class="row"><span>Value</span><span>${money(it.value)}</span></div>`));
    hit.addEventListener('mouseleave', hideTip);
    g.appendChild(hit);
    svg.appendChild(g);
  });

  if (zero !== L) svg.appendChild(el('line', { x1: zero, x2: zero, y1: 4, y2: H - 4,
    stroke: 'var(--axis)', 'stroke-width': 1 }));
  mount.appendChild(svg);
}

/* ---------- tables ---------- */
function table(id, head, rows) {
  document.getElementById(id).innerHTML =
    '<thead><tr>' + head.map(h => `<th>${h}</th>`).join('') + '</tr></thead><tbody>' +
    rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('') +
    '</tbody>';
}

/* ---------- boot ---------- */
(function () {
  const s = DATA.summary;
  document.getElementById('window').textContent =
    DATA.window_label + ' · ' + num(s.requests) + ' requests · baseline model ' + DATA.baseline_model;
  document.getElementById('foot').textContent =
    'Generated by tokenwise ' + DATA.generated_at + '. Baseline = the same traffic priced on '
    + DATA.baseline_model + ' with no caching, compaction, or routing.';

  tiles();
  lineChart(document.getElementById('chart-time'), DATA.days);

  // One series, one hue: the category is named on the axis, so color would be
  // decoration -- and decorative color-by-rank repaints when the set changes.
  const strat = Object.entries(s.by_strategy)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => ({ label: k.replace(/_/g, ' '), value: v, color: 'var(--seq-450)' }));
  if (strat.length) barChart(document.getElementById('chart-strategy'), strat,
    { label: 'Savings by strategy' });
  else document.getElementById('card-strategy').hidden = true;

  const models = Object.entries(s.by_model)
    .sort((a, b) => b[1].cost - a[1].cost)
    .map(([k, v]) => ({
      label: k, value: v.cost, color: 'var(--seq-450)',
      tip: `<b>${k}</b>
        <div class="row"><span>Cost</span><span>${money(v.cost)}</span></div>
        <div class="row"><span>Requests</span><span>${num(v.requests)}</span></div>
        <div class="row"><span>Input tokens</span><span>${num(v.input)}</span></div>
        <div class="row"><span>Output tokens</span><span>${num(v.output)}</span></div>`
    }));
  barChart(document.getElementById('chart-model'), models, { label: 'Spend by model', labelWidth: 210 });

  table('t-days', ['Day', 'Requests', 'Baseline', 'Actual', 'Saved'],
    DATA.days.map(d => [d.day, num(d.requests), money(d.baseline), money(d.actual),
      money(d.baseline - d.actual)]));
  table('t-strat', ['Strategy', 'Saved'],
    strat.map(r => [r.label, money(r.value)]));
  table('t-model', ['Model', 'Requests', 'Cost', 'Input tokens', 'Output tokens'],
    Object.entries(s.by_model).map(([k, v]) =>
      [k, num(v.requests), money(v.cost), num(v.input), num(v.output)]));

  const vt = document.getElementById('viewToggle');
  vt.addEventListener('click', () => {
    const on = vt.getAttribute('aria-pressed') === 'true';
    vt.setAttribute('aria-pressed', String(!on));
    vt.textContent = on ? 'Table view' : 'Chart view';
    ['card-time', 'card-strategy', 'card-model'].forEach(id =>
      document.getElementById(id).hidden = !on);
    document.getElementById('tables').hidden = on;
  });
  document.getElementById('themeToggle').addEventListener('click', () => {
    const root = document.documentElement;
    const cur = root.getAttribute('data-theme');
    const dark = cur === 'dark' || (!cur && matchMedia('(prefers-color-scheme: dark)').matches);
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });
})();
</script>
</body>
</html>
"""


def build(summary: dict[str, Any], baseline_model: str = "claude-opus-5", title: str = "tokenwise — savings") -> str:
    days = [
        {"day": day, "actual": v["actual"], "baseline": v["baseline"], "requests": v["requests"]}
        for day, v in summary.get("by_day", {}).items()
    ]
    if not days:
        days = [{"day": time.strftime("%Y-%m-%d"), "actual": 0.0, "baseline": 0.0, "requests": 0}]

    w = summary.get("window") or {"start": time.time(), "end": time.time()}
    label = (
        time.strftime("%b %d, %Y %H:%M", time.localtime(w["start"]))
        + " → "
        + time.strftime("%b %d, %Y %H:%M", time.localtime(w["end"]))
    )
    payload = {
        "summary": summary,
        "days": days,
        "window_label": label,
        "baseline_model": baseline_model,
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    return (
        _TEMPLATE.replace("__DATA__", json.dumps(payload, default=str))
        .replace("__TITLE__", title)
        .replace("__THEME__", "")
    )


def write(summary: dict[str, Any], path: str, baseline_model: str = "claude-opus-5",
          title: str = "tokenwise — savings") -> str:
    html = build(summary, baseline_model, title)
    with open(path, "w") as fh:
        fh.write(html)
    return path
