"""
Chanlun Trading System v2 - Visualization Module.

Generates a self-contained HTML dashboard with ECharts showing:
  - Candlestick K-line charts
  - Strokes (笔) as connected lines
  - Hubs (中枢) as shaded rectangles
  - Buy/Sell points as markers
  - MACD histogram subplot
  - Segments (线段) as thick lines

Supports 8 indices × 3 timeframes with tab switching.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

from .data_fetcher import load_index_watchlist, IndexConfig, _PROJECT_ROOT
from .chanlun_engine import (
    load_bars_from_csv, analyze, AnalysisResult, MultiLevelSynthesis,
    RawBar, Stroke, Hub, BuySellPoint, Segment,
    synthesize_multi_level,
)


# ════════════════════════════════════════════════════════════════════
# Data Serialization for ECharts
# ════════════════════════════════════════════════════════════════════

def _result_to_echarts_data(result: AnalysisResult, max_bars: int = 0) -> dict:
    """Convert AnalysisResult to JSON-serializable dict for ECharts.

    Args:
        max_bars: if > 0, only keep the most recent N bars for visualization.
                  Strokes/hubs/BSP outside the visible window are excluded.
    """
    bars = result.raw_bars
    if max_bars > 0 and len(bars) > max_bars:
        bars = bars[-max_bars:]

    kline_data = []
    dates = []
    volumes = []
    macd_hist = []
    dif_line = []
    dea_line = []

    for b in bars:
        dates.append(b.dt)
        kline_data.append([round(b.open, 3), round(b.close, 3),
                           round(b.low, 3), round(b.high, 3)])
        volumes.append(b.volume)
        macd_hist.append(round(b.macd_hist, 4))
        dif_line.append(round(b.dif, 4))
        dea_line.append(round(b.dea, 4))

    dt_index = {b.dt: i for i, b in enumerate(bars)}
    stroke_lines = []
    for s in result.strokes:
        si = dt_index.get(s.start.dt)
        ei = dt_index.get(s.end.dt)
        if si is not None and ei is not None:
            start_price = s.start.low if s.direction == 1 else s.start.high
            end_price = s.end.high if s.direction == 1 else s.end.low
            stroke_lines.append({
                "coords": [[si, start_price], [ei, end_price]],
                "dir": s.direction,
                "idx": s.idx,
            })

    # Segments as connected polyline turning points (首尾相接) with labels
    segment_points = []
    segment_labels = []
    for seg in result.segments:
        start_s = seg.strokes[0]
        end_s = seg.strokes[-1]
        si = dt_index.get(start_s.start.dt)
        ei = dt_index.get(end_s.end.dt)
        if si is None or ei is None:
            continue
        if start_s.direction == 1:
            start_price = start_s.start.low
        else:
            start_price = start_s.start.high
        if end_s.direction == 1:
            end_price = end_s.end.high
        else:
            end_price = end_s.end.low
        if not segment_points:
            segment_points.append([si, start_price])
        segment_points.append([ei, end_price])
        mid_x = (si + ei) // 2
        mid_y = (start_price + end_price) / 2
        segment_labels.append({
            "idx": seg.idx,
            "x": mid_x,
            "y": mid_y,
            "dir": seg.direction,
        })

    # Stroke-level hubs as rectangles
    hub_rects = []
    for h in result.hubs:
        si = dt_index.get(h.start_dt)
        ei = dt_index.get(h.end_dt)
        if si is not None and ei is not None:
            hub_rects.append({
                "x0": si, "x1": ei,
                "zg": h.zg, "zd": h.zd,
                "gg": h.gg, "dd": h.dd,
                "idx": h.idx,
                "evo": h.evolution_type,
            })

    # Buy/sell points as markers
    bsp_markers = []
    for p in result.buy_sell_points:
        pi = dt_index.get(p.dt)
        if pi is not None:
            is_buy = p.type in ("1B", "2B", "3B", "PB")
            ranges = []
            for r in p.area_ranges:
                si = dt_index.get(r["start_dt"])
                ei = dt_index.get(r["end_dt"])
                if si is not None and ei is not None:
                    ranges.append({
                        "label": r["label"],
                        "x0": si, "x1": ei,
                        "area": r["area"],
                    })
            entry = {
                "idx": pi,
                "bsp_idx": p.idx,
                "price": p.price,
                "type": p.type,
                "label": p.label,
                "desc": p.description,
                "conf": p.confidence,
                "is_buy": is_buy,
                "stroke_idx": p.stroke_idx,
                "seg_idx": p.seg_idx,
                "wolf": p.wolf_warning,
                "zone": p.macd_zone,
                "strength": p.strength,
                "pos_advice": p.position_advice,
                "status": p.status,
                "inv_reason": p.invalidation_reason,
            }
            if ranges:
                entry["ranges"] = ranges
            struct_list = []
            for st in p.structure:
                si = dt_index.get(st.get("start_dt"))
                ei = dt_index.get(st.get("end_dt"))
                if si is not None and ei is not None:
                    item = {"tag": st["tag"], "x0": si, "x1": ei}
                    if "zg" in st:
                        item["zg"] = st["zg"]
                        item["zd"] = st["zd"]
                    struct_list.append(item)
            if struct_list:
                entry["structure"] = struct_list
            bsp_markers.append(entry)

    return {
        "dates": dates,
        "kline": kline_data,
        "volumes": volumes,
        "macd_hist": macd_hist,
        "dif": dif_line,
        "dea": dea_line,
        "strokes": stroke_lines,
        "segments": segment_points,
        "seg_labels": segment_labels,
        "hubs": hub_rects,
        "bsp": bsp_markers,
        "trend": result.trend,
        "hub_position": result.position_vs_hub,
        "hub_detail": result.hub_position_detail,
        "trend_completion": result.trend_completion,
        "stats": {
            "bars": len(bars),
            "merged": len(result.merged_bars),
            "fractals": len(result.fractals),
            "strokes": len(result.strokes),
            "segments": len(result.segments),
            "hubs": len(result.hubs),
            "bsp": len(result.buy_sell_points),
        },
    }


# ════════════════════════════════════════════════════════════════════
# HTML Template
# ════════════════════════════════════════════════════════════════════

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>缠论交易系统 v2 — 可视化仪表盘</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0d1117; color: #c9d1d9; font-size: 16px; line-height: 1.6; }

.page-wrap { max-width: 1400px; margin: 0 auto; }

.header { padding: 20px 32px; background: #161b22; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 24px; }
.header h1 { font-size: 26px; color: #58a6ff; letter-spacing: -0.5px; }
.header > span { font-size: 16px; }
.header .gen-time { font-size: 15px; color: #8b949e; margin-left: auto; }

.nav { display: flex; background: #161b22; border-bottom: 1px solid #30363d;
       padding: 0 32px; overflow-x: auto; align-items: stretch; }
.nav-sep { display: flex; align-items: center; padding: 0 6px; color: #484f58;
           font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
           border-left: 2px solid #30363d; margin-left: 4px; padding-left: 12px; white-space: nowrap; }
.nav-btn { padding: 14px 18px; cursor: pointer; border: none; background: none;
           color: #8b949e; font-size: 15px; white-space: nowrap; border-bottom: 3px solid transparent;
           transition: color 0.15s, border-color 0.15s; }
.nav-btn:hover { color: #c9d1d9; }
.nav-btn.active { color: #58a6ff; border-bottom-color: #58a6ff; font-weight: 600; }

.level-tabs { display: flex; padding: 12px 32px; gap: 12px; background: #0d1117; }
.level-btn { padding: 10px 22px; border-radius: 8px; cursor: pointer; border: 1px solid #30363d;
             background: #161b22; color: #8b949e; font-size: 15px;
             transition: all 0.15s; }
.level-btn:hover { border-color: #58a6ff; color: #c9d1d9; }
.level-btn.active { background: #1f6feb; border-color: #1f6feb; color: #fff; font-weight: 600; }

.info-bar { display: flex; padding: 12px 32px; gap: 18px; font-size: 15px; color: #8b949e;
            flex-wrap: wrap; align-items: center; }
.info-bar .tag { padding: 5px 12px; border-radius: 6px; font-size: 15px; font-weight: 500; }
.tag-up { background: #3a1a1a; color: #f85149; }
.tag-down { background: #1a3a2a; color: #3fb950; }
.tag-neutral { background: #2a2a1a; color: #d29922; }

#conclusion-bar { padding: 16px 32px; background: #161b22; border-bottom: 1px solid #30363d;
                  display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
#conclusion-bar .concl-group { display: flex; gap: 8px; align-items: center;
                               padding-right: 16px; border-right: 1px solid #30363d; }
#conclusion-bar .concl-group:last-child { border-right: none; }
#conclusion-bar .concl-label { font-size: 13px; color: #8b949e; }
#conclusion-bar .concl-value { font-size: 15px; font-weight: 600; }
#conclusion-bar .concl-advice { font-size: 15px; color: #d2a8ff; background: #1a1a2e;
                                padding: 4px 12px; border-radius: 6px; }
.struct-bar { display: flex; padding: 6px 32px; gap: 16px; font-size: 13px; color: #484f58;
              flex-wrap: wrap; }

#chart-container { width: 100%; height: calc(100vh - 380px); min-height: 420px; }

.detail-tabs { display: flex; padding: 12px 32px 0; gap: 8px; border-top: 1px solid #30363d; }
.detail-tab { padding: 8px 18px; border-radius: 6px 6px 0 0; cursor: pointer; border: none;
              background: none; color: #8b949e; font-size: 14px;
              border-bottom: 2px solid transparent; transition: all 0.15s; }
.detail-tab:hover { color: #c9d1d9; }
.detail-tab.active { color: #58a6ff; border-bottom-color: #58a6ff; background: #161b22; }
.detail-panel { display: none; padding: 16px 32px; }
.detail-panel.active { display: block; }

#synthesis-panel h3 { font-size: 18px; color: #c9d1d9; margin-bottom: 12px; }
#synthesis-panel h4 { font-size: 16px; margin-top: 12px; }

#signal-panel { max-height: 400px; overflow-y: auto; }
#signal-panel h3 { font-size: 18px; color: #c9d1d9; margin-bottom: 12px; }
.signal-table { width: 100%; border-collapse: collapse; font-size: 15px; }
.signal-table th { text-align: left; padding: 10px 14px; color: #8b949e; border-bottom: 2px solid #21262d;
                   font-weight: 600; font-size: 14px; letter-spacing: 0.5px;
                   position: sticky; top: 0; background: #0d1117; }
.signal-table td { padding: 10px 14px; border-bottom: 1px solid #161b22; color: #c9d1d9; }
.signal-table tr:hover { background: #161b22; }
.sig-buy { color: #f85149; font-weight: 600; }
.sig-sell { color: #3fb950; font-weight: 600; }
.conf-high { color: #3fb950; font-weight: 600; }
.conf-medium { color: #d29922; }
.conf-low { color: #8b949e; }
</style>
</head>
<body>
<div class="page-wrap">

<div class="header">
  <h1>缠论交易系统 v2</h1>
  <span>日线（方向）→ 30分钟（买卖点）→ 5分钟（择时）</span>
  <span class="gen-time">数据：__DATA_TIME__ | 生成：__GEN_TIME__</span>
</div>

<div id="global-signals-table" style="margin:0 32px 16px;overflow-x:auto"></div>

<h2 style="color:#c9d1d9;margin:24px 32px 8px;font-size:17px;border-bottom:1px solid #30363d;padding-bottom:6px">📈 技术分析详情</h2>
<div class="nav" id="index-nav"></div>

<div class="level-tabs" id="level-tabs">
  <button class="level-btn active" data-level="daily">日线</button>
  <button class="level-btn" data-level="30min">30分钟</button>
  <button class="level-btn" data-level="5min">5分钟</button>
</div>

<div id="conclusion-bar"></div>
<div class="struct-bar" id="struct-bar"></div>

<div id="chart-container"></div>

<div class="detail-tabs" id="detail-tabs">
  <button class="detail-tab active" data-panel="synthesis-panel">多级别联立</button>
  <button class="detail-tab" data-panel="signal-panel">买卖点信号</button>
</div>
<div id="synthesis-panel" class="detail-panel active"></div>
<div id="signal-panel" class="detail-panel"></div>

<h2 style="color:#c9d1d9;margin:24px 32px 8px;font-size:17px;border-bottom:1px solid #30363d;padding-bottom:6px">📊 标的可操作性总览</h2>
<div id="overview-table" style="margin:0 32px 16px;overflow-x:auto"></div>

<script>
// ─── Data: lazy-loaded per index via script injection ───
var DATA_CACHE = {};
const DATA_KEYS = __ALL_DATA_JSON__;
const INDEX_LIST = __INDEX_LIST_JSON__;
const SYNTHESIS = __SYNTHESIS_JSON__;
const GLOBAL_SIGNALS = __GLOBAL_SIGNALS_JSON__;

function getChartData(key) { return DATA_CACHE[key] || null; }

function loadChartData(key) {
  return new Promise(resolve => {
    if (DATA_CACHE[key]) { resolve(DATA_CACHE[key]); return; }
    const s = document.createElement('script');
    s.src = 'data/' + key + '.js';
    s.onload = () => resolve(DATA_CACHE[key] || null);
    s.onerror = () => resolve(null);
    document.head.appendChild(s);
  });
}

let currentIndex = INDEX_LIST[0].etf_code;
let currentLevel = 'daily';
let chart = null;

// ─── Global Signals Table (latest type-1/2/3 buy/sell, tabbed by level) ───
let gsActiveTab = '日线';
function renderGlobalSignals() {
  const el = document.getElementById('global-signals-table');
  const levels = ['日线', '30分钟', '5分钟'];
  const hasAny = levels.some(lv => (GLOBAL_SIGNALS[lv]||[]).length > 0);
  if (!hasAny) { el.innerHTML = ''; return; }

  const confIcons = {'high': '🔴高', 'medium': '🟡中', 'low': '⚪低'};
  const typeColors = {'1B': '#f85149', '2B': '#f85149', '3B': '#f85149', '1S': '#3fb950', '2S': '#3fb950', '3S': '#3fb950'};
  const strengthMap = {'strongest': '🔥最强', 'strong': '💪强势', 'standard': '📌标准', 'weak': '⚠弱'};

  let h = '<h3 style="color:#c9d1d9;margin:0 0 8px;font-size:15px">📡 最新买卖点（每级别最新 20 个）</h3>';
  h += '<div style="display:flex;gap:6px;margin-bottom:8px">';
  levels.forEach(lv => {
    const items = GLOBAL_SIGNALS[lv]||[];
    const total = items.length;
    const confirmedCnt = items.filter(s => s.status === 'confirmed').length;
    const pendingCnt = items.filter(s => s.status === 'pending').length;
    const invCnt = items.filter(s => s.status === 'invalidated').length;
    let cntLabel = '' + total;
    if (invCnt > 0 || pendingCnt > 0) {
      const parts = [];
      if (confirmedCnt > 0) parts.push(confirmedCnt + '✓');
      if (pendingCnt > 0) parts.push(pendingCnt + '⏳');
      if (invCnt > 0) parts.push(invCnt + '✗');
      cntLabel = parts.join(' ');
    }
    const active = lv === gsActiveTab;
    const bg = active ? '#21262d' : 'transparent';
    const clr = active ? '#58a6ff' : '#8b949e';
    const border = active ? '2px solid #58a6ff' : '2px solid transparent';
    h += `<button onclick="gsActiveTab='${lv}';renderGlobalSignals()" style="padding:6px 16px;border:none;border-bottom:${border};background:${bg};color:${clr};cursor:pointer;font-size:13px;border-radius:6px 6px 0 0">${lv} (${cntLabel})</button>`;
  });
  h += '</div>';

  const signals = GLOBAL_SIGNALS[gsActiveTab] || [];
  h += '<table style="width:100%;border-collapse:collapse;font-size:13px;color:#c9d1d9;background:#161b22;border-radius:8px;overflow:hidden">';
  h += '<thead><tr style="background:#21262d;color:#8b949e;font-size:12px">';
  h += '<th style="padding:8px;text-align:left">时间</th>';
  h += '<th style="padding:8px;text-align:left">标的</th>';
  h += '<th style="padding:8px;text-align:center">类型</th>';
  h += '<th style="padding:8px;text-align:right">价格</th>';
  h += '<th style="padding:8px;text-align:center">状态</th>';
  h += '<th style="padding:8px;text-align:center">置信度</th>';
  h += '<th style="padding:8px;text-align:left">背驰段对比</th>';
  h += '<th style="padding:8px;text-align:center">强弱</th>';
  h += '<th style="padding:8px;text-align:left">仓位建议</th>';
  h += '<th style="padding:8px;text-align:center">防狼</th>';
  h += '</tr></thead><tbody>';
  signals.forEach((s, i) => {
    const bg = i % 2 === 0 ? '#0d1117' : '#161b22';
    const tClr = typeColors[s.type] || '#c9d1d9';
    const confStr = confIcons[s.conf] || s.conf || '-';
    const wolfStr = s.wolf ? '⚠' : '✓';
    const wolfClr = s.wolf ? '#d29922' : '#3fb950';
    const strStr = strengthMap[s.strength] || s.strength || '-';
    const inv = s.status === 'invalidated';
    const pending = s.status === 'pending';
    const confirmed = s.status === 'confirmed';
    const rowOpacity = inv ? 'opacity:0.45;' : '';
    const strike = inv ? 'text-decoration:line-through;' : '';
    h += '<tr style="background:' + bg + ';border-bottom:1px solid #21262d;' + rowOpacity + '">';
    h += '<td style="padding:6px 8px;white-space:nowrap;font-family:monospace;font-size:12px;' + strike + '">' + (s.dt || '-') + '</td>';
    h += '<td style="padding:6px 8px;font-weight:600;' + strike + '">' + s.etf_name + '</td>';
    h += '<td style="padding:6px 8px;text-align:center;font-weight:bold;color:' + tClr + ';' + strike + '">' + s.label + '</td>';
    h += '<td style="padding:6px 8px;text-align:right;font-family:monospace;' + strike + '">' + (s.price ? s.price.toFixed(3) : '-') + '</td>';
    const isBuyType = ['1B','2B','3B','PB'].includes(s.type);
    const confirmedColor = isBuyType ? '#f85149' : '#3fb950';
    const confirmedIcon = isBuyType ? '🔴' : '🟢';
    const statusHtml = inv
      ? '<span title="' + (s.inv_reason||'').replace(/"/g,'&quot;') + '" style="color:#da3633;cursor:help">❌已失效</span>'
      : pending
        ? '<span style="color:#d29922">⏳待确认</span>'
        : '<span style="color:' + confirmedColor + '">' + confirmedIcon + '已确认</span>';
    h += '<td style="padding:6px 8px;text-align:center">' + statusHtml + '</td>';
    h += '<td style="padding:6px 8px;text-align:center">' + confStr + '</td>';
    h += '<td style="padding:6px 8px;font-size:12px">' + (s.area_cmp || '-') + '</td>';
    h += '<td style="padding:6px 8px;text-align:center;font-size:12px">' + strStr + '</td>';
    h += '<td style="padding:6px 8px;font-size:12px">' + (s.pos_advice || '-') + '</td>';
    h += '<td style="padding:6px 8px;text-align:center;color:' + wolfClr + '">' + wolfStr + '</td>';
    h += '</tr>';
  });
  if (signals.length === 0) {
    h += '<tr><td colspan="10" style="padding:16px;text-align:center;color:#484f58">暂无信号</td></tr>';
  }
  h += '</tbody></table>';
  el.innerHTML = h;
}

// ─── Overview Table ───
function renderOverview() {
  const el = document.getElementById('overview-table');
  const ncols = 5;
  let h = '<table style="width:100%;border-collapse:collapse;font-size:14px;color:#c9d1d9;background:#161b22;border-radius:8px;overflow:hidden">';
  h += '<thead><tr style="background:#21262d;color:#8b949e;font-size:13px">';
  h += '<th style="padding:10px 12px;text-align:left">标的</th>';
  h += '<th style="padding:10px 8px;text-align:center;width:50px">评分</th>';
  h += '<th style="padding:10px 8px;text-align:center">长线（日线）</th>';
  h += '<th style="padding:10px 8px;text-align:center">短线（30分钟）</th>';
  h += '<th style="padding:10px 12px;text-align:left;min-width:220px">操作建议</th>';
  h += '</tr></thead><tbody>';
  let lastType = null;
  INDEX_LIST.forEach((idx, i) => {
    if (idx.type !== lastType) {
      const label = idx.type === 'broad' ? '📊 宽基指数' : (idx.type === 'stock' ? '📈 个股' : '🏭 行业ETF');
      h += `<tr style="background:#1a1e24"><td colspan="${ncols}" style="padding:8px 12px;font-size:13px;font-weight:700;color:#58a6ff;letter-spacing:1px">${label}</td></tr>`;
      lastType = idx.type;
    }
    const bg = i % 2 === 0 ? '#0d1117' : '#161b22';
    const trendColor = (idx.trend||'').includes('上涨') ? '#f85149' : ((idx.trend||'').includes('下跌') ? '#3fb950' : '#8b949e');
    const trendIcon = (idx.trend||'').includes('上涨') ? '↑' : ((idx.trend||'').includes('下跌') ? '↓' : '—');
    const isUp = (idx.trend||'').includes('上涨');
    const tcActiveColor = isUp ? '#f85149' : '#3fb950';
    const tcDoneColor = isUp ? '#3fb950' : '#f85149';
    const tcText = (idx.status||'').includes('疑似') ? '<br><span style="font-size:11px;color:#d29922">⚠️ 疑似完成</span>' : ((idx.status||'').includes('已确认') ? `<br><span style="font-size:11px;color:${tcDoneColor}">✅ 已完成</span>` : `<br><span style="font-size:11px;color:${tcActiveColor}">🔄 进行中</span>`);
    const m30TrendColor = (idx.m30_trend||'').includes('上涨') ? '#f85149' : ((idx.m30_trend||'').includes('下跌') ? '#3fb950' : '#8b949e');
    const m30Icon = (idx.m30_trend||'').includes('上涨') ? '↑' : ((idx.m30_trend||'').includes('下跌') ? '↓' : '—');
    const m30IsUp = (idx.m30_trend||'').includes('上涨');
    const m30ActiveColor = m30IsUp ? '#f85149' : '#3fb950';
    const m30DoneColor = m30IsUp ? '#3fb950' : '#f85149';
    const m30TcText = (idx.m30_status||'').includes('疑似') ? '<br><span style="font-size:11px;color:#d29922">⚠️ 疑似完成</span>' : ((idx.m30_status||'').includes('已确认') ? `<br><span style="font-size:11px;color:${m30DoneColor}">✅ 已完成</span>` : `<br><span style="font-size:11px;color:${m30ActiveColor}">🔄 进行中</span>`);
    const m30Sig = idx.m30_signal || '-';
    const m30Type = idx.m30_signal_type || '';
    const m30SigColor = m30Type.includes('B') ? '#f85149' : (m30Type.includes('S') ? '#3fb950' : '#8b949e');
    const sc = idx.score || 0;
    const scoreBg = sc >= 140 ? '#3a1a1a' : (sc >= 110 ? '#2a2a1a' : (sc >= 80 ? '#1a2a1a' : '#1a1a2a'));
    const scoreClr = sc >= 140 ? '#f85149' : (sc >= 110 ? '#d29922' : (sc >= 80 ? '#3fb950' : '#8b949e'));
    h += `<tr style="background:${bg};cursor:pointer" onclick="selectIndex('${idx.etf_code}')">`;
    h += `<td style="padding:8px 12px;font-weight:600;white-space:nowrap">${idx.index_name}<br><span style="color:#484f58;font-size:11px">${idx.etf_code || ''}</span></td>`;
    h += `<td style="padding:8px;text-align:center"><span style="background:${scoreBg};color:${scoreClr};padding:3px 8px;border-radius:4px;font-weight:700;font-size:13px">${sc}</span></td>`;
    const dSig = idx.latest_signal || '-';
    const dSigType = idx.latest_signal_type || '';
    const dSigColor = dSigType.includes('B') ? '#f85149' : (dSigType.includes('S') ? '#3fb950' : '#8b949e');
    h += `<td style="padding:8px;text-align:center;white-space:nowrap"><span style="color:${trendColor};font-weight:600">${trendIcon} ${(idx.trend||'-').replace('趋势','')}</span>${tcText}<br><span style="color:${dSigColor};font-size:12px">${dSig}</span></td>`;
    h += `<td style="padding:8px;text-align:center;white-space:nowrap"><span style="color:${m30TrendColor};font-weight:600">${m30Icon} ${(idx.m30_trend||'-').replace('趋势','')}</span>${m30TcText}<br><span style="color:${m30SigColor};font-size:12px">${m30Sig}</span></td>`;
    const conParts = (idx.conclusion||'-').split(' · ');
    let conHtml = conParts.map(p => {
      let color = '#c9d1d9';
      if (p.includes('买点') || p.includes('加仓') || p.includes('满仓') || p.includes('多头共振')) color = '#f85149';
      else if (p.includes('卖点') || p.includes('清仓') || p.includes('减仓') || p.includes('空头共振')) color = '#3fb950';
      else if (p.startsWith('⚠')) color = '#d29922';
      return `<span style="color:${color}">• ${p}</span>`;
    }).join('<br>');
    h += `<td style="padding:8px 12px;font-size:12px;line-height:1.6">${conHtml}</td>`;
    h += '</tr>';
  });
  h += '</tbody></table>';
  el.innerHTML = h;
}

// ─── Initialize ───
async function init() {
  renderGlobalSignals();
  renderOverview();

  const nav = document.getElementById('index-nav');
  let lastType = null;
  INDEX_LIST.forEach((idx, i) => {
    if (idx.type !== lastType) {
      const sep = document.createElement('span');
      sep.className = 'nav-sep';
      sep.textContent = idx.type === 'broad' ? '宽基' : (idx.type === 'stock' ? '个股' : '行业');
      nav.appendChild(sep);
      lastType = idx.type;
    }
    const btn = document.createElement('button');
    btn.className = 'nav-btn' + (i === 0 ? ' active' : '');
    const trendIcon = (idx.trend||'').includes('上涨') ? '🔺' : ((idx.trend||'').includes('下跌') ? '🔻' : '➖');
    btn.innerHTML = trendIcon + ' ' + idx.index_name;
    btn.title = (idx.summary || '') + ' | 评分:' + idx.score;
    btn.dataset.code = idx.etf_code;
    btn.onclick = () => selectIndex(idx.etf_code);
    nav.appendChild(btn);
  });

  document.querySelectorAll('.level-btn').forEach(btn => {
    btn.onclick = () => selectLevel(btn.dataset.level);
  });

  document.querySelectorAll('.detail-tab').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('.detail-tab').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.detail-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(btn.dataset.panel).classList.add('active');
    };
  });

  chart = echarts.init(document.getElementById('chart-container'));
  window.addEventListener('resize', () => chart.resize());
  await render();
}

function selectIndex(code) {
  currentIndex = code;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.code === code));
  render();
}

function selectLevel(level) {
  currentLevel = level;
  document.querySelectorAll('.level-btn').forEach(b => b.classList.toggle('active', b.dataset.level === level));
  render();
}

async function render() {
  const key = currentIndex + '_' + currentLevel;
  const cached = getChartData(key);
  if (!cached) {
    chart.showLoading({text: '加载数据中...', color: '#58a6ff', textColor: '#c9d1d9',
                       maskColor: 'rgba(13,17,23,0.8)', fontSize: 14});
  }
  const data = cached || await loadChartData(key);
  chart.hideLoading();
  if (!data) { chart.clear(); return; }

  updateConclusionBar(data);
  updateStructBar(data);
  renderChart(data);
  updateSynthesisPanel();
  updateSignalPanel(data);
}

function updateConclusionBar(data) {
  const bar = document.getElementById('conclusion-bar');
  const idx = INDEX_LIST.find(x => x.etf_code === currentIndex);
  if (!idx) { bar.innerHTML = ''; return; }

  const sc = idx.score || 0;
  const scoreBg = sc >= 140 ? '#3a1a1a' : (sc >= 110 ? '#2a2a1a' : (sc >= 80 ? '#1a2a1a' : '#1a1a2a'));
  const scoreClr = sc >= 140 ? '#f85149' : (sc >= 110 ? '#d29922' : (sc >= 80 ? '#3fb950' : '#8b949e'));

  const isUp = (idx.trend||'').includes('上涨');
  const trendCls = isUp ? 'tag-up' : ((idx.trend||'').includes('下跌') ? 'tag-down' : 'tag-neutral');
  const tcActiveColor = isUp ? '#f85149' : '#3fb950';
  const tcDoneColor = isUp ? '#3fb950' : '#f85149';
  const tcText = (idx.status||'').includes('疑似') ? `<span style="color:#d29922">⚠️ 疑似完成</span>`
    : ((idx.status||'').includes('已确认') ? `<span style="color:${tcDoneColor}">✅ 已完成</span>`
    : `<span style="color:${tcActiveColor}">🔄 进行中</span>`);
  const dSigType = idx.latest_signal_type || '';
  const dSigCls = dSigType.includes('B') ? 'tag-up' : (dSigType.includes('S') ? 'tag-down' : 'tag-neutral');

  const m30IsUp = (idx.m30_trend||'').includes('上涨');
  const m30TrendCls = m30IsUp ? 'tag-up' : ((idx.m30_trend||'').includes('下跌') ? 'tag-down' : 'tag-neutral');
  const m30ActiveColor = m30IsUp ? '#f85149' : '#3fb950';
  const m30DoneColor = m30IsUp ? '#3fb950' : '#f85149';
  const m30TcText = (idx.m30_status||'').includes('疑似') ? `<span style="color:#d29922">⚠️ 疑似完成</span>`
    : ((idx.m30_status||'').includes('已确认') ? `<span style="color:${m30DoneColor}">✅ 已完成</span>`
    : `<span style="color:${m30ActiveColor}">🔄 进行中</span>`);
  const m30SigType = idx.m30_signal_type || '';
  const m30SigCls = m30SigType.includes('B') ? 'tag-up' : (m30SigType.includes('S') ? 'tag-down' : 'tag-neutral');

  const conParts = (idx.conclusion||'-').split(' · ');
  const conHtml = conParts.map(p => {
    let color = '#c9d1d9';
    if (p.includes('买点') || p.includes('加仓') || p.includes('满仓') || p.includes('多头共振')) color = '#f85149';
    else if (p.includes('卖点') || p.includes('清仓') || p.includes('减仓') || p.includes('空头共振')) color = '#3fb950';
    else if (p.startsWith('⚠')) color = '#d29922';
    return `<span style="color:${color}">• ${p}</span>`;
  }).join(' ');

  bar.innerHTML = `
    <div class="concl-group">
      <span class="concl-label">评分</span>
      <span style="background:${scoreBg};color:${scoreClr};padding:3px 10px;border-radius:5px;font-weight:700;font-size:16px">${sc}</span>
    </div>
    <div class="concl-group">
      <span class="concl-label">长线</span>
      <span class="tag ${trendCls}">${(idx.trend||'-').replace('趋势','')}</span>
      ${tcText}
      ${idx.latest_signal && idx.latest_signal !== '-' ? `<span class="tag ${dSigCls}">${idx.latest_signal}</span>` : ''}
    </div>
    <div class="concl-group">
      <span class="concl-label">短线</span>
      <span class="tag ${m30TrendCls}">${(idx.m30_trend||'-').replace('趋势','')}</span>
      ${m30TcText}
      ${idx.m30_signal && idx.m30_signal !== '-' ? `<span class="tag ${m30SigCls}">${idx.m30_signal}</span>` : ''}
    </div>
    <div class="concl-group" style="border-right:none">
      <span class="concl-label">建议</span>
      <span style="font-size:13px;line-height:1.5">${conHtml}</span>
    </div>
  `;
}

function updateStructBar(data) {
  const bar = document.getElementById('struct-bar');
  const s = data.stats;
  bar.innerHTML = `
    <span>K线 ${s.bars}</span><span>合并 ${s.merged}</span>
    <span>分型 ${s.fractals}</span><span>笔 ${s.strokes}</span>
    <span>线段 ${s.segments}</span><span>笔中枢 ${s.hubs}</span>
    <span>信号 ${s.bsp}</span>
  `;
}

function updateSynthesisPanel() {
  const panel = document.getElementById('synthesis-panel');
  const syn = SYNTHESIS[currentIndex];
  if (!syn) { panel.innerHTML = ''; return; }

  let html = '<h3>多级别联立分析</h3>';

  html += '<table class="signal-table"><thead><tr>';
  html += '<th>级别</th><th>走势</th><th>走势状态</th><th>中枢位置</th><th>DIF区域</th><th>中枢数</th><th>信号数</th><th>最新信号</th>';
  html += '</tr></thead><tbody>';
  (syn.levels || []).forEach(lv => {
    const tCls = (lv.trend||'').includes('上涨') ? 'sig-buy' : ((lv.trend||'').includes('下跌') ? 'sig-sell' : '');
    const hpCls = (lv.hub_position||'').includes('上方') ? 'sig-buy' : ((lv.hub_position||'').includes('下方') ? 'sig-sell' : '');
    const sigStr = lv.latest_signal ? lv.latest_signal.label : '-';
    const tcIcons = {'进行中': '🔄', '疑似完成': '⚠️', '已确认完成': '✅'};
    const tcStr = lv.trend_completion ? (tcIcons[lv.trend_completion]||'') + lv.trend_completion : '-';
    const tcCls = lv.trend_completion === '已确认完成' ? 'sig-sell' : (lv.trend_completion === '疑似完成' ? '' : 'sig-buy');
    html += '<tr>';
    html += `<td style="font-weight:bold">${lv.level}</td>`;
    html += `<td class="${tCls}">${lv.trend || '-'}</td>`;
    html += `<td class="${tcCls}" title="${lv.completion_reason||''}">${tcStr}</td>`;
    html += `<td class="${hpCls}">${lv.hub_position || '-'}</td>`;
    html += `<td>${lv.dif_zone === 'above_zero' ? '0轴上' : '0轴下'}</td>`;
    html += `<td>${lv.num_hubs}</td>`;
    html += `<td>${lv.num_signals}</td>`;
    html += `<td>${sigStr}</td>`;
    html += '</tr>';
  });
  html += '</tbody></table>';

  if (syn.resonance && syn.resonance.length > 0) {
    html += `<h4 style="margin-top:8px;color:#f0883e">跨级别共振 (${syn.resonance.length})</h4>`;
    html += '<ul style="margin:4px 0;padding-left:16px">';
    syn.resonance.forEach(r => {
      const icon = r.direction === 'buy' ? '🔺' : '🔻';
      html += `<li>${icon} ${r.date} — ${r.note}</li>`;
    });
    html += '</ul>';
  }

  if (syn.enriched && syn.enriched.length > 0) {
    html += `<h4 style="margin-top:8px;color:#d2a8ff">置信度调整 (${syn.enriched.length})</h4>`;
    html += '<ul style="margin:4px 0;padding-left:16px">';
    syn.enriched.forEach(s => {
      const arrow = s.adjusted_confidence === 'high' || (s.adjusted_confidence === 'medium' && s.original_confidence === 'low') ? '↑' : '↓';
      html += `<li>${s.source_level}-${s.label} @ ${s.dt}: ${s.original_confidence}${arrow}${s.adjusted_confidence} (${s.context_note})</li>`;
    });
    html += '</ul>';
  }

  if (syn.interval_nests && syn.interval_nests.length > 0) {
    const deep = syn.interval_nests.filter(n => n.depth >= 2);
    html += `<h4 style="margin-top:8px;color:#58a6ff">区间套精确定位 (${syn.interval_nests.length}个背驰段, ${deep.length}个已嵌套)</h4>`;
    syn.interval_nests.forEach((n, i) => {
      const dirIcon = n.direction === -1 ? '🟢买' : '🔴卖';
      const typeStr = n.big_type === 'trend' ? '趋势' : '盘整';
      const stars = '★'.repeat(n.depth);
      const depthCls = n.depth >= 3 ? 'sig-buy' : (n.depth === 2 ? '' : 'sig-sell');
      html += `<div style="margin:10px 0;padding:14px 16px;background:#161b22;border-radius:8px;border-left:4px solid ${n.depth>=2?'#58a6ff':'#484f58'}">`;
      html += `<div style="font-weight:bold">${stars} ${i+1}. ${typeStr}${dirIcon} <span class="${depthCls}">深度${n.depth}</span></div>`;
      html += `<table style="width:100%;font-size:15px;margin:8px 0"><tr>`;
      html += `<td style="color:#8b949e">大级别</td><td>${n.big_level} ${n.big_dt}</td>`;
      html += `<td style="color:#8b949e">范围</td><td>${n.big_range[0]}~${n.big_range[1]}</td></tr>`;
      if (n.mid_level) {
        html += `<tr><td style="color:#8b949e">中级别</td><td>${n.mid_level} ${n.mid_dt}</td>`;
        const mr = n.mid_range[0] ? n.mid_range[0]+'~'+n.mid_range[1] : '-';
        html += `<td style="color:#8b949e">范围</td><td>${mr}</td></tr>`;
      }
      if (n.small_level) {
        html += `<tr><td style="color:#8b949e">小级别</td><td>${n.small_level} ${n.small_dt}</td>`;
        html += `<td></td><td></td></tr>`;
      }
      html += `</table>`;
      const priceStr = n.precision_price ? ` 价格 ${n.precision_price.toFixed(2)}` : '';
      html += `<div style="color:#58a6ff;font-weight:bold">精确定位: ${n.precision_dt}${priceStr}</div>`;
      html += `<div style="color:#8b949e;font-size:14px;margin-top:4px">${n.note}</div>`;
      html += `</div>`;
    });
  }

  panel.innerHTML = html;
}

function updateSignalPanel(data) {
  const panel = document.getElementById('signal-panel');
  if (!data.bsp || data.bsp.length === 0) {
    panel.innerHTML = '<h3>买卖点信号：无</h3>';
    return;
  }
  const sorted = [...data.bsp].sort((a, b) => b.idx - a.idx);
  const activeCount = sorted.filter(p => p.status !== 'invalidated').length;
  const invCount = sorted.length - activeCount;
  const countLabel = invCount > 0 ? sorted.length + ' 个，其中 ' + invCount + ' 个已失效' : sorted.length + ' 个';
  let html = '<h3>买卖点信号（共 ' + countLabel + '）</h3>';
  html += '<table class="signal-table"><thead><tr>';
  html += '<th>#</th><th>类型</th><th>状态</th><th>日期</th><th>价格</th><th>位置</th><th>面积对比</th><th>强弱</th><th>仓位</th><th>置信</th><th>防狼</th><th>依据</th>';
  html += '</tr></thead><tbody>';
  sorted.forEach(p => {
    const cls = p.is_buy ? 'sig-buy' : 'sig-sell';
    const confCls = p.conf === 'high' ? 'conf-high' : (p.conf === 'medium' ? 'conf-medium' : 'conf-low');
    const confLabel = p.conf === 'high' ? '高' : (p.conf === 'medium' ? '中' : '低');
    let locStr = '';
    if (p.stroke_idx >= 0) {
      locStr = 'S' + p.stroke_idx;
      if (p.seg_idx >= 0) locStr += '/D' + p.seg_idx;
    }
    const wolfStr = p.wolf ? '<span style="color:#d29922">⚠</span>' : '<span style="color:#3fb950">✓</span>';
    const strMap = {strongest: '🔥最强', strong: '💪强势', standard: '📌标准', weak: '⚠弱'};
    const strStr = p.strength ? ('<span style="color:#f0883e">' + (strMap[p.strength]||p.strength) + '</span>') : '-';
    let areaStr = '-';
    if (p.ranges && p.ranges.length >= 2) {
      const r0 = p.ranges[0], r1 = p.ranges[1];
      const ratio = r0.area > 0 ? (r1.area / r0.area * 100).toFixed(0) : '-';
      areaStr = '<span style="color:#58a6ff">' + r0.label + '=' + r0.area + '</span> vs '
              + '<span style="color:#f85149">' + r1.label + '=' + r1.area + '</span>'
              + ' (' + ratio + '%)';
    }
    const posStr = p.pos_advice ? p.pos_advice.split(' — ')[0] : '-';
    const posTitle = p.pos_advice || '';
    const inv = p.status === 'invalidated';
    const pending = p.status === 'pending';
    const confirmed = p.status === 'confirmed';
    const rowStyle = inv ? ' style="opacity:0.45;text-decoration:line-through"' : '';
    const pIsBuy = p.is_buy;
    const pConfColor = pIsBuy ? '#f85149' : '#3fb950';
    const pConfIcon = pIsBuy ? '🔴' : '🟢';
    const statusCell = inv
      ? '<td title="' + (p.inv_reason||'').replace(/"/g,'&quot;') + '" style="color:#da3633;cursor:help">❌失效</td>'
      : pending
        ? '<td style="color:#d29922">⏳待确认</td>'
        : '<td style="color:' + pConfColor + '">' + pConfIcon + '已确认</td>';
    html += '<tr' + rowStyle + '>';
    html += '<td style="color:#484f58;font-weight:bold">#' + p.bsp_idx + '</td>';
    html += '<td class="' + cls + '" style="font-weight:bold">' + p.label + '</td>';
    html += statusCell;
    html += '<td>' + data.dates[p.idx] + '</td>';
    html += '<td>' + p.price.toFixed(3) + '</td>';
    html += '<td style="color:#d2a8ff">' + locStr + '</td>';
    html += '<td style="font-size:13px">' + areaStr + '</td>';
    html += '<td>' + strStr + '</td>';
    html += `<td title="${posTitle}" style="color:#f0883e">${posStr}</td>`;
    html += '<td class="' + confCls + '">' + confLabel + '</td>';
    html += '<td>' + wolfStr + '</td>';
    html += '<td style="font-size:13px;color:#8b949e">' + p.desc + '</td>';
    html += '</tr>';
  });
  html += '</tbody></table>';
  panel.innerHTML = html;
}

function renderChart(data) {
  const upColor = '#f85149';
  const downColor = '#3fb950';

  // Hub mark areas
  const hubAreas = data.hubs.map(h => ({
    xAxis: data.dates[h.x0],
    yAxis: h.zd,
    name: '中枢' + (h.idx + 1),
  }));
  const hubAreas2 = data.hubs.map(h => ({
    xAxis: data.dates[h.x1],
    yAxis: h.zg,
  }));

  // Stroke lines as markLine data with index labels
  const strokeMarkData = data.strokes.map(s => ([
    { coord: [data.dates[s.coords[0][0]], s.coords[0][1]] },
    { coord: [data.dates[s.coords[1][0]], s.coords[1][1]],
      label: { show: true, formatter: 'S' + s.idx, fontSize: 12, fontWeight: 'bold', color: '#d29922',
               position: 'middle', distance: -14 } },
  ]));

  // Segment polyline: sparse array with values only at turning points
  const segData = new Array(data.dates.length).fill(null);
  data.segments.forEach(pt => { segData[pt[0]] = pt[1]; });
  const segMarkPts = data.segments.map(pt => ({
    coord: [data.dates[pt[0]], pt[1]],
    symbol: 'diamond',
    symbolSize: 8,
    itemStyle: { color: '#bc8cff' },
  }));

  // Segment index labels
  const segLabelPts = (data.seg_labels || []).map(lb => ({
    coord: [data.dates[lb.x], lb.y],
    symbol: 'circle',
    symbolSize: 1,
    itemStyle: { color: 'transparent' },
    label: { show: true, formatter: 'D' + lb.idx,
             fontSize: 13, fontWeight: 'bold',
             color: '#bc8cff',
             backgroundColor: 'rgba(13,17,23,0.85)',
             padding: [2, 6], borderRadius: 4,
             position: lb.dir === 1 ? 'insideRight' : 'insideLeft',
             distance: 0 },
  }));

  // Hub labels (stroke-level) with evolution type
  const evoColors = {'延伸': '#8b949e', '新生（上）': '#f85149', '新生（下）': '#3fb950', '扩展': '#d29922'};
  const hubLabelPts = data.hubs.map(h => {
    const midX = Math.round((h.x0 + Math.min(h.x1, data.dates.length - 1)) / 2);
    const evoTag = h.evo ? ' ' + h.evo : '';
    const evoClr = evoColors[h.evo] || '#58a6ff';
    return {
      coord: [data.dates[midX], h.zg],
      symbol: 'circle', symbolSize: 1, itemStyle: { color: 'transparent' },
      label: { show: true, formatter: '中枢' + (h.idx + 1) + evoTag,
               fontSize: 10, color: evoClr,
               backgroundColor: 'rgba(13,17,23,0.7)',
               padding: [1, 4], borderRadius: 2, position: 'top', distance: 5 },
    };
  });

  // BSP markers — 3-tier label system to prevent overlap
  const confIcons = {'high': '🔴高', 'medium': '🟡中', 'low': '⚪低'};
  const confShort = {'high': '🔴', 'medium': '🟡', 'low': '⚪'};
  const maxBspIdx = data.bsp.length > 0 ? Math.max(...data.bsp.map(p => p.bsp_idx)) : 0;
  const TIER1_THRESHOLD = maxBspIdx - 1;  // latest 2: full detail
  const TIER2_THRESHOLD = maxBspIdx - 5;  // next 4: compact #N type
  function bspTier(p) {
    if (p.bsp_idx >= TIER1_THRESHOLD) return 1;
    if (p.bsp_idx >= TIER2_THRESHOLD) return 2;
    return 3;
  }
  function bspLabel(p) {
    const tier = bspTier(p);
    if (tier === 3) return '#' + p.bsp_idx;
    let text = '#' + p.bsp_idx + ' ' + p.label;
    if (tier === 1) {
      if (p.conf) text += ' [' + (confIcons[p.conf] || p.conf) + ']';
      if (p.ranges && p.ranges.length >= 2) {
        const r0 = p.ranges[0], r1 = p.ranges[1];
        const ratio = r0.area > 0 ? (r1.area / r0.area).toFixed(2) : '-';
        text += '\n' + r1.label + '/' + r0.label + '=' + ratio;
      }
    } else {
      if (p.conf) text += (confShort[p.conf] || '');
      if (p.ranges && p.ranges.length >= 2) {
        const r0 = p.ranges[0], r1 = p.ranges[1];
        const ratio = r0.area > 0 ? (r1.area / r0.area).toFixed(2) : '-';
        text += ' ' + r1.label + '/' + r0.label + '=' + ratio;
      }
    }
    return text;
  }
  // Sort BSP by x-index to detect neighboring labels; alternate distance offset
  const sortedBuys = data.bsp.filter(p => p.is_buy).sort((a,b) => a.idx - b.idx);
  const sortedSells = data.bsp.filter(p => !p.is_buy).sort((a,b) => a.idx - b.idx);
  function buildPoints(list, isBuy) {
    const baseColor = isBuy ? upColor : downColor;
    const pos = isBuy ? 'bottom' : 'top';
    const rot = isBuy ? 0 : 180;
    return list.map((p, i) => {
      const tier = bspTier(p);
      const inv = p.status === 'invalidated';
      const pending = p.status === 'pending';
      const sz = tier === 1 ? 12 : (tier === 2 ? 8 : 6);
      const fs = tier === 1 ? 10 : (tier === 2 ? 8 : 7);
      const lh = tier === 1 ? 13 : 10;
      const prevClose = i > 0 && (p.idx - list[i-1].idx) < 8;
      const baseDist = tier === 1 ? 10 : (tier === 2 ? 6 : 4);
      const dist = prevClose && (i % 2 === 1) ? baseDist + 18 : baseDist;
      const color = inv ? '#484f58' : (pending ? '#d29922' : baseColor);
      const statusSuffix = inv ? '\n❌失效' : (pending ? '\n⏳待确认' : '');
      const labelText = bspLabel(p) + statusSuffix;
      return {
        coord: [data.dates[p.idx], p.price],
        value: p.label,
        symbol: 'triangle',
        symbolSize: inv ? Math.max(sz - 2, 4) : sz,
        symbolRotate: rot,
        itemStyle: { color: color, opacity: inv ? 0.4 : 1 },
        label: { show: true, formatter: labelText, position: pos,
                 fontSize: fs, color: color,
                 lineHeight: lh, align: 'center', distance: dist },
      };
    });
  }
  const buyPoints = buildPoints(sortedBuys, true);
  const sellPoints = buildPoints(sortedSells, false);

  // a+A+b+B+c / A+a+c structure labels on K-line chart
  // Show structure areas + MACD areas for the same set of recent signals
  const structAreas = [];
  const structLabels = [];
  const tagColors = {
    'a': 'rgba(88,166,255,0.10)', 'c': 'rgba(248,81,73,0.10)',
    'b': 'rgba(139,148,158,0.06)',
    'A': 'rgba(255,215,0,0.08)', 'B': 'rgba(255,165,0,0.08)',
  };
  const tagBorders = {
    'a': '#58a6ff', 'c': '#f85149', 'b': '#8b949e', 'A': '#ffd700', 'B': '#ffa500',
  };
  const bspWithStruct = data.bsp.filter(p => p.structure && p.structure.length > 0);
  const latestStruct = [...bspWithStruct].sort((a,b) => b.idx - a.idx).slice(0, 1);
  const visibleBspIdx = new Set(latestStruct.map(p => p.idx));
  latestStruct.forEach(p => {
    const tag_prefix = '#' + p.bsp_idx + ' ';
    p.structure.forEach(st => {
      const x0 = data.dates[st.x0], x1 = data.dates[st.x1];
      if (st.zg !== undefined) {
        structAreas.push([
          { xAxis: x0, yAxis: st.zd,
            itemStyle: { color: tagColors[st.tag] || 'rgba(255,255,255,0.05)',
                         borderColor: tagBorders[st.tag] || '#666',
                         borderWidth: 1, borderType: 'dashed' } },
          { xAxis: x1, yAxis: st.zg },
        ]);
      } else {
        structAreas.push([
          { xAxis: x0,
            itemStyle: { color: tagColors[st.tag] || 'rgba(255,255,255,0.05)',
                         borderColor: tagBorders[st.tag] || '#666',
                         borderWidth: 1, borderType: 'dashed' } },
          { xAxis: x1 },
        ]);
      }
      const midX = data.dates[Math.round((st.x0 + st.x1) / 2)];
      const kMid = data.kline[Math.round((st.x0 + st.x1) / 2)];
      const yVal = kMid ? Math.max(kMid[1], kMid[2], kMid[3], kMid[0]) : 0;
      structLabels.push({
        coord: [midX, yVal],
        symbol: 'circle', symbolSize: 1,
        itemStyle: { color: 'transparent' },
        label: {
          show: true,
          formatter: tag_prefix + st.tag,
          fontSize: 14, fontWeight: 'bold', fontStyle: 'italic',
          color: tagBorders[st.tag] || '#fff',
          position: 'top', distance: 15,
          textShadowColor: '#000', textShadowBlur: 3,
        },
      });
    });
  });

  // MACD area highlight regions — synced with K-line structure visibility
  const areaStyles = [
    { fill: 'rgba(88,166,255,0.12)', border: 'rgba(88,166,255,0.45)', clr: '#79b8ff' },
    { fill: 'rgba(248,81,73,0.14)', border: 'rgba(248,81,73,0.50)', clr: '#f85149' },
  ];
  const macdAreaLabels = [];
  const macdMarkAreaItems = [];
  data.bsp.forEach(p => {
    if (!p.ranges || p.ranges.length < 2) return;
    if (!visibleBspIdx.has(p.idx)) return;
    const r0 = p.ranges[0], r1 = p.ranges[1];
    const ratio = r0.area > 0 ? Math.round(r1.area / r0.area * 100) : 0;
    const idx = p.bsp_idx >= 0 ? '#' + p.bsp_idx + ' ' : '';
    const divergeStrong = ratio < 60;
    const confTag = p.conf ? ' [' + (confIcons[p.conf] || p.conf) + ']' : '';

    p.ranges.forEach((r, ri) => {
      macdMarkAreaItems.push([
        { xAxis: data.dates[r.x0], itemStyle: { color: areaStyles[ri].fill, borderColor: areaStyles[ri].border, borderWidth: 1 } },
        { xAxis: data.dates[r.x1] },
      ]);
      const midIdx = Math.round((r.x0 + r.x1) / 2);
      const midVal = data.macd_hist[midIdx] || 0;
      let labelText, labelClr;
      if (ri === 0) {
        labelText = idx + p.type + confTag + '  ' + r.label + ':' + r.area;
        labelClr = areaStyles[0].clr;
      } else {
        labelText = r.label + ':' + r.area + ' (' + ratio + '%) 背驰';
        labelClr = divergeStrong ? '#ffa657' : areaStyles[1].clr;
      }
      macdAreaLabels.push({
        coord: [data.dates[midIdx], midVal],
        symbol: 'none',
        label: {
          show: true,
          formatter: labelText,
          fontSize: ri === 0 ? 11 : 10,
          fontWeight: ri === 0 ? 'bold' : 'normal',
          color: labelClr,
          backgroundColor: 'rgba(13,17,23,0.88)',
          padding: [2, 6],
          borderRadius: 3,
          borderColor: ri === 1 && divergeStrong ? '#ffa657' : 'transparent',
          borderWidth: ri === 1 && divergeStrong ? 1 : 0,
          position: ri === 0 ? 'top' : 'bottom',
          distance: 4,
        },
      });
    });
  });

  // MACD colors
  const macdColors = data.macd_hist.map(v => v >= 0 ? upColor : downColor);

  const option = {
    animation: false,
    backgroundColor: '#0d1117',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: '#161b22',
      borderColor: '#30363d',
      textStyle: { color: '#c9d1d9', fontSize: 12 },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 10, height: 20,
        borderColor: '#30363d', fillerColor: 'rgba(88,166,255,0.1)',
        textStyle: { color: '#8b949e' } },
    ],
    grid: [
      { left: 60, right: 30, top: 20, bottom: '30%' },
      { left: 60, right: 30, top: '75%', bottom: 50 },
    ],
    xAxis: [
      { type: 'category', data: data.dates, gridIndex: 0, boundaryGap: true,
        axisLine: { lineStyle: { color: '#30363d' } },
        axisLabel: { color: '#8b949e', fontSize: 10 },
        splitLine: { show: false } },
      { type: 'category', data: data.dates, gridIndex: 1, boundaryGap: true,
        axisLine: { lineStyle: { color: '#30363d' } },
        axisLabel: { show: false },
        splitLine: { show: false } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0,
        axisLine: { lineStyle: { color: '#30363d' } },
        axisLabel: { color: '#8b949e', fontSize: 10 },
        splitLine: { lineStyle: { color: '#21262d' } } },
      { scale: true, gridIndex: 1,
        axisLine: { lineStyle: { color: '#30363d' } },
        axisLabel: { color: '#8b949e', fontSize: 10 },
        splitLine: { lineStyle: { color: '#21262d' } } },
    ],
    series: [
      // Candlestick
      {
        name: 'K线',
        type: 'candlestick',
        data: data.kline,
        xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: {
          color: upColor, color0: downColor,
          borderColor: upColor, borderColor0: downColor,
        },
        markPoint: {
          data: [...buyPoints, ...sellPoints, ...hubLabelPts, ...structLabels],
          animation: false,
        },
        markArea: {
          silent: true,
          animation: false,
          data: (() => {
            const hubAreas = data.hubs.map(h => [
              { xAxis: data.dates[h.x0], yAxis: h.zd,
                itemStyle: {
                  color: 'rgba(88,166,255,0.05)',
                  borderColor: 'rgba(88,166,255,0.35)',
                  borderWidth: 1,
                  borderType: 'dashed',
                } },
              { xAxis: data.dates[Math.min(h.x1, data.dates.length - 1)], yAxis: h.zg },
            ]);
            return [...hubAreas, ...structAreas];
          })(),
          label: { show: false },
        },
      },
      // Strokes
      {
        name: '笔',
        type: 'line',
        data: [],
        xAxisIndex: 0, yAxisIndex: 0,
        markLine: {
          symbol: ['circle', 'circle'],
          symbolSize: 4,
          lineStyle: { color: '#d29922', width: 1.5, type: 'solid' },
          label: { show: false },
          data: strokeMarkData,
          animation: false,
        },
      },
      // Segments as connected polyline (首尾相接)
      {
        name: '线段',
        type: 'line',
        data: segData,
        xAxisIndex: 0, yAxisIndex: 0,
        connectNulls: true,
        symbol: 'none',
        lineStyle: { color: '#bc8cff', width: 3 },
        markPoint: {
          data: [...segMarkPts, ...segLabelPts],
          animation: false,
        },
        z: 5,
      },
      // MACD Histogram
      {
        name: 'MACD',
        type: 'bar',
        data: data.macd_hist,
        xAxisIndex: 1, yAxisIndex: 1,
        itemStyle: {
          color: (params) => params.data >= 0 ? upColor : downColor,
        },
        barWidth: '60%',
      },
      // DIF line (also carries MACD area annotations for stable zoom behavior)
      {
        name: 'DIF',
        type: 'line',
        data: data.dif,
        xAxisIndex: 1, yAxisIndex: 1,
        lineStyle: { color: '#58a6ff', width: 1 },
        symbol: 'none',
        markArea: {
          silent: true,
          animation: false,
          data: macdMarkAreaItems,
        },
        markPoint: {
          data: macdAreaLabels,
          animation: false,
        },
      },
      // DEA line
      {
        name: 'DEA',
        type: 'line',
        data: data.dea,
        xAxisIndex: 1, yAxisIndex: 1,
        lineStyle: { color: '#f0883e', width: 1 },
        symbol: 'none',
      },
    ],
  };

  chart.setOption(option, true);
}

init();
</script>
</div><!-- .page-wrap -->
</body>
</html>"""


