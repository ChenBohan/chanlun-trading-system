#!/usr/bin/env python3
"""
Merge six index 缠论分析图.html files into one rotation dashboard HTML.

Reads dailyData / min30Data from each source (brace-balanced), orders tabs by
rotation_scores.json final_rank, writes indices/reports/指数轮动分析图.html.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)
INDICES_DIR = os.path.join(PROJECT_ROOT, "indices")
REPORT_DIR = os.path.join(INDICES_DIR, "reports")
ROTATION_SCORES_PATH = os.path.join(REPORT_DIR, "rotation_scores.json")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "index_watchlist.json")
OUTPUT_PATH = os.path.join(REPORT_DIR, "指数轮动分析图.html")

# (code, folder_name_suffix, etf) — path = indices/{code}_{name}/analysis/缠论分析图.html
INDEX_SPECS: List[Tuple[str, str, str]] = [
    ("000300", "沪深300", "510300"),
    ("000016", "上证50", "510050"),
    ("000905", "中证500", "510500"),
    ("000852", "中证1000", "512100"),
    ("399006", "创业板指", "159915"),
    ("000688", "科创50", "588000"),
]


def _extract_balanced_object(source: str, anchor: str) -> str:
    """Extract `{ ... }` after anchor (e.g. 'const dailyData = '), handling strings and nesting."""
    pos = source.find(anchor)
    if pos < 0:
        raise ValueError(f"Anchor not found: {anchor!r}")
    i = pos + len(anchor)
    while i < len(source) and source[i] in " \t\n\r":
        i += 1
    if i >= len(source) or source[i] != "{":
        raise ValueError(f"Expected '{{' after {anchor!r}")
    start = i
    depth = 0
    n = len(source)
    while i < n:
        c = source[i]
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n:
                if source[i] == "\\":
                    i = min(i + 2, n)
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                i += 1
                while i < n and source[i] in " \t\n\r":
                    i += 1
                if i < n and source[i] == ";":
                    i += 1
                return source[start:i].rstrip().rstrip(";").strip()
        i += 1
    raise ValueError(f"Unclosed object for anchor {anchor!r}")


def _load_rotation_order() -> Tuple[List[str], str]:
    """
    Returns (ordered_keys like '399006_创业板指', subtitle_time).
    Keys match ALL_DATA keys. Fallback: INDEX_SPECS order.
    """
    default_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not os.path.isfile(ROTATION_SCORES_PATH):
        keys = [f"{c}_{n}" for c, n, _ in INDEX_SPECS]
        return keys, default_time
    with open(ROTATION_SCORES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    ts = data.get("timestamp", "")
    subtitle = default_time
    if ts:
        try:
            subtitle = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            subtitle = ts[:16].replace("T", " ")
    scores = data.get("scores") or []
    code_to_key = {c: f"{c}_{n}" for c, n, _ in INDEX_SPECS}
    ranked: List[Tuple[int, str]] = []
    for s in scores:
        code = s.get("code", "")
        fr = s.get("final_rank", s.get("rank", 99))
        key = code_to_key.get(code)
        if key:
            ranked.append((int(fr) if fr is not None else 99, key))
    ranked.sort(key=lambda x: x[0])
    seen = {k for _, k in ranked}
    keys = [k for _, k in ranked]
    for c, n, _ in INDEX_SPECS:
        k = f"{c}_{n}"
        if k not in seen:
            keys.append(k)
    return keys, subtitle


def _parse_meta_from_html(html: str) -> Tuple[Optional[int], Optional[int]]:
    """Parse '日线 297 根 | 30分钟 248 根' from .meta if present."""
    m = re.search(r"日线\s*(\d+)\s*根", html)
    d = int(m.group(1)) if m else None
    m2 = re.search(r"30分钟\s*(\d+)\s*根", html)
    m30 = int(m2.group(1)) if m2 else None
    return d, m30


def _read_index_data(code: str, name: str) -> Tuple[str, str, Optional[int], Optional[int]]:
    path = os.path.join(INDICES_DIR, f"{code}_{name}", "analysis", "缠论分析图.html")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    daily = _extract_balanced_object(html, "const dailyData = ")
    min30 = _extract_balanced_object(html, "const min30Data = ")
    bd, bm = _parse_meta_from_html(html)
    return daily, min30, bd, bm


def _build_all_data_js() -> Tuple[str, int, int]:
    """Returns (all_data_assignment, max_daily_bars, max_min30_bars)."""
    parts: List[str] = []
    max_d = 0
    max_m = 0
    for code, name, etf in INDEX_SPECS:
        key = f"{code}_{name}"
        daily, min30, bd, bm = _read_index_data(code, name)
        if bd:
            max_d = max(max_d, bd)
        if bm:
            max_m = max(max_m, bm)
        parts.append(
            f'  "{key}": {{ name: "{name}", code: "{code}", etf: "{etf}", '
            f"daily: {daily},\n  min30: {min30} }}"
        )
    body = ",\n".join(parts)
    return f"const ALL_DATA = {{\n{body}\n}};", max_d, max_m


RENDER_JS = r"""
let currentIndexKey = __FIRST_KEY_JSON__;
let currentLevel = 'daily';
const ANALYSIS_TIME = __ANALYSIS_TIME_JSON__;

