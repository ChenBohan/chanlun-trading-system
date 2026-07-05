"""
Chanlun Trading System v2 - Visualization Module.

Generates a self-contained HTML dashboard with ECharts showing:
  - Candlestick K-line charts
  - Strokes (笔) as connected lines
  - Hubs (中枢) as shaded rectangles
  - Buy/Sell points as markers
  - MACD histogram subplot
  - Segments (线段) as thick lines

Supports 8 indices × 4 timeframes with tab switching.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .data_fetcher import load_index_watchlist, IndexConfig, _PROJECT_ROOT
from .chanlun_engine import (
    load_bars_from_csv, analyze, AnalysisResult, MultiLevelSynthesis,
    RawBar, Stroke, Hub, BuySellPoint, Segment,
    synthesize_multi_level,
)

_TZ_CHINA = timezone(timedelta(hours=8))

_SNAPSHOT_FILE = os.path.join(_PROJECT_ROOT, "data", "signal_snapshots.jsonl")
_SNAPSHOT_PRUNE_DAYS = 0       # 0 = keep forever

_BASELINE_FILE = ".baseline.json"


def save_deploy_baseline(data_out_dir: str, all_data: dict) -> None:
    """Save bar counts and last dates for each key as the full-deploy baseline.

    Called at full deploy time. Delta deploys compare against this to produce live.js.
    """
    bars_info = {}
    for key, d in all_data.items():
        dates = d.get("dates", [])
        bars_info[key] = len(dates)
    last_dates = {}
    for key, d in all_data.items():
        dates = d.get("dates", [])
        if dates:
            last_dates[key] = dates[-1]
    baseline = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "bars": bars_info,
        "last_dates": last_dates,
    }
    path = os.path.join(data_out_dir, _BASELINE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False)


def generate_live_js(data_out_dir: str, all_data: dict) -> Optional[str]:
    """Generate live.js containing only bars added since the last full deploy.

    Returns the path to live.js, or None if no baseline exists.
    The file uses the global LIVE_DATA variable which the frontend merges
    with the static per-symbol .js files.
    """
    baseline_path = os.path.join(data_out_dir, _BASELINE_FILE)
    if not os.path.exists(baseline_path):
        return None

    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    base_bars = baseline.get("bars", {})
    base_last_dates = baseline.get("last_dates", {})
    base_time_str = baseline.get("time", "")[:10]  # e.g. "2026-06-04"
    live_entries = {}
    array_fields = ("dates", "kline", "volumes", "macd_hist", "dif", "dea", "ma5", "ma10", "ma250")
    analysis_fields = ("bsp", "strokes", "segments", "seg_labels", "fractals",
                       "hubs", "trend", "hub_position", "hub_detail",
                       "trend_completion", "volume_profile", "tentative")

    for key, data in all_data.items():
        base_n = base_bars.get(key, 0)
        cur_dates = data.get("dates", [])
        cur_n = len(cur_dates)
        cur_last = cur_dates[-1] if cur_dates else ""
        base_last = base_last_dates.get(key, "")
        # Legacy baseline without last_dates: infer from baseline creation date
        if not base_last and "_daily" in key and base_time_str:
            base_last = base_time_str

        has_new_bars = cur_n > base_n
        # Sliding window: bar count unchanged but content shifted (e.g. daily max_bars)
        window_shifted = (cur_n == base_n > 0 and base_last and cur_last != base_last)

        if not has_new_bars and not window_shifted:
            continue

        if window_shifted:
            # Sliding window: bar count unchanged but dates shifted.
            # Find overlap by locating base_last in current dates, then
            # send a compact drop_head + append delta instead of full replace.
            try:
                bl_idx = cur_dates.index(base_last)
            except ValueError:
                bl_idx = -1

            if bl_idx >= 0 and bl_idx < cur_n - 1:
                drop = cur_n - 1 - bl_idx
                entry: dict = {"drop_head": drop}
                for field in array_fields:
                    arr = data.get(field, [])
                    entry[field] = arr[bl_idx + 1:] if len(arr) > bl_idx + 1 else []
            else:
                # base_last not found or at end → full replace
                entry = {"full_replace": True}
                for field in array_fields:
                    entry[field] = data.get(field, [])
            # Include all analysis fields (bsp, strokes, hubs, etc.)
            for field in analysis_fields:
                if field in data:
                    entry[field] = data[field]
        else:
            # Incremental: append new bars
            entry = {"base_len": base_n}
            for field in array_fields:
                arr = data.get(field, [])
                entry[field] = arr[base_n:] if len(arr) > base_n else []
            # Include analysis fields when data in new range exists
            bsp_list = data.get("bsp", [])
            strokes_list = data.get("strokes", [])
            bsp_changed = any(p.get("idx", 0) >= base_n for p in bsp_list)
            strokes_changed = any(
                s.get("coords", [[0]])[1][0] >= base_n
                for s in strokes_list if len(s.get("coords", [])) >= 2
            )
            if bsp_changed or strokes_changed:
                for field in analysis_fields:
                    if field in data:
                        entry[field] = data[field]

        live_entries[key] = entry

    if not live_entries:
        return None

    live_js_path = os.path.join(data_out_dir, "live.js")
    content = json.dumps(live_entries, ensure_ascii=False, separators=(",", ":"))
    with open(live_js_path, "w", encoding="utf-8") as f:
        f.write(f"var LIVE_DATA={content};\n")

    size_kb = os.path.getsize(live_js_path) / 1024
    print(f"  live.js: {size_kb:.0f} KB ({len(live_entries)} keys)")
    return live_js_path


def generate_live_js_from_cache(cache: dict, data_dir: str = None) -> Optional[str]:
    """Generate live.js from analysis cache without rewriting main .js files.

    This is the intraday mode: uses the existing deploy baseline to compute
    the delta, and writes only live.js. The main per-symbol .js files and
    baseline are left untouched.

    Returns path to live.js, or None if no baseline exists.
    """
    if data_dir is None:
        data_dir = os.path.join(_PROJECT_ROOT, "reports", "data")

    baseline_path = os.path.join(data_dir, _BASELINE_FILE)
    if not os.path.exists(baseline_path):
        return None

    all_data = cache.get("all_data", {})
    if not all_data:
        return None

    return generate_live_js(data_dir, all_data)
_SNAPSHOT_DISPLAY_DAYS = 5     # only show disappeared signals from last N days in dashboard


def _signal_key(sig: dict) -> str:
    """Unique key for deduplication: code + level + type + datetime."""
    return f"{sig['etf_code']}|{sig['level_key']}|{sig['type']}|{sig['dt']}"


def _load_snapshots() -> dict[str, dict]:
    """Load existing signal snapshots from JSONL, keyed by signal_key."""
    result: dict[str, dict] = {}
    if not os.path.exists(_SNAPSHOT_FILE):
        return result
    with open(_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                key = _signal_key(entry)
                result[key] = entry
            except (json.JSONDecodeError, KeyError):
                continue
    return result


def _save_snapshots(snapshots: dict[str, dict]) -> None:
    """Save snapshots back to JSONL. Prune if _SNAPSHOT_PRUNE_DAYS > 0."""
    os.makedirs(os.path.dirname(_SNAPSHOT_FILE), exist_ok=True)
    cutoff = ""
    if _SNAPSHOT_PRUNE_DAYS > 0:
        cutoff = (datetime.now(_TZ_CHINA) - timedelta(days=_SNAPSHOT_PRUNE_DAYS)).strftime("%Y-%m-%d")
    with open(_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        for entry in sorted(snapshots.values(), key=lambda x: x.get("dt", ""), reverse=True):
            if cutoff and entry.get("first_seen", "")[:10] < cutoff and entry.get("source") == "snapshot":
                continue
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def update_signal_snapshots(live_signals: list[dict]) -> list[dict]:
    """Compare live signals with stored snapshots, persist new ones, return merged list.

    Returns all signals: live ones (source="live") + historical snapshot-only ones
    (source="snapshot") that no longer appear in the current analysis.
    """
    now_str = datetime.now(_TZ_CHINA).strftime("%Y-%m-%d %H:%M")
    existing = _load_snapshots()

    live_keys = set()
    for sig in live_signals:
        key = _signal_key(sig)
        live_keys.add(key)
        if key in existing:
            existing[key]["last_seen"] = now_str
            existing[key]["source"] = "live"
            for field in ("conf", "conf_score", "strength", "str_score",
                          "area_cmp", "status", "inv_reason", "price"):
                if field in sig:
                    existing[key][field] = sig[field]
        else:
            entry = dict(sig)
            entry["first_seen"] = now_str
            entry["last_seen"] = now_str
            entry["source"] = "live"
            existing[key] = entry

    for key, entry in existing.items():
        if key not in live_keys and entry.get("source") != "snapshot":
            entry["source"] = "snapshot"

    _save_snapshots(existing)

    merged: list[dict] = []
    for sig in live_signals:
        s = dict(sig)
        s["source"] = "live"
        merged.append(s)

    today = datetime.now(_TZ_CHINA).strftime("%Y-%m-%d")
    cutoff_recent = (datetime.now(_TZ_CHINA) - timedelta(days=_SNAPSHOT_DISPLAY_DAYS)).strftime("%Y-%m-%d")
    for key, entry in existing.items():
        if key in live_keys:
            continue
        if entry.get("source") != "snapshot":
            continue
        if entry.get("dt", "")[:10] < cutoff_recent:
            continue
        s = dict(entry)
        s["source"] = "snapshot"
        merged.append(s)

    merged.sort(key=lambda x: x.get("dt", ""), reverse=True)
    return merged


def _inject_snapshot_bsp(all_data: dict) -> int:
    """Inject recent snapshot signals into chart bsp arrays so charts match monitoring.

    Only injects signals from the last _SNAPSHOT_DISPLAY_DAYS days, and only
    types that appear in the monitoring panel (1B/1S/2B/2S/3B/3S).
    Returns the number of snapshot markers injected.
    """
    snapshots = _load_snapshots()
    if not snapshots:
        return 0

    cutoff = (datetime.now(_TZ_CHINA) - timedelta(days=_SNAPSHOT_DISPLAY_DAYS)).strftime("%Y-%m-%d")
    valid_types = {"1B", "1S", "2B", "2S", "3B", "3S"}

    by_key: dict[str, list[dict]] = {}
    for sig in snapshots.values():
        if sig.get("dt", "")[:10] < cutoff:
            continue
        if sig.get("type") not in valid_types:
            continue
        chart_key = f"{sig['etf_code']}_{sig['level_key']}"
        by_key.setdefault(chart_key, []).append(sig)

    injected = 0
    for chart_key, sigs in by_key.items():
        data = all_data.get(chart_key)
        if not data:
            continue
        dates = data.get("dates", [])
        if not dates:
            continue
        dt_index = {d: i for i, d in enumerate(dates)}
        existing_idx_type = {(p["idx"], p["type"]) for p in data.get("bsp", [])}

        for sig in sigs:
            dt = sig.get("dt", "")
            idx = dt_index.get(dt)
            if idx is None:
                continue
            if (idx, sig["type"]) in existing_idx_type:
                continue
            is_buy = sig["type"] in ("1B", "2B", "3B")
            marker = {
                "idx": idx, "bsp_idx": -1,
                "price": sig["price"], "type": sig["type"],
                "label": sig["label"],
                "desc": f"[快照] {sig.get('area_cmp', '')}",
                "conf": sig.get("conf", "low"),
                "is_buy": is_buy,
                "stroke_idx": -1, "seg_idx": -1,
                "wolf": "", "zone": "",
                "strength": sig.get("strength", ""),
                "str_score": sig.get("str_score", 0),
                "str_details": [],
                "conf_score": sig.get("conf_score", 0),
                "conf_details": [],
                "pos_advice": "",
                "status": sig.get("status", "active"),
                "inv_reason": sig.get("inv_reason", ""),
                "hub_rank": sig.get("hub_rank", -1),
                "hub_width": sig.get("hub_width", 0),
                "hub_idx": -1, "inv_price": 0,
                "signal_level": sig.get("signal_level", ""),
                "source": "snapshot",
                "dt": dt,
            }
            data["bsp"].append(marker)
            injected += 1

    return injected


def backfill_signal_snapshots(data_dir: str = None, max_workers: int = 8) -> int:
    """Replay historical K-line data to recover all buy/sell points that ever existed.

    For each symbol × level, analyzes data at expanding windows (one trading day
    at a time) and records every unique signal discovered. This populates the
    snapshot file with historical signals that would otherwise be lost.

    Returns total number of unique signals discovered.
    """
    from .data_fetcher import load_index_watchlist
    if data_dir is None:
        data_dir = os.path.join(_PROJECT_ROOT, "data")

    indices = load_index_watchlist()
    level_labels = {"weekly": "WF", "daily": "DF", "30min": "30F", "5min": "5F", "1min": "1F"}
    step_sizes = {"weekly": 10, "daily": 20, "30min": 8, "5min": 48, "1min": 120}
    min_bars = 200

    tasks = []
    for idx in indices:
        sym_dir = os.path.join(data_dir, f"{idx.etf_code}_{idx.etf_name}")
        if not os.path.isdir(sym_dir):
            continue
        for level_key in ["weekly", "daily", "30min", "5min", "1min"]:
            csv_path = os.path.join(sym_dir, f"{level_key}.csv")
            if os.path.isfile(csv_path):
                tasks.append((idx.etf_code, idx.etf_name, level_key, csv_path,
                              step_sizes[level_key], min_bars))

    print(f"Backfill: {len(tasks)} tasks ({len(indices)} symbols × 5 levels)")
    print(f"Step sizes: WF={step_sizes['weekly']}, DF={step_sizes['daily']}, "
          f"30F={step_sizes['30min']}, 5F={step_sizes['5min']}, 1F={step_sizes['1min']}")

    all_discovered: dict[str, dict] = {}
    done = 0

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_backfill_one, *t): t for t in tasks}
        for fut in as_completed(futures):
            done += 1
            signals = fut.result()
            for sig in signals:
                key = _signal_key(sig)
                if key not in all_discovered or sig.get("first_seen", "") < all_discovered[key].get("first_seen", "z"):
                    all_discovered[key] = sig
            if done % 50 == 0 or done == len(tasks):
                print(f"  [{done}/{len(tasks)}] discovered {len(all_discovered)} unique signals so far")

    existing = _load_snapshots()
    merged_count = 0
    for key, sig in all_discovered.items():
        if key not in existing:
            existing[key] = sig
            merged_count += 1
        else:
            if sig.get("first_seen", "z") < existing[key].get("first_seen", "z"):
                existing[key]["first_seen"] = sig["first_seen"]

    _save_snapshots(existing)
    print(f"Backfill complete: {len(all_discovered)} signals discovered, "
          f"{merged_count} new entries merged. Total: {len(existing)}")
    return len(all_discovered)


def _backfill_one(etf_code: str, etf_name: str, level_key: str,
                  csv_path: str, step: int, min_bars: int) -> list[dict]:
    """Backfill signals for one symbol × one level (runs in subprocess)."""
    bars = load_bars_from_csv(csv_path)
    if len(bars) < min_bars:
        return []

    level_cn = {"weekly": "WF", "daily": "DF", "30min": "30F", "5min": "5F", "1min": "1F"}.get(level_key, level_key)
    all_seen: dict[str, dict] = {}

    for end_idx in range(min_bars, len(bars) + 1, step):
        sub = bars[:end_idx]
        try:
            result = analyze(sub, level_key)
        except Exception:
            continue
        last_dt = sub[-1].dt
        for p in result.buy_sell_points:
            if p.type in ("PB", "PS"):
                continue
            if p.type in ("3B", "3S") and getattr(p, "strength", "") == "weak":
                continue
            sig_key = f"{etf_code}|{level_key}|{p.type}|{p.dt}"
            if sig_key not in all_seen:
                entry = {
                    "etf_code": etf_code, "etf_name": etf_name,
                    "level": level_cn, "level_key": level_key,
                    "type": p.type, "label": p.label,
                    "dt": p.dt, "price": p.price,
                    "conf": getattr(p, "confidence", ""),
                    "strength": getattr(p, "strength", ""),
                    "status": "active", "hub_rank": getattr(p, "hub_rank", -1),
                    "first_seen": last_dt, "last_seen": last_dt,
                    "source": "backfill",
                }
                all_seen[sig_key] = entry
            else:
                all_seen[sig_key]["last_seen"] = last_dt

    final = []
    last_run = analyze(bars, level_key)
    last_keys = set()
    for p in last_run.buy_sell_points:
        last_keys.add(f"{etf_code}|{level_key}|{p.type}|{p.dt}")
    for key, sig in all_seen.items():
        if key not in last_keys:
            sig["source"] = "snapshot"
        else:
            sig["source"] = "live"
        final.append(sig)
    return final


def _is_ashare_market_open() -> bool:
    """Check if A-share market is currently in trading hours (Beijing time)."""
    now = datetime.now(_TZ_CHINA)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= t < 11 * 60 + 30) or (13 * 60 <= t < 15 * 60)


def _is_daily_bar_tentative() -> bool:
    """For daily K-line: bar is tentative if today is a trading day and market hasn't closed (before 15:00)."""
    now = datetime.now(_TZ_CHINA)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return t < 15 * 60


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
    closes = []

    for b in bars:
        dates.append(b.dt)
        kline_data.append([round(b.open, 3), round(b.close, 3),
                           round(b.low, 3), round(b.high, 3)])
        volumes.append(b.volume)
        macd_hist.append(round(b.macd_hist, 4))
        dif_line.append(round(b.dif, 4))
        dea_line.append(round(b.dea, 4))
        closes.append(b.close)

    # MA5 / MA10 / MA250
    ma5 = []
    ma10 = []
    ma250 = []
    for i in range(len(closes)):
        if i < 4:
            ma5.append(None)
        else:
            ma5.append(round(sum(closes[i-4:i+1]) / 5, 3))
        if i < 9:
            ma10.append(None)
        else:
            ma10.append(round(sum(closes[i-9:i+1]) / 10, 3))
        if i < 249:
            ma250.append(None)
        else:
            ma250.append(round(sum(closes[i-249:i+1]) / 250, 3))

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
                "vol_trend": s.volume_trend,
                "div": s.divergence,
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
    all_hubs = result.hubs
    for i, h in enumerate(all_hubs):
        si = dt_index.get(h.start_dt)
        ei = dt_index.get(h.end_dt)
        if si is not None and ei is not None:
            # Hub color direction: prefer sequence direction (midpoint shift)
            # over context_direction (entry stroke), because in chained hubs
            # the exit-becomes-entry pattern yields misleading UP bounces in
            # downtrends (and vice versa).  Fall back to context_direction
            # only for the first hub where no sequence direction exists.
            if h.direction == "上":
                hub_dir = 1
            elif h.direction == "下":
                hub_dir = -1
            else:
                hub_dir = h.context_direction
            hub_rects.append({
                "x0": si, "x1": ei,
                "zg": h.zg, "zd": h.zd,
                "gg": h.gg, "dd": h.dd,
                "idx": h.idx,
                "evo": h.evolution_type,
                "vol_trend": h.volume_trend,
                "hub_level": h.hub_level,
                "is_merged": h.is_merged,
                "duration_bars": h.duration_bars,
                "dir": hub_dir,
                "direction": h.direction,
                "trend_seq": h.trend_seq,
            })

    # Segment-level hubs as rectangles (larger level)
    seg_hub_rects = []
    for i, sh in enumerate(result.seg_hubs):
        si = dt_index.get(sh.start_dt)
        ei = dt_index.get(sh.end_dt)
        if si is not None and ei is not None:
            if sh.direction == "上":
                seg_dir = 1
            elif sh.direction == "下":
                seg_dir = -1
            else:
                seg_dir = sh.context_direction
            seg_hub_rects.append({
                "x0": si, "x1": ei,
                "zg": sh.zg, "zd": sh.zd,
                "gg": sh.gg, "dd": sh.dd,
                "idx": sh.idx,
                "evo": sh.evolution_type,
                "hub_level": sh.hub_level,
                "direction": sh.direction,
                "trend_seq": sh.trend_seq,
                "dir": seg_dir,
            })

    # Build fractal merge map: fractal_dt → list of raw bar indices (if merged)
    fractal_merge = {}
    for f in result.fractals:
        if f.mk_idx < len(result.merged_bars):
            mb = result.merged_bars[f.mk_idx]
            if len(mb.dates) > 1:
                raw_idxs = [dt_index.get(d) for d in mb.dates]
                raw_idxs = [x for x in raw_idxs if x is not None]
                if raw_idxs:
                    fractal_merge[f.dt] = raw_idxs

    # Fractal markers: only keep last N fractals to avoid clutter
    _FRACTAL_TAIL = 8
    fractal_markers = []
    for f in result.fractals[-_FRACTAL_TAIL:]:
        fi = dt_index.get(f.dt)
        if fi is not None:
            fractal_markers.append({
                "idx": fi,
                "type": f.type,
                "price": f.high if f.type == "top" else f.low,
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
                "str_score": p.strength_score,
                "str_details": p.strength_details,
                "conf_score": p.conf_score,
                "conf_details": p.conf_details,
                "pos_advice": p.position_advice,
                "status": p.status,
                "inv_reason": p.invalidation_reason,
                "hub_rank": p.trend_hub_rank,
                "hub_idx": p.hub_idx,
                "inv_price": p.invalidation_price,
                "signal_level": p.signal_level,
            }
            if p.type in ("3B", "3S") and p.hub_idx >= 0:
                hub_obj = all_hubs[p.hub_idx] if p.hub_idx < len(all_hubs) else None
                if hub_obj:
                    entry["hub_zg"] = hub_obj.zg
                    entry["hub_zd"] = hub_obj.zd
                    entry["hub_width"] = len(hub_obj.strokes)
                    entry["hub_evo"] = hub_obj.evolution_type
            if ranges:
                entry["ranges"] = ranges
            if p.dt in fractal_merge:
                entry["fractal_bars"] = fractal_merge[p.dt]
            entry = {k: v for k, v in entry.items()
                     if v is not None and v != "" and v != []}
            bsp_markers.append(entry)

    # Segment-level buy/sell points
    seg_bsp_markers = []
    for p in result.seg_buy_sell_points:
        pi = dt_index.get(p.dt)
        if pi is not None:
            is_buy = p.type in ("1B", "2B", "3B", "PB")
            entry = {
                "idx": pi,
                "price": p.price,
                "type": p.type,
                "label": p.label,
                "desc": p.description,
                "is_buy": is_buy,
                "hub_idx": p.hub_idx,
                "signal_level": p.signal_level,
                "conf": p.confidence,
                "seg_idx": p.seg_idx,
            }
            entry = {k: v for k, v in entry.items()
                     if v is not None and v != "" and v != []}
            seg_bsp_markers.append(entry)

    # For daily K-lines: tentative if last bar is today and market hasn't closed
    # For intraday: tentative if market is currently open
    tentative = 0
    if len(bars) > 0:
        today_str = datetime.now(_TZ_CHINA).strftime("%Y-%m-%d")
        last_dt = bars[-1].dt.split(" ")[0] if " " in bars[-1].dt else bars[-1].dt
        if last_dt == today_str and _is_daily_bar_tentative():
            tentative = 1
        elif _is_ashare_market_open():
            tentative = 1

    return {
        "dates": dates,
        "kline": kline_data,
        "volumes": volumes,
        "macd_hist": macd_hist,
        "dif": dif_line,
        "dea": dea_line,
        "ma5": ma5,
        "ma10": ma10,
        "ma250": ma250,
        "strokes": stroke_lines,
        "segments": segment_points,
        "seg_labels": segment_labels,
        "fractals": fractal_markers,
        "hubs": hub_rects,
        "seg_hubs": seg_hub_rects,
        "bsp": bsp_markers,
        "seg_bsp": seg_bsp_markers,
        "trend": result.trend,
        "hub_position": result.position_vs_hub,
        "hub_detail": result.hub_position_detail,
        "trend_completion": result.trend_completion,
        "volume_profile": result.volume_profile,
        "tentative": tentative,
        "stats": {
            "bars": len(bars),
            "merged": len(result.merged_bars),
            "fractals": len(result.fractals),
            "strokes": len(result.strokes),
            "segments": len(result.segments),
            "hubs": len(result.hubs),
            "seg_hubs": len(result.seg_hubs),
            "bsp": len(result.buy_sell_points),
            "latest_dif": bars[-1].dif if bars else 0,
        },
    }



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
        "three_buy_confirmations": [
            {
                "source_level": c.source_level,
                "dt": c.source_3b_dt,
                "price": c.source_3b_price,
                "strength": c.source_3b_strength,
                "pullback_range": f"{c.pullback_start_dt}~{c.pullback_end_dt}",
                "sub_level": c.sub_level,
                "confirmed": c.confirmed,
                "confirmation_type": c.confirmation_type,
                "confirmation_dt": c.confirmation_dt,
                "confirmation_price": c.confirmation_price,
                "daily_env": c.daily_env,
                "status": c.overall_status,
                "note": c.note,
            }
            for c in syn.three_buy_confirmations
        ],
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
        bullets.append("DF多头+中枢上方，趋势最强格局")
        bullets.append("回调到中枢顶是加仓机会")
    elif is_up and inside:
        bullets.append("DF多头+中枢震荡，等待方向选择")
        bullets.append("中枢上沿附近减仓，下沿回补")
    elif is_up and below:
        bullets.append("DF多头但中枢下方，需确认支撑")
        bullets.append("等价格重新站上中枢再考虑")
    elif is_down and below:
        bullets.append("DF空头+中枢下方，趋势最弱格局")
        bullets.append("反弹到中枢底是减仓/出逃机会")
    elif is_down and inside:
        bullets.append("DF空头+中枢震荡，方向未明")
        bullets.append("反弹可减仓，不宜重仓")
    elif is_down and above:
        bullets.append("DF空头但中枢上方，关注是否破位")
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
        bullets.append("⚠ DF背驰出现，趋势可能反转")
    m30_tc = m30_data.get("trend_completion", {})
    m30_tc_status = m30_tc.get("status", "")
    if "疑似" in m30_tc_status:
        bullets.append("⚠ 30分背驰出现，短线注意反转")

    conclusion = " · ".join(bullets)

    # --- Daily environment assessment for 30min 3B suitability ---
    # Per 图解缠论3 §1.1 / 三买筛选体系 §3.8:
    #   Daily trend + MACD DIF zone → suitability for lower-level 3B.
    daily_stats = daily_data.get("stats", {})
    daily_dif = daily_stats.get("latest_dif", 0) if isinstance(daily_stats, dict) else 0
    dif_above = daily_dif > 0

    env_score = 0
    env_factors = []

    if is_up:
        env_score += 2
        env_factors.append("DF上涨趋势")
    elif is_down:
        env_score -= 2
        env_factors.append("DF下跌趋势")
    elif "破坏" in trend:
        env_score -= 1
        env_factors.append("DF趋势破坏")
    else:
        env_factors.append("DF盘整")

    if dif_above:
        env_score += 1
        env_factors.append("MACD零轴上方")
    else:
        env_score -= 1
        env_factors.append("MACD零轴下方")

    if above:
        env_score += 1
        env_factors.append("价格在中枢上方")
    elif inside:
        env_factors.append("价格在中枢内")
    elif below:
        env_score -= 1
        env_factors.append("价格在中枢下方")

    if env_score >= 3:
        env_label = "强多头环境"
        env_color = "#f85149"
        m30_3b_ok = True
        env_advice = "30分三买可放心操作"
    elif env_score >= 1:
        env_label = "偏多环境"
        env_color = "#f0883e"
        m30_3b_ok = True
        env_advice = "30分三买可操作，正常仓位"
    elif env_score >= 0:
        env_label = "中性环境"
        env_color = "#d29922"
        m30_3b_ok = False
        env_advice = "30分三买需谨慎，轻仓试探"
    elif env_score >= -1:
        env_label = "偏空环境"
        env_color = "#8b949e"
        m30_3b_ok = False
        env_advice = "30分三买不建议，仅极强信号轻仓"
    else:
        env_label = "强空头环境"
        env_color = "#3fb950"
        m30_3b_ok = False
        env_advice = "30F三买回避，等待DF转势"

    daily_env = {
        "label": env_label,
        "color": env_color,
        "score": env_score,
        "m30_3b_ok": m30_3b_ok,
        "advice": env_advice,
        "factors": env_factors,
    }

    # Three-buy sub-level confirmation summary
    tbc_list = syn.get("three_buy_confirmations", [])
    tbc_confirmed = [c for c in tbc_list if c.get("confirmed")]
    tbc_pending = [c for c in tbc_list if not c.get("confirmed")]
    three_buy_status = None
    if tbc_confirmed:
        latest_conf = max(tbc_confirmed, key=lambda c: c.get("dt", ""))
        three_buy_status = {
            "has_confirmed": True,
            "count_confirmed": len(tbc_confirmed),
            "count_pending": len(tbc_pending),
            "latest": {
                "level": latest_conf.get("source_level", ""),
                "dt": latest_conf.get("dt", ""),
                "price": latest_conf.get("price", 0),
                "strength": latest_conf.get("strength", ""),
                "confirmation_type": latest_conf.get("confirmation_type", ""),
                "confirmation_dt": latest_conf.get("confirmation_dt", ""),
                "note": latest_conf.get("note", ""),
            },
        }
    elif tbc_pending:
        three_buy_status = {
            "has_confirmed": False,
            "count_confirmed": 0,
            "count_pending": len(tbc_pending),
            "latest": None,
        }

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
        "daily_env": daily_env,
        "three_buy_status": three_buy_status,
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
        parts.append("DF↑")
    elif is_down:
        score += 8
        parts.append("DF↓")
    else:
        score += 3
        parts.append("DF盘整")

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
# Shared analysis pipeline (used by both desktop & mobile dashboards)
# ════════════════════════════════════════════════════════════════════

def _analyze_one_index_worker(args: tuple) -> dict:
    """Worker function for multiprocess analysis. Must be top-level for pickling."""
    etf_code, idx_dir, levels_cfg = args
    echarts_items = {}
    level_results = {}

    for level_key, csv_name, _label in levels_cfg:
        csv_path = os.path.join(idx_dir, csv_name)
        if not os.path.exists(csv_path):
            continue
        bars = load_bars_from_csv(csv_path)
        result = analyze(bars, level_key)
        mb = 1250 if level_key == "daily" else 1800
        echarts_data = _result_to_echarts_data(result, max_bars=mb)
        echarts_items[f"{etf_code}_{level_key}"] = echarts_data
        level_results[level_key] = result

    syn_dict = None
    syn_text = None
    if "daily" in level_results:
        syn = synthesize_multi_level(
            level_results["daily"],
            level_results.get("30min"),
            level_results.get("5min"),
            weekly=level_results.get("weekly"),
        )
        syn_dict = _synthesis_to_dict(syn)
        # Collect pending_3b from all levels
        all_pending = []
        for lk, lr in level_results.items():
            for p3b in lr.pending_3b:
                all_pending.append({
                    "level": p3b.level,
                    "hub_idx": p3b.hub_idx,
                    "hub_zg": p3b.hub_zg,
                    "hub_zd": p3b.hub_zd,
                    "hub_strokes": p3b.hub_strokes,
                    "breakout_dt": p3b.breakout_dt,
                    "breakout_high": p3b.breakout_high,
                    "breakout_pct": p3b.breakout_pct,
                    "current_low": p3b.current_low,
                    "margin_pct": p3b.margin_pct,
                    "status": p3b.status,
                    "stop_loss": p3b.stop_loss,
                    "hub_rank": p3b.hub_rank,
                    "note": p3b.note,
                })
        syn_dict["pending_3b"] = all_pending
        syn_text = f"{syn.direction_alignment} | {syn.overall_bias}"

    return {
        "etf_code": etf_code,
        "echarts": echarts_items,
        "synthesis": syn_dict,
        "syn_text": syn_text,
    }


def run_analysis_pipeline(data_dir: str = None,
                          max_workers: int = None,
                          indices_override: list = None) -> dict:
    """Run chanlun analysis on all indices and return shared results.

    Supports multiprocess parallelism for analyze() calls.
    The returned dict can be passed to generate_dashboard() and
    generate_mobile_dashboard() to avoid redundant computation.

    Args:
        indices_override: If provided, use this list instead of loading
            from watchlist. Used for MA250-filtered stock pool.

    Returns dict with keys: all_data, synthesis_data, index_list,
    latest_data_time, indices.
    """
    if data_dir is None:
        data_dir = os.path.join(_PROJECT_ROOT, "data")

    indices = indices_override if indices_override is not None else load_index_watchlist()
    levels_cfg = [("weekly", "weekly.csv", "WF"),
                  ("daily", "daily.csv", "DF"),
                  ("30min", "30min.csv", "30F"),
                  ("5min", "5min.csv", "5F"),
                  ("1min", "1min.csv", "1F")]

    total = len(indices) * len(levels_cfg)
    t0 = time.perf_counter()

    tasks = []
    for idx in indices:
        idx_dir = os.path.join(data_dir, f"{idx.etf_code}_{idx.etf_name}")
        tasks.append((idx.etf_code, idx_dir, levels_cfg))

    all_data = {}
    synthesis_data = {}

    if max_workers and max_workers > 1:
        print(f"  Analyzing {total} tasks with {max_workers} processes...")
        done = 0
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            future_map = {pool.submit(_analyze_one_index_worker, t): t[0]
                          for t in tasks}
            for future in as_completed(future_map):
                result = future.result()
                code = result["etf_code"]
                all_data.update(result["echarts"])
                if result["synthesis"]:
                    synthesis_data[code] = result["synthesis"]
                done += len(result["echarts"])
                name = future_map[future]
                syn_info = f" → {result['syn_text']}" if result["syn_text"] else ""
                print(f"  [{done}/{total}] {name} ({len(result['echarts'])} levels){syn_info}")
    else:
        done = 0
        for idx in indices:
            idx_dir = os.path.join(data_dir, f"{idx.etf_code}_{idx.etf_name}")
            level_results: dict[str, AnalysisResult] = {}
            for level_key, csv_name, level_label in levels_cfg:
                done += 1
                csv_path = os.path.join(idx_dir, csv_name)
                if not os.path.exists(csv_path):
                    print(f"  [{done}/{total}] SKIP {idx.etf_name} {level_label} (no data)")
                    continue
                print(f"  [{done}/{total}] {idx.etf_name} {level_label}...", end=" ", flush=True)
                bars = load_bars_from_csv(csv_path)
                result = analyze(bars, level_key)
                level_results[level_key] = result
                mb = 1250 if level_key == "daily" else 1800
                echarts_data = _result_to_echarts_data(result, max_bars=mb)
                all_data[f"{idx.etf_code}_{level_key}"] = echarts_data
                print(f"OK ({result.trend}, {len(result.buy_sell_points)} signals)")

            if "daily" in level_results:
                syn = synthesize_multi_level(
                    level_results["daily"],
                    level_results.get("30min"),
                    level_results.get("5min"),
                    weekly=level_results.get("weekly"),
                )
                syn_dict = _synthesis_to_dict(syn)
                # Collect pending_3b from all analyzed levels
                all_pending = []
                for lk, lr in level_results.items():
                    for p3b in lr.pending_3b:
                        all_pending.append({
                            "level": p3b.level,
                            "hub_idx": p3b.hub_idx,
                            "hub_zg": p3b.hub_zg,
                            "hub_zd": p3b.hub_zd,
                            "hub_strokes": p3b.hub_strokes,
                            "breakout_dt": p3b.breakout_dt,
                            "breakout_high": p3b.breakout_high,
                            "breakout_pct": p3b.breakout_pct,
                            "current_low": p3b.current_low,
                            "margin_pct": p3b.margin_pct,
                            "status": p3b.status,
                            "stop_loss": p3b.stop_loss,
                            "hub_rank": p3b.hub_rank,
                            "note": p3b.note,
                        })
                syn_dict["pending_3b"] = all_pending
                synthesis_data[idx.etf_code] = syn_dict
                print(f"  → 联立: {syn.direction_alignment} | {syn.overall_bias}")

    elapsed = time.perf_counter() - t0
    print(f"  Analysis complete: {len(all_data)} charts in {elapsed:.1f}s")

    index_list = _build_index_list(indices, all_data, synthesis_data)

    latest_data_time = ""
    for key, data in all_data.items():
        dates = data.get("dates", [])
        if dates and dates[-1] > latest_data_time:
            latest_data_time = dates[-1]

    return {
        "all_data": all_data,
        "synthesis_data": synthesis_data,
        "index_list": index_list,
        "latest_data_time": latest_data_time,
        "indices": indices,
    }


def _build_index_list(indices, all_data, synthesis_data):
    """Build the sorted index_list from analysis results."""
    index_list = []
    _valid_sig = {"1B", "1S", "2B", "2S", "3B", "3S"}
    for i in indices:
        entry = {"etf_code": i.etf_code, "index_name": i.index_name,
                 "etf_name": i.etf_name, "category": i.category,
                 "type": i.type, "index_code": i.index_code,
                 "market": i.market, "notes": i.notes}
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

        latest_sig_dt = ""
        for lk in ("daily", "30min"):
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
    return broad + sector + stocks


# ════════════════════════════════════════════════════════════════════
# Cross-level departure/pullback reclassification (per 108课 lesson 18)
# ════════════════════════════════════════════════════════════════════

_SUB_LEVEL_MAP = {"daily": "30min", "30min": "5min"}


def _reclassify_dep_pb(all_data: dict):
    """Reclassify departure/pullback combinations using sub-level hub counts.

    Per 108课 lesson 18, departure/pullback types should be classified by
    whether the segment forms a sub-level trend (≥2 hubs) or consolidation
    (0-1 hubs), not by price magnitude.
    """
    for key in list(all_data.keys()):
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        etf_code, level_key = parts
        sub_level = _SUB_LEVEL_MAP.get(level_key)
        if not sub_level:
            continue
        sub_key = f"{etf_code}_{sub_level}"
        sub_data = all_data.get(sub_key)
        if not sub_data:
            continue

        parent = all_data[key]
        p_dates = parent.get("dates", [])
        p_kline = parent.get("kline", [])
        p_hubs = parent.get("hubs", [])
        p_bsp = parent.get("bsp", [])
        s_dates = sub_data.get("dates", [])
        s_hubs = sub_data.get("hubs", [])

        if not p_dates or not s_dates or not s_hubs:
            continue

        hub_by_idx = {h["idx"]: h for h in p_hubs}
        sub_date_start = s_dates[0][:10] if s_dates else ""
        sub_date_end = s_dates[-1][:10] if s_dates else ""

        for sig in p_bsp:
            if sig["type"] not in ("3B", "3S"):
                continue
            hi = sig.get("hub_idx", -1)
            hub = hub_by_idx.get(hi)
            if not hub:
                continue

            sig_di = sig["idx"]
            hub_end_di = hub["x1"]
            if sig_di <= hub_end_di or sig_di >= len(p_dates):
                continue

            dep_day_start = p_dates[hub_end_di][:10]
            sig_day = p_dates[sig_di][:10]

            if dep_day_start < sub_date_start or sig_day > sub_date_end:
                continue

            is_buy = sig["type"] == "3B"
            scan_start = hub_end_di + 1
            scan_end = sig_di

            if scan_end <= scan_start or scan_end >= len(p_kline):
                continue

            if is_buy:
                peak_price = max(p_kline[i][1] for i in range(scan_start, scan_end + 1))
                peak_di = next(i for i in range(scan_start, scan_end + 1)
                               if p_kline[i][1] == peak_price)
            else:
                peak_price = min(p_kline[i][2] for i in range(scan_start, scan_end + 1))
                peak_di = next(i for i in range(scan_start, scan_end + 1)
                               if p_kline[i][2] == peak_price)

            dep_day_end = p_dates[peak_di][:10]
            pb_day_start = dep_day_end
            pb_day_end = sig_day

            dep_hubs = sum(1 for h in s_hubs
                           if s_dates[h["x0"]][:10] >= dep_day_start
                           and s_dates[h["x1"]][:10] <= dep_day_end)
            pb_hubs = sum(1 for h in s_hubs
                          if s_dates[h["x0"]][:10] >= pb_day_start
                          and s_dates[h["x1"]][:10] <= pb_day_end)

            dep_type = "趋势" if dep_hubs >= 2 else "盘整"
            pb_type = "反趋势" if pb_hubs >= 2 else "盘整"
            combo = f"{dep_type}+{pb_type}"

            score_map = {
                "趋势+盘整": 3,
                "趋势+反趋势": 1,
                "盘整+盘整": -2,
                "盘整+反趋势": 0,
            }
            new_score = score_map.get(combo, 0)
            new_label = f"{combo}(离{dep_hubs}枢/回{pb_hubs}枢)"

            old_score = 0
            for d in sig.get("str_details", []):
                if d["dim"] == "离开回抽":
                    old_score = d["score"]
                    d["label"] = new_label
                    d["score"] = new_score
                    break

            delta = new_score - old_score
            if delta != 0:
                sig["str_score"] = sig.get("str_score", 0) + delta
                new_total = sig["str_score"]
                sig["strength"] = ("strongest" if new_total >= 8 else
                                   "strong" if new_total >= 5 else
                                   "standard" if new_total >= 1 else "weak")


# ════════════════════════════════════════════════════════════════════
# Trend Hub Collection (shared by desktop & mobile)
# ════════════════════════════════════════════════════════════════════

def _collect_trend_hubs(all_data: dict, idx_name_map: dict,
                        level_labels: dict) -> list[dict]:
    """Collect hubs with trend_seq >= 1 (rank >= 2) across all indices/levels.

    Returns a list sorted by end_dt descending (most recent first).
    """
    entries: list[dict] = []
    for key, data in all_data.items():
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        etf_code, level_key = parts
        etf_name = idx_name_map.get(etf_code, etf_code)
        level_cn = level_labels.get(level_key, level_key)
        dates = data.get("dates", [])
        for h in data.get("hubs", []):
            if h.get("trend_seq", -1) < 1:
                continue
            direction = h.get("direction", "")
            if direction not in ("上", "下"):
                continue
            rank = h["trend_seq"] + 1
            start_dt = dates[h["x0"]] if h["x0"] < len(dates) else ""
            end_dt = dates[h["x1"]] if h["x1"] < len(dates) else ""
            entries.append({
                "etf_code": etf_code,
                "etf_name": etf_name,
                "level": level_cn,
                "level_key": level_key,
                "direction": "上涨" if direction == "上" else "下跌",
                "rank": rank,
                "zg": round(h["zg"], 3),
                "zd": round(h["zd"], 3),
                "start_dt": start_dt,
                "end_dt": end_dt,
                "evo": h.get("evo", ""),
                "hub_level": h.get("hub_level", ""),
            })
    entries.sort(key=lambda x: x["end_dt"], reverse=True)
    return entries


def _split_trend_hubs(trend_hub_all: list[dict], idx_type_map: dict,
                      levels: list[str], limit: int = 30
                      ) -> tuple[dict, dict]:
    """Split trend hubs into stock vs ETF buckets by level."""
    stock: dict[str, dict[str, list]] = {lv: {"trend_hubs": []} for lv in levels}
    etf: dict[str, dict[str, list]] = {lv: {"trend_hubs": []} for lv in levels}
    for th in trend_hub_all:
        lv = th["level"]
        if lv not in stock:
            continue
        item_type = idx_type_map.get(th["etf_code"], "stock")
        target = stock if item_type == "stock" else etf
        if len(target[lv]["trend_hubs"]) < limit:
            target[lv]["trend_hubs"].append(th)
    return stock, etf


# ════════════════════════════════════════════════════════════════════
# Data File I/O
# ════════════════════════════════════════════════════════════════════

_LEVEL_SUFFIXES = ("_weekly", "_daily", "_30min", "_5min", "_1min")


def _extract_code(key: str) -> str:
    """Extract stock/ETF code from a data key like '300502_daily' → '300502'."""
    for suffix in _LEVEL_SUFFIXES:
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def _write_merged_data_files(data_out_dir: str, all_data: dict) -> None:
    """Write per-level data files: one .js file per stock+level for fast loading.

    File format (single DATA_CACHE assignment per file):
        DATA_CACHE["300502_daily"]={...};

    Files: 300502_daily.js, 300502_30min.js, 300502_5min.js
    """
    # Remove legacy merged per-code files (e.g. 300502.js containing all levels)
    from collections import defaultdict
    by_code: dict[str, list] = defaultdict(list)
    for key in sorted(all_data.keys()):
        code = _extract_code(key)
        by_code[code].append(key)

    for code in by_code:
        legacy = os.path.join(data_out_dir, f"{code}.js")
        if os.path.exists(legacy):
            os.remove(legacy)

    # Write per-level files
    for key, chart_data in all_data.items():
        fpath = os.path.join(data_out_dir, f"{key}.js")
        json_str = json.dumps(chart_data, ensure_ascii=False,
                              separators=(",", ":"))
        with open(fpath, "w", encoding="utf-8") as df:
            df.write(f'DATA_CACHE["{key}"]={json_str};\n')


def parse_merged_data_files(data_dir: str) -> dict:
    """Read merged .js data files and return {key: data_dict}.

    Handles both merged format (multiple DATA_CACHE assignments per file)
    and legacy format (single assignment per file).
    """
    import re
    _pat = re.compile(r'^DATA_CACHE\["([^"]+)"\]=(.+);$')
    all_data: dict = {}

    for p in sorted(Path(data_dir).iterdir()):
        if not p.is_file() or p.suffix != ".js" or p.name == "live.js":
            continue
        content = p.read_text(encoding="utf-8")
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = _pat.match(line)
            if m:
                key, json_str = m.group(1), m.group(2)
                try:
                    all_data[key] = json.loads(json_str)
                except json.JSONDecodeError:
                    print(f"  WARNING: Failed to parse key {key} in {p.name}")
    return all_data



# ════════════════════════════════════════════════════════════════════
# Mobile Dashboard (Canvas-based, same data as desktop)
# ════════════════════════════════════════════════════════════════════

def generate_mobile_dashboard(data_dir: str = None,
                              output_path: str = None,
                              cache: dict = None) -> str:
    """Generate a mobile-optimized HTML dashboard with Canvas K-line charts.

    Reuses pre-computed analysis from cache if provided,
    avoiding redundant 492 analyze() calls.
    """
    if data_dir is None:
        data_dir = os.path.join(_PROJECT_ROOT, "data")
    if output_path is None:
        output_path = os.path.join(_PROJECT_ROOT, "reports", "dashboard_mobile.html")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    if cache is not None:
        all_data = cache["all_data"]
        synthesis_data = cache["synthesis_data"]
        index_list = cache["index_list"]
        latest_data_time = cache["latest_data_time"]
        indices = cache["indices"]
    else:
        pipe = run_analysis_pipeline(data_dir=data_dir)
        all_data = pipe["all_data"]
        synthesis_data = pipe["synthesis_data"]
        index_list = pipe["index_list"]
        latest_data_time = pipe["latest_data_time"]
        indices = pipe["indices"]

    _reclassify_dep_pb(all_data)

    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_time = latest_data_time or "-"

    # Extract filter stats for display
    filter_stats = cache.get("filter_stats") if cache else None
    pool_total = filter_stats["total"] if filter_stats else len(indices)
    pool_selected = filter_stats["selected"] if filter_stats else len(indices)

    data_out_dir = os.path.join(os.path.dirname(output_path), "data")
    os.makedirs(data_out_dir, exist_ok=True)
    _write_merged_data_files(data_out_dir, all_data)

    # Keep baseline in sync with base data files so that subsequent delta
    # deploys don't produce duplicate bars in live.js.
    save_deploy_baseline(data_out_dir, all_data)

    # Remove stale live.js to prevent delta corruption when viewing locally.
    # live.js is now generated only by the deploy script (delta deploy path).
    _stale_live = os.path.join(data_out_dir, "live.js")
    if os.path.exists(_stale_live):
        os.remove(_stale_live)

    data_keys_json = json.dumps(sorted(all_data.keys()), ensure_ascii=False)
    index_list_json = json.dumps(index_list, ensure_ascii=False)

    # Watchlist codes for mobile
    watchlist_codes_m = []
    _wl_path_m = os.path.join(_PROJECT_ROOT, "config", "watchlist.json")
    if os.path.exists(_wl_path_m):
        with open(_wl_path_m, "r", encoding="utf-8") as wf:
            _wl_m = json.load(wf)
            watchlist_codes_m = [item["etf_code"] for item in _wl_m.get("watchlist", [])]
    watchlist_codes_json = json.dumps(watchlist_codes_m, ensure_ascii=False)
    _wl_codes_set_m = set(watchlist_codes_m)

    # Collect global signals for mobile (same logic as desktop)
    level_labels_m = {"weekly": "WF", "daily": "DF", "30min": "30F", "5min": "5F", "1min": "1F"}
    idx_name_map_m = {i.etf_code: i.etf_name for i in indices}
    idx_type_map_m = {i.etf_code: i.type for i in indices}
    mobile_global_signals: list[dict] = []
    valid_types_m = {"1B", "1S", "2B", "2S", "3B", "3S"}
    for key, data in all_data.items():
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        etf_code, level_key = parts
        etf_name = idx_name_map_m.get(etf_code, etf_code)
        level_cn = level_labels_m.get(level_key, level_key)

        # Stroke-level BSP (次级别)
        for p in data.get("bsp", []):
            if p["type"] not in valid_types_m:
                continue
            if p["type"] in ("3B", "3S") and p.get("strength") == "weak":
                continue
            dt_str = data["dates"][p["idx"]] if p["idx"] < len(data["dates"]) else ""
            entry_m = {
                "dt": dt_str, "etf_code": etf_code, "etf_name": etf_name,
                "level": level_cn, "level_key": level_key,
                "type": p["type"], "label": p["label"],
                "price": p["price"], "conf": p.get("conf", ""),
                "conf_score": p.get("conf_score"),
                "strength": p.get("strength", ""),
                "str_score": p.get("str_score"),
                "status": p.get("status", "active"),
                "inv_reason": p.get("inv_reason", ""),
                "hub_rank": p.get("hub_rank", -1),
                "hub_width": p.get("hub_width", 0),
                "signal_level": p.get("signal_level", ""),
                "hub_type": "笔中枢",
            }
            if p.get("ranges") and len(p["ranges"]) >= 2:
                r0, r1 = p["ranges"][0], p["ranges"][1]
                ratio_val = r1["area"] / r0["area"] if r0["area"] > 0 else 0
                entry_m["area_cmp"] = f"{r1['label']}/{r0['label']}={ratio_val:.2f}"
            else:
                entry_m["area_cmp"] = ""
            mobile_global_signals.append(entry_m)

        # Segment-level BSP (当前级别)
        for p in data.get("seg_bsp", []):
            if p["type"] not in valid_types_m:
                continue
            dt_str = data["dates"][p["idx"]] if p["idx"] < len(data["dates"]) else ""
            entry_m = {
                "dt": dt_str, "etf_code": etf_code, "etf_name": etf_name,
                "level": level_cn, "level_key": level_key,
                "type": p["type"], "label": p["label"],
                "price": p["price"], "conf": p.get("conf", ""),
                "conf_score": 0,
                "strength": p.get("strength", ""),
                "str_score": 0,
                "status": "active",
                "inv_reason": "",
                "hub_rank": -1,
                "hub_width": 0,
                "signal_level": p.get("signal_level", ""),
                "hub_type": "线段中枢",
                "area_cmp": "",
            }
            mobile_global_signals.append(entry_m)

    # Annotate 3B signals with sub-level confirmation status (mobile)
    for s in mobile_global_signals:
        if s["type"] != "3B":
            continue
        syn = synthesis_data.get(s["etf_code"], {})
        tbcs = syn.get("three_buy_confirmations", [])
        lk = s["level_key"]
        for c in tbcs:
            if c.get("source_level") == lk and c.get("dt") == s["dt"]:
                s["tbc"] = {
                    "confirmed": c.get("confirmed", False),
                    "type": c.get("confirmation_type", ""),
                    "dt": c.get("confirmation_dt", ""),
                    "note": c.get("note", ""),
                }
                break

    mobile_global_signals.sort(key=lambda x: x["dt"], reverse=True)
    mobile_type_limits = {"type1": 100, "type2": 100, "type3": 100}
    mobile_levels = ["WF", "DF", "30F", "5F", "1F"]

    # Split mobile signals into stock vs ETF
    mobile_stock_by_level: dict[str, dict[str, list]] = {
        lv: {"type1": [], "type2": [], "type3": [], "trend_hubs": []} for lv in mobile_levels
    }
    mobile_etf_by_level: dict[str, dict[str, list]] = {
        lv: {"type1": [], "type2": [], "type3": [], "trend_hubs": []} for lv in mobile_levels
    }
    for s in mobile_global_signals:
        lv = s["level"]
        if lv not in mobile_stock_by_level:
            continue
        if s["type"] in ("1B", "1S"):
            bucket = "type1"
        elif s["type"] in ("2B", "2S"):
            bucket = "type2"
        else:
            bucket = "type3"
        item_type = idx_type_map_m.get(s["etf_code"], "stock")
        if item_type == "stock":
            target = mobile_stock_by_level
        else:
            target = mobile_etf_by_level
        if len(target[lv][bucket]) < mobile_type_limits[bucket]:
            target[lv][bucket].append(s)

    # Collect trend hubs for mobile
    m_trend_hub_all = _collect_trend_hubs(all_data, idx_name_map_m, level_labels_m)
    m_th_stock, m_th_etf = _split_trend_hubs(m_trend_hub_all, idx_type_map_m, mobile_levels, limit=50)
    for lv in mobile_levels:
        mobile_stock_by_level[lv]["trend_hubs"] = m_th_stock[lv]["trend_hubs"]
        mobile_etf_by_level[lv]["trend_hubs"] = m_th_etf[lv]["trend_hubs"]

    mobile_global_signals_json = json.dumps({"stock": mobile_stock_by_level, "etf": mobile_etf_by_level}, ensure_ascii=False)

    # Build watchlist-specific signals for mobile (pre-filtered)
    mobile_wl_type_limits = {"type1": 100, "type2": 100, "type3": 100}
    mobile_wl_signals: dict[str, dict[str, list]] = {
        lv: {"type1": [], "type2": [], "type3": [], "trend_hubs": []} for lv in mobile_levels
    }
    if _wl_codes_set_m:
        for s in mobile_global_signals:
            if s["etf_code"] not in _wl_codes_set_m:
                continue
            lv = s["level"]
            if lv not in mobile_wl_signals:
                continue
            if s["type"] in ("1B", "1S"):
                bucket = "type1"
            elif s["type"] in ("2B", "2S"):
                bucket = "type2"
            else:
                bucket = "type3"
            if len(mobile_wl_signals[lv][bucket]) < mobile_wl_type_limits[bucket]:
                mobile_wl_signals[lv][bucket].append(s)
    if _wl_codes_set_m:
        for th in m_trend_hub_all:
            if th["etf_code"] not in _wl_codes_set_m:
                continue
            lv = th["level"]
            if lv in mobile_wl_signals and len(mobile_wl_signals[lv]["trend_hubs"]) < 50:
                mobile_wl_signals[lv]["trend_hubs"].append(th)
    mobile_watchlist_signals_json = json.dumps(mobile_wl_signals, ensure_ascii=False)

    # ── Market Thermometer data ──
    thermo_levels = {"weekly": "WF", "daily": "DF", "30min": "30F", "5min": "5F", "1min": "1F"}
    thermo = {}
    total_stocks = len(indices)
    for lk, lv_label in thermo_levels.items():
        hub_above = hub_inside = hub_below = 0
        sig_3b = sig_pb = sig_1b = sig_ps = sig_3s = sig_other = 0
        for idx in indices:
            key = f"{idx.etf_code}_{lk}"
            d = all_data.get(key)
            if not d:
                continue
            hp = d.get("hub_position", "")
            if "上方" in hp:
                hub_above += 1
            elif "下方" in hp:
                hub_below += 1
            else:
                hub_inside += 1
            bsp_list = d.get("bsp", [])
            if bsp_list:
                lt = bsp_list[-1].get("type", "")
                if lt == "3B":
                    sig_3b += 1
                elif lt in ("PB",):
                    sig_pb += 1
                elif lt in ("1B", "2B"):
                    sig_1b += 1
                elif lt in ("PS",):
                    sig_ps += 1
                elif lt in ("3S",):
                    sig_3s += 1
                elif lt in ("1S", "2S"):
                    sig_other += 1

        thermo[lv_label] = {
            "hub": {"above": hub_above, "inside": hub_inside, "below": hub_below},
            "sig": {"3B": sig_3b, "PB": sig_pb, "1B": sig_1b, "PS": sig_ps, "3S": sig_3s, "other": sig_other},
        }

    above_30 = thermo.get("30F", {}).get("hub", {}).get("above", 0)
    below_30 = thermo.get("30F", {}).get("hub", {}).get("below", 0)
    buy3_30 = thermo.get("30F", {}).get("sig", {}).get("3B", 0)
    sell3_30 = thermo.get("30F", {}).get("sig", {}).get("3S", 0)
    above_pct = above_30 / total_stocks * 100 if total_stocks else 0
    below_pct = below_30 / total_stocks * 100 if total_stocks else 0

    if above_pct > 50 and buy3_30 > sell3_30 * 2:
        thermo_assess = "强势"
        thermo_color = "#f85149"
    elif above_pct > 30 and buy3_30 > sell3_30:
        thermo_assess = "偏强"
        thermo_color = "#f0883e"
    elif below_pct > 50 and sell3_30 > buy3_30 * 2:
        thermo_assess = "弱势"
        thermo_color = "#3fb950"
    elif below_pct > 35 and sell3_30 > buy3_30:
        thermo_assess = "偏弱"
        thermo_color = "#7ee787"
    else:
        thermo_assess = "震荡"
        thermo_color = "#d29922"

    # Persist daily snapshot to thermo_history.json
    thermo_history_path = os.path.join(_PROJECT_ROOT, "reports", "data", "thermo_history.json")
    today_str = datetime.now().strftime("%Y-%m-%d")
    thermo_history = {}
    if os.path.exists(thermo_history_path):
        try:
            with open(thermo_history_path, "r", encoding="utf-8") as f:
                thermo_history = json.load(f)
        except Exception:
            thermo_history = {}

    thermo_history[today_str] = {
        "levels": thermo,
        "assess": thermo_assess,
        "total": total_stocks,
    }

    sorted_dates = sorted(thermo_history.keys(), reverse=True)[:20]
    thermo_history = {d: thermo_history[d] for d in sorted_dates}
    os.makedirs(os.path.dirname(thermo_history_path), exist_ok=True)
    with open(thermo_history_path, "w", encoding="utf-8") as f:
        json.dump(thermo_history, f, ensure_ascii=False, indent=2)

    recent_5 = sorted_dates[:5]
    thermo_hist_recent = {d: thermo_history[d] for d in recent_5}

    thermo_data = {
        "total": total_stocks,
        "levels": thermo,
        "assess": thermo_assess,
        "color": thermo_color,
        "history": thermo_hist_recent,
    }
    market_thermo_json = json.dumps(thermo_data, ensure_ascii=False)

    # Collect pending_3b for mobile
    mobile_pending_3b = []
    for code, syn in synthesis_data.items():
        for p3b in syn.get("pending_3b", []):
            p3b_entry = dict(p3b)
            p3b_entry["etf_code"] = code
            p3b_entry["etf_name"] = idx_name_map_m.get(code, code)
            mobile_pending_3b.append(p3b_entry)
    mobile_pending_3b.sort(key=lambda x: x.get("margin_pct", 999))
    mobile_pending_3b_json = json.dumps(mobile_pending_3b, ensure_ascii=False)

    tab_parts = []
    last_type = None
    for i, il in enumerate(index_list):
        if il.get("type") != last_type:
            label = "宽基" if il.get("type") == "broad" else ("个股" if il.get("type") == "stock" else "行业")
            tab_parts.append(
                f'<div class="idx-sep" style="display:flex;align-items:center;padding:0 6px;'
                f'color:#484f58;font-size:10px;font-weight:700;letter-spacing:1px;'
                f'border-left:2px solid #30363d;margin-left:2px;padding-left:8px">'
                f'{label}</div>')
            last_type = il.get("type")
        active = ' active' if i == 0 else ''
        _t = il.get('trend', '')
        _broken = '破坏' in _t
        trend_up = not _broken and '上涨' in _t
        trend_dn = not _broken and '下跌' in _t
        trend_pan_up = not _broken and not trend_up and not trend_dn and '盘整偏多' in _t
        trend_pan_dn = not _broken and not trend_up and not trend_dn and '盘整偏空' in _t
        trend_pan = not _broken and not trend_up and not trend_dn and not trend_pan_up and not trend_pan_dn and '盘整' in _t
        trend_icon = ('<span style="color:#e3b341" title="DF趋势破坏">⚠</span>' if _broken
                      else '<span style="color:#f85149" title="DF上涨趋势">▲</span>' if trend_up
                      else '<span style="color:#3fb950" title="DF下跌趋势">▼</span>' if trend_dn
                      else '<span style="color:#f0883e" title="DF盘整偏多">◆↑</span>' if trend_pan_up
                      else '<span style="color:#7ee787" title="DF盘整偏空">◆↓</span>' if trend_pan_dn
                      else '<span style="color:#d29922" title="DF盘整">◆</span>' if trend_pan
                      else '<span style="color:#8b949e" title="DF方向不明">—</span>')
        search_text = f'{il["index_name"]} {il.get("etf_code", "")}'.lower()
        tab_parts.append(
            f'<div class="idx-tab{active}" data-search="{search_text}" '
            f'onclick="switchIndex(\'{il["etf_code"]}\')">'
            f'{trend_icon} {il["index_name"]}</div>')
    idx_tabs_html = "\n      ".join(tab_parts)
    first_code = index_list[0]["etf_code"] if index_list else ""
    gen_ts = int(datetime.now().timestamp())

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
.idx-tabs {{ display: flex; gap: 3px; padding: 4px 8px;
             flex-wrap: wrap; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
.idx-tab {{ padding: 3px 7px; cursor: pointer; color: #8b949e;
            border: 1px solid transparent; font-size: 11px; white-space: nowrap;
            border-radius: 4px; line-height: 1.3; }}
.idx-tab.active {{ color: #58a6ff; background: #1a2332; border-color: #1f3a5f; }}
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
<div class="subtitle">移动版 · 数据 {data_time} · 生成 {gen_time} · DF→30F→5F · 股票池 {pool_selected}/{pool_total}</div>

<div id="mobileGlobalSignals" style="margin-bottom:8px"></div>

<div style="display:flex;align-items:center;margin:12px 0 6px;gap:8px;border-bottom:1px solid #30363d;padding-bottom:4px">
  <span style="color:#c9d1d9;font-size:14px;font-weight:bold">📈 技术分析详情</span>
  <span id="mCurrentAsset" style="color:#58a6ff;font-size:12px;font-weight:600"></span>
  <button id="mToggleNav" onclick="toggleMobileNav()" style="margin-left:auto;padding:3px 8px;border:1px solid #30363d;background:#21262d;color:#8b949e;cursor:pointer;font-size:11px;border-radius:4px">展开 ▼</button>
</div>
<div class="chart-section">
  <div id="mNavPanel" style="display:none">
    <div style="padding:6px 8px 2px;background:#161b22">
      <input id="idxSearch" type="text" placeholder="🔍 搜索标的..."
             style="width:100%;padding:6px 10px;font-size:13px;border:1px solid #30363d;
                    border-radius:6px;background:#0d1117;color:#c9d1d9;outline:none;
                    box-sizing:border-box"
             oninput="if(!window._imeComposing)filterIdxTabs(this.value)"
             oncompositionstart="window._imeComposing=true"
             oncompositionend="window._imeComposing=false;filterIdxTabs(this.value)">
    </div>
    <div class="idx-tabs" id="idxTabs">
      {idx_tabs_html}
    </div>
  </div>
  <div class="level-tabs" id="levelTabs">
    <div class="level-tab" onclick="switchLevel('weekly')">WF</div>
    <div class="level-tab" onclick="switchLevel('daily')">DF</div>
    <div class="level-tab active" onclick="switchLevel('30min')">30F</div>
    <div class="level-tab" onclick="switchLevel('5min')">5F</div>
    <div class="level-tab" onclick="switchLevel('1min')">1F</div>
  </div>
  <div class="info-bar" id="infoBar"></div>
  <div id="loadingOverlay" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(13,17,23,0.7);z-index:999;align-items:center;justify-content:center"><span style="color:#58a6ff;font-size:15px">加载数据中...</span></div>
  <div id="mChartPlaceholder" style="display:flex;align-items:center;justify-content:center;height:320px;background:#0d1117;border-radius:4px;margin:6px 10px;cursor:pointer" onclick="mInitChart()">
    <div style="text-align:center;color:#8b949e"><div style="font-size:36px;margin-bottom:8px">📈</div><div style="font-size:14px">点击加载K线图</div></div>
  </div>
  <div id="mChartSection" style="display:none">
  <div class="chart-area"><canvas id="klineCanvas" height="320"></canvas></div>
  <div class="legend">
    <div class="legend-item"><div class="legend-color" style="background:#f85149"></div>阳线</div>
    <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div>阴线</div>
    <div class="legend-item"><div class="legend-color" style="background:#ffd700"></div>MA5</div>
    <div class="legend-item"><div class="legend-color" style="background:#58a6ff"></div>MA10</div>
    <div class="legend-item"><div class="legend-color" style="background:#e040fb;border:1px dashed #e040fb"></div>MA250</div>
    <div class="legend-item"><div class="legend-color" style="background:#f0883e"></div>笔</div>
    <div class="legend-item"><div class="legend-color" style="background:#79c0ff"></div>上涨背驰笔</div>
    <div class="legend-item"><div class="legend-color" style="background:#d2a8ff"></div>下跌背驰笔</div>
    <div class="legend-item"><div class="legend-color" style="background:#bc8cff"></div>线段</div>
    <div class="legend-item"><div class="legend-color" style="background:rgba(248,81,73,0.4)"></div>上涨枢(↓↑↓)</div>
    <div class="legend-item"><div class="legend-color" style="background:rgba(63,185,80,0.4)"></div>下跌枢(↑↓↑)</div>
    <div class="legend-item"><div class="legend-color" style="background:rgba(248,81,73,0.55);border:2px solid rgba(248,81,73,0.7)"></div>段枢↑</div>
    <div class="legend-item"><div class="legend-color" style="background:rgba(63,185,80,0.55);border:2px solid rgba(63,185,80,0.7)"></div>段枢↓</div>
    <div class="legend-item"><div class="legend-color" style="background:#f85149"></div>买▲</div>
    <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div>卖▼</div>
    <div class="legend-item"><div class="legend-color" style="background:rgba(248,81,73,0.35);border:1px dashed rgba(248,81,73,0.6)"></div>暂定</div>
  </div>
  <div class="chart-area" style="margin-bottom:2px"><canvas id="macdCanvas" height="56"></canvas></div>
  <div style="display:flex;gap:8px;padding:0 4px;margin-bottom:2px;font-size:10px;color:#8b949e">
    <span><span style="color:#58a6ff">\u2501</span> DIF</span>
    <span><span style="color:#f0883e">\u2501</span> DEA</span>
    <span style="color:#f85149">\u2588</span><span>MACD</span>
  </div>
  <div class="chart-area" style="margin-bottom:0"><canvas id="volumeCanvas" height="44"></canvas></div>
  <div id="bspTooltip" style="display:none;position:fixed;z-index:1000;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 12px;max-width:88vw;box-shadow:0 4px 16px rgba(0,0,0,0.5);font-size:12px;line-height:1.6;color:#c9d1d9;pointer-events:auto"></div>
  </div>
</div>

<div id="marketThermo" style="margin-top:8px;margin-bottom:8px"></div>

</div>

<script>
var DATA_CACHE = {{}};
var LIVE_DATA = null;
const DATA_KEYS = {data_keys_json};
const INDEX_LIST = {index_list_json};
const SIGNAL_DATA = {mobile_global_signals_json};
const WATCHLIST_SIGNALS = {mobile_watchlist_signals_json};
const WATCHLIST_CODES = {watchlist_codes_json};
const MARKET_THERMO = {market_thermo_json};
const PENDING_3B = {mobile_pending_3b_json};

function applyLiveDelta(key, base) {{
  if (!LIVE_DATA || !LIVE_DATA[key]) return base;
  const live = LIVE_DATA[key];
  const arrFields = ['dates','kline','volumes','macd_hist','dif','dea','ma5','ma10','ma250'];
  const replaceFields = ['bsp','strokes','segments','seg_labels','fractals','hubs',
                         'trend','hub_position','hub_detail',
                         'trend_completion','volume_profile','tentative'];
  if (live.full_replace) {{
    const merged = {{}};
    for (const f of arrFields) {{ if (live[f] !== undefined) merged[f] = live[f]; }}
    for (const f of replaceFields) {{ if (live[f] !== undefined) merged[f] = live[f]; }}
    return merged;
  }}
  const merged = Object.assign({{}}, base);
  if (live.drop_head) {{
    const drop = live.drop_head;
    const baseDates = base.dates || [];
    const liveDates = live.dates || [];
    // Guard: if base already contains the live data (stale cache), skip merge
    if (liveDates.length > 0 && baseDates.length > 0) {{
      const lastBase = baseDates[baseDates.length - 1];
      const lastLive = liveDates[liveDates.length - 1];
      if (lastBase === lastLive) {{
        for (const f of replaceFields) {{
          if (live[f] !== undefined) merged[f] = live[f];
        }}
        return merged;
      }}
    }}
    for (const f of arrFields) {{
      merged[f] = (base[f] || []).slice(drop).concat(live[f] || []);
    }}
  }} else {{
    const bn = live.base_len || 0;
    for (const f of arrFields) {{
      merged[f] = (base[f] || []).slice(0, bn).concat(live[f] || []);
    }}
  }}
  for (const f of replaceFields) {{
    if (live[f] !== undefined) merged[f] = live[f];
  }}
  return merged;
}}

function getChartData(key) {{ return DATA_CACHE[key] || null; }}

function _extractCode(key) {{
  return key.replace(/_(daily|30min|5min)$/, '');
}}
function _loadLevelFile(key) {{
  return new Promise(resolve => {{
    const s = document.createElement('script');
    s.src = 'data/' + key + '.js?v={gen_ts}';
    s.onload = () => {{
      if (DATA_CACHE[key] && !DATA_CACHE['_base_' + key]) {{
        DATA_CACHE['_base_' + key] = DATA_CACHE[key];
        DATA_CACHE[key] = applyLiveDelta(key, DATA_CACHE[key]);
      }}
      resolve();
    }};
    s.onerror = () => resolve();
    document.head.appendChild(s);
  }});
}}
const _levelLoadPromises = {{}};
function loadChartData(key) {{
  if (DATA_CACHE[key]) return Promise.resolve(DATA_CACHE[key]);
  if (!_levelLoadPromises[key]) {{
    _levelLoadPromises[key] = _loadLevelFile(key);
    const code = _extractCode(key);
    ['daily','30min','5min'].forEach(lv => {{
      const other = code + '_' + lv;
      if (other !== key && !_levelLoadPromises[other]) {{
        _levelLoadPromises[other] = new Promise(r => setTimeout(() => _loadLevelFile(other).then(r), 50));
      }}
    }});
  }}
  return _levelLoadPromises[key].then(() => DATA_CACHE[key] || null);
}}

// Load live.js at startup for delta merge
let _liveReady = false;
(function() {{
  const ls = document.createElement('script');
  ls.src = 'data/live.js?v=' + Date.now();
  ls.onload = () => {{
    _liveReady = true;
    // Re-apply delta to any chart data that loaded before live.js
    if (typeof LIVE_DATA !== 'undefined') {{
      for (const key of Object.keys(DATA_CACHE)) {{
        if (LIVE_DATA[key]) {{
          DATA_CACHE[key] = applyLiveDelta(key, DATA_CACHE['_base_' + key] || DATA_CACHE[key]);
        }}
      }}
    }}
  }};
  ls.onerror = () => {{ _liveReady = true; }};
  document.head.appendChild(ls);
}})();

// ─── Mobile Watchlist Panel (自选股最新买卖点) ───

let mgsTab = 'DF';
let mgsExpanded = true;
let mgsPage = 1;
const mgsPageSize = 10;
let mgsT1Open = false;
let mgsT2Open = false;
let mgsT3Open = true;
let mgsPending3bOpen = false;
let mgsPending3bLv = 'all';
let mgsCat = 'stock';
function renderMobileGlobalSignals() {{
  const el = document.getElementById('mobileGlobalSignals');
  const levels = ['WF', 'DF', '30F', '5F', '1F'];
  const categories = [
    {{id:'stock', label:'📊 个股'}},
    {{id:'etf', label:'📈 ETF'}},
    {{id:'watchlist', label:'⭐ 自选'}}
  ];
  const confIcons = {{'high': '🔴', 'medium': '🟡', 'low': '⚪'}};
  const tClrs = {{'1B': '#f85149', '2B': '#f85149', '3B': '#f85149', '1S': '#3fb950', '2S': '#3fb950', '3S': '#3fb950'}};

  const strMap = {{strongest: '🔥最强', strong: '💪强', standard: '📌标准', weak: '⚠弱'}};

  function getMobileSrc(cat) {{
    if (cat === 'stock') return SIGNAL_DATA.stock || {{}};
    if (cat === 'etf') return SIGNAL_DATA.etf || {{}};
    return WATCHLIST_SIGNALS || {{}};
  }}

  let totalAll = 0, buyCnt = 0, sellCnt = 0;
  categories.forEach(cat => {{
    const src = getMobileSrc(cat.id);
    levels.forEach(lv => {{
      const d = src[lv] || {{}};
      ['type1','type2','type3'].forEach(k => {{
        const arr = d[k] || [];
        totalAll += arr.length;
        arr.forEach(s => {{ if (s.type && s.type.endsWith('B')) buyCnt++; else sellCnt++; }});
      }});
    }});
  }});
  function mgsRow(s, i, isType3) {{
    const bg = i % 2 === 0 ? '#0d1117' : '#161b22';
    const tc = tClrs[s.type] || '#c9d1d9';
    let confStr = (confIcons[s.conf] || '') + (s.conf === 'high' ? '高' : s.conf === 'medium' ? '中' : s.conf === 'low' ? '低' : '');
    if (s.conf_score !== undefined && s.conf_score !== null) confStr += '<span style="color:#8b949e;font-size:9px">(' + s.conf_score + ')</span>';
    let strStr = strMap[s.strength] || s.strength || '-';
    if (s.str_score !== undefined && s.str_score !== null) strStr += '<span style="color:#8b949e;font-size:9px">(' + s.str_score + ')</span>';
    const dtShort = s.dt ? s.dt.substring(5) : '-';
    const inv = s.status === 'invalidated';
    const pending = s.status === 'pending';
    const rowOpacity = inv ? 'opacity:0.45;' : '';
    const strike = inv ? 'text-decoration:line-through;' : '';
    const mGsBuyType = ['1B','2B','3B','PB'].includes(s.type);
    const mGsConfClr = mGsBuyType ? '#f85149' : '#3fb950';
    const statusTag = inv ? '<span style="font-size:9px;color:#da3633;margin-left:2px">✗</span>' : (pending ? '<span style="font-size:9px;color:#d29922;margin-left:2px">⏳</span>' : '<span style="font-size:9px;color:' + mGsConfClr + ';margin-left:2px">✓</span>');
    const mIdxInfo = INDEX_LIST.find(x => x.etf_code === s.etf_code);
    const mTrend = mIdxInfo ? (mIdxInfo.trend || '') : '';
    const _mBk = mTrend.includes('破坏');
    const _mUp = !_mBk && mTrend.includes('上涨');
    const _mDn = !_mBk && mTrend.includes('下跌');
    const _mPanUp = !_mBk && !_mUp && !_mDn && mTrend.includes('盘整偏多');
    const _mPanDn = !_mBk && !_mUp && !_mDn && mTrend.includes('盘整偏空');
    const _mPan = !_mBk && !_mUp && !_mDn && !_mPanUp && !_mPanDn && mTrend.includes('盘整');
    const mTrendIcon = _mBk ? '<span style="color:#e3b341" title="DF趋势破坏">⚠</span>'
      : _mUp ? '<span style="color:#f85149" title="DF上涨趋势">▲</span>'
      : _mDn ? '<span style="color:#3fb950" title="DF下跌趋势">▼</span>'
      : _mPanUp ? '<span style="color:#f0883e" title="DF盘整偏多">◆↑</span>'
      : _mPanDn ? '<span style="color:#7ee787" title="DF盘整偏空">◆↓</span>'
      : _mPan ? '<span style="color:#d29922" title="DF盘整">◆</span>'
      : '<span style="color:#8b949e" title="DF方向不明">—</span>';
    let r = `<tr style="background:${{bg}};border-bottom:1px solid #21262d;white-space:nowrap;${{rowOpacity}}">`;
    r += `<td style="padding:3px 4px;font-family:monospace;font-size:10px;white-space:nowrap;${{strike}}">${{dtShort}}</td>`;
    r += `<td style="padding:3px 4px;font-weight:600;${{strike}}">${{mTrendIcon}} <a href="javascript:void(0)" onclick="switchIndex('${{s.etf_code}}');switchLevel('${{s.level_key||'daily'}}')" style="color:#58a6ff;text-decoration:none">${{s.etf_name}}</a></td>`;
    r += `<td style="padding:3px 4px;text-align:center;font-weight:bold;color:${{tc}};${{strike}}">${{s.label}}${{statusTag}}</td>`;
    const mHtClr = s.hub_type === '线段中枢' ? '#ffd700' : '#58a6ff';
    const _mSlvl = s.signal_level || '';
    const _mLpfx = _mSlvl.replace(/[一二三][买卖]$/, '');
    const mHtLabel = _mLpfx ? _mLpfx + '枢' : (s.hub_type === '线段中枢' ? '段枢' : '笔枢');
    r += `<td style="padding:3px 4px;text-align:center;font-size:9px;color:${{mHtClr}}">${{mHtLabel}}</td>`;
    if (!isType3) {{
      r += `<td style="padding:3px 4px;text-align:center;font-size:9px;color:#e3b341;${{strike}}">${{s.signal_level || '-'}}</td>`;
      r += `<td style="padding:3px 4px;text-align:center;font-size:10px">${{strStr}}</td>`;
      r += `<td style="padding:3px 4px;text-align:center;font-size:10px">${{confStr}}</td>`;
    }}
    if (isType3) {{
      const rk = s.hub_rank;
      const rkL = {{0:'0末端',1:'1首个',2:'2第二',3:'3第三'}};
      const rkC = {{0:'#f0883e',1:'#3fb950',2:'#d29922',3:'#8b949e'}};
      let rkS = '-', rkClr = '#8b949e';
      if (rk !== undefined && rk >= 0) {{
        rkS = rkL[rk] || rk + '第' + rk;
        rkClr = rkC[rk] || (rk <= 5 ? '#da3633' : '#6e7681');
      }}
      r += `<td style="padding:3px 4px;text-align:center;font-size:10px;font-weight:600;color:${{rkClr}}">${{rkS}}</td>`;
      const hw = s.hub_width;
      r += `<td style="padding:3px 4px;text-align:center;font-size:10px">${{hw > 0 ? hw + '笔' : '-'}}</td>`;
      // Sub-level confirmation for 3B
      let tbcM = '-';
      if (s.type === '3B' && s.tbc) {{
        tbcM = s.tbc.confirmed ? `<span style="color:#3fb950;font-size:9px">✅${{s.tbc.type}}</span>` : '<span style="color:#d29922;font-size:9px">⏳</span>';
      }}
      r += `<td style="padding:3px 4px;text-align:center;font-size:10px">${{tbcM}}</td>`;
    }}
    r += '</tr>';
    return r;
  }}

  function mgsTable(title, signals, isType3, toggleVar, isOpen) {{
    const cnt = signals.length;
    const arrow = isOpen ? '▼' : '▶';
    let t = '<div style="margin-bottom:4px">';
    t += '<div onclick="' + toggleVar + '=!' + toggleVar + ';renderMobileGlobalSignals()" style="font-size:11px;font-weight:bold;color:#c9d1d9;margin:6px 0 3px;cursor:pointer;user-select:none;display:flex;align-items:center;gap:4px">';
    t += '<span style="font-size:9px;color:#8b949e">' + arrow + '</span> ' + title + ' (' + cnt + ')</div>';
    if (isOpen) {{
      t += '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">';
      t += '<table style="width:100%;border-collapse:collapse;font-size:11px;color:#c9d1d9;background:#161b22">';
      t += '<thead><tr style="background:#21262d;color:#8b949e;font-size:10px;white-space:nowrap">';
      t += '<th style="padding:4px;text-align:left">时间</th>';
      t += '<th style="padding:4px;text-align:left">标的</th>';
      t += '<th style="padding:4px;text-align:center">类型</th>';
      t += '<th style="padding:4px;text-align:center">中枢</th>';
      if (!isType3) {{ t += '<th style="padding:4px;text-align:center">级别</th>'; t += '<th style="padding:4px;text-align:center">强度</th>'; t += '<th style="padding:4px;text-align:center">置信</th>'; }}
      if (isType3) {{ t += '<th style="padding:4px;text-align:center">位次</th>'; t += '<th style="padding:4px;text-align:center">笔数</th>'; t += '<th style="padding:4px;text-align:center">次级</th>'; }}
      t += '</tr></thead><tbody>';
      const cols = isType3 ? 7 : 7;
      signals.forEach((s, i) => {{ t += mgsRow(s, i, isType3); }});
      if (signals.length === 0) {{
        t += `<tr><td colspan="${{cols}}" style="padding:8px;text-align:center;color:#484f58;font-size:10px">暂无信号</td></tr>`;
      }}
      t += '</tbody></table></div>';
    }}
    t += '</div>';
    return t;
  }}

  let h = '<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden">';
  h += '<div onclick="mgsExpanded=!mgsExpanded;renderMobileGlobalSignals()" style="display:flex;align-items:center;padding:8px 10px;cursor:pointer;user-select:none">';
  h += '<span style="color:#c9d1d9;font-size:12px;font-weight:bold;flex:1">📡 最新买卖点';
  h += ' <span style="font-size:10px;color:#8b949e;font-weight:400">' + totalAll + '个';
  if (buyCnt > 0) h += ' · <span style="color:#f85149">' + buyCnt + '买</span>';
  if (sellCnt > 0) h += ' · <span style="color:#3fb950">' + sellCnt + '卖</span>';
  h += '</span></span>';
  h += '<span style="color:#8b949e;font-size:10px;transition:transform 0.2s;transform:rotate(' + (mgsExpanded ? '180' : '0') + 'deg)">▼</span>';
  h += '</div>';

  if (mgsExpanded) {{
  h += '<div style="padding:0 10px 8px">';

  // Level 1 tabs: category
  h += '<div style="display:flex;gap:0;margin-bottom:6px;border-bottom:1px solid #30363d">';
  categories.forEach(cat => {{
    const src = getMobileSrc(cat.id);
    let catTotal = 0;
    levels.forEach(lv => {{
      const d = src[lv] || {{}};
      catTotal += (d.type1||[]).length + (d.type2||[]).length + (d.type3||[]).length;
    }});
    const active = cat.id === mgsCat;
    const bg = active ? '#21262d' : 'transparent';
    const clr = active ? '#58a6ff' : '#8b949e';
    const border = active ? '2px solid #58a6ff' : '2px solid transparent';
    h += `<button onclick="mgsCat='${{cat.id}}';mgsPage=1;renderMobileGlobalSignals()" style="padding:5px 10px;border:none;border-bottom:${{border}};background:${{bg}};color:${{clr}};cursor:pointer;font-size:11px;font-weight:600;border-radius:4px 4px 0 0">${{cat.label}} (${{catTotal}})</button>`;
  }});
  h += '</div>';

  // Level 2 tabs: timeframe
  const currentSrc = getMobileSrc(mgsCat);
  h += '<div style="display:flex;gap:4px;margin-bottom:6px">';
  levels.forEach(lv => {{
    const d = currentSrc[lv] || {{}};
    const total = (d.type1||[]).length + (d.type2||[]).length + (d.type3||[]).length;
    const active = lv === mgsTab;
    const bg = active ? '#21262d' : 'transparent';
    const clr = active ? '#58a6ff' : '#8b949e';
    const border = active ? '2px solid #58a6ff' : '2px solid transparent';
    h += `<button onclick="mgsTab='${{lv}}';mgsPage=1;renderMobileGlobalSignals()" style="padding:4px 10px;border:none;border-bottom:${{border}};background:${{bg}};color:${{clr}};cursor:pointer;font-size:12px;border-radius:4px 4px 0 0">${{lv}} (${{total}})</button>`;
  }});
  h += '</div>';

  const data = currentSrc[mgsTab] || {{}};
  const gAllT1 = data.type1 || [];
  const gAllT2 = data.type2 || [];
  const gAllT3 = data.type3 || [];
  const gAllP3B = (PENDING_3B || []).filter(p => p.status !== '突破等待回抽');
  const gMaxItems = Math.max(gAllT1.length, gAllT2.length, gAllT3.length, gAllP3B.length);
  const gTotalPages = Math.min(10, Math.max(1, Math.ceil(gMaxItems / mgsPageSize)));
  if (mgsPage > gTotalPages) mgsPage = gTotalPages;
  const gOff = (mgsPage - 1) * mgsPageSize;
  const gT1 = gAllT1.slice(gOff, gOff + mgsPageSize);
  const gT2 = gAllT2.slice(gOff, gOff + mgsPageSize);
  const gT3 = gAllT3.slice(gOff, gOff + mgsPageSize);
  h += mgsTable('🔴 第一类买卖点', gT1, false, 'mgsT1Open', mgsT1Open);
  h += mgsTable('🟠 第二类买卖点', gT2, false, 'mgsT2Open', mgsT2Open);
  h += mgsTable('🔵 第三类买卖点', gT3, true, 'mgsT3Open', mgsT3Open);

  // Pending 3B table with level tabs
  (function() {{
    if (gAllP3B.length === 0) return;
    const lvMap = {{weekly:'WF',daily:'DF','30min':'30F','5min':'5F','1min':'1F'}};
    const lvReverse = {{'WF':'weekly','DF':'daily','30F':'30min','5F':'5min','1F':'1min'}};
    const p3bLevels = ['all', 'WF', 'DF', '30F', '5F', '1F'];
    const p3bFiltered = mgsPending3bLv === 'all' ? gAllP3B : gAllP3B.filter(p => lvMap[p.level] === mgsPending3bLv || p.level === mgsPending3bLv);
    const arrow = mgsPending3bOpen ? '▼' : '▶';
    let t = '<div style="margin-bottom:4px">';
    t += '<div style="display:flex;align-items:center;justify-content:space-between;margin:6px 0 3px">';
    t += '<div onclick="mgsPending3bOpen=!mgsPending3bOpen;renderMobileGlobalSignals()" style="font-size:11px;font-weight:bold;color:#c9d1d9;cursor:pointer;user-select:none;display:flex;align-items:center;gap:4px">';
    t += '<span style="font-size:9px;color:#8b949e">' + arrow + '</span> 👁 三买观察 (' + p3bFiltered.length + ')</div>';
    t += '<div style="display:flex;gap:2px">';
    p3bLevels.forEach(lv => {{
      const cnt = lv === 'all' ? gAllP3B.length : gAllP3B.filter(p => lvMap[p.level] === lv || p.level === lv).length;
      if (cnt === 0 && lv !== 'all') return;
      const active = lv === mgsPending3bLv;
      const bg = active ? '#21262d' : 'transparent';
      const clr = active ? '#58a6ff' : '#8b949e';
      const label = lv === 'all' ? '全部' : lv;
      t += `<button onclick="mgsPending3bLv='${{lv}}';renderMobileGlobalSignals()" style="padding:2px 6px;border:1px solid ${{active?'#58a6ff':'#30363d'}};border-radius:3px;background:${{bg}};color:${{clr}};cursor:pointer;font-size:9px">${{label}}(${{cnt}})</button>`;
    }});
    t += '</div></div>';
    if (mgsPending3bOpen) {{
      const items = p3bFiltered.slice(gOff, gOff + mgsPageSize);
      if (items.length > 0) {{
        t += '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">';
        t += '<table style="width:100%;border-collapse:collapse;font-size:11px;color:#c9d1d9;background:#161b22">';
        t += '<thead><tr style="background:#21262d;color:#8b949e;font-size:10px;white-space:nowrap">';
        t += '<th style="padding:4px;text-align:left">标的</th>';
        t += '<th style="padding:4px;text-align:center">级别</th>';
        t += '<th style="padding:4px;text-align:center">状态</th>';
        t += '<th style="padding:4px;text-align:center">ZG</th>';
        t += '<th style="padding:4px;text-align:center">余量</th>';
        t += '</tr></thead><tbody>';
        items.forEach((p, i) => {{
          const bg = i % 2 === 0 ? '#0d1117' : '#161b22';
          const statusColor = p.status === '回抽已至ZG附近' ? '#f85149' : p.status === '回抽进行中' ? '#d29922' : '#8b949e';
          const marginColor = p.margin_pct < 15 ? '#f85149' : p.margin_pct < 50 ? '#d29922' : '#3fb950';
          t += `<tr style="background:${{bg}};border-bottom:1px solid #21262d;white-space:nowrap;cursor:pointer" onclick="switchIndex('${{p.etf_code}}')">`;
          t += `<td style="padding:3px 4px;color:#58a6ff;font-weight:600">${{p.etf_name||p.etf_code}}</td>`;
          t += `<td style="padding:3px 4px;text-align:center">${{lvMap[p.level]||p.level}}</td>`;
          t += `<td style="padding:3px 4px;text-align:center;color:${{statusColor}};font-size:10px">${{p.status}}</td>`;
          t += `<td style="padding:3px 4px;text-align:center;font-weight:600">${{(p.hub_zg||0).toFixed(2)}}</td>`;
          t += `<td style="padding:3px 4px;text-align:center;color:${{marginColor}};font-weight:600">${{(p.margin_pct||0).toFixed(0)}}%</td>`;
          t += '</tr>';
        }});
        t += '</tbody></table></div>';
        t += '<div style="margin-top:2px;font-size:9px;color:#484f58">余量=距ZG距离，越小越接近三买确认</div>';
      }}
    }}
    t += '</div>';
    h += t;
  }})();

  if (gTotalPages > 1) {{
    h += '<div style="display:flex;justify-content:center;align-items:center;gap:6px;margin:8px 0 4px">';
    for (let pg = 1; pg <= gTotalPages; pg++) {{
      const isActive = pg === mgsPage;
      const bgP = isActive ? '#58a6ff' : '#21262d';
      const clrP = isActive ? '#fff' : '#8b949e';
      h += `<button onclick="mgsPage=${{pg}};renderMobileGlobalSignals()" style="min-width:28px;padding:3px 8px;border:1px solid ${{isActive?'#58a6ff':'#30363d'}};border-radius:4px;background:${{bgP}};color:${{clrP}};cursor:pointer;font-size:11px">${{pg}}</button>`;
    }}
    h += `<span style="color:#484f58;font-size:10px;margin-left:4px">${{mgsPage}}/${{gTotalPages}}</span>`;
    h += '</div>';
  }}
  h += '</div>';
  }}
  h += '</div>';
  el.innerHTML = h;
}}
renderMobileGlobalSignals();

let currentIndex = '{first_code}';
let currentLevel = '30min';
let viewStart = 0, viewEnd = 0;
let isDragging = false, dragStartX = 0, dragStartView = 0;
let pinchStartDist = 0, pinchStartRange = 0;
const MIN_VIEW = 20;
let dataLoading = false;
let bspHitAreas = [];

function formatAssetLabel(idx) {{
  const suffix = idx.market === 'SH' ? '.SH' : (idx.market === 'SZ' ? '.SZ' : '');
  if (idx.type === 'stock') {{
    return idx.index_name + ' (' + idx.etf_code + suffix + ')';
  }}
  if (idx.index_code && idx.index_code !== idx.etf_code) {{
    return idx.index_name + ' (' + idx.index_code + ' / ETF: ' + idx.etf_code + ')';
  }}
  return idx.index_name + ' (' + idx.etf_code + suffix + ')';
}}
if (INDEX_LIST.length > 0) {{
  document.getElementById('mCurrentAsset').textContent = formatAssetLabel(INDEX_LIST[0]);
}}

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
  const total = d.dates.length;
  const hubs = d.hubs || [];
  const need2Hub = hubs.length >= 2 ? (total - hubs[hubs.length - 2].x0 + 10) : 120;
  const VISIBLE_BARS = Math.max(120, need2Hub);
  viewEnd = total;
  viewStart = Math.max(0, total - VISIBLE_BARS);
  if (viewEnd - viewStart < MIN_VIEW) viewStart = Math.max(0, viewEnd - MIN_VIEW);
}}
function clampView(total) {{
  let range = viewEnd - viewStart;
  if (range < MIN_VIEW) {{ const mid = (viewStart + viewEnd) / 2; viewStart = Math.round(mid - MIN_VIEW / 2); viewEnd = viewStart + MIN_VIEW; }}
  if (viewStart < 0) {{ viewStart = 0; viewEnd = Math.min(viewEnd - viewStart, total); }}
  if (viewEnd > total) {{ viewEnd = total; viewStart = Math.max(0, total - (viewEnd - viewStart)); }}
}}

let mNavOpen = false;
function toggleMobileNav() {{
  mNavOpen = !mNavOpen;
  document.getElementById('mNavPanel').style.display = mNavOpen ? '' : 'none';
  document.getElementById('mToggleNav').textContent = mNavOpen ? '收起 ▲' : '展开 ▼';
}}

async function switchIndex(code) {{
  currentIndex = code;
  document.querySelectorAll('.idx-tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('idxSearch').value = '';
  filterIdxTabs('');
  const idx = INDEX_LIST.find(x => x.etf_code === code);
  if (idx) document.getElementById('mCurrentAsset').textContent = formatAssetLabel(idx);
  if (mNavOpen) toggleMobileNav();
  mUpdateInfoBarFromIndex();
  if (!_mChartInited) {{ await mInitChart(); return; }}
  await loadAndRender();
}}

function filterIdxTabs(query) {{
  const q = query.trim().toLowerCase();
  const tabs = document.querySelectorAll('#idxTabs .idx-tab');
  const seps = document.querySelectorAll('#idxTabs .idx-sep');
  if (!q) {{
    tabs.forEach(t => t.style.display = '');
    seps.forEach(s => s.style.display = '');
    return;
  }}
  seps.forEach(s => s.style.display = 'none');
  tabs.forEach(t => {{
    const text = (t.dataset.search || t.textContent).toLowerCase();
    t.style.display = text.includes(q) ? '' : 'none';
  }});
}}
async function switchLevel(level) {{
  currentLevel = level;
  document.querySelectorAll('.level-tab').forEach(t => t.classList.remove('active'));
  const order = ['weekly', 'daily', '30min', '5min', '1min'];
  const tabs = document.querySelectorAll('.level-tab');
  const i = order.indexOf(level);
  if (i >= 0 && tabs[i]) tabs[i].classList.add('active');
  if (!_mChartInited) {{ await mInitChart(); return; }}
  await loadAndRender();
}}

function render() {{
  const d = getData();
  if (!d) return;
  if (viewEnd === 0) viewEnd = d.dates.length;
  updateInfoBar(d);
  renderKline(d);
  renderMACD(d);
  renderVolume(d);
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

  const mEnv = idx.daily_env || {{}};
  const mEnvColor = mEnv.color || '#8b949e';
  const mEnvAdvice = mEnv.advice || '';

  const isUp = (idx.trend||'').includes('上涨');
  const tCls = isUp ? 'tag-up' : ((idx.trend||'').includes('下跌') ? 'tag-down' : 'tag-neutral');
  const m30IsUp = (idx.m30_trend||'').includes('上涨');
  const m30Cls = m30IsUp ? 'tag-up' : ((idx.m30_trend||'').includes('下跌') ? 'tag-down' : 'tag-neutral');
  const dSigType = idx.latest_signal_type || '';
  const dSigCls = dSigType.includes('B') ? 'tag-up' : (dSigType.includes('S') ? 'tag-down' : 'tag-neutral');
  const m30SigType = idx.m30_signal_type || '';
  const m30SigCls = m30SigType.includes('B') ? 'tag-up' : (m30SigType.includes('S') ? 'tag-down' : 'tag-neutral');

  // One-line action conclusion: pick the most important bullet
  const conParts = (idx.conclusion||'').split(' · ').filter(p => p);
  let actionText = '';
  let actionColor = '#c9d1d9';
  for (const p of conParts) {{
    if (p.includes('共振') || p.includes('买点') || p.includes('卖点') || p.startsWith('⚠')) {{
      actionText = p;
      if (p.includes('买') || p.includes('加仓') || p.includes('多头')) actionColor = '#f85149';
      else if (p.includes('卖') || p.includes('清仓') || p.includes('空头')) actionColor = '#3fb950';
      else actionColor = '#d29922';
      break;
    }}
  }}
  if (!actionText && conParts.length > 0) {{
    actionText = conParts[0];
  }}

  const d = getData();
  const tentTag = (d && d.tentative > 0)
    ? ' <span style="color:#d29922;font-size:10px">⚠暂定</span>'
    : '';

  const notesHtml = idx.notes ? `<span style="color:#6e7681;font-size:10px">${{idx.notes}}</span>` : '';

  // Three-buy sub-level confirmation badge (mobile)
  const tbs = idx.three_buy_status;
  let tbcTag = '';
  if (tbs) {{
    if (tbs.has_confirmed && tbs.latest) {{
      const lt = tbs.latest;
      tbcTag = `<span style="background:#1a2a1a;color:#3fb950;padding:1px 5px;border-radius:3px;font-size:10px;border:1px solid #3fb950" title="${{lt.level}}三买@${{lt.dt}} ${{lt.note}}">✅三买${{lt.confirmation_type}}</span>`;
    }} else if (tbs.count_pending > 0) {{
      tbcTag = `<span style="background:#2a2a1a;color:#d29922;padding:1px 5px;border-radius:3px;font-size:10px;border:1px solid #d29922">⏳三买待确认(${{tbs.count_pending}})</span>`;
    }}
  }}

  bar.innerHTML = `
    <span style="background:${{scoreBg}};color:${{scoreClr}};padding:1px 6px;border-radius:3px;font-weight:700">${{sc}}</span>
    <span style="color:${{mEnvColor}};font-size:11px" title="${{mEnvAdvice}}">${{mEnvAdvice || '-'}}</span>
    <span style="color:${{actionColor}};font-size:11px;font-weight:600">${{actionText}}</span>${{tentTag}}
    ${{tbcTag}}
    <br>
    <span class="tag ${{tCls}}" style="font-size:10px">${{(idx.trend||'-').replace('趋势','')}}</span>
    ${{idx.latest_signal && idx.latest_signal !== '-' ? '<span class="tag ' + dSigCls + '" style="font-size:10px">' + idx.latest_signal + '</span>' : ''}}
    <span style="color:#484f58;font-size:10px">→</span>
    <span class="tag ${{m30Cls}}" style="font-size:10px">${{(idx.m30_trend||'-').replace('趋势','')}}</span>
    ${{idx.m30_signal && idx.m30_signal !== '-' ? '<span class="tag ' + m30SigCls + '" style="font-size:10px">' + idx.m30_signal + '</span>' : ''}}
    ${{notesHtml}}
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

  const evoColors = {{'延伸': '#8b949e', '新生（上）': '#f85149', '新生（下）': '#3fb950', '扩展': '#d29922', '延伸升级': '#e3b341', '扩张': '#e3b341', '扩展合并': '#d29922', '扩张合并': '#d29922'}};
  const mHubDirClr = {{1: {{f:'rgba(248,81,73,0.18)', s:'rgba(248,81,73,0.55)'}}, '-1': {{f:'rgba(63,185,80,0.18)', s:'rgba(63,185,80,0.55)'}}, 0: {{f:'rgba(88,166,255,0.12)', s:'rgba(88,166,255,0.45)'}}}};
  // Stroke-level hubs (笔中枢, drawn behind candlesticks)
  data.hubs.forEach(h => {{
    if (h.x1 < viewStart || h.x0 >= viewEnd) return;
    const x0 = scaleX(Math.max(h.x0, viewStart)) - cw / 2;
    const x1 = scaleX(Math.min(h.x1, viewEnd - 1)) + cw / 2;
    const hdc = mHubDirClr[h.dir] || mHubDirClr[0];
    ctx.fillStyle = hdc.f;
    ctx.fillRect(x0, scaleY(h.zg), x1 - x0, scaleY(h.zd) - scaleY(h.zg));
    ctx.strokeStyle = hdc.s; ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.strokeRect(x0, scaleY(h.zg), x1 - x0, scaleY(h.zd) - scaleY(h.zg));
    ctx.setLineDash([]);
    const evoClr = evoColors[h.evo] || '#58a6ff';
    const mVolIcons = {{'shrink': '📉', 'expand': '📈'}};
    const volMark = mVolIcons[h.vol_trend] || '';
    const dirIcon = h.direction === '上' ? '↑' : (h.direction === '下' ? '↓' : '');
    const seqLabel = h.trend_seq >= 0 ? '#' + (h.trend_seq + 1) : '';
    const lvlTag = h.hub_level || '';
    ctx.fillStyle = evoClr; ctx.font = '8px sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(lvlTag + h.idx + dirIcon + seqLabel + (h.evo ? ' ' + h.evo : '') + volMark, x1 + 2, scaleY(h.zg) + 8);
    ctx.fillStyle = '#58a6ff';
    ctx.fillText('ZG=' + h.zg.toFixed(2), x1 + 2, scaleY(h.zg) + 17);
    ctx.fillText('ZD=' + h.zd.toFixed(2), x1 + 2, scaleY(h.zd) + 10);
  }});

  // Segment-level hubs (线段中枢, direction-colored, drawn behind stroke hubs)
  const canvasSegHubClr = {{1: ['rgba(248,81,73,0.10)', 'rgba(248,81,73,0.60)', 'rgba(248,81,73,0.85)'],
                            '-1': ['rgba(63,185,80,0.10)', 'rgba(63,185,80,0.60)', 'rgba(63,185,80,0.85)'],
                            0: ['rgba(88,166,255,0.08)', 'rgba(88,166,255,0.50)', '#8b949e']}};
  (data.seg_hubs || []).forEach(sh => {{
    if (sh.x1 < viewStart || sh.x0 >= viewEnd) return;
    const x0 = scaleX(Math.max(sh.x0, viewStart)) - cw / 2;
    const x1 = scaleX(Math.min(sh.x1, viewEnd - 1)) + cw / 2;
    const cc = canvasSegHubClr[sh.dir] || canvasSegHubClr[0];
    ctx.fillStyle = cc[0];
    ctx.fillRect(x0, scaleY(sh.zg), x1 - x0, scaleY(sh.zd) - scaleY(sh.zg));
    ctx.strokeStyle = cc[1]; ctx.lineWidth = 1.5;
    ctx.setLineDash([]);
    ctx.strokeRect(x0, scaleY(sh.zg), x1 - x0, scaleY(sh.zd) - scaleY(sh.zg));
    const dirIcon = sh.direction === '上' ? '↑' : (sh.direction === '下' ? '↓' : '');
    const seqLabel = sh.trend_seq >= 0 ? '#' + (sh.trend_seq + 1) : '';
    const lvlTag = sh.hub_level || '线段中枢';
    ctx.fillStyle = cc[2]; ctx.font = 'bold 9px sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(lvlTag + sh.idx + dirIcon + seqLabel + (sh.evo ? ' ' + sh.evo : ''), x0 + 2, scaleY(sh.zg) - 3);
  }});

  // MA5 / MA10 lines
  function drawMA(maArr, color) {{
    if (!maArr || maArr.length === 0) return;
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash([]);
    ctx.beginPath();
    let started = false;
    for (let gi = viewStart; gi < viewEnd; gi++) {{
      if (maArr[gi] === null || maArr[gi] === undefined) {{ started = false; continue; }}
      const x = scaleX(gi); const y = scaleY(maArr[gi]);
      if (!started) {{ ctx.moveTo(x, y); started = true; }}
      else ctx.lineTo(x, y);
    }}
    ctx.stroke();
  }}
  drawMA(data.ma5, '#ffd700');
  drawMA(data.ma10, '#58a6ff');
  // MA250 with dashed line
  if (data.ma250 && data.ma250.length > 0) {{
    ctx.strokeStyle = '#e040fb'; ctx.lineWidth = 1.2; ctx.setLineDash([4, 3]);
    ctx.beginPath();
    let _ma250s = false;
    for (let gi = viewStart; gi < viewEnd; gi++) {{
      if (data.ma250[gi] == null) {{ _ma250s = false; continue; }}
      const x = scaleX(gi), y = scaleY(data.ma250[gi]);
      if (!_ma250s) {{ ctx.moveTo(x, y); _ma250s = true; }}
      else ctx.lineTo(x, y);
    }}
    ctx.stroke(); ctx.setLineDash([]);
  }}

  // Candlesticks
  const tentLast = data.tentative > 0 ? kline.length - 1 : -1;
  for (let gi = viewStart; gi < viewEnd; gi++) {{
    const [o, c, lo, hi] = kline[gi];
    const x = scaleX(gi);
    const bw = Math.max(cw * 0.6, 1);
    const isUp = c >= o;
    const isTent = gi === tentLast;
    ctx.globalAlpha = isTent ? 0.45 : 1.0;
    ctx.strokeStyle = ctx.fillStyle = isUp ? '#f85149' : '#3fb950';
    ctx.lineWidth = 1;
    if (isTent) {{ ctx.setLineDash([3, 2]); }}
    ctx.beginPath(); ctx.moveTo(x, scaleY(hi)); ctx.lineTo(x, scaleY(lo)); ctx.stroke();
    const top = scaleY(Math.max(o, c));
    const bot = scaleY(Math.min(o, c));
    ctx.fillRect(x - bw / 2, top, bw, Math.max(bot - top, 1));
    if (isTent) {{ ctx.setLineDash([]); ctx.globalAlpha = 1.0; }}
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

  // Fractal markers: same triangle style as BSP
  (data.fractals || []).forEach(f => {{
    if (f.idx < viewStart || f.idx >= viewEnd) return;
    const x = scaleX(f.idx);
    const isTop = f.type === 'top';
    const kBar = kline[f.idx];
    const price = isTop ? (kBar ? Math.max(kBar[1], kBar[2], kBar[3], kBar[0]) : f.price)
                        : (kBar ? Math.min(kBar[1], kBar[2], kBar[3], kBar[0]) : f.price);
    const y = scaleY(price);
    ctx.fillStyle = isTop ? 'rgba(248,81,73,0.7)' : 'rgba(63,185,80,0.7)';
    ctx.beginPath();
    if (isTop) {{
      ctx.moveTo(x, y - 6); ctx.lineTo(x - 4, y - 12); ctx.lineTo(x + 4, y - 12);
    }} else {{
      ctx.moveTo(x, y + 6); ctx.lineTo(x - 4, y + 12); ctx.lineTo(x + 4, y + 12);
    }}
    ctx.closePath(); ctx.fill();
  }});

  // Strokes with volume trend markers + divergence color
  const sVolIcons = {{'shrink': '↓', 'expand': '↑'}};
  data.strokes.forEach(s => {{
    const si = s.coords[0][0], ei = s.coords[1][0];
    if (ei < viewStart || si >= viewEnd) return;
    const sColor = s.div ? (s.dir === 1 ? '#00d4aa' : '#ff6b9d') : '#f0883e';
    ctx.strokeStyle = sColor; ctx.lineWidth = s.div ? 2.5 : 1.5;
    const x1 = scaleX(Math.max(si, viewStart));
    const x2 = scaleX(Math.min(ei, viewEnd - 1));
    ctx.beginPath(); ctx.moveTo(x1, scaleY(s.coords[0][1])); ctx.lineTo(x2, scaleY(s.coords[1][1])); ctx.stroke();
    const labelColor = sColor;
    ctx.fillStyle = labelColor; ctx.font = 'bold 8px sans-serif'; ctx.textAlign = 'center';
    const mx = (x1 + x2) / 2;
    const volSuf = sVolIcons[s.vol_trend] || '';
    ctx.fillText('S' + s.idx + volSuf, mx, (scaleY(s.coords[0][1]) + scaleY(s.coords[1][1])) / 2 - 6);
  }});

  // Buy/Sell markers (compact)
  bspHitAreas = [];
  data.bsp.forEach(p => {{
    if (p.idx < viewStart || p.idx >= viewEnd) return;
    const x = scaleX(p.idx);
    const y = scaleY(p.price);
    const mIsInv = p.status === 'invalidated';
    const mIsPending = p.status === 'pending';
    const mIsT3 = p.type === '3B' || p.type === '3S';
    const triColor = mIsInv ? '#484f58' : (mIsPending ? '#f0883e' : (p.is_buy ? '#f85149' : '#3fb950'));
    const markerSize = mIsT3 && !mIsInv ? 5 : 4;
    const mOff = 6;
    ctx.globalAlpha = mIsInv ? 0.35 : 1.0;
    ctx.beginPath();
    if (mIsT3 && !mIsInv) {{
      const cy = y + (p.is_buy ? mOff : -mOff);
      ctx.moveTo(x, cy - markerSize);
      ctx.lineTo(x + markerSize, cy);
      ctx.lineTo(x, cy + markerSize);
      ctx.lineTo(x - markerSize, cy);
      ctx.closePath();
      ctx.fillStyle = triColor; ctx.fill();
      ctx.strokeStyle = '#ffd700'; ctx.lineWidth = 1; ctx.stroke();
    }} else if (p.is_buy) {{
      ctx.moveTo(x, y + mOff); ctx.lineTo(x - 4, y + mOff + 6); ctx.lineTo(x + 4, y + mOff + 6); ctx.closePath();
      ctx.fillStyle = triColor; ctx.fill();
    }} else {{
      ctx.moveTo(x, y - mOff); ctx.lineTo(x - 4, y - mOff - 6); ctx.lineTo(x + 4, y - mOff - 6); ctx.closePath();
      ctx.fillStyle = triColor; ctx.fill();
    }}
    bspHitAreas.push({{cx: x, cy: p.is_buy ? y + 10 : y - 10, bp: p}});
    ctx.fillStyle = mIsInv ? '#484f58' : (mIsPending ? '#d29922' : (p.is_buy ? '#f85149' : '#3fb950'));
    ctx.globalAlpha = mIsInv ? 0.35 : 0.9;
    ctx.font = '7px sans-serif'; ctx.textAlign = 'center';
    const mLabel = p.label.substring(0, 2);
    let bspText = mLabel;
    if (mIsInv) bspText += '✗';
    else if (mIsPending) bspText += '⏳';
    ctx.fillText(bspText, x, p.is_buy ? y + 20 : y - 14);
    ctx.globalAlpha = 1.0;
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
  const H = Math.min(rect.height || 56, 56);
  canvas.width = rect.width * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width;
  ctx.clearRect(0, 0, W, H);

  const n = viewEnd - viewStart;
  if (!n) return;
  const pad = {{t: 3, b: 8, l: 50, r: 12}};
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

  const tentMacdLast = data.tentative > 0 ? macds.length - 1 : -1;
  macds.forEach((m, i) => {{
    const x = scaleXi(i); const bw = Math.max(cw * 0.5, 1);
    const isTent = i === tentMacdLast;
    ctx.globalAlpha = isTent ? 0.45 : 1.0;
    ctx.fillStyle = m >= 0 ? '#f85149' : '#3fb950';
    const y1 = zeroY, y2 = scaleY(m);
    ctx.fillRect(x - bw / 2, Math.min(y1, y2), bw, Math.abs(y2 - y1) || 1);
    if (isTent) {{ ctx.globalAlpha = 1.0; }}
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

function renderVolume(data) {{
  const canvas = document.getElementById('volumeCanvas');
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const H = Math.min(rect.height || 44, 44);
  canvas.width = rect.width * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width;
  ctx.clearRect(0, 0, W, H);

  const n = viewEnd - viewStart;
  if (!n) return;
  const pad = {{t: 4, b: 4, l: 50, r: 12}};
  const cw = (W - pad.l - pad.r) / n;
  const vols = data.volumes.slice(viewStart, viewEnd);
  const kline = data.kline.slice(viewStart, viewEnd);
  const maxVol = Math.max(...vols) || 1;
  const barH = H - pad.t - pad.b;

  ctx.fillStyle = '#8b949e'; ctx.font = '8px monospace'; ctx.textAlign = 'right';
  ctx.fillText('VOL', pad.l - 4, pad.t + 8);

  const tentLast = data.tentative > 0 ? vols.length - 1 : -1;
  vols.forEach((v, i) => {{
    const x = pad.l + i * cw + cw / 2;
    const bw = Math.max(cw * 0.6, 1);
    const h = (v / maxVol) * barH;
    const [o, c] = kline[i];
    const isTent = i === tentLast;
    ctx.globalAlpha = isTent ? 0.45 : 0.8;
    ctx.fillStyle = c >= o ? '#f85149' : '#3fb950';
    ctx.fillRect(x - bw / 2, H - pad.b - h, bw, h);
  }});
  ctx.globalAlpha = 1.0;
}}


// === BSP Tooltip ===
function showBspTooltip(bp, screenX, screenY) {{
  const el = document.getElementById('bspTooltip');
  const confMap = {{'high': '🔴 高', 'medium': '🟡 中', 'low': '⚪ 低'}};
  const strMap = {{strongest: '🔥最强', strong: '💪强势', standard: '📌标准', weak: '⚠弱'}};
  const statusMap = {{'active': '✅ 有效', 'confirmed': '✅ 已确认', 'invalidated': '❌ 失效', 'pending': '⏳ 待确认'}};
  const d = getData();
  const dateStr = d && d.dates && d.dates[bp.idx] ? d.dates[bp.idx] : '';
  const typeColor = bp.is_buy ? '#f85149' : '#3fb950';
  let h = '<div style="font-weight:bold;font-size:14px;color:' + typeColor + '">#' + bp.bsp_idx + ' ' + bp.label;
  h += ' <span style="float:right;cursor:pointer;color:#8b949e;font-size:16px" onclick="hideBspTooltip()">✕</span></div>';
  h += '<div style="color:#8b949e;margin:2px 0">日期: ' + dateStr + ' | 价格: ' + bp.price.toFixed(3) + '</div>';
  h += '<table style="width:100%;border-collapse:collapse;margin:4px 0">';
  h += '<tr><td style="color:#8b949e;padding:2px 4px 2px 0;white-space:nowrap">强度</td><td>' + (strMap[bp.strength] || bp.strength || '-');
  if (bp.str_score !== undefined && bp.str_score !== null) h += ' <span style="color:#8b949e;font-size:11px">(' + bp.str_score + '分)</span>';
  h += '</td>';
  h += '<td style="color:#8b949e;padding:2px 4px 2px 8px;white-space:nowrap">置信度</td><td>' + (confMap[bp.conf] || bp.conf || '-');
  if (bp.conf_score !== undefined && bp.conf_score !== null) h += ' <span style="color:#8b949e;font-size:11px">(' + bp.conf_score + '分)</span>';
  h += '</td></tr>';
  h += '<tr><td style="color:#8b949e;padding:2px 4px 2px 0;white-space:nowrap">状态</td><td>' + (statusMap[bp.status] || bp.status) + '</td>';
  h += '<td style="color:#8b949e;padding:2px 4px 2px 8px;white-space:nowrap">防狼</td><td>' + (bp.wolf ? '<span style="color:#d29922">⚠ ' + bp.wolf + '</span>' : '✓ 安全') + '</td></tr>';
  h += '</table>';
  function mRenderGradeTable(title, color, details) {{
    if (!details || details.length === 0) return '';
    var t = '<div style="margin:4px 0 2px;color:' + color + ';font-size:12px;font-weight:bold">' + title + '</div>';
    t += '<table style="width:100%;border-collapse:collapse;margin:0 0 4px;font-size:12px">';
    t += '<tr style="border-bottom:1px solid #30363d"><th style="color:#8b949e;text-align:left;padding:2px 4px;font-weight:normal">维度</th><th style="color:#8b949e;text-align:left;padding:2px 4px;font-weight:normal">判断</th><th style="color:#8b949e;text-align:right;padding:2px 4px;font-weight:normal">分值</th></tr>';
    details.forEach(function(dd) {{
      var sc = dd.score;
      var scColor = sc > 0 ? '#3fb950' : (sc < 0 ? '#f85149' : '#8b949e');
      var scStr = sc > 0 ? '+' + sc : String(sc);
      t += '<tr><td style="color:#d2a8ff;padding:1px 4px;white-space:nowrap">' + dd.dim + '</td>';
      t += '<td style="color:#c9d1d9;padding:1px 4px">' + dd.label + '</td>';
      t += '<td style="color:' + scColor + ';text-align:right;padding:1px 4px;font-weight:bold">' + scStr + '</td></tr>';
    }});
    t += '</table>';
    return t;
  }}
  h += mRenderGradeTable('📊 强度明细', '#58a6ff', bp.str_details);
  h += mRenderGradeTable('🎯 置信度明细', '#d2a8ff', bp.conf_details);
  if (bp.ranges && bp.ranges.length >= 2) {{
    const r0 = bp.ranges[0], r1 = bp.ranges[1];
    const ratio = r0.area > 0 ? (r1.area / r0.area * 100).toFixed(1) : '-';
    h += '<div style="margin:3px 0;padding:3px 6px;background:#0d1117;border-radius:4px">';
    h += '<span style="color:#58a6ff">' + r0.label + '=' + r0.area + '</span>';
    h += ' vs <span style="color:#f85149">' + r1.label + '=' + r1.area + '</span>';
    h += ' 背驰比 <b>' + ratio + '%</b></div>';
  }}
  if (bp.pos_advice) {{
    h += '<div style="margin:3px 0;color:#f0883e">💰 ' + bp.pos_advice + '</div>';
  }}
  if (bp.status === 'invalidated' && bp.inv_reason) {{
    h += '<div style="margin:3px 0;color:#da3633">失效: ' + bp.inv_reason + '</div>';
  }}
  if (bp.fractal_bars && bp.fractal_bars.length > 1) {{
    const barLabels = bp.fractal_bars.map(function(bi) {{ return 'K' + (dates[bi] || '').substring(5).replace(':00', ''); }});
    h += '<div style="margin:3px 0;padding:3px 6px;background:#161b22;border-radius:4px;font-size:11px;color:#d2a8ff">';
    h += '📐 分型中间 = ' + barLabels.join('+') + ' 包含合并';
    h += '</div>';
  }}
  h += '<div style="margin:4px 0 0;color:#8b949e;font-size:11px;border-top:1px solid #30363d;padding-top:4px">' + (bp.desc || '') + '</div>';
  el.innerHTML = h;
  el.style.display = 'block';
  const vw = window.innerWidth, vh = window.innerHeight;
  let left = screenX + 8, top = screenY + 8;
  el.style.left = '0px'; el.style.top = '0px';
  const ew = el.offsetWidth, eh = el.offsetHeight;
  if (left + ew > vw - 8) left = Math.max(8, vw - ew - 8);
  if (top + eh > vh - 8) top = Math.max(8, screenY - eh - 8);
  el.style.left = left + 'px'; el.style.top = top + 'px';
}}
function hideBspTooltip() {{
  document.getElementById('bspTooltip').style.display = 'none';
}}
function findNearestBsp(canvasX, canvasY) {{
  const dpr = window.devicePixelRatio || 1;
  const hitRadius = 24;
  let best = null, bestDist = Infinity;
  bspHitAreas.forEach(h => {{
    const dx = canvasX / dpr - h.cx, dy = canvasY / dpr - h.cy;
    const dist = Math.sqrt(dx * dx + dy * dy);
    if (dist < hitRadius && dist < bestDist) {{ best = h; bestDist = dist; }}
  }});
  return best;
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
  let tapStartX = 0, tapStartY = 0, tapMoved = false;
  function handleMouseDown(e) {{ isDragging = true; dragStartX = e.clientX; dragStartView = viewStart; kCanvas.style.cursor = 'grabbing'; hideBspTooltip(); }}
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
  function handleClick(e) {{
    const rect = kCanvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const cx = (e.clientX - rect.left) * dpr;
    const cy = (e.clientY - rect.top) * dpr;
    const hit = findNearestBsp(cx, cy);
    if (hit) {{ showBspTooltip(hit.bp, e.clientX, e.clientY); }}
    else {{ hideBspTooltip(); }}
  }}
  function handleTouchStart(e) {{
    if (e.touches.length === 2) {{
      pinchStartDist = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
      pinchStartRange = viewEnd - viewStart;
      tapMoved = true;
    }} else if (e.touches.length === 1) {{
      isDragging = true; dragStartX = e.touches[0].clientX; dragStartView = viewStart;
      tapStartX = e.touches[0].clientX; tapStartY = e.touches[0].clientY; tapMoved = false;
    }}
  }}
  function handleTouchMove(e) {{
    e.preventDefault();
    tapMoved = true;
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
  function handleTouchEnd(e) {{
    isDragging = false;
    if (!tapMoved && e.changedTouches && e.changedTouches.length > 0) {{
      const t = e.changedTouches[0];
      const rect = kCanvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const cx = (t.clientX - rect.left) * dpr;
      const cy = (t.clientY - rect.top) * dpr;
      const hit = findNearestBsp(cx, cy);
      if (hit) {{ showBspTooltip(hit.bp, t.clientX, t.clientY); }}
      else {{ hideBspTooltip(); }}
    }}
  }}
  const vCanvas = document.getElementById('volumeCanvas');
  [kCanvas, mCanvas, vCanvas].filter(Boolean).forEach(c => {{ c.addEventListener('wheel', handleWheel, {{passive: false}}); c.style.cursor = 'crosshair'; c.style.touchAction = 'none'; }});
  kCanvas.addEventListener('mousedown', handleMouseDown);
  document.addEventListener('mousemove', handleMouseMove);
  document.addEventListener('mouseup', handleMouseUp);
  kCanvas.addEventListener('click', handleClick);
  kCanvas.addEventListener('touchstart', handleTouchStart, {{passive: false}});
  kCanvas.addEventListener('touchmove', handleTouchMove, {{passive: false}});
  kCanvas.addEventListener('touchend', handleTouchEnd);
  kCanvas.addEventListener('dblclick', () => {{ resetView(); render(); }});
  document.addEventListener('click', (e) => {{
    if (!e.target.closest('#bspTooltip') && !e.target.closest('#klineCanvas')) hideBspTooltip();
  }});
}}

// ─── Market Thermometer ───
let thermoTab = '30F';
let thermoExpanded = true;
function renderThermo() {{
  const el = document.getElementById('marketThermo');
  if (!MARKET_THERMO || !MARKET_THERMO.levels) {{ el.innerHTML = ''; return; }}
  const T = MARKET_THERMO;
  const levels = ['WF', 'DF', '30F', '5F', '1F'];

  let h = '<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden">';
  h += '<div onclick="thermoExpanded=!thermoExpanded;renderThermo()" style="display:flex;align-items:center;padding:8px 10px;cursor:pointer;user-select:none">';
  h += '<span style="color:#c9d1d9;font-size:12px;font-weight:bold;flex:1">🌡️ 市场温度计';
  h += ` <span style="font-size:11px;color:${{T.color}};font-weight:700;margin-left:4px">${{T.assess}}</span>`;
  h += ` <span style="font-size:10px;color:#8b949e;font-weight:400">${{T.total}}个标的</span></span>`;
  h += '<span style="color:#8b949e;font-size:10px;transition:transform 0.2s;transform:rotate(' + (thermoExpanded ? '180' : '0') + 'deg)">▼</span>';
  h += '</div>';

  if (thermoExpanded) {{
    h += '<div style="padding:0 10px 10px">';

    h += '<div style="display:flex;gap:4px;margin-bottom:8px">';
    levels.forEach(lv => {{
      const active = lv === thermoTab;
      const bg = active ? '#21262d' : 'transparent';
      const clr = active ? '#58a6ff' : '#8b949e';
      const bdr = active ? '2px solid #58a6ff' : '2px solid transparent';
      h += `<button onclick="thermoTab='${{lv}}';renderThermo()" style="padding:4px 10px;border:none;border-bottom:${{bdr}};background:${{bg}};color:${{clr}};cursor:pointer;font-size:12px;border-radius:4px 4px 0 0">${{lv}}</button>`;
    }});
    h += '</div>';

    const lv = T.levels[thermoTab];
    if (lv) {{
      const hub = lv.hub;
      const sig = lv.sig;
      const total = T.total;

      // Hub position bar
      const abPct = (hub.above / total * 100).toFixed(1);
      const inPct = (hub.inside / total * 100).toFixed(1);
      const blPct = (hub.below / total * 100).toFixed(1);
      h += '<div style="font-size:10px;color:#8b949e;margin-bottom:4px">中枢位置分布</div>';
      h += '<div style="display:flex;height:20px;border-radius:4px;overflow:hidden;margin-bottom:2px">';
      if (hub.above > 0) h += `<div style="flex:${{hub.above}};background:#f85149;display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff;font-weight:600">${{hub.above}}</div>`;
      if (hub.inside > 0) h += `<div style="flex:${{hub.inside}};background:#d29922;display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff;font-weight:600">${{hub.inside}}</div>`;
      if (hub.below > 0) h += `<div style="flex:${{hub.below}};background:#3fb950;display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff;font-weight:600">${{hub.below}}</div>`;
      h += '</div>';
      h += '<div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:10px">';
      h += `<span style="color:#f85149">▲ 上方 ${{abPct}}%</span>`;
      h += `<span style="color:#d29922">◆ 区间 ${{inPct}}%</span>`;
      h += `<span style="color:#3fb950">▼ 下方 ${{blPct}}%</span>`;
      h += '</div>';

      // Signal distribution bar
      const sigTotal = sig['3B'] + sig.PB + sig['1B'] + sig.PS + sig['3S'] + sig.other;
      h += '<div style="font-size:10px;color:#8b949e;margin-bottom:4px">最后信号分布</div>';
      h += '<div style="display:flex;height:20px;border-radius:4px;overflow:hidden;margin-bottom:2px">';
      const sigItems = [
        {{val: sig['3B'], clr: '#f85149', lbl: '三买'}},
        {{val: sig.PB, clr: '#da3633', lbl: '盘买'}},
        {{val: sig['1B'], clr: '#b62324', lbl: '一/二买'}},
        {{val: sig.PS, clr: '#238636', lbl: '盘卖'}},
        {{val: sig['3S'], clr: '#3fb950', lbl: '三卖'}},
        {{val: sig.other, clr: '#7ee787', lbl: '一/二卖'}},
      ];
      sigItems.forEach(s => {{
        if (s.val > 0) h += `<div style="flex:${{s.val}};background:${{s.clr}};display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff;font-weight:600" title="${{s.lbl}}: ${{s.val}}">${{s.val > 5 ? s.val : ''}}</div>`;
      }});
      h += '</div>';
      h += '<div style="display:flex;flex-wrap:wrap;gap:6px 10px;font-size:10px;margin-bottom:10px">';
      sigItems.forEach(s => {{
        if (s.val > 0) {{
          const pct = (s.val / total * 100).toFixed(1);
          h += `<span style="color:${{s.clr}}">${{s.lbl}} ${{s.val}}(${{pct}}%)</span>`;
        }}
      }});
      h += '</div>';

      // Key metrics
      const ratio3 = sig['3S'] > 0 ? (sig['3B'] / sig['3S']).toFixed(2) : (sig['3B'] > 0 ? '∞' : '-');
      const buyPct = ((sig['3B'] + sig.PB + sig['1B']) / total * 100).toFixed(1);
      const sellPct = ((sig['3S'] + sig.PS + sig.other) / total * 100).toFixed(1);
      h += '<div style="display:flex;gap:8px;flex-wrap:wrap">';
      h += `<div style="flex:1;min-width:80px;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:6px 8px;text-align:center">`;
      h += `<div style="font-size:9px;color:#8b949e">三买/三卖比</div>`;
      const ratioClr = sig['3B'] > sig['3S'] ? '#f85149' : (sig['3B'] < sig['3S'] ? '#3fb950' : '#d29922');
      h += `<div style="font-size:16px;font-weight:700;color:${{ratioClr}}">${{ratio3}}</div>`;
      h += `<div style="font-size:9px;color:#8b949e">${{sig['3B']}}买 / ${{sig['3S']}}卖</div>`;
      h += '</div>';
      h += `<div style="flex:1;min-width:80px;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:6px 8px;text-align:center">`;
      h += `<div style="font-size:9px;color:#8b949e">多方占比</div>`;
      h += `<div style="font-size:16px;font-weight:700;color:#f85149">${{buyPct}}%</div>`;
      h += `<div style="font-size:9px;color:#8b949e">${{sig['3B']+sig.PB+sig['1B']}}个标的</div>`;
      h += '</div>';
      h += `<div style="flex:1;min-width:80px;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:6px 8px;text-align:center">`;
      h += `<div style="font-size:9px;color:#8b949e">空方占比</div>`;
      h += `<div style="font-size:16px;font-weight:700;color:#3fb950">${{sellPct}}%</div>`;
      h += `<div style="font-size:9px;color:#8b949e">${{sig['3S']+sig.PS+sig.other}}个标的</div>`;
      h += '</div>';
      h += `<div style="flex:1;min-width:80px;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:6px 8px;text-align:center">`;
      h += `<div style="font-size:9px;color:#8b949e">中枢上方</div>`;
      const abClr = parseFloat(abPct) > 40 ? '#f85149' : (parseFloat(abPct) < 25 ? '#3fb950' : '#d29922');
      h += `<div style="font-size:16px;font-weight:700;color:${{abClr}}">${{abPct}}%</div>`;
      h += `<div style="font-size:9px;color:#8b949e">${{hub.above}}个标的</div>`;
      h += '</div>';
      h += '</div>';

      // Brief assessment
      h += '<div style="margin-top:8px;padding:6px 8px;background:#0d1117;border:1px solid #21262d;border-radius:6px;font-size:11px;color:#c9d1d9;line-height:1.6">';
      let assess = '';
      if (parseFloat(blPct) > 50) assess += `<span style="color:#3fb950">⚠ ${{blPct}}% 标的在中枢下方，偏弱格局。</span> `;
      else if (parseFloat(abPct) > 40) assess += `<span style="color:#f85149">✅ ${{abPct}}% 标的在中枢上方，偏强格局。</span> `;
      else assess += `<span style="color:#d29922">◆ 中枢位置三分天下，震荡格局。</span> `;
      if (sig['3B'] > sig['3S'] * 2) assess += '<span style="color:#f85149">三买显著多于三卖，做多窗口。</span>';
      else if (sig['3S'] > sig['3B'] * 2) assess += '<span style="color:#3fb950">三卖显著多于三买，防守为主。</span>';
      else if (sig['3B'] > 0 || sig['3S'] > 0) assess += `三买${{sig['3B']}}个 vs 三卖${{sig['3S']}}个，多空接近。`;
      h += assess;
      h += '</div>';

      // 5-day history comparison
      const hist = T.history;
      if (hist) {{
        const dates = Object.keys(hist).sort().reverse();
        if (dates.length > 1) {{
          h += '<div style="margin-top:10px;font-size:10px;color:#8b949e;margin-bottom:4px">近' + dates.length + '日趋势对比（' + thermoTab + '）</div>';
          h += '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">';
          h += '<table style="width:100%;border-collapse:collapse;font-size:10px;color:#c9d1d9;background:#0d1117">';
          h += '<thead><tr style="background:#21262d;color:#8b949e">';
          h += '<th style="padding:4px 6px;text-align:left;white-space:nowrap">日期</th>';
          h += '<th style="padding:4px 6px;text-align:center">上方%</th>';
          h += '<th style="padding:4px 6px;text-align:center">区间%</th>';
          h += '<th style="padding:4px 6px;text-align:center">下方%</th>';
          h += '<th style="padding:4px 6px;text-align:center">三买</th>';
          h += '<th style="padding:4px 6px;text-align:center">三卖</th>';
          h += '<th style="padding:4px 6px;text-align:center">评估</th>';
          h += '</tr></thead><tbody>';
          dates.forEach((dt, idx) => {{
            const d = hist[dt];
            const dl = d.levels ? d.levels[thermoTab] : null;
            if (!dl) return;
            const dHub = dl.hub || {{}};
            const dSig = dl.sig || {{}};
            const dTotal = d.total || 1;
            const dAbPct = (dHub.above / dTotal * 100).toFixed(1);
            const dInPct = (dHub.inside / dTotal * 100).toFixed(1);
            const dBlPct = (dHub.below / dTotal * 100).toFixed(1);
            const d3B = dSig['3B'] || 0;
            const d3S = dSig['3S'] || 0;
            const bg = idx === 0 ? '#161b22' : (idx % 2 === 0 ? '#0d1117' : '#161b22');
            const today = idx === 0;
            const dtLabel = dt.substring(5);
            const assessClr = {{'强势':'#f85149','偏强':'#f0883e','震荡':'#d29922','偏弱':'#7ee787','弱势':'#3fb950'}};
            const aC = assessClr[d.assess] || '#8b949e';

            // Mini bar for hub position
            const abW = Math.max(2, parseFloat(dAbPct));
            const inW = Math.max(2, parseFloat(dInPct));
            const blW = Math.max(2, parseFloat(dBlPct));

            // Trend arrows comparing with previous day
            let abArrow = '', blArrow = '', b3Arrow = '', s3Arrow = '';
            if (idx < dates.length - 1) {{
              const prevD = hist[dates[idx + 1]];
              const prevL = prevD && prevD.levels ? prevD.levels[thermoTab] : null;
              if (prevL) {{
                const prevAbPct = (prevL.hub.above || 0) / (prevD.total || 1) * 100;
                const prevBlPct = (prevL.hub.below || 0) / (prevD.total || 1) * 100;
                const curAbN = parseFloat(dAbPct);
                const curBlN = parseFloat(dBlPct);
                if (curAbN > prevAbPct + 2) abArrow = '<span style="color:#f85149;font-size:8px">↑</span>';
                else if (curAbN < prevAbPct - 2) abArrow = '<span style="color:#3fb950;font-size:8px">↓</span>';
                if (curBlN > prevBlPct + 2) blArrow = '<span style="color:#3fb950;font-size:8px">↑</span>';
                else if (curBlN < prevBlPct - 2) blArrow = '<span style="color:#f85149;font-size:8px">↓</span>';
                const prev3B = prevL.sig['3B'] || 0;
                const prev3S = prevL.sig['3S'] || 0;
                if (d3B > prev3B + 3) b3Arrow = '<span style="color:#f85149;font-size:8px">↑</span>';
                else if (d3B < prev3B - 3) b3Arrow = '<span style="color:#3fb950;font-size:8px">↓</span>';
                if (d3S > prev3S + 3) s3Arrow = '<span style="color:#3fb950;font-size:8px">↑</span>';
                else if (d3S < prev3S - 3) s3Arrow = '<span style="color:#f85149;font-size:8px">↓</span>';
              }}
            }}

            h += `<tr style="background:${{bg}};border-bottom:1px solid #21262d;${{today?'font-weight:600':''}}">`;
            h += `<td style="padding:3px 6px;white-space:nowrap">${{today?'📅 ':''}}${{dtLabel}}</td>`;
            h += `<td style="padding:3px 6px;text-align:center;color:#f85149">${{dAbPct}}${{abArrow}}</td>`;
            h += `<td style="padding:3px 6px;text-align:center;color:#d29922">${{dInPct}}</td>`;
            h += `<td style="padding:3px 6px;text-align:center;color:#3fb950">${{dBlPct}}${{blArrow}}</td>`;
            h += `<td style="padding:3px 6px;text-align:center;color:#f85149;font-weight:600">${{d3B}}${{b3Arrow}}</td>`;
            h += `<td style="padding:3px 6px;text-align:center;color:#3fb950;font-weight:600">${{d3S}}${{s3Arrow}}</td>`;
            h += `<td style="padding:3px 6px;text-align:center;color:${{aC}};font-weight:600">${{d.assess}}</td>`;
            h += '</tr>';
          }});
          h += '</tbody></table></div>';
        }}
      }}
    }}
    h += '</div>';
  }}
  h += '</div>';
  el.innerHTML = h;
}}
renderThermo();

let _mChartInited = false;
async function mInitChart() {{
  if (_mChartInited) return;
  _mChartInited = true;
  const ph = document.getElementById('mChartPlaceholder');
  if (ph) ph.style.display = 'none';
  document.getElementById('mChartSection').style.display = '';
  await loadAndRender();
  setupInteraction();
}}

function mUpdateInfoBarFromIndex() {{
  const idx = INDEX_LIST.find(x => x.etf_code === currentIndex);
  if (!idx) return;
  const bar = document.getElementById('infoBar');
  const sc = idx.score || 0;
  const scoreBg = sc >= 140 ? '#3a1a1a' : (sc >= 110 ? '#2a2a1a' : (sc >= 80 ? '#1a2a1a' : '#1a1a2a'));
  const scoreClr = sc >= 140 ? '#f85149' : (sc >= 110 ? '#d29922' : (sc >= 80 ? '#3fb950' : '#8b949e'));
  bar.innerHTML = '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">'
    + '<span style="background:' + scoreBg + ';color:' + scoreClr + ';padding:1px 6px;border-radius:3px;font-weight:700">' + sc + '</span>'
    + '<span>' + (idx.alignment || '-') + '</span>'
    + '<span style="color:#d2a8ff">' + (idx.conclusion || idx.summary || '-') + '</span>'
    + '</div>';
}}

window.addEventListener('load', () => {{ mUpdateInfoBarFromIndex(); renderThermo(); }});
window.addEventListener('resize', () => {{ if (_mChartInited) render(); }});
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
    args = parser.parse_args()

    print("=" * 60)
    print("缠论交易系统 v2 — 可视化仪表盘生成")
    print("=" * 60)

    generate_mobile_dashboard(data_dir=args.data_dir, output_path=args.output)


if __name__ == "__main__":
    main()