# ════════════════════════════════════════════════════════════════════
# Multi-level Synthesis Serialization
# ════════════════════════════════════════════════════════════════════

def _nest_to_dict(n) -> dict:
    """Serialize an IntervalNest to JSON-friendly dict."""
    return {
        "big_level": n.big_level,
        "big_type": n.big_signal_type,
        "big_dt": n.big_signal_dt,
        "big_range": list(n.big_time_range),
        "direction": n.big_direction,
        "mid_level": n.mid_level,
        "mid_dt": n.mid_signal_dt,
        "mid_range": list(n.mid_time_range),
        "small_level": n.small_level,
        "small_dt": n.small_signal_dt,
        "precision_price": n.precision_price,
        "precision_dt": n.precision_dt,
        "depth": n.depth,
        "note": n.note,
    }


def _synthesis_to_dict(syn: MultiLevelSynthesis) -> dict:
    """Serialize MultiLevelSynthesis to JSON-friendly dict for the dashboard."""
    return {
        "levels": syn.level_summary,
        "alignment": syn.direction_alignment,
        "bias": syn.overall_bias,
        "advice": syn.action_advice,
        "summary": syn.summary,
        "resonance": syn.resonance_signals,
        "enriched": [
            s for s in syn.enriched_signals if s["confidence_changed"]
        ],
        "interval_nests": [_nest_to_dict(n) for n in syn.interval_nests],
    }