function getData() {
  const entry = ALL_DATA[currentIndexKey];
  if (!entry) return { klines: [], strokes: [], hubs: [], bsp: [], trend: '' };
  return currentLevel === 'daily' ? entry.daily : entry.min30;
}

function switchIndex(key) {
  currentIndexKey = key;
  document.querySelectorAll('.tab.index-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.indexKey === key);
  });
  render();
}

function switchLevel(level) {
  currentLevel = level;
  document.querySelectorAll('.tab.level-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.level === level);
  });
  render();
}

function render() {
  const data = getData();
  const entry = ALL_DATA[currentIndexKey] || {};
  const levelLabel = currentLevel === 'daily' ? '日线' : '30分钟';
  document.getElementById('dashSubtitle').textContent =
    '分析时间：' + ANALYSIS_TIME + ' | ' + (entry.name || '') + '（' + (entry.code || '') + '）/ ETF ' + (entry.etf || '') +
    ' | ' + levelLabel;
  const bt = document.getElementById('bspTitle');
  if (bt) bt.textContent = '买卖点列表 — ' + (entry.name || '') + ' · ' + levelLabel;
  renderInfo(data);
  renderKline(data);
  renderMACD(data);
  renderBSP(data);
}

function renderInfo(data) {
  const k = data.klines;
  const panel = document.getElementById('infoPanel');
  if (!k.length) {
    panel.innerHTML = '<div class="info-card"><div class="label">数据</div><div class="value">无K线</div></div>';
    return;
  }
  const last = k[k.length-1];
  const prev = k.length > 1 ? k[k.length-2] : last;
  const change = last[2] - prev[2];
  const changePct = (change / prev[2] * 100);
  const cls = change >= 0 ? 'up' : 'down';
  const sign = change >= 0 ? '+' : '';
  panel.innerHTML = `
    <div class="info-card"><div class="label">最新价</div><div class="value ${cls}">${last[2].toFixed(2)}</div></div>
    <div class="info-card"><div class="label">涨跌</div><div class="value ${cls}">${sign}${change.toFixed(2)} (${sign}${changePct.toFixed(2)}%)</div></div>
    <div class="info-card"><div class="label">走势类型</div><div class="value">${data.trend}</div></div>
    <div class="info-card"><div class="label">中枢数</div><div class="value">${data.hubs.length}</div></div>
    <div class="info-card"><div class="label">笔数</div><div class="value">${data.strokes.length}</div></div>
    <div class="info-card"><div class="label">买卖点</div><div class="value">${data.bsp.length}</div></div>
  `;
}