def _build_index_overview(daily_data: dict, m30_data: dict, syn: dict) -> dict:
    """Build a summary row for the overview table.

    Returns a dict with: trend, status, status_detail, hub_pos, hub_range,
    latest_signal, alignment, bias, conclusion.
    """
    trend = daily_data.get("trend", "-")
    tc = daily_data.get("trend_completion", {})
    status = tc.get("status", "-")
    status_detail = tc.get("reason", "")
    hub_pos = daily_data.get("hub_position", "-")
    hd = daily_data.get("hub_detail", {})
    sh = hd.get("stroke_hub") if isinstance(hd, dict) else None
    hub_range = ""
    if sh:
        hub_range = f"ZG={sh['zg']:.2f} ZD={sh['zd']:.2f}"

    m30_trend = m30_data.get("trend", "-")
    m30_tc = m30_data.get("trend_completion", {})
    m30_status = m30_tc.get("status", "-")

    latest_sig = "-"
    latest_sig_type = ""
    bsp = daily_data.get("bsp", [])
    daily_dates = daily_data.get("dates", [])
    if bsp:
        lt = max(bsp, key=lambda p: p.get("idx", 0))
        latest_sig_type = lt.get("type", "")
        sig_dt = daily_dates[lt["idx"]] if lt["idx"] < len(daily_dates) else ""
        sig_dt_short = sig_dt[:10] if sig_dt else ""
        latest_sig = f"{lt['label']} {sig_dt_short}"

    alignment = syn.get("alignment", "-")
    bias = syn.get("bias", "-")

    is_up = "上涨" in trend
    is_down = "下跌" in trend
    above = "上方" in hub_pos
    inside = "内" in hub_pos
    below = "下方" in hub_pos

    bullets = []
    resonance = "共振" in alignment and "空" not in alignment
    resonance_bear = "共振" in alignment and "空" in alignment

    if is_up and above:
        bullets.append("日线多头+中枢上方，趋势最强格局")
        bullets.append("回调到中枢顶是加仓机会")
    elif is_up and inside:
        bullets.append("日线多头+中枢震荡，等待方向选择")
        bullets.append("中枢上沿附近减仓，下沿回补")
    elif is_up and below:
        bullets.append("日线多头但中枢下方，需确认支撑")
        bullets.append("等价格重新站上中枢再考虑")
    elif is_down and below:
        bullets.append("日线空头+中枢下方，趋势最弱格局")
        bullets.append("反弹到中枢底是减仓/出逃机会")
    elif is_down and inside:
        bullets.append("日线空头+中枢震荡，方向未明")
        bullets.append("反弹可减仓，不宜重仓")
    elif is_down and above:
        bullets.append("日线空头但中枢上方，关注是否破位")
        bullets.append("不破中枢上沿可观望，破则减仓")
    else:
        bullets.append("方向不明，暂时观望")

    if resonance:
        bullets.append("多头共振：持仓待涨或逢低加仓")
    elif resonance_bear:
        bullets.append("空头共振：空仓观望或逢高减仓")

    m30_signal = "-"
    m30_signal_type = ""
    m30_bsp = m30_data.get("bsp", [])
    m30_dates = m30_data.get("dates", [])
    m30_total = len(m30_dates)
    if m30_bsp and m30_total > 0:
        recency_cutoff = int(m30_total * 0.90)
        recent_m30 = [p for p in m30_bsp if p.get("idx", 0) >= recency_cutoff]
        if recent_m30:
            lt30 = max(recent_m30, key=lambda p: p.get("idx", 0))
            sig30 = lt30.get("type", "")
            m30_signal_type = sig30
            sig_dt = m30_dates[lt30["idx"]] if lt30["idx"] < len(m30_dates) else ""
            sig_dt_short = sig_dt[:16] if sig_dt else ""
            m30_signal = f"{lt30['label']} {sig_dt_short}"

            pos_map = {
                "1B": "轻仓试探(1/3)", "2B": "加至标准(2/3)",
                "3B": "可满仓", "PB": "轻仓试探",
                "1S": "减至1/3或清仓", "2S": "清仓",
                "3S": "必须清仓", "PS": "减仓1/3",
            }
            pos_advice = pos_map.get(sig30, "")

            if is_up and "B" in sig30:
                bullets.append(f"30分顺势买点→{pos_advice}")
            elif is_down and "S" in sig30:
                bullets.append(f"30分顺势卖点→{pos_advice}")
            elif is_down and "B" in sig30 and sig30 in ("1B", "2B"):
                bullets.append(f"30分反转信号→关注{pos_advice}")
            elif is_up and "S" in sig30:
                bullets.append(f"30分逆势卖点→{pos_advice}，注意回调深度")
            elif "B" in sig30:
                bullets.append(f"30分买点→{pos_advice}")
            elif "S" in sig30:
                bullets.append(f"30分卖点→{pos_advice}")

    d_tc = daily_data.get("trend_completion", {})
    d_status = d_tc.get("status", "")
    if "疑似" in d_status:
        bullets.append("⚠ 日线背驰出现，趋势可能反转")
    m30_tc = m30_data.get("trend_completion", {})
    m30_tc_status = m30_tc.get("status", "")
    if "疑似" in m30_tc_status:
        bullets.append("⚠ 30分背驰出现，短线注意反转")

    conclusion = " · ".join(bullets)

    return {
        "trend": trend,
        "status": status,
        "status_detail": status_detail,
        "hub_pos": hub_pos,
        "hub_range": hub_range,
        "latest_signal": latest_sig,
        "latest_signal_type": latest_sig_type,
        "m30_trend": m30_trend,
        "m30_status": m30_status,
        "m30_signal": m30_signal,
        "m30_signal_type": m30_signal_type,
        "alignment": alignment,
        "bias": bias,
        "conclusion": conclusion,
    }


def _actionability_score(daily_data: dict, m30_data: dict, syn: dict) -> tuple[int, str]:
    """Score based on Chanlun operation framework principles.

    Scoring dimensions (aligned with 缠论108课§5-6 + 图解缠论§2-4):
      1. Daily context     (0~50): trend + hub position + completion
      2. 30min signal      (0~80): signal type (缠论hierarchy) + recency + alignment
      3. Multi-level       (0~30): synthesis resonance / divergence
      4. MACD context      (0~10): DIF zone awareness

    Signal type weights follow 缠论 buy/sell point hierarchy:
      三买/三卖: highest (确认度最高，利润最大模式核心)
      一买/一卖: high (趋势反转，利润空间最大)
      二买/二卖: mid-high (确认度高，稳健首选)
      盘整买卖: low (可能假信号)
    """
    score = 0
    parts: list[str] = []

    daily_trend = daily_data.get("trend", "")
    is_up = "上涨" in daily_trend
    is_down = "下跌" in daily_trend

    if is_up:
        score += 15
        parts.append("日线↑")
    elif is_down:
        score += 8
        parts.append("日线↓")
    else:
        score += 3
        parts.append("日线盘整")

    hub_pos = daily_data.get("hub_position", "")
    above = "上方" in hub_pos
    inside = "内" in hub_pos or "震荡" in hub_pos
    below = "下方" in hub_pos
    if is_up and above:
        score += 20
    elif is_up and inside:
        score += 12
    elif is_up and below:
        score += 5
    elif is_down and below:
        score += 15
    elif is_down and inside:
        score += 8
    elif is_down and above:
        score += 5
    else:
        score += 3

    d_tc = daily_data.get("trend_completion", {})
    d_status = d_tc.get("status", "")
    if is_up:
        if "进行中" in d_status:
            score += 10
        elif "疑似" in d_status:
            score -= 5
        elif "已确认" in d_status:
            score -= 8
    elif is_down:
        if "进行中" in d_status:
            score -= 5
        elif "疑似" in d_status:
            score += 10
        elif "已确认" in d_status:
            score += 8

    alignment = syn.get("alignment", "")
    bias = syn.get("bias", "")
    if "共振" in alignment and "空" not in alignment:
        score += 30
    elif "共振" in alignment and "空" in alignment:
        score += 25
    elif "偏多" in bias:
        score += 18
    elif "偏空" in bias:
        score += 12
    elif "中性偏多" in bias or "中性偏空" in bias:
        score += 8
    elif "中性" in bias:
        score += 4

    daily_stats = daily_data.get("stats", {})
    dif_val = daily_stats.get("latest_dif", 0) if isinstance(daily_stats, dict) else 0
    if is_up and dif_val > 0:
        score += 8
    elif is_up and dif_val <= 0:
        score += 2
    elif is_down and dif_val < 0:
        score += 5
    elif is_down and dif_val >= 0:
        score += 2

    m30_bsp = m30_data.get("bsp", [])
    m30_total = len(m30_data.get("dates", []))
    if not m30_bsp or m30_total == 0:
        parts.append("30分无信号")
        return score, " | ".join(parts)

    type_weights = {
        "3B": 55, "3S": 55,
        "1B": 50, "1S": 50,
        "2B": 45, "2S": 45,
        "PB": 15, "PS": 15,
    }
    # Downweight weak 3B/3S in scoring (趋势末端信号价值低)
    _STRENGTH_MULT = {"strongest": 1.0, "strong": 0.8, "standard": 0.5, "weak": 0.15}

    recency_cutoff = int(m30_total * 0.85)
    recent = [p for p in m30_bsp if p.get("idx", 0) >= recency_cutoff]

    if not recent:
        parts.append("30分近期无信号")
        return score, " | ".join(parts)

    latest = max(recent, key=lambda p: p.get("idx", 0))
    sig_type = latest.get("type", "")
    sig_label = latest.get("label", sig_type)
    is_buy = "B" in sig_type
    base = type_weights.get(sig_type, 5)

    recency_ratio = latest["idx"] / max(m30_total - 1, 1)
    str_mult = _STRENGTH_MULT.get(latest.get("strength", ""), 1.0)
    score += int(base * recency_ratio * str_mult)

    if is_up and is_buy:
        score += 25
        parts.append(f"30分{sig_label}(顺势买)")
    elif is_down and is_buy and sig_type in ("1B", "2B"):
        score += 20
        parts.append(f"30分{sig_label}(反转买)")
    elif is_down and is_buy:
        score += 10
        parts.append(f"30分{sig_label}(逆势买)")
    elif is_up and not is_buy:
        score -= 5
        parts.append(f"30分{sig_label}(逆势卖)")
    elif is_down and not is_buy:
        score -= 8
        parts.append(f"30分{sig_label}(顺势卖)")
    else:
        score += 3
        parts.append(f"30分{sig_label}")

    for p in recent:
        if p is not latest:
            score += type_weights.get(p.get("type", ""), 3) // 4

    return score, " | ".join(parts)