function renderKline(data) {
  const canvas = document.getElementById('klineCanvas');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = 400 * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = 400;
  ctx.clearRect(0, 0, W, H);

  const k = data.klines;
  if (!k.length) return;
  const pad = {t:20, b:30, l:60, r:20};
  const cw = (W - pad.l - pad.r) / k.length;
  const allH = k.map(x => x[3]);
  const allL = k.map(x => x[4]);
  const maxP = Math.max(...allH) * 1.01;
  const minP = Math.min(...allL) * 0.99;
  const scaleY = p => pad.t + (maxP - p) / (maxP - minP) * (H - pad.t - pad.b);
  const scaleX = i => pad.l + i * cw + cw / 2;

  ctx.strokeStyle = '#21262d';
  ctx.lineWidth = 0.5;
  for (let i = 0; i < 5; i++) {
    const y = pad.t + i * (H - pad.t - pad.b) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W-pad.r, y); ctx.stroke();
    const p = maxP - i * (maxP - minP) / 4;
    ctx.fillStyle = '#8b949e'; ctx.font = '11px monospace'; ctx.textAlign = 'right';
    ctx.fillText(p.toFixed(2), pad.l - 6, y + 4);
  }

  const dateIdx = {};
  k.forEach((kk, i) => { dateIdx[kk[0]] = i; });
  data.hubs.forEach(h => {
    const si = dateIdx[h.start] ?? 0;
    const ei = dateIdx[h.end] ?? k.length - 1;
    const x1 = scaleX(si) - cw/2;
    const x2 = scaleX(ei) + cw/2;
    ctx.fillStyle = 'rgba(88,166,255,0.08)';
    ctx.fillRect(x1, scaleY(h.zg), x2-x1, scaleY(h.zd)-scaleY(h.zg));
    ctx.strokeStyle = 'rgba(88,166,255,0.4)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(x1, scaleY(h.zg)); ctx.lineTo(x2, scaleY(h.zg)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x1, scaleY(h.zd)); ctx.lineTo(x2, scaleY(h.zd)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#58a6ff'; ctx.font = '10px sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(`ZG=${h.zg.toFixed(1)}`, x2+3, scaleY(h.zg)+3);
    ctx.fillText(`ZD=${h.zd.toFixed(1)}`, x2+3, scaleY(h.zd)+12);
  });

  k.forEach((kk, i) => {
    const [dt, o, c, hi, lo] = kk;
    const x = scaleX(i);
    const bw = Math.max(cw * 0.6, 1);
    const isUp = c >= o;
    ctx.strokeStyle = ctx.fillStyle = isUp ? '#f85149' : '#3fb950';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, scaleY(hi)); ctx.lineTo(x, scaleY(lo)); ctx.stroke();
    const top = scaleY(Math.max(o, c));
    const bot = scaleY(Math.min(o, c));
    const bh = Math.max(bot - top, 1);
    ctx.fillRect(x-bw/2, top, bw, bh);
  });

  ctx.strokeStyle = '#f0883e';
  ctx.lineWidth = 1.5;
  data.strokes.forEach(s => {
    const si = dateIdx[s.start] ?? 0;
    const ei = dateIdx[s.end] ?? 0;
    ctx.beginPath();
    ctx.moveTo(scaleX(si), scaleY(s.startP));
    ctx.lineTo(scaleX(ei), scaleY(s.endP));
    ctx.stroke();
  });

  data.bsp.forEach(p => {
    const pi = dateIdx[p.date];
    if (pi === undefined) return;
    const x = scaleX(pi);
    const y = scaleY(p.price);
    const isBuy = p.type.includes('B');
    ctx.beginPath();
    if (isBuy) {
      ctx.moveTo(x, y+12); ctx.lineTo(x-7, y+22); ctx.lineTo(x+7, y+22); ctx.closePath();
      ctx.fillStyle = '#ffd33d'; ctx.fill();
    } else {
      ctx.moveTo(x, y-12); ctx.lineTo(x-7, y-22); ctx.lineTo(x+7, y-22); ctx.closePath();
      ctx.fillStyle = '#da3633'; ctx.fill();
    }
    ctx.fillStyle = isBuy ? '#ffd33d' : '#da3633';
    ctx.font = 'bold 10px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(p.label.substring(0,2), x, isBuy ? y+34 : y-24);
  });

  ctx.fillStyle = '#8b949e'; ctx.font = '10px monospace'; ctx.textAlign = 'center';
  const step = Math.max(Math.floor(k.length / 8), 1);
  for (let i = 0; i < k.length; i += step) {
    const label = k[i][0].length > 10 ? k[i][0].substring(5) : k[i][0].substring(5);
    ctx.fillText(label, scaleX(i), H - 8);
  }
}

function renderMACD(data) {
  const canvas = document.getElementById('macdCanvas');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = 150 * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = 150;
  ctx.clearRect(0, 0, W, H);

  const k = data.klines;
  if (!k.length) return;
  const pad = {t:10, b:20, l:60, r:20};
  const cw = (W - pad.l - pad.r) / k.length;

  const macds = k.map(x => x[6]);
  const difs = k.map(x => x[7]);
  const deas = k.map(x => x[8]);
  const allVals = [...macds, ...difs, ...deas];
  const maxV = Math.max(...allVals.map(Math.abs)) * 1.1 || 1;

  const scaleY = v => pad.t + (maxV - v) / (2 * maxV) * (H - pad.t - pad.b);
  const scaleX = i => pad.l + i * cw + cw / 2;
  const zeroY = scaleY(0);

  ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(W-pad.r, zeroY); ctx.stroke();
  ctx.fillStyle = '#8b949e'; ctx.font = '10px monospace'; ctx.textAlign = 'right';
  ctx.fillText('0', pad.l - 6, zeroY + 3);

  k.forEach((kk, i) => {
    const m = kk[6];
    const x = scaleX(i);
    const bw = Math.max(cw * 0.5, 1);
    ctx.fillStyle = m >= 0 ? '#f85149' : '#3fb950';
    const y1 = zeroY;
    const y2 = scaleY(m);
    ctx.fillRect(x - bw/2, Math.min(y1,y2), bw, Math.abs(y2-y1) || 1);
  });

  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1.2;
  ctx.beginPath();
  k.forEach((kk, i) => {
    const x = scaleX(i), y = scaleY(kk[7]);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  ctx.strokeStyle = '#f0883e'; ctx.lineWidth = 1.2;
  ctx.beginPath();
  k.forEach((kk, i) => {
    const x = scaleX(i), y = scaleY(kk[8]);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  ctx.fillStyle = '#58a6ff'; ctx.font = '10px sans-serif'; ctx.textAlign = 'left';
  ctx.fillText('DIF', W-pad.r-80, pad.t+12);
  ctx.fillStyle = '#f0883e';
  ctx.fillText('DEA', W-pad.r-40, pad.t+12);
}

function renderBSP(data) {
  const list = document.getElementById('bspList');
  if (!data.bsp.length) {
    list.innerHTML = '<div style="color:#8b949e;padding:10px">当前级别未识别出标准买卖点</div>';
    return;
  }
  list.innerHTML = data.bsp.map(p => {
    const isBuy = p.type.includes('B');
    const cls = isBuy ? 'buy' : 'sell';
    const stars = p.conf === 'high' ? '⭐⭐⭐' : p.conf === 'medium' ? '⭐⭐' : '⭐';
    return `<div class="bsp-item">
      <span class="bsp-badge ${cls}">${p.label.substring(0,2)}</span>
      <span class="bsp-date">${p.date}</span>
      <span class="bsp-price" style="color:${isBuy?'#3fb950':'#f85149'}">${p.price.toFixed(2)}</span>
      <span class="bsp-desc">${p.desc}</span>
      <span class="bsp-conf">${stars}</span>
    </div>`;
  }).join('');
}

window.addEventListener('load', render);
window.addEventListener('resize', render);
"""


def _build_index_tabs_html(order: List[str]) -> str:
    lines = []
    name_by_key = {f"{c}_{n}": n for c, n, _ in INDEX_SPECS}
    for i, key in enumerate(order):
        name = name_by_key.get(key, key)
        active = " active" if i == 0 else ""
        lines.append(
            f'  <div class="tab index-tab{active}" data-index-key="{key}" '
            f'onclick="switchIndex(\'{key}\')">{name}</div>'
        )
    return "\n".join(lines)


def generate() -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    order, subtitle_time = _load_rotation_order()
    all_data_js, max_d, max_m = _build_all_data_js()
    default_key = f"{INDEX_SPECS[0][0]}_{INDEX_SPECS[0][1]}"
    first_key = order[0] if order else default_key
    if first_key not in {f"{c}_{n}" for c, n, _ in INDEX_SPECS}:
        first_key = default_key
    if first_key not in order:
        order = [first_key] + [k for k in order if k != first_key]

    meta_suffix = ""
    if max_d or max_m:
        parts = []
        if max_d:
            parts.append(f"日线最多 {max_d} 根")
        if max_m:
            parts.append(f"30分钟最多 {max_m} 根")
        meta_suffix = " | " + " | ".join(parts)

    render_js = RENDER_JS.replace("__FIRST_KEY_JSON__", json.dumps(first_key))
    render_js = render_js.replace("__ANALYSIS_TIME_JSON__", json.dumps(subtitle_time))

    tabs_index = _build_index_tabs_html(order)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股宽基指数轮动 — 缠论多级别综合分析</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }}
.header {{ padding: 20px 30px; border-bottom: 1px solid #21262d; }}
.header h1 {{ font-size: 22px; color: #58a6ff; }}
.header .meta {{ color: #8b949e; font-size: 13px; margin-top: 6px; }}
.tabs-row {{ border-bottom: 1px solid #21262d; padding: 0 30px; }}
.tabs-row .row-label {{ font-size: 11px; color: #6e7681; padding: 8px 0 4px; }}
.tabs {{ display: flex; gap: 0; flex-wrap: wrap; }}
.tab {{ padding: 10px 16px; cursor: pointer; color: #8b949e; border-bottom: 2px solid transparent; font-size: 14px; }}
.tab.active {{ color: #58a6ff; border-bottom-color: #58a6ff; }}
.tab:hover {{ color: #c9d1d9; }}
.tab.index-tab {{ margin-right: 4px; }}
.chart-container {{ padding: 20px 30px; }}
canvas {{ display: block; width: 100%; background: #0d1117; border-radius: 6px; }}
.legend {{ display: flex; gap: 20px; padding: 10px 30px; flex-wrap: wrap; }}
.legend-item {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: #8b949e; }}
.legend-color {{ width: 14px; height: 14px; border-radius: 2px; }}
.info-panel {{ padding: 15px 30px; display: flex; gap: 30px; flex-wrap: wrap; }}
.info-card {{ background: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 12px 16px; min-width: 200px; }}
.info-card .label {{ font-size: 11px; color: #8b949e; text-transform: uppercase; }}
.info-card .value {{ font-size: 18px; font-weight: 600; margin-top: 4px; }}
.info-card .value.up {{ color: #f85149; }}
.info-card .value.down {{ color: #3fb950; }}
.bsp-list {{ padding: 10px 30px 30px; }}
.bsp-item {{ display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-radius: 6px; margin-bottom: 4px; }}
.bsp-item:hover {{ background: #161b22; }}
.bsp-badge {{ padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }}
.bsp-badge.buy {{ background: rgba(63,185,80,0.2); color: #3fb950; }}
.bsp-badge.sell {{ background: rgba(248,81,73,0.2); color: #f85149; }}
.bsp-date {{ color: #8b949e; font-size: 13px; width: 120px; }}
.bsp-price {{ font-weight: 600; width: 80px; }}
.bsp-desc {{ color: #8b949e; font-size: 12px; flex: 1; }}
.bsp-conf {{ font-size: 11px; }}
</style>
</head>
<body>
<div class="header">
  <h1>A股宽基指数轮动 — 缠论多级别综合分析</h1>
  <div class="meta" id="dashSubtitle">分析时间：{subtitle_time}{meta_suffix}</div>
</div>

<div class="tabs-row">
  <div class="row-label">指数（按轮动综合排名）</div>
  <div class="tabs" id="indexTabs">
{tabs_index}
  </div>
  <div class="row-label">级别</div>
  <div class="tabs">
    <div class="tab level-tab active" data-level="daily" onclick="switchLevel('daily')">日线</div>
    <div class="tab level-tab" data-level="min30" onclick="switchLevel('min30')">30分钟</div>
  </div>
</div>

<div class="info-panel" id="infoPanel"></div>

<div class="chart-container">
  <canvas id="klineCanvas" height="400"></canvas>
</div>
<div class="chart-container">
  <canvas id="macdCanvas" height="150"></canvas>
</div>

<div class="legend">
  <div class="legend-item"><div class="legend-color" style="background:#f85149"></div>阳线</div>
  <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div>阴线</div>
  <div class="legend-item"><div class="legend-color" style="background:rgba(88,166,255,0.2);border:1px solid #58a6ff"></div>中枢区间</div>
  <div class="legend-item"><div class="legend-color" style="background:#f0883e"></div>笔连线</div>
  <div class="legend-item"><div class="legend-color" style="background:#ffd33d;border-radius:50%"></div>买点 ▲</div>
  <div class="legend-item"><div class="legend-color" style="background:#da3633;border-radius:50%"></div>卖点 ▼</div>
  <div class="legend-item"><span style="color:#58a6ff">━</span> MACD DIF</div>
  <div class="legend-item"><span style="color:#f0883e">━</span> MACD DEA</div>
  <div class="legend-item"><span style="color:#f85149">▮</span> MACD 柱零上</div>
  <div class="legend-item"><span style="color:#3fb950">▮</span> MACD 柱零下</div>
</div>

<h3 style="padding:10px 30px;color:#58a6ff;font-size:16px" id="bspTitle">买卖点列表</h3>
<div class="bsp-list" id="bspList"></div>

<script>
{all_data_js}

{render_js}
</script>
</body>
</html>
"""
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    return OUTPUT_PATH


def main() -> None:
    path = generate()
    print(path)


if __name__ == "__main__":
    main()