# ════════════════════════════════════════════════════════════════════
# Generate HTML
# ════════════════════════════════════════════════════════════════════

def generate_dashboard(data_dir: str = None,
                       output_path: str = None) -> str:
    """Run analysis on all indices and generate HTML dashboard.

    Args:
        data_dir: directory containing ETF CSV data (default: PROJECT_ROOT/data)
        output_path: output HTML path (default: PROJECT_ROOT/reports/dashboard.html)

    Returns:
        path to the generated HTML file
    """
    if data_dir is None:
        data_dir = os.path.join(_PROJECT_ROOT, "data")
    if output_path is None:
        output_path = os.path.join(_PROJECT_ROOT, "reports", "dashboard.html")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    indices = load_index_watchlist()
    all_data = {}
    levels = [("daily", "daily.csv", "日线"),
              ("30min", "30min.csv", "30分钟"),
              ("5min", "5min.csv", "5分钟")]

    total = len(indices) * len(levels)
    done = 0

    synthesis_data = {}

    for idx in indices:
        idx_dir = os.path.join(data_dir, f"{idx.etf_code}_{idx.etf_name}")
        level_results: dict[str, AnalysisResult] = {}
        for level_key, csv_name, level_label in levels:
            done += 1
            csv_path = os.path.join(idx_dir, csv_name)
            if not os.path.exists(csv_path):
                print(f"  [{done}/{total}] SKIP {idx.etf_name} {level_label} (no data)")
                continue

            print(f"  [{done}/{total}] {idx.etf_name} {level_label}...", end=" ", flush=True)
            bars = load_bars_from_csv(csv_path)
            result = analyze(bars, level_key)
            level_results[level_key] = result
            echarts_data = _result_to_echarts_data(result)
            key = f"{idx.etf_code}_{level_key}"
            all_data[key] = echarts_data
            print(f"OK ({result.trend}, {len(result.buy_sell_points)} signals)")

        if "daily" in level_results:
            syn = synthesize_multi_level(
                level_results["daily"],
                level_results.get("30min"),
                level_results.get("5min"),
            )
            synthesis_data[idx.etf_code] = _synthesis_to_dict(syn)
            print(f"  → 联立: {syn.direction_alignment} | {syn.overall_bias}")

    index_list = []
    _valid_sig = {"1B", "1S", "2B", "2S", "3B", "3S"}
    for i in indices:
        entry = {"etf_code": i.etf_code, "index_name": i.index_name,
                 "etf_name": i.etf_name, "category": i.category,
                 "type": i.type}
        daily_key = f"{i.etf_code}_daily"
        m30_key = f"{i.etf_code}_30min"
        dd = all_data.get(daily_key, {})
        m30 = all_data.get(m30_key, {})
        syn = synthesis_data.get(i.etf_code, {})
        score, summary = _actionability_score(dd, m30, syn)
        overview = _build_index_overview(dd, m30, syn)
        entry["score"] = score
        entry["summary"] = summary
        entry.update(overview)

        # Find most recent signal datetime across all levels for sorting
        latest_sig_dt = ""
        for lk in ("daily", "30min", "5min"):
            d = all_data.get(f"{i.etf_code}_{lk}", {})
            dates = d.get("dates", [])
            for p in d.get("bsp", []):
                if p.get("type") not in _valid_sig:
                    continue
                if p.get("type") in ("3B", "3S") and p.get("strength") == "weak":
                    continue
                pi = p.get("idx", -1)
                if 0 <= pi < len(dates):
                    dt = dates[pi]
                    if dt > latest_sig_dt:
                        latest_sig_dt = dt
        entry["latest_sig_dt"] = latest_sig_dt
        index_list.append(entry)

    def _sort_key(x):
        return (x.get("latest_sig_dt", ""), x["score"])

    broad = [x for x in index_list if x.get("type") == "broad"]
    sector = [x for x in index_list if x.get("type") == "sector"]
    stocks = [x for x in index_list if x.get("type") == "stock"]
    broad.sort(key=_sort_key, reverse=True)
    sector.sort(key=_sort_key, reverse=True)
    stocks.sort(key=_sort_key, reverse=True)
    index_list = broad + sector + stocks

    latest_data_time = ""
    for key, data in all_data.items():
        dates = data.get("dates", [])
        if dates:
            last_dt = dates[-1]
            if last_dt > latest_data_time:
                latest_data_time = last_dt

    # Collect global signals (1B/1S/2B/2S/3B/3S) across all indices
    # Filter out weak 3B/3S — they have low operational value per theory.
    level_labels = {"daily": "日线", "30min": "30分钟", "5min": "5分钟"}
    idx_name_map = {i.etf_code: i.etf_name for i in indices}
    global_signals: list[dict] = []
    valid_types = {"1B", "1S", "2B", "2S", "3B", "3S"}
    for key, data in all_data.items():
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        etf_code, level_key = parts
        etf_name = idx_name_map.get(etf_code, etf_code)
        level_cn = level_labels.get(level_key, level_key)
        for p in data.get("bsp", []):
            if p["type"] not in valid_types:
                continue
            if p["type"] in ("3B", "3S") and p.get("strength") == "weak":
                continue
            dt_str = data["dates"][p["idx"]] if p["idx"] < len(data["dates"]) else ""
            entry = {
                "dt": dt_str,
                "etf_code": etf_code,
                "etf_name": etf_name,
                "level": level_cn,
                "type": p["type"],
                "label": p["label"],
                "price": p["price"],
                "conf": p.get("conf", ""),
                "strength": p.get("strength", ""),
                "pos_advice": p.get("pos_advice", ""),
                "desc": p.get("desc", ""),
                "wolf": p.get("wolf", ""),
                "status": p.get("status", "active"),
                "inv_reason": p.get("inv_reason", ""),
            }
            if p.get("ranges") and len(p["ranges"]) >= 2:
                r0, r1 = p["ranges"][0], p["ranges"][1]
                ratio = round(r1["area"] / r0["area"] * 100) if r0["area"] > 0 else 0
                entry["area_cmp"] = f"{r1['label']}/{r0['label']}={r1['area']/r0['area']:.2f}"
            else:
                entry["area_cmp"] = ""
            global_signals.append(entry)
    global_signals.sort(key=lambda x: x["dt"], reverse=True)
    global_signals_by_level: dict[str, list] = {"日线": [], "30分钟": [], "5分钟": []}
    for s in global_signals:
        lv = s["level"]
        if lv in global_signals_by_level and len(global_signals_by_level[lv]) < 20:
            global_signals_by_level[lv].append(s)
    global_signals_top = global_signals_by_level

    html = _HTML_TEMPLATE
    html = html.replace("__GEN_TIME__", datetime.now().strftime("%Y-%m-%d %H:%M"))
    html = html.replace("__DATA_TIME__", latest_data_time or "-")
    # Write per-index data files for lazy loading (JS format for file:// compat)
    data_out_dir = os.path.join(os.path.dirname(output_path), "data")
    os.makedirs(data_out_dir, exist_ok=True)
    for key, chart_data in all_data.items():
        fpath = os.path.join(data_out_dir, f"{key}.js")
        json_str = json.dumps(chart_data, ensure_ascii=False, separators=(",", ":"))
        with open(fpath, "w", encoding="utf-8") as df:
            df.write(f'DATA_CACHE["{key}"]={json_str};\n')

    total_data_kb = sum(
        os.path.getsize(os.path.join(data_out_dir, f))
        for f in os.listdir(data_out_dir) if f.endswith(".js")
    ) / 1024

    html = html.replace("__ALL_DATA_JSON__",
                         json.dumps(sorted(all_data.keys()), ensure_ascii=False))
    html = html.replace("__INDEX_LIST_JSON__", json.dumps(index_list, ensure_ascii=False))
    html = html.replace("__SYNTHESIS_JSON__", json.dumps(synthesis_data, ensure_ascii=False))
    html = html.replace("__GLOBAL_SIGNALS_JSON__", json.dumps(global_signals_top, ensure_ascii=False))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\nDashboard saved to: {output_path}")
    print(f"HTML size: {size_kb:.0f} KB  |  Data files: {total_data_kb:.0f} KB ({len(all_data)} files)")
    return output_path


# ════════════════════════════════════════════════════════════════════
# Mobile Dashboard (Canvas-based, same data as desktop)
# ════════════════════════════════════════════════════════════════════

def generate_mobile_dashboard(data_dir: str = None,
                              output_path: str = None) -> str:
    """Generate a mobile-optimized HTML dashboard with Canvas K-line charts.

    Uses identical data as the desktop (ECharts) version via _result_to_echarts_data
    and _synthesis_to_dict. Only the rendering differs (Canvas vs ECharts).
    """
    if data_dir is None:
        data_dir = os.path.join(_PROJECT_ROOT, "data")
    if output_path is None:
        output_path = os.path.join(_PROJECT_ROOT, "reports", "dashboard_mobile.html")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    indices = load_index_watchlist()
    levels_cfg = [("daily", "daily.csv", "日线"),
                  ("30min", "30min.csv", "30分钟"),
                  ("5min", "5min.csv", "5分钟")]

    all_data = {}
    synthesis_data = {}

    for idx in indices:
        idx_dir = os.path.join(data_dir, f"{idx.etf_code}_{idx.etf_name}")
        level_results: dict[str, AnalysisResult] = {}

        for level_key, csv_name, level_label in levels_cfg:
            csv_path = os.path.join(idx_dir, csv_name)
            if not os.path.exists(csv_path):
                continue
            result = analyze(load_bars_from_csv(csv_path), level_key)
            level_results[level_key] = result
            echarts_data = _result_to_echarts_data(result)
            key = f"{idx.etf_code}_{level_key}"
            all_data[key] = echarts_data
            print(f"  [{idx.etf_name} {level_label}] {result.trend} | "
                  f"{len(result.buy_sell_points)} signals")

        if "daily" in level_results:
            syn = synthesize_multi_level(
                level_results["daily"],
                level_results.get("30min"),
                level_results.get("5min"),
            )
            synthesis_data[idx.etf_code] = _synthesis_to_dict(syn)
            print(f"  → 联立: {syn.direction_alignment} | {syn.overall_bias}")

    index_list = []
    _valid_sig_m = {"1B", "1S", "2B", "2S", "3B", "3S"}
    for idx in indices:
        entry = {"etf_code": idx.etf_code, "index_name": idx.index_name,
                 "etf_name": idx.etf_name, "category": idx.category,
                 "type": idx.type}
        daily_key = f"{idx.etf_code}_daily"
        m30_key = f"{idx.etf_code}_30min"
        dd = all_data.get(daily_key, {})
        m30 = all_data.get(m30_key, {})
        syn = synthesis_data.get(idx.etf_code, {})
        score, summary = _actionability_score(dd, m30, syn)
        overview = _build_index_overview(dd, m30, syn)
        entry["score"] = score
        entry["summary"] = summary
        entry.update(overview)

        latest_sig_dt = ""
        for lk in ("daily", "30min", "5min"):
            d = all_data.get(f"{idx.etf_code}_{lk}", {})
            dates = d.get("dates", [])
            for p in d.get("bsp", []):
                if p.get("type") not in _valid_sig_m:
                    continue
                if p.get("type") in ("3B", "3S") and p.get("strength") == "weak":
                    continue
                pi = p.get("idx", -1)
                if 0 <= pi < len(dates):
                    dt = dates[pi]
                    if dt > latest_sig_dt:
                        latest_sig_dt = dt
        entry["latest_sig_dt"] = latest_sig_dt
        index_list.append(entry)

    def _sort_key_m(x):
        return (x.get("latest_sig_dt", ""), x["score"])

    broad = [x for x in index_list if x.get("type") == "broad"]
    sector = [x for x in index_list if x.get("type") == "sector"]
    stocks = [x for x in index_list if x.get("type") == "stock"]
    broad.sort(key=_sort_key_m, reverse=True)
    sector.sort(key=_sort_key_m, reverse=True)
    stocks.sort(key=_sort_key_m, reverse=True)
    index_list = broad + sector + stocks

    latest_data_time = ""
    for key, data in all_data.items():
        dates = data.get("dates", [])
        if dates:
            last_dt = dates[-1]
            if last_dt > latest_data_time:
                latest_data_time = last_dt

    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_time = latest_data_time or "-"

    # Write per-index data files for lazy loading (shared with desktop)
    data_out_dir = os.path.join(os.path.dirname(output_path), "data")
    os.makedirs(data_out_dir, exist_ok=True)
    for key, chart_data in all_data.items():
        fpath = os.path.join(data_out_dir, f"{key}.js")
        if not os.path.exists(fpath):
            json_str = json.dumps(chart_data, ensure_ascii=False, separators=(",", ":"))
            with open(fpath, "w", encoding="utf-8") as df:
                df.write(f'DATA_CACHE["{key}"]={json_str};\n')

    data_keys_json = json.dumps(sorted(all_data.keys()), ensure_ascii=False)
    index_list_json = json.dumps(index_list, ensure_ascii=False)
    synthesis_json = json.dumps(synthesis_data, ensure_ascii=False)

    # Collect global signals for mobile (same logic as desktop)
    level_labels_m = {"daily": "日线", "30min": "30分钟", "5min": "5分钟"}
    idx_name_map_m = {i.etf_code: i.etf_name for i in indices}
    mobile_global_signals: list[dict] = []
    valid_types_m = {"1B", "1S", "2B", "2S", "3B", "3S"}
    for key, data in all_data.items():
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        etf_code, level_key = parts
        etf_name = idx_name_map_m.get(etf_code, etf_code)
        level_cn = level_labels_m.get(level_key, level_key)
        for p in data.get("bsp", []):
            if p["type"] not in valid_types_m:
                continue
            if p["type"] in ("3B", "3S") and p.get("strength") == "weak":
                continue
            dt_str = data["dates"][p["idx"]] if p["idx"] < len(data["dates"]) else ""
            entry_m = {
                "dt": dt_str, "etf_name": etf_name, "level": level_cn,
                "type": p["type"], "label": p["label"],
                "price": p["price"], "conf": p.get("conf", ""),
                "status": p.get("status", "active"),
                "inv_reason": p.get("inv_reason", ""),
            }
            if p.get("ranges") and len(p["ranges"]) >= 2:
                r0, r1 = p["ranges"][0], p["ranges"][1]
                ratio = round(r1["area"] / r0["area"] * 100) if r0["area"] > 0 else 0
                entry_m["area_cmp"] = f"{r1['label']}/{r0['label']}={r1['area']/r0['area']:.2f}"
            else:
                entry_m["area_cmp"] = ""
            mobile_global_signals.append(entry_m)
    mobile_global_signals.sort(key=lambda x: x["dt"], reverse=True)
    mobile_gs_by_level: dict[str, list] = {"日线": [], "30分钟": [], "5分钟": []}
    for s in mobile_global_signals:
        lv = s["level"]
        if lv in mobile_gs_by_level and len(mobile_gs_by_level[lv]) < 20:
            mobile_gs_by_level[lv].append(s)
    mobile_global_signals_top = mobile_gs_by_level
    mobile_global_signals_json = json.dumps(mobile_global_signals_top, ensure_ascii=False)

    tab_parts = []
    last_type = None
    for i, il in enumerate(index_list):
        if il.get("type") != last_type:
            label = "宽基" if il.get("type") == "broad" else ("个股" if il.get("type") == "stock" else "行业")
            tab_parts.append(
                f'<div style="display:flex;align-items:center;padding:0 6px;'
                f'color:#484f58;font-size:10px;font-weight:700;letter-spacing:1px;'
                f'border-left:2px solid #30363d;margin-left:2px;padding-left:8px">'
                f'{label}</div>')
            last_type = il.get("type")
        active = ' active' if i == 0 else ''
        trend_icon = ('🔺' if '上涨' in il.get('trend', '')
                      else ('🔻' if '下跌' in il.get('trend', '') else '➖'))
        tab_parts.append(
            f'<div class="idx-tab{active}" onclick="switchIndex(\'{il["etf_code"]}\')">'
            f'{trend_icon} {il["index_name"]}</div>')
    idx_tabs_html = "\n      ".join(tab_parts)
    first_code = index_list[0]["etf_code"] if index_list else ""

    mobile_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>缠论分析 — 移动版</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif;
       background: #0d1117; color: #c9d1d9; font-size: 14px; line-height: 1.6;
       -webkit-text-size-adjust: 100%; }}
.container {{ max-width: 100%; margin: 0 auto; padding: 10px; }}
h1 {{ font-size: 20px; color: #58a6ff; text-align: center; padding: 12px 0 4px; }}
.subtitle {{ text-align: center; color: #8b949e; font-size: 12px; margin-bottom: 12px; }}

.chart-section {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                  margin-bottom: 14px; overflow: hidden; }}
.idx-tabs {{ display: flex; gap: 0; border-bottom: 1px solid #21262d;
             flex-wrap: wrap; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
.idx-tab {{ padding: 8px 12px; cursor: pointer; color: #8b949e;
            border-bottom: 2px solid transparent; font-size: 13px; white-space: nowrap; }}
.idx-tab.active {{ color: #58a6ff; border-bottom-color: #58a6ff; background: rgba(88,166,255,0.05); }}
.level-tabs {{ display: flex; gap: 0; border-bottom: 1px solid #21262d;
               padding: 0 12px; background: #0d1117; }}
.level-tab {{ padding: 7px 14px; cursor: pointer; color: #8b949e;
              border-bottom: 2px solid transparent; font-size: 13px; }}
.level-tab.active {{ color: #f0883e; border-bottom-color: #f0883e; }}

/* Info bar */
.info-bar {{ display: flex; gap: 6px; flex-wrap: wrap; padding: 8px 12px;
             font-size: 12px; border-bottom: 1px solid #21262d; }}
.info-bar span {{ white-space: nowrap; }}
.tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
        font-size: 12px; font-weight: 600; margin: 1px 2px; }}
.tag-up {{ background: #3a1a1a; color: #f85149; }}
.tag-down {{ background: #1a3a2a; color: #3fb950; }}
.tag-neutral {{ background: #2a2a1a; color: #d29922; }}

/* Chart */
.chart-area {{ padding: 6px 10px; }}
canvas {{ display: block; width: 100%; background: #0d1117; border-radius: 4px; }}
.legend {{ display: flex; gap: 8px; padding: 4px 10px; flex-wrap: wrap; }}
.legend-item {{ display: flex; align-items: center; gap: 3px; font-size: 10px; color: #8b949e; }}
.legend-color {{ width: 10px; height: 10px; border-radius: 2px; }}

/* Synthesis panel */
#synthesis-panel {{ padding: 8px 12px; }}
#synthesis-panel h3 {{ font-size: 14px; color: #58a6ff; margin-bottom: 6px; }}
#synthesis-panel h4 {{ font-size: 13px; margin-top: 8px; }}

/* Signal panel */
#signal-panel {{ padding: 8px 12px; border-top: 1px solid #21262d; }}
#signal-panel h3 {{ font-size: 14px; color: #58a6ff; margin-bottom: 6px; }}

/* Tables */
.signal-table {{ width: 100%; border-collapse: collapse; font-size: 11px;
                 display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
.signal-table th {{ background: #0d1117; color: #58a6ff; padding: 4px 6px;
                    text-align: left; border-bottom: 2px solid #30363d; white-space: nowrap; }}
.signal-table td {{ padding: 4px 6px; border-bottom: 1px solid #21262d; white-space: nowrap; }}
.signal-table tr:hover {{ background: #1c2333; }}
.sig-buy {{ color: #f85149; }}
.sig-sell {{ color: #3fb950; }}
.conf-high {{ color: #3fb950; font-weight: bold; }}
.conf-medium {{ color: #d29922; }}
.conf-low {{ color: #8b949e; }}

.footer {{ text-align: center; color: #484f58; font-size: 11px; padding: 20px 0 12px; }}
</style>
</head>
<body>
<div class="container">
<h1>缠论交易系统 v2</h1>
<div class="subtitle">移动版 · 数据 {data_time} · 生成 {gen_time} · 日线→30分→5分</div>

<div id="mobileGlobalSignals" style="margin-bottom:8px"></div>

<div style="color:#c9d1d9;font-size:14px;font-weight:bold;border-bottom:1px solid #30363d;padding-bottom:4px;margin:12px 0 6px">📈 技术分析详情</div>
<div class="chart-section">
  <div class="idx-tabs" id="idxTabs">
    {idx_tabs_html}
  </div>
  <div class="level-tabs" id="levelTabs">
    <div class="level-tab active" onclick="switchLevel('daily')">日线</div>
    <div class="level-tab" onclick="switchLevel('30min')">30分钟</div>
    <div class="level-tab" onclick="switchLevel('5min')">5分钟</div>
  </div>
  <div class="info-bar" id="infoBar"></div>
  <div id="loadingOverlay" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(13,17,23,0.7);z-index:999;align-items:center;justify-content:center"><span style="color:#58a6ff;font-size:15px">加载数据中...</span></div>
  <div class="chart-area"><canvas id="klineCanvas" height="320"></canvas></div>
  <div class="legend">
    <div class="legend-item"><div class="legend-color" style="background:#f85149"></div>阳线</div>
    <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div>阴线</div>
    <div class="legend-item"><div class="legend-color" style="background:#f0883e"></div>笔</div>
    <div class="legend-item"><div class="legend-color" style="background:#bc8cff"></div>线段</div>
    <div class="legend-item"><div class="legend-color" style="background:rgba(88,166,255,0.4)"></div>笔中枢</div>
    <div class="legend-item"><div class="legend-color" style="background:#f85149"></div>买▲</div>
    <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div>卖▼</div>
  </div>
  <div class="chart-area"><canvas id="macdCanvas" height="120"></canvas></div>
  <div class="legend">
    <div class="legend-item"><div class="legend-color" style="background:#58a6ff"></div>DIF</div>
    <div class="legend-item"><div class="legend-color" style="background:#f0883e"></div>DEA</div>
  </div>
  <div id="synthesis-panel"></div>
  <div id="signal-panel"></div>
</div>

<div style="color:#c9d1d9;font-size:14px;font-weight:bold;border-bottom:1px solid #30363d;padding-bottom:4px;margin:12px 0 6px">📊 标的可操作性总览</div>
<div id="mobileOverview" style="margin-bottom:12px"></div>
</div>

<script>
var DATA_CACHE = {{}};
const DATA_KEYS = {data_keys_json};
const INDEX_LIST = {index_list_json};
const SYNTHESIS = {synthesis_json};
const GLOBAL_SIGNALS = {mobile_global_signals_json};

function getChartData(key) {{ return DATA_CACHE[key] || null; }}

function loadChartData(key) {{
  return new Promise(resolve => {{
    if (DATA_CACHE[key]) {{ resolve(DATA_CACHE[key]); return; }}
    const s = document.createElement('script');
    s.src = 'data/' + key + '.js';
    s.onload = () => resolve(DATA_CACHE[key] || null);
    s.onerror = () => resolve(null);
    document.head.appendChild(s);
  }});
}}

let currentIndex = '{first_code}';
let currentLevel = 'daily';
let viewStart = 0, viewEnd = 0;
let isDragging = false, dragStartX = 0, dragStartView = 0;
let pinchStartDist = 0, pinchStartRange = 0;
const MIN_VIEW = 20;
let dataLoading = false;

let mgsTab = '日线';
function renderMobileGlobalSignals() {{
  const el = document.getElementById('mobileGlobalSignals');
  const levels = ['日线', '30分钟', '5分钟'];
  const hasAny = levels.some(lv => (GLOBAL_SIGNALS[lv]||[]).length > 0);
  if (!hasAny) {{ el.innerHTML = ''; return; }}

  const confIcons = {{'high': '🔴', 'medium': '🟡', 'low': '⚪'}};
  const tClrs = {{'1B': '#f85149', '2B': '#f85149', '3B': '#f85149', '1S': '#3fb950', '2S': '#3fb950', '3S': '#3fb950'}};

  let h = '<div style="font-size:13px;font-weight:bold;color:#c9d1d9;margin-bottom:4px">📡 最新买卖点 (每级别20)</div>';
  h += '<div style="display:flex;gap:4px;margin-bottom:6px">';
  levels.forEach(lv => {{
    const items = GLOBAL_SIGNALS[lv]||[];
    const total = items.length;
    const confirmedCnt = items.filter(s => s.status === 'confirmed').length;
    const pendingCnt = items.filter(s => s.status === 'pending').length;
    const invCnt = items.filter(s => s.status === 'invalidated').length;
    let cntLabel = '' + total;
    if (invCnt > 0 || pendingCnt > 0) {{
      const parts = [];
      if (confirmedCnt > 0) parts.push(confirmedCnt + '✓');
      if (pendingCnt > 0) parts.push(pendingCnt + '⏳');
      if (invCnt > 0) parts.push(invCnt + '✗');
      cntLabel = parts.join(' ');
    }}
    const active = lv === mgsTab;
    const bg = active ? '#21262d' : 'transparent';
    const clr = active ? '#58a6ff' : '#8b949e';
    const border = active ? '2px solid #58a6ff' : '2px solid transparent';
    h += `<button onclick="mgsTab='${{lv}}';renderMobileGlobalSignals()" style="padding:4px 10px;border:none;border-bottom:${{border}};background:${{bg}};color:${{clr}};cursor:pointer;font-size:12px;border-radius:4px 4px 0 0">${{lv}} (${{cntLabel}})</button>`;
  }});
  h += '</div>';

  const signals = GLOBAL_SIGNALS[mgsTab] || [];
  h += '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">';
  h += '<table style="width:100%;border-collapse:collapse;font-size:11px;color:#c9d1d9;background:#161b22">';
  h += '<thead><tr style="background:#21262d;color:#8b949e;font-size:10px">';
  h += '<th style="padding:4px;text-align:left">时间</th>';
  h += '<th style="padding:4px;text-align:left">标的</th>';
  h += '<th style="padding:4px;text-align:center">类型</th>';
  h += '<th style="padding:4px;text-align:right">价格</th>';
  h += '<th style="padding:4px;text-align:center">置信</th>';
  h += '<th style="padding:4px;text-align:left">背驰</th>';
  h += '</tr></thead><tbody>';
  signals.forEach((s, i) => {{
    const bg = i % 2 === 0 ? '#0d1117' : '#161b22';
    const tc = tClrs[s.type] || '#c9d1d9';
    const confStr = (confIcons[s.conf] || '') + (s.conf === 'high' ? '高' : s.conf === 'medium' ? '中' : s.conf === 'low' ? '低' : '');
    const dtShort = s.dt ? s.dt.substring(5) : '-';
    const inv = s.status === 'invalidated';
    const pending = s.status === 'pending';
    const rowOpacity = inv ? 'opacity:0.45;' : '';
    const strike = inv ? 'text-decoration:line-through;' : '';
    const mGsBuyType = ['1B','2B','3B','PB'].includes(s.type);
    const mGsConfClr = mGsBuyType ? '#f85149' : '#3fb950';
    const statusTag = inv ? '<span style="font-size:9px;color:#da3633;margin-left:2px">✗</span>' : (pending ? '<span style="font-size:9px;color:#d29922;margin-left:2px">⏳</span>' : '<span style="font-size:9px;color:' + mGsConfClr + ';margin-left:2px">✓</span>');
    h += `<tr style="background:${{bg}};border-bottom:1px solid #21262d;${{rowOpacity}}">`;
    h += `<td style="padding:3px 4px;font-family:monospace;font-size:10px;white-space:nowrap;${{strike}}">${{dtShort}}</td>`;
    h += `<td style="padding:3px 4px;font-weight:600;${{strike}}">${{s.etf_name}}</td>`;
    h += `<td style="padding:3px 4px;text-align:center;font-weight:bold;color:${{tc}};${{strike}}">${{s.label}}${{statusTag}}</td>`;
    h += `<td style="padding:3px 4px;text-align:right;font-family:monospace;${{strike}}">${{s.price ? s.price.toFixed(3) : '-'}}</td>`;
    h += `<td style="padding:3px 4px;text-align:center">${{confStr}}</td>`;
    h += `<td style="padding:3px 4px;font-size:10px">${{s.area_cmp || '-'}}</td>`;
    h += '</tr>';
  }});
  if (signals.length === 0) {{
    h += '<tr><td colspan="6" style="padding:12px;text-align:center;color:#484f58">暂无信号</td></tr>';
  }}
  h += '</tbody></table></div>';
  el.innerHTML = h;
}}

function renderMobileOverview() {{
  const el = document.getElementById('mobileOverview');
  const thSt = 'padding:6px 4px;text-align:center;font-size:11px;color:#8b949e;white-space:nowrap;position:sticky;top:0;background:#21262d;z-index:1';
  let h = '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">';
  h += '<table style="width:100%;border-collapse:collapse;font-size:12px;color:#c9d1d9;background:#161b22">';
  h += `<thead><tr style="background:#21262d">`;
  h += `<th style="${{thSt}};text-align:left;padding-left:8px;min-width:56px">标的</th>`;
  h += `<th style="${{thSt}};width:36px">评分</th>`;
  h += `<th style="${{thSt}}">长线（日线）</th>`;
  h += `<th style="${{thSt}}">短线（30分钟）</th>`;
  h += `</tr></thead><tbody>`;
  let lastType = null;
  INDEX_LIST.forEach((idx, i) => {{
    if (idx.type !== lastType) {{
      const label = idx.type === 'broad' ? '📊 宽基指数' : (idx.type === 'stock' ? '📈 个股' : '🏭 行业ETF');
      h += `<tr style="background:#1a1e24"><td colspan="4" style="padding:5px 8px;font-size:11px;font-weight:700;color:#58a6ff;letter-spacing:1px">${{label}}</td></tr>`;
      lastType = idx.type;
    }}
    const bg = i % 2 === 0 ? '#0d1117' : '#161b22';

    const isUp = (idx.trend||'').includes('上涨');
    const trendColor = isUp ? '#f85149' : ((idx.trend||'').includes('下跌') ? '#3fb950' : '#8b949e');
    const trendIcon = isUp ? '↑' : ((idx.trend||'').includes('下跌') ? '↓' : '—');
    const tcActiveColor = isUp ? '#f85149' : '#3fb950';
    const tcDoneColor = isUp ? '#3fb950' : '#f85149';
    const tcTag = (idx.status||'').includes('疑似') ? `<span style="color:#d29922">⚠️疑似完成</span>`
      : ((idx.status||'').includes('已确认') ? `<span style="color:${{tcDoneColor}}">✅完成</span>`
      : `<span style="color:${{tcActiveColor}}">🔄进行</span>`);
    const dSigType = idx.latest_signal_type || '';
    const dSigColor = dSigType.includes('B') ? '#f85149' : (dSigType.includes('S') ? '#3fb950' : '#8b949e');
    const dSig = idx.latest_signal || '-';

    const m30IsUp = (idx.m30_trend||'').includes('上涨');
    const m30Color = m30IsUp ? '#f85149' : ((idx.m30_trend||'').includes('下跌') ? '#3fb950' : '#8b949e');
    const m30Icon = m30IsUp ? '↑' : ((idx.m30_trend||'').includes('下跌') ? '↓' : '—');
    const m30ActiveColor = m30IsUp ? '#f85149' : '#3fb950';
    const m30DoneColor = m30IsUp ? '#3fb950' : '#f85149';
    const m30TcTag = (idx.m30_status||'').includes('疑似') ? `<span style="color:#d29922">⚠️疑似完成</span>`
      : ((idx.m30_status||'').includes('已确认') ? `<span style="color:${{m30DoneColor}}">✅完成</span>`
      : `<span style="color:${{m30ActiveColor}}">🔄进行</span>`);
    const m30SigType = idx.m30_signal_type || '';
    const m30SigColor = m30SigType.includes('B') ? '#f85149' : (m30SigType.includes('S') ? '#3fb950' : '#8b949e');
    const m30Sig = idx.m30_signal || '-';

    const sc = idx.score || 0;
    const scoreBg = sc >= 140 ? '#3a1a1a' : (sc >= 110 ? '#2a2a1a' : (sc >= 80 ? '#1a2a1a' : '#1a1a2a'));
    const scoreClr = sc >= 140 ? '#f85149' : (sc >= 110 ? '#d29922' : (sc >= 80 ? '#3fb950' : '#8b949e'));

    const conParts = (idx.conclusion||'-').split(' · ');
    const conHtml = conParts.map(p => {{
      let color = '#c9d1d9';
      if (p.includes('买点') || p.includes('加仓') || p.includes('满仓') || p.includes('多头共振')) color = '#f85149';
      else if (p.includes('卖点') || p.includes('清仓') || p.includes('减仓') || p.includes('空头共振')) color = '#3fb950';
      else if (p.startsWith('⚠')) color = '#d29922';
      return `<span style="color:${{color}}">• ${{p}}</span>`;
    }}).join(' ');

    const tdSt = 'padding:6px 4px;text-align:center;vertical-align:top;white-space:nowrap;font-size:11px';
    h += `<tr style="background:${{bg}};cursor:pointer" onclick="switchIndex('${{idx.etf_code}}')">`;
    h += `<td style="${{tdSt}};text-align:left;padding-left:8px;font-weight:600;font-size:12px;color:#c9d1d9">${{idx.index_name}}<br><span style="color:#484f58;font-size:10px">${{idx.etf_code||''}}</span></td>`;
    h += `<td style="${{tdSt}}"><span style="background:${{scoreBg}};color:${{scoreClr}};padding:2px 5px;border-radius:3px;font-weight:700;font-size:12px">${{sc}}</span></td>`;
    h += `<td style="${{tdSt}}"><span style="color:${{trendColor}};font-weight:600">${{trendIcon}} ${{(idx.trend||'-').replace('趋势','')}}</span><br>${{tcTag}}<br><span style="color:${{dSigColor}};font-size:10px">${{dSig}}</span></td>`;
    h += `<td style="${{tdSt}}"><span style="color:${{m30Color}};font-weight:600">${{m30Icon}} ${{(idx.m30_trend||'-').replace('趋势','')}}</span><br>${{m30TcTag}}<br><span style="color:${{m30SigColor}};font-size:10px">${{m30Sig}}</span></td>`;
    h += `</tr>`;
    h += `<tr style="background:${{bg}};cursor:pointer;border-bottom:1px solid #21262d" onclick="switchIndex('${{idx.etf_code}}')">`;
    h += `<td colspan="4" style="padding:2px 8px 6px;font-size:11px;line-height:1.4">${{conHtml}}</td>`;
    h += `</tr>`;
  }});
  h += '</tbody></table></div>';
  el.innerHTML = `<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden">${{h}}</div>`;
}}
renderMobileGlobalSignals();
renderMobileOverview();

function getData() {{
  const key = currentIndex + '_' + currentLevel;
  return getChartData(key);
}}
async function ensureData() {{
  const key = currentIndex + '_' + currentLevel;
  return await loadChartData(key);
}}
function resetView() {{
  const d = getData();
  if (!d) return;
  viewStart = 0; viewEnd = d.dates.length;
}}
function clampView(total) {{
  let range = viewEnd - viewStart;
  if (range < MIN_VIEW) {{ const mid = (viewStart + viewEnd) / 2; viewStart = Math.round(mid - MIN_VIEW / 2); viewEnd = viewStart + MIN_VIEW; }}
  if (viewStart < 0) {{ viewStart = 0; viewEnd = Math.min(viewEnd - viewStart, total); }}
  if (viewEnd > total) {{ viewEnd = total; viewStart = Math.max(0, total - (viewEnd - viewStart)); }}
}}

async function switchIndex(code) {{
  currentIndex = code;
  document.querySelectorAll('.idx-tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  await loadAndRender();
}}
async function switchLevel(level) {{
  currentLevel = level;
  document.querySelectorAll('.level-tab').forEach(t => t.classList.remove('active'));
  const order = ['daily', '30min', '5min'];
  const tabs = document.querySelectorAll('.level-tab');
  const i = order.indexOf(level);
  if (i >= 0 && tabs[i]) tabs[i].classList.add('active');
  await loadAndRender();
}}

function render() {{
  const d = getData();
  if (!d) return;
  if (viewEnd === 0) viewEnd = d.dates.length;
  updateInfoBar(d);
  renderKline(d);
  renderMACD(d);
  updateSynthesisPanel();
  updateSignalPanel(d);
}}

async function loadAndRender() {{
  const loadingEl = document.getElementById('loadingOverlay');
  if (loadingEl) loadingEl.style.display = 'flex';
  await ensureData();
  if (loadingEl) loadingEl.style.display = 'none';
  resetView();
  render();
}}

function updateInfoBar(data) {{
  const bar = document.getElementById('infoBar');
  const idx = INDEX_LIST.find(x => x.etf_code === currentIndex);
  if (!idx) {{ bar.innerHTML = ''; return; }}

  const sc = idx.score || 0;
  const scoreBg = sc >= 140 ? '#3a1a1a' : (sc >= 110 ? '#2a2a1a' : (sc >= 80 ? '#1a2a1a' : '#1a1a2a'));
  const scoreClr = sc >= 140 ? '#f85149' : (sc >= 110 ? '#d29922' : (sc >= 80 ? '#3fb950' : '#8b949e'));

  const isUp = (idx.trend||'').includes('上涨');
  const tCls = isUp ? 'tag-up' : ((idx.trend||'').includes('下跌') ? 'tag-down' : 'tag-neutral');
  const tcActiveColor = isUp ? '#f85149' : '#3fb950';
  const tcDoneColor = isUp ? '#3fb950' : '#f85149';
  const tcText = (idx.status||'').includes('疑似') ? `<span style="color:#d29922">⚠️疑似</span>`
    : ((idx.status||'').includes('已确认') ? `<span style="color:${{tcDoneColor}}">✅完成</span>`
    : `<span style="color:${{tcActiveColor}}">🔄进行</span>`);
  const dSigType = idx.latest_signal_type || '';
  const dSigCls = dSigType.includes('B') ? 'tag-up' : (dSigType.includes('S') ? 'tag-down' : 'tag-neutral');

  const m30IsUp = (idx.m30_trend||'').includes('上涨');
  const m30Cls = m30IsUp ? 'tag-up' : ((idx.m30_trend||'').includes('下跌') ? 'tag-down' : 'tag-neutral');
  const m30ActiveColor = m30IsUp ? '#f85149' : '#3fb950';
  const m30DoneColor = m30IsUp ? '#3fb950' : '#f85149';
  const m30TcText = (idx.m30_status||'').includes('疑似') ? `<span style="color:#d29922">⚠️疑似</span>`
    : ((idx.m30_status||'').includes('已确认') ? `<span style="color:${{m30DoneColor}}">✅完成</span>`
    : `<span style="color:${{m30ActiveColor}}">🔄进行</span>`);
  const m30SigType = idx.m30_signal_type || '';
  const m30SigCls = m30SigType.includes('B') ? 'tag-up' : (m30SigType.includes('S') ? 'tag-down' : 'tag-neutral');

  const conParts = (idx.conclusion||'-').split(' · ');
  const conHtml = conParts.map(p => {{
    let color = '#c9d1d9';
    if (p.includes('买点') || p.includes('加仓') || p.includes('满仓') || p.includes('多头共振')) color = '#f85149';
    else if (p.includes('卖点') || p.includes('清仓') || p.includes('减仓') || p.includes('空头共振')) color = '#3fb950';
    else if (p.startsWith('⚠')) color = '#d29922';
    return `<span style="color:${{color}}">• ${{p}}</span>`;
  }}).join(' ');

  bar.innerHTML = `
    <span style="background:${{scoreBg}};color:${{scoreClr}};padding:1px 6px;border-radius:3px;font-weight:700">${{sc}}</span>
    <span class="tag ${{tCls}}">${{(idx.trend||'-').replace('趋势','')}}</span> ${{tcText}}
    ${{idx.latest_signal && idx.latest_signal !== '-' ? '<span class="tag ' + dSigCls + '">' + idx.latest_signal + '</span>' : ''}}
    <span style="color:#484f58">|</span>
    <span class="tag ${{m30Cls}}">${{(idx.m30_trend||'-').replace('趋势','')}}</span> ${{m30TcText}}
    ${{idx.m30_signal && idx.m30_signal !== '-' ? '<span class="tag ' + m30SigCls + '">' + idx.m30_signal + '</span>' : ''}}
    <br><span style="font-size:11px;line-height:1.4">${{conHtml}}</span>
  `;
}}

function renderKline(data) {{
  const canvas = document.getElementById('klineCanvas');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const H = Math.min(rect.height || 320, 320);
  canvas.width = rect.width * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width;
  ctx.clearRect(0, 0, W, H);

  const dates = data.dates;
  const kline = data.kline;
  if (!dates.length) return;
  const n = viewEnd - viewStart;
  const pad = {{t: 16, b: 24, l: 50, r: 12}};
  const cw = (W - pad.l - pad.r) / n;

  const slicedK = kline.slice(viewStart, viewEnd);
  const highs = slicedK.map(k => k[3]);
  const lows = slicedK.map(k => k[2]);
  const maxP = Math.max(...highs) * 1.01;
  const minP = Math.min(...lows) * 0.99;
  const scaleY = p => pad.t + (maxP - p) / (maxP - minP) * (H - pad.t - pad.b);
  const scaleX = i => pad.l + (i - viewStart) * cw + cw / 2;

  // Grid
  ctx.strokeStyle = '#21262d'; ctx.lineWidth = 0.5;
  for (let i = 0; i < 5; i++) {{
    const y = pad.t + i * (H - pad.t - pad.b) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    const p = maxP - i * (maxP - minP) / 4;
    ctx.fillStyle = '#8b949e'; ctx.font = '10px monospace'; ctx.textAlign = 'right';
    ctx.fillText(p.toFixed(2), pad.l - 4, y + 3);
  }}

  const evoColors = {{'延伸': '#8b949e', '新生（上）': '#f85149', '新生（下）': '#3fb950', '扩展': '#d29922'}};
  // Stroke-level hubs (笔中枢, drawn behind candlesticks)
  data.hubs.forEach(h => {{
    if (h.x1 < viewStart || h.x0 >= viewEnd) return;
    const x0 = scaleX(Math.max(h.x0, viewStart)) - cw / 2;
    const x1 = scaleX(Math.min(h.x1, viewEnd - 1)) + cw / 2;
    ctx.fillStyle = 'rgba(88,166,255,0.08)';
    ctx.fillRect(x0, scaleY(h.zg), x1 - x0, scaleY(h.zd) - scaleY(h.zg));
    ctx.strokeStyle = 'rgba(88,166,255,0.4)'; ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(x0, scaleY(h.zg)); ctx.lineTo(x1, scaleY(h.zg)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x0, scaleY(h.zd)); ctx.lineTo(x1, scaleY(h.zd)); ctx.stroke();
    ctx.setLineDash([]);
    const evoClr = evoColors[h.evo] || '#58a6ff';
    ctx.fillStyle = evoClr; ctx.font = '8px sans-serif'; ctx.textAlign = 'left';
    ctx.fillText('枢' + (h.idx + 1) + (h.evo ? ' ' + h.evo : ''), x1 + 2, scaleY(h.zg) + 8);
    ctx.fillStyle = '#58a6ff';
    ctx.fillText('ZG=' + h.zg.toFixed(2), x1 + 2, scaleY(h.zg) + 17);
    ctx.fillText('ZD=' + h.zd.toFixed(2), x1 + 2, scaleY(h.zd) + 10);
  }});

  // Candlesticks
  for (let gi = viewStart; gi < viewEnd; gi++) {{
    const [o, c, lo, hi] = kline[gi];
    const x = scaleX(gi);
    const bw = Math.max(cw * 0.6, 1);
    const isUp = c >= o;
    ctx.strokeStyle = ctx.fillStyle = isUp ? '#f85149' : '#3fb950';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, scaleY(hi)); ctx.lineTo(x, scaleY(lo)); ctx.stroke();
    const top = scaleY(Math.max(o, c));
    const bot = scaleY(Math.min(o, c));
    ctx.fillRect(x - bw / 2, top, bw, Math.max(bot - top, 1));
  }}

  // Segments polyline (thick purple)
  if (data.segments && data.segments.length >= 2) {{
    ctx.strokeStyle = '#bc8cff'; ctx.lineWidth = 2.5;
    ctx.beginPath();
    let started = false;
    data.segments.forEach(pt => {{
      if (pt[0] < viewStart || pt[0] >= viewEnd) return;
      const x = scaleX(pt[0]); const y = scaleY(pt[1]);
      if (!started) {{ ctx.moveTo(x, y); started = true; }}
      else ctx.lineTo(x, y);
    }});
    ctx.stroke();
    data.segments.forEach(pt => {{
      if (pt[0] < viewStart || pt[0] >= viewEnd) return;
      const x = scaleX(pt[0]); const y = scaleY(pt[1]);
      ctx.fillStyle = '#bc8cff';
      ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill();
    }});
  }}

  // Segment labels
  (data.seg_labels || []).forEach(lb => {{
    if (lb.x < viewStart || lb.x >= viewEnd) return;
    ctx.fillStyle = '#bc8cff'; ctx.font = 'bold 9px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('D' + lb.idx, scaleX(lb.x), scaleY(lb.y) - 8);
  }});

  // Strokes
  ctx.strokeStyle = '#f0883e'; ctx.lineWidth = 1.5;
  data.strokes.forEach(s => {{
    const si = s.coords[0][0], ei = s.coords[1][0];
    if (ei < viewStart || si >= viewEnd) return;
    const x1 = scaleX(Math.max(si, viewStart));
    const x2 = scaleX(Math.min(ei, viewEnd - 1));
    ctx.beginPath(); ctx.moveTo(x1, scaleY(s.coords[0][1])); ctx.lineTo(x2, scaleY(s.coords[1][1])); ctx.stroke();
    ctx.fillStyle = '#d29922'; ctx.font = 'bold 8px sans-serif'; ctx.textAlign = 'center';
    const mx = (x1 + x2) / 2;
    ctx.fillText('S' + s.idx, mx, (scaleY(s.coords[0][1]) + scaleY(s.coords[1][1])) / 2 - 6);
  }});

  // Buy/Sell markers
  data.bsp.forEach(p => {{
    if (p.idx < viewStart || p.idx >= viewEnd) return;
    const x = scaleX(p.idx);
    const y = scaleY(p.price);
    const mIsInv = p.status === 'invalidated';
    const mIsPending = p.status === 'pending';
    const triColor = mIsInv ? '#484f58' : (mIsPending ? '#d29922' : (p.is_buy ? '#f85149' : '#3fb950'));
    ctx.globalAlpha = mIsInv ? 0.4 : 1.0;
    ctx.beginPath();
    if (p.is_buy) {{
      ctx.moveTo(x, y + 10); ctx.lineTo(x - 6, y + 18); ctx.lineTo(x + 6, y + 18); ctx.closePath();
      ctx.fillStyle = triColor; ctx.fill();
    }} else {{
      ctx.moveTo(x, y - 10); ctx.lineTo(x - 6, y - 18); ctx.lineTo(x + 6, y - 18); ctx.closePath();
      ctx.fillStyle = triColor; ctx.fill();
    }}
    ctx.fillStyle = mIsInv ? '#484f58' : (mIsPending ? '#d29922' : (p.is_buy ? '#f85149' : '#3fb950'));
    ctx.globalAlpha = mIsInv ? 0.4 : 1.0;
    ctx.font = 'bold 8px sans-serif'; ctx.textAlign = 'center';
    const mConfIcons = {{'high': '🔴', 'medium': '🟡', 'low': '⚪'}};
    let bspText = '#' + p.bsp_idx + ' ' + p.label.substring(0, 2);
    if (p.conf) bspText += (mConfIcons[p.conf] || '');
    if (mIsInv) bspText += '✗';
    else if (mIsPending) bspText += '⏳';
    ctx.fillText(bspText, x, p.is_buy ? y + 28 : y - 20);
    ctx.globalAlpha = 1.0;
    if (p.ranges && p.ranges.length >= 2) {{
      const r0 = p.ranges[0], r1 = p.ranges[1];
      const ratio = r0.area > 0 ? Math.round(r1.area / r0.area * 100) : 0;
      ctx.font = '7px sans-serif';
      ctx.fillText(r0.label + '↔' + r1.label + ' 背驰 ' + ratio + '%', x, p.is_buy ? y + 36 : y - 12);
    }}
  }});

  // X-axis dates
  ctx.fillStyle = '#8b949e'; ctx.font = '9px monospace'; ctx.textAlign = 'center';
  const step = Math.max(Math.floor(n / 6), 1);
  for (let i = viewStart; i < viewEnd; i += step) {{
    const lbl = dates[i].substring(5);
    ctx.fillText(lbl, scaleX(i), H - 6);
  }}
}}

function renderMACD(data) {{
  const canvas = document.getElementById('macdCanvas');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const H = Math.min(rect.height || 120, 120);
  canvas.width = rect.width * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width;
  ctx.clearRect(0, 0, W, H);

  const n = viewEnd - viewStart;
  if (!n) return;
  const pad = {{t: 8, b: 16, l: 50, r: 12}};
  const cw = (W - pad.l - pad.r) / n;
  const macds = data.macd_hist.slice(viewStart, viewEnd);
  const difs = data.dif.slice(viewStart, viewEnd);
  const deas = data.dea.slice(viewStart, viewEnd);
  const allVals = [...macds, ...difs, ...deas];
  const maxV = Math.max(...allVals.map(Math.abs)) * 1.1 || 1;
  const scaleY = v => pad.t + (maxV - v) / (2 * maxV) * (H - pad.t - pad.b);
  const scaleXi = i => pad.l + i * cw + cw / 2;
  const zeroY = scaleY(0);

  ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(W - pad.r, zeroY); ctx.stroke();
  ctx.fillStyle = '#8b949e'; ctx.font = '9px monospace'; ctx.textAlign = 'right';
  ctx.fillText('0', pad.l - 4, zeroY + 3);

  macds.forEach((m, i) => {{
    const x = scaleXi(i); const bw = Math.max(cw * 0.5, 1);
    ctx.fillStyle = m >= 0 ? '#f85149' : '#3fb950';
    const y1 = zeroY, y2 = scaleY(m);
    ctx.fillRect(x - bw / 2, Math.min(y1, y2), bw, Math.abs(y2 - y1) || 1);
  }});

  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1.2;
  ctx.beginPath();
  difs.forEach((v, i) => {{ const x = scaleXi(i); const y = scaleY(v); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); }});
  ctx.stroke();

  ctx.strokeStyle = '#f0883e'; ctx.lineWidth = 1.2;
  ctx.beginPath();
  deas.forEach((v, i) => {{ const x = scaleXi(i); const y = scaleY(v); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); }});
  ctx.stroke();
}}

function updateSynthesisPanel() {{
  const panel = document.getElementById('synthesis-panel');
  const syn = SYNTHESIS[currentIndex];
  if (!syn) {{ panel.innerHTML = ''; return; }}

  let html = '<h3>多级别联立分析</h3>';
  const alignCls = syn.alignment.includes('共振') ? 'tag-up' : (syn.alignment.includes('分歧') ? 'tag-down' : 'tag-neutral');
  const biasCls = syn.bias.includes('多') ? 'tag-up' : (syn.bias.includes('空') ? 'tag-down' : 'tag-neutral');
  html += `<span class="tag ${{alignCls}}">${{syn.alignment}}</span>`;
  html += `<span class="tag ${{biasCls}}">${{syn.bias}}</span>`;
  html += `<span style="color:#8b949e;font-size:12px"> ${{syn.advice}}</span>`;

  html += '<table class="signal-table" style="margin-top:6px"><thead><tr>';
  html += '<th>级别</th><th>走势</th><th>状态</th><th>中枢位</th><th>DIF</th><th>枢数</th><th>信号</th><th>最新</th>';
  html += '</tr></thead><tbody>';
  (syn.levels || []).forEach(lv => {{
    const tCls = (lv.trend||'').includes('上涨') ? 'sig-buy' : ((lv.trend||'').includes('下跌') ? 'sig-sell' : '');
    const hpCls = (lv.hub_position||'').includes('上方') ? 'sig-buy' : ((lv.hub_position||'').includes('下方') ? 'sig-sell' : '');
    const sigStr = lv.latest_signal ? lv.latest_signal.label : '-';
    const tcIcons = {{'进行中': '🔄', '疑似完成': '⚠️', '已确认完成': '✅'}};
    const tcStr = lv.trend_completion ? (tcIcons[lv.trend_completion]||'') + lv.trend_completion : '-';
    html += `<tr><td style="font-weight:bold">${{lv.level}}</td>`;
    html += `<td class="${{tCls}}">${{lv.trend||'-'}}</td>`;
    html += `<td title="${{lv.completion_reason||''}}">${{tcStr}}</td>`;
    html += `<td class="${{hpCls}}">${{lv.hub_position||'-'}}</td>`;
    html += `<td>${{lv.dif_zone === 'above_zero' ? '0轴上' : '0轴下'}}</td>`;
    html += `<td>${{lv.num_hubs}}</td><td>${{lv.num_signals}}</td><td>${{sigStr}}</td></tr>`;
  }});
  html += '</tbody></table>';

  if (syn.resonance && syn.resonance.length > 0) {{
    html += `<h4 style="color:#f0883e">跨级别共振 (${{syn.resonance.length}})</h4>`;
    html += '<ul style="padding-left:14px;font-size:12px;margin:4px 0">';
    syn.resonance.forEach(r => {{
      html += `<li>${{r.direction==='buy'?'🔺':'🔻'}} ${{r.date}} — ${{r.note}}</li>`;
    }});
    html += '</ul>';
  }}

  if (syn.enriched && syn.enriched.length > 0) {{
    html += `<h4 style="color:#d2a8ff">置信度调整 (${{syn.enriched.length}})</h4>`;
    html += '<ul style="padding-left:14px;font-size:12px;margin:4px 0">';
    syn.enriched.forEach(s => {{
      const arrow = s.adjusted_confidence === 'high' || (s.adjusted_confidence === 'medium' && s.original_confidence === 'low') ? '↑' : '↓';
      html += `<li>${{s.source_level}}-${{s.label}} @ ${{s.dt}}: ${{s.original_confidence}}${{arrow}}${{s.adjusted_confidence}} (${{s.context_note}})</li>`;
    }});
    html += '</ul>';
  }}

  if (syn.interval_nests && syn.interval_nests.length > 0) {{
    const deep = syn.interval_nests.filter(n => n.depth >= 2);
    html += `<h4 style="color:#58a6ff">区间套精确定位 (${{syn.interval_nests.length}}段, ${{deep.length}}嵌套)</h4>`;
    syn.interval_nests.forEach((n, i) => {{
      const dirIcon = n.direction === -1 ? '🟢买' : '🔴卖';
      const typeStr = n.big_type === 'trend' ? '趋势' : '盘整';
      const stars = '★'.repeat(n.depth);
      html += `<div style="margin:6px 0;padding:8px;background:#0d1117;border-radius:6px;border-left:3px solid ${{n.depth>=2?'#58a6ff':'#484f58'}};font-size:12px">`;
      html += `<div style="font-weight:bold">${{stars}} ${{i+1}}. ${{typeStr}}${{dirIcon}} 深度${{n.depth}}</div>`;
      html += `<div style="color:#8b949e">大: ${{n.big_level}} ${{n.big_dt}} [${{n.big_range[0]}}~${{n.big_range[1]}}]</div>`;
      if (n.mid_level) html += `<div style="color:#8b949e">中: ${{n.mid_level}} ${{n.mid_dt}}</div>`;
      if (n.small_level) html += `<div style="color:#8b949e">小: ${{n.small_level}} ${{n.small_dt}}</div>`;
      const priceStr = n.precision_price ? ` ¥${{n.precision_price.toFixed(2)}}` : '';
      html += `<div style="color:#58a6ff;font-weight:bold">精确: ${{n.precision_dt}}${{priceStr}}</div>`;
      if (n.note) html += `<div style="color:#484f58;font-size:11px">${{n.note}}</div>`;
      html += '</div>';
    }});
  }}

  panel.innerHTML = html;
}}

function updateSignalPanel(data) {{
  const panel = document.getElementById('signal-panel');
  if (!data.bsp || data.bsp.length === 0) {{
    panel.innerHTML = '<h3>买卖点信号：无</h3>';
    return;
  }}
  const sorted = [...data.bsp].sort((a, b) => b.idx - a.idx);
  const mActiveCount = sorted.filter(p => p.status !== 'invalidated').length;
  const mInvCount = sorted.length - mActiveCount;
  const mCountLabel = mInvCount > 0 ? sorted.length + ' 个，' + mInvCount + ' 已失效' : sorted.length + ' 个';
  let html = '<h3>买卖点信号（' + mCountLabel + '）</h3>';
  html += '<table class="signal-table"><thead><tr>';
  html += '<th>#</th><th>日期</th><th>位</th><th>类型</th><th>状态</th><th>价格</th><th>强弱</th><th>仓位</th><th>信心</th><th>狼</th><th>依据</th><th>面积</th>';
  html += '</tr></thead><tbody>';
  sorted.forEach(p => {{
    const cls = p.is_buy ? 'sig-buy' : 'sig-sell';
    const confCls = p.conf === 'high' ? 'conf-high' : (p.conf === 'medium' ? 'conf-medium' : 'conf-low');
    const confLabel = p.conf === 'high' ? '高' : (p.conf === 'medium' ? '中' : '低');
    let locStr = '';
    if (p.stroke_idx >= 0) {{ locStr = 'S' + p.stroke_idx; if (p.seg_idx >= 0) locStr += '/D' + p.seg_idx; }}
    const wolfStr = p.wolf ? '<span style="color:#d29922">⚠</span>' : '<span style="color:#3fb950">✓</span>';
    const strMap = {{strongest: '🔥', strong: '💪', standard: '📌', weak: '⚠'}};
    const strStr = p.strength ? (strMap[p.strength]||p.strength) : '-';
    let areaStr = '-';
    if (p.ranges && p.ranges.length >= 2) {{
      const r0 = p.ranges[0], r1 = p.ranges[1];
      const ratio = r0.area > 0 ? (r1.area / r0.area * 100).toFixed(0) : '-';
      areaStr = r0.label + '=' + r0.area + ' vs ' + r1.label + '=' + r1.area + ' (' + ratio + '%)';
    }}
    const posStr = p.pos_advice ? p.pos_advice.split(' — ')[0] : '-';
    const mInv = p.status === 'invalidated';
    const mPending = p.status === 'pending';
    const mRowStyle = mInv ? ' style="opacity:0.45"' : '';
    const mIsBuyType = p.is_buy;
    const mConfClr = mIsBuyType ? '#f85149' : '#3fb950';
    const mStatusCell = mInv
      ? '<td style="color:#da3633;font-size:10px">✗</td>'
      : mPending
        ? '<td style="color:#d29922;font-size:10px">⏳</td>'
        : '<td style="color:' + mConfClr + ';font-size:10px">✓</td>';
    html += '<tr' + mRowStyle + '>';
    html += '<td style="color:#484f58;font-weight:bold">#' + p.bsp_idx + '</td>';
    html += '<td>' + data.dates[p.idx] + '</td>';
    html += '<td style="color:#d2a8ff">' + locStr + '</td>';
    html += '<td class="' + cls + '">' + p.label + '</td>';
    html += mStatusCell;
    html += '<td>' + p.price.toFixed(3) + '</td>';
    html += '<td>' + strStr + '</td>';
    html += `<td title="${{p.pos_advice||''}}" style="color:#f0883e">${{posStr}}</td>`;
    html += '<td class="' + confCls + '">' + confLabel + '</td>';
    html += '<td>' + wolfStr + '</td>';
    html += '<td style="max-width:120px;overflow:hidden;text-overflow:ellipsis">' + p.desc + '</td>';
    html += '<td style="max-width:100px;overflow:hidden;text-overflow:ellipsis">' + areaStr + '</td>';
    html += '</tr>';
  }});
  html += '</tbody></table>';
  panel.innerHTML = html;
}}

// === Touch & Mouse Zoom/Pan ===
function setupInteraction() {{
  const kCanvas = document.getElementById('klineCanvas');
  const mCanvas = document.getElementById('macdCanvas');
  function handleWheel(e) {{
    e.preventDefault();
    const d = getData(); if (!d) return;
    const total = d.dates.length;
    const range = viewEnd - viewStart;
    const rect = kCanvas.getBoundingClientRect();
    const mouseX = (e.clientX - rect.left) / rect.width;
    const zoomFactor = e.deltaY > 0 ? 1.15 : 0.87;
    const newRange = Math.round(Math.max(MIN_VIEW, Math.min(total, range * zoomFactor)));
    const pivot = viewStart + Math.round(mouseX * range);
    viewStart = Math.round(pivot - mouseX * newRange);
    viewEnd = viewStart + newRange;
    clampView(total); render();
  }}
  function handleMouseDown(e) {{ isDragging = true; dragStartX = e.clientX; dragStartView = viewStart; kCanvas.style.cursor = 'grabbing'; }}
  function handleMouseMove(e) {{
    if (!isDragging) return;
    const d = getData(); if (!d) return;
    const total = d.dates.length;
    const rect = kCanvas.getBoundingClientRect();
    const range = viewEnd - viewStart;
    const dx = e.clientX - dragStartX;
    const shift = Math.round(-dx / rect.width * range);
    viewStart = dragStartView + shift; viewEnd = viewStart + range;
    clampView(total); render();
  }}
  function handleMouseUp() {{ isDragging = false; kCanvas.style.cursor = 'crosshair'; }}
  function handleTouchStart(e) {{
    if (e.touches.length === 2) {{
      pinchStartDist = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
      pinchStartRange = viewEnd - viewStart;
    }} else if (e.touches.length === 1) {{ isDragging = true; dragStartX = e.touches[0].clientX; dragStartView = viewStart; }}
  }}
  function handleTouchMove(e) {{
    e.preventDefault();
    const d = getData(); if (!d) return;
    const total = d.dates.length;
    if (e.touches.length === 2) {{
      const dist = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
      const scale = pinchStartDist / dist;
      const newRange = Math.round(Math.max(MIN_VIEW, Math.min(total, pinchStartRange * scale)));
      const mid = Math.round((viewStart + viewEnd) / 2);
      viewStart = Math.round(mid - newRange / 2); viewEnd = viewStart + newRange;
      clampView(total); render();
    }} else if (e.touches.length === 1 && isDragging) {{
      const rect = kCanvas.getBoundingClientRect();
      const range = viewEnd - viewStart;
      const dx = e.touches[0].clientX - dragStartX;
      const shift = Math.round(-dx / rect.width * range);
      viewStart = dragStartView + shift; viewEnd = viewStart + range;
      clampView(total); render();
    }}
  }}
  function handleTouchEnd() {{ isDragging = false; }}
  [kCanvas, mCanvas].forEach(c => {{ c.addEventListener('wheel', handleWheel, {{passive: false}}); c.style.cursor = 'crosshair'; c.style.touchAction = 'none'; }});
  kCanvas.addEventListener('mousedown', handleMouseDown);
  document.addEventListener('mousemove', handleMouseMove);
  document.addEventListener('mouseup', handleMouseUp);
  kCanvas.addEventListener('touchstart', handleTouchStart, {{passive: false}});
  kCanvas.addEventListener('touchmove', handleTouchMove, {{passive: false}});
  kCanvas.addEventListener('touchend', handleTouchEnd);
  kCanvas.addEventListener('dblclick', () => {{ resetView(); render(); }});
}}

window.addEventListener('load', async () => {{ await loadAndRender(); setupInteraction(); }});
window.addEventListener('resize', render);
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(mobile_html)

    print(f"\nMobile dashboard saved to: {output_path}")
    size_kb = os.path.getsize(output_path) / 1024
    print(f"File size: {size_kb:.0f} KB")
    return output_path


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate Chanlun Dashboard HTML")
    parser.add_argument("--data-dir", default=None, help="Data directory")
    parser.add_argument("--output", default=None, help="Output HTML path")
    parser.add_argument("--mobile", action="store_true", help="Generate mobile version")
    args = parser.parse_args()

    print("=" * 60)
    print("缠论交易系统 v2 — 可视化仪表盘生成")
    print("=" * 60)

    if args.mobile:
        generate_mobile_dashboard(data_dir=args.data_dir, output_path=args.output)
    else:
        generate_dashboard(data_dir=args.data_dir, output_path=args.output)


if __name__ == "__main__":
    main()
