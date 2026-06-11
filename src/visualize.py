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
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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
    array_fields = ("dates", "kline", "volumes", "macd_hist", "dif", "dea", "ma5", "ma10")
    analysis_fields = ("bsp", "strokes", "segments", "seg_labels", "hubs",
                       "trend", "hub_position", "hub_detail",
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
                "hub_idx": -1, "inv_price": 0,
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
    level_labels = {"daily": "DF", "30min": "30F", "5min": "5F"}
    step_sizes = {"daily": 20, "30min": 8, "5min": 48}
    min_bars = 200

    tasks = []
    for idx in indices:
        sym_dir = os.path.join(data_dir, f"{idx.etf_code}_{idx.etf_name}")
        if not os.path.isdir(sym_dir):
            continue
        for level_key in ["daily", "30min", "5min"]:
            csv_path = os.path.join(sym_dir, f"{level_key}.csv")
            if os.path.isfile(csv_path):
                tasks.append((idx.etf_code, idx.etf_name, level_key, csv_path,
                              step_sizes[level_key], min_bars))

    print(f"Backfill: {len(tasks)} tasks ({len(indices)} symbols × 3 levels)")
    print(f"Step sizes: DF={step_sizes['daily']}, 30F={step_sizes['30min']}, 5F={step_sizes['5min']}")

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

    level_cn = {"daily": "DF", "30min": "30F", "5min": "5F"}.get(level_key, level_key)
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

    # MA5 / MA10
    ma5 = []
    ma10 = []
    for i in range(len(closes)):
        if i < 4:
            ma5.append(None)
        else:
            ma5.append(round(sum(closes[i-4:i+1]) / 5, 3))
        if i < 9:
            ma10.append(None)
        else:
            ma10.append(round(sum(closes[i-9:i+1]) / 10, 3))

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
            # Hub context direction from first stroke pattern:
            # 1 = uptrend hub (下-上-下), -1 = downtrend hub (上-下-上)
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
            seg_hub_rects.append({
                "x0": si, "x1": ei,
                "zg": sh.zg, "zd": sh.zd,
                "gg": sh.gg, "dd": sh.dd,
                "idx": sh.idx,
                "evo": sh.evolution_type,
                "hub_level": sh.hub_level,
                "direction": sh.direction,
                "trend_seq": sh.trend_seq,
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
            if p.dt in fractal_merge:
                entry["fractal_bars"] = fractal_merge[p.dt]
            bsp_markers.append(entry)

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
        "strokes": stroke_lines,
        "segments": segment_points,
        "seg_labels": segment_labels,
        "hubs": hub_rects,
        "seg_hubs": seg_hub_rects,
        "bsp": bsp_markers,
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

.nav { display: flex; flex-wrap: wrap; background: #161b22; border-bottom: 1px solid #30363d;
       padding: 4px 16px; gap: 4px; align-items: center; }
.nav-sep { display: inline-flex; align-items: center; padding: 2px 6px; color: #484f58;
           font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
           border-left: 2px solid #30363d; margin-left: 2px; white-space: nowrap; }
.nav-btn { padding: 4px 8px; cursor: pointer; border: 1px solid transparent; background: none;
           color: #8b949e; font-size: 12px; white-space: nowrap; border-radius: 4px;
           transition: color 0.15s, background 0.15s; line-height: 1.4; }
.nav-btn:hover { color: #c9d1d9; background: #21262d; }
.nav-btn.active { color: #58a6ff; background: #1a2332; border-color: #1f3a5f; font-weight: 600; }

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

.detail-panel { padding: 16px 32px; }


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
  <span>DF（方向）→ 30F（买卖点）→ 5F（择时）</span>
  <span class="gen-time">数据：__DATA_TIME__ | 生成：__GEN_TIME__</span>
</div>

<div id="signals-panel" style="margin:0 32px 16px"></div>

<div style="display:flex;align-items:center;margin:24px 32px 0;gap:12px">
  <h2 style="color:#c9d1d9;font-size:17px;margin:0">📈 技术分析详情</h2>
  <span id="current-asset-label" style="color:#58a6ff;font-size:14px;font-weight:600"></span>
  <button id="toggle-nav-btn" onclick="toggleNav()" style="padding:3px 10px;border:1px solid #30363d;background:#21262d;color:#8b949e;cursor:pointer;font-size:12px;border-radius:4px">展开标的 ▼</button>
</div>
<div class="nav" id="index-nav" style="display:none"></div>

<div class="level-tabs" id="level-tabs">
  <button class="level-btn" data-level="daily">DF</button>
  <button class="level-btn active" data-level="30min">30F</button>
  <button class="level-btn" data-level="5min">5F</button>
</div>

<div id="conclusion-bar"></div>
<div class="struct-bar" id="struct-bar"></div>
<div style="display:flex;gap:14px;padding:4px 32px;font-size:11px;color:#8b949e;flex-wrap:wrap;align-items:center">
  <span style="color:#f0883e">━ 笔</span>
  <span style="color:#00d4aa">━ 背驰笔↑</span>
  <span style="color:#ff6b9d">━ 背驰笔↓</span>
  <span style="color:#bc8cff;font-weight:bold">━ 线段</span>
  <span style="color:#ffd700">━ MA5</span>
  <span style="color:#58a6ff">━ MA10</span>
  <span>█<span style="color:rgba(248,81,73,0.6)">中枢↑</span></span>
  <span>█<span style="color:rgba(63,185,80,0.6)">中枢↓</span></span>
  <span>█<span style="color:rgba(255,215,0,0.7)">段中枢</span></span>
</div>

<div id="chart-container"></div>


<script>
// ─── Data: lazy-loaded per index via script injection ───
var DATA_CACHE = {};
var LIVE_DATA = null;
const DATA_KEYS = __ALL_DATA_JSON__;
const INDEX_LIST = __INDEX_LIST_JSON__;
const SYNTHESIS = __SYNTHESIS_JSON__;
const SIGNAL_DATA = __GLOBAL_SIGNALS_JSON__;
const WATCHLIST_SIGNALS = __WATCHLIST_SIGNALS_JSON__;
const WATCHLIST_CODES = __WATCHLIST_CODES_JSON__;

function applyLiveDelta(key, base) {
  if (!LIVE_DATA || !LIVE_DATA[key]) return base;
  const live = LIVE_DATA[key];
  const arrFields = ['dates','kline','volumes','macd_hist','dif','dea','ma5','ma10'];
  const replaceFields = ['bsp','strokes','segments','seg_labels','hubs',
                         'trend','hub_position','hub_detail',
                         'trend_completion','volume_profile','tentative'];
  if (live.full_replace) {
    const merged = {};
    for (const f of arrFields) { if (live[f] !== undefined) merged[f] = live[f]; }
    for (const f of replaceFields) { if (live[f] !== undefined) merged[f] = live[f]; }
    return merged;
  }
  const merged = Object.assign({}, base);
  if (live.drop_head) {
    const drop = live.drop_head;
    for (const f of arrFields) {
      merged[f] = (base[f] || []).slice(drop).concat(live[f] || []);
    }
  } else {
    const bn = live.base_len || 0;
    for (const f of arrFields) {
      merged[f] = (base[f] || []).slice(0, bn).concat(live[f] || []);
    }
  }
  for (const f of replaceFields) {
    if (live[f] !== undefined) merged[f] = live[f];
  }
  return merged;
}

function getChartData(key) { return DATA_CACHE[key] || null; }

function loadChartData(key) {
  return new Promise(resolve => {
    if (DATA_CACHE[key]) { resolve(DATA_CACHE[key]); return; }
    const s = document.createElement('script');
    s.src = 'data/' + key + '.js';
    s.onload = () => {
      let d = DATA_CACHE[key] || null;
      if (d) {
        DATA_CACHE['_base_' + key] = d;
        d = applyLiveDelta(key, d);
        DATA_CACHE[key] = d;
      }
      resolve(d);
    };
    s.onerror = () => resolve(null);
    document.head.appendChild(s);
  });
}

// Load live.js for delta merge
let _liveReady = false;
(function() {
  const ls = document.createElement('script');
  ls.src = 'data/live.js';
  ls.onload = () => {
    _liveReady = true;
    if (typeof LIVE_DATA !== 'undefined') {
      for (const key of Object.keys(DATA_CACHE)) {
        if (LIVE_DATA[key]) {
          DATA_CACHE[key] = applyLiveDelta(key, DATA_CACHE['_base_' + key] || DATA_CACHE[key]);
        }
      }
    }
  };
  ls.onerror = () => { _liveReady = true; };
  document.head.appendChild(ls);
})();

// 确保默认选择有数据文件的指数
const coreIndices = ["510300", "510050", "510500", "512100", "159915", "588000", "513180", "513100"];
let defaultIndex = INDEX_LIST.find(x => coreIndices.includes(x.etf_code));
let currentIndex = defaultIndex ? defaultIndex.etf_code : INDEX_LIST[0].etf_code;
let currentLevel = '30min';
let chart = null;

// ─── Unified Signals Panel (个股/ETF/自选股 × DF/30F/5F) ───
let spCategory = 'stock';
let spLevel = '30F';
let spExpanded = true;
let spT1Open = false;
let spT2Open = false;
let spT3Open = true;

function getSignalSource(cat) {
  if (cat === 'stock') return SIGNAL_DATA.stock || {};
  if (cat === 'etf') return SIGNAL_DATA.etf || {};
  return WATCHLIST_SIGNALS || {};
}

function renderSignalsPanel() {
  const el = document.getElementById('signals-panel');
  const levels = ['DF', '30F', '5F'];
  const categories = [
    {id:'stock', label:'📊 个股', icon:'📊'},
    {id:'etf', label:'📈 ETF', icon:'📈'},
    {id:'watchlist', label:'⭐ 自选股', icon:'⭐'}
  ];

  const confIcons = {'high': '🔴高', 'medium': '🟡中', 'low': '⚪低'};
  const typeColors = {'1B': '#f85149', '2B': '#f85149', '3B': '#f85149', '1S': '#3fb950', '2S': '#3fb950', '3S': '#3fb950', 'PB': '#f85149', 'PS': '#3fb950'};
  const strengthMap = {'strongest': '🔥最强', 'strong': '💪强势', 'standard': '📌标准', 'weak': '⚠弱'};

  function sigRow(s, i, isType3) {
    const isSnapshot = s.source === 'snapshot';
    const bg = isSnapshot ? '#1a1510' : (i % 2 === 0 ? '#0d1117' : '#161b22');
    const tClr = typeColors[s.type] || '#c9d1d9';
    let confStr = confIcons[s.conf] || s.conf || '-';
    if (s.conf_score !== undefined && s.conf_score !== null) confStr += ' <span style="color:#8b949e;font-size:11px">(' + s.conf_score + ')</span>';
    const wolfStr = s.wolf ? '⚠' : '✓';
    const wolfClr = s.wolf ? '#d29922' : '#3fb950';
    let strStr = strengthMap[s.strength] || s.strength || '-';
    if (s.str_score !== undefined && s.str_score !== null) strStr += ' <span style="color:#8b949e;font-size:11px">(' + s.str_score + ')</span>';
    const inv = s.status === 'invalidated';
    const pending = s.status === 'pending';
    const rowOpacity = inv ? 'opacity:0.45;' : (isSnapshot ? 'opacity:0.75;' : '');
    const strike = inv ? 'text-decoration:line-through;' : '';
    const isBuyType = ['1B','2B','3B','PB'].includes(s.type);
    const confirmedColor = isBuyType ? '#f85149' : '#3fb950';
    const confirmedIcon = isBuyType ? '🔴' : '🟢';
    const statusHtml = inv
      ? '<span title="' + (s.inv_reason||'').replace(/"/g,'&quot;') + '" style="color:#da3633;cursor:help">❌已失效</span>'
      : pending ? '<span style="color:#d29922">⏳待确认</span>'
      : '<span style="color:' + confirmedColor + '">' + confirmedIcon + '已确认</span>';
    const idxInfo = INDEX_LIST.find(x => x.etf_code === s.etf_code);
    const trend = idxInfo ? (idxInfo.trend || '') : '';
    const _isBroken = trend.includes('破坏');
    const _isUp = !_isBroken && trend.includes('上涨');
    const _isDown = !_isBroken && trend.includes('下跌');
    const _isPanUp = !_isBroken && !_isUp && !_isDown && trend.includes('盘整偏多');
    const _isPanDn = !_isBroken && !_isUp && !_isDown && trend.includes('盘整偏空');
    const _isPan = !_isBroken && !_isUp && !_isDown && !_isPanUp && !_isPanDn && trend.includes('盘整');
    const trendIcon = _isBroken ? '<span style="color:#e3b341" title="DF趋势破坏">⚠</span>'
      : _isUp ? '<span style="color:#f85149" title="DF上涨趋势">▲</span>'
      : _isDown ? '<span style="color:#3fb950" title="DF下跌趋势">▼</span>'
      : _isPanUp ? '<span style="color:#f0883e" title="DF盘整偏多">◆↑</span>'
      : _isPanDn ? '<span style="color:#7ee787" title="DF盘整偏空">◆↓</span>'
      : _isPan ? '<span style="color:#d29922" title="DF盘整">◆</span>'
      : '<span style="color:#8b949e" title="DF方向不明">—</span>';
    const snapBadge = isSnapshot ? ' <span title="历史快照：曾于' + (s.first_seen||'') + '发现" style="font-size:10px;color:#d29922;cursor:help">📸</span>' : '';
    let r = '<tr style="background:' + bg + ';border-bottom:1px solid #21262d;' + rowOpacity + '">';
    r += '<td style="padding:6px 8px;white-space:nowrap;font-family:monospace;font-size:12px;' + strike + '">' + (s.dt || '-') + '</td>';
    r += '<td style="padding:6px 8px;font-weight:600;' + strike + '">' + trendIcon + ' <a href="javascript:void(0)" onclick="selectIndex(\'' + s.etf_code + '\');selectLevel(\'' + (s.level_key||'daily') + '\')" style="color:#58a6ff;text-decoration:none;cursor:pointer" title="DF:' + trend + '">' + s.etf_name + '</a></td>';
    r += '<td style="padding:6px 8px;text-align:center;font-weight:bold;color:' + tClr + ';' + strike + '">' + s.label + snapBadge + '</td>';
    r += '<td style="padding:6px 8px;text-align:center;font-size:11px;color:#e3b341;' + strike + '">' + (s.signal_level || '-') + '</td>';
    r += '<td style="padding:6px 8px;text-align:center">' + (isSnapshot ? '<span style="color:#d29922" title="走势延续后结构变化">📸历史</span>' : statusHtml) + '</td>';
    r += '<td style="padding:6px 8px;text-align:center">' + confStr + '</td>';
    r += '<td style="padding:6px 8px;text-align:center;font-size:12px">' + strStr + '</td>';
    r += '<td style="padding:6px 8px;font-size:12px">' + (s.pos_advice || '-') + '</td>';
    r += '<td style="padding:6px 8px;text-align:center;color:' + wolfClr + '">' + wolfStr + '</td>';
    if (isType3) {
      const rk = s.hub_rank;
      const rkLabels = {0:'⓪末端',1:'①首个',2:'②第二',3:'③第三'};
      const rkColors = {0:'#f0883e',1:'#3fb950',2:'#d29922',3:'#8b949e'};
      let rkStr = '-', rkClr = '#8b949e';
      if (rk !== undefined && rk >= 0) {
        rkStr = rkLabels[rk] || '⑤+第' + rk;
        rkClr = rkColors[rk] || (rk <= 5 ? '#da3633' : '#6e7681');
      }
      r += '<td style="padding:6px 8px;text-align:center;font-size:12px;font-weight:600;color:' + rkClr + '">' + rkStr + '</td>';
    }
    r += '</tr>';
    return r;
  }

  function spTable(title, signals, isType3, toggleVar, isOpen) {
    const cnt = signals.length;
    const arrow = isOpen ? '▼' : '▶';
    const cntBadge = cnt > 0 ? ' <span style="font-size:11px;color:#8b949e;font-weight:400">' + cnt + '个</span>' : '';
    let t = '<div style="margin-bottom:6px">';
    t += '<h4 onclick="' + toggleVar + '=!' + toggleVar + ';renderSignalsPanel()" style="color:#c9d1d9;margin:12px 0 6px;font-size:14px;cursor:pointer;user-select:none;display:flex;align-items:center;gap:6px">';
    t += '<span style="font-size:10px;color:#8b949e;transition:transform 0.2s">' + arrow + '</span> ' + title + cntBadge;
    t += '</h4>';
    if (isOpen) {
      t += '<table style="width:100%;border-collapse:collapse;font-size:13px;color:#c9d1d9;background:#161b22;border-radius:8px;overflow:hidden">';
      t += '<thead><tr style="background:#21262d;color:#8b949e;font-size:12px">';
      t += '<th style="padding:8px;text-align:left">时间</th>';
      t += '<th style="padding:8px;text-align:left">标的</th>';
      t += '<th style="padding:8px;text-align:center">类型</th>';
      t += '<th style="padding:8px;text-align:center">级别</th>';
      t += '<th style="padding:8px;text-align:center">状态</th>';
      t += '<th style="padding:8px;text-align:center">置信度</th>';
      t += '<th style="padding:8px;text-align:center">强弱</th>';
      t += '<th style="padding:8px;text-align:left">仓位建议</th>';
      t += '<th style="padding:8px;text-align:center">防狼</th>';
      if (isType3) t += '<th style="padding:8px;text-align:center">位次</th>';
      t += '</tr></thead><tbody>';
      signals.forEach((s, i) => { t += sigRow(s, i, isType3); });
      const cols = isType3 ? 10 : 9;
      if (signals.length === 0) {
        t += '<tr><td colspan="' + cols + '" style="padding:12px;text-align:center;color:#484f58">暂无信号</td></tr>';
      }
      t += '</tbody></table>';
    }
    t += '</div>';
    return t;
  }

  // Count totals for each category
  let totalAll = 0, buyCnt = 0, sellCnt = 0;
  categories.forEach(cat => {
    const src = getSignalSource(cat.id);
    levels.forEach(lv => {
      const d = src[lv] || {};
      ['type1','type2','type3'].forEach(k => {
        const arr = d[k] || [];
        totalAll += arr.length;
        arr.forEach(s => { if (s.type && s.type.endsWith('B')) buyCnt++; else sellCnt++; });
      });
    });
  });

  let h = '<div style="background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden">';
  h += '<div onclick="spExpanded=!spExpanded;renderSignalsPanel()" style="display:flex;align-items:center;padding:10px 16px;cursor:pointer;user-select:none">';
  h += '<h3 style="color:#c9d1d9;font-size:15px;margin:0;flex:1;display:flex;align-items:center;gap:6px">📡 最新买卖点';
  h += ' <span style="font-size:12px;color:#8b949e;font-weight:400">共 ' + totalAll + ' 个';
  if (buyCnt > 0) h += ' · <span style="color:#f85149">' + buyCnt + '买</span>';
  if (sellCnt > 0) h += ' · <span style="color:#3fb950">' + sellCnt + '卖</span>';
  h += '</span></h3>';
  h += '<span style="color:#8b949e;font-size:12px;transition:transform 0.2s;transform:rotate(' + (spExpanded ? '180' : '0') + 'deg)">▼</span>';
  h += '</div>';

  if (spExpanded) {
    h += '<div style="padding:0 16px 12px">';

    // Level 1 tabs: 个股 / ETF / 自选股
    h += '<div style="display:flex;gap:0;margin-bottom:10px;border-bottom:1px solid #30363d">';
    categories.forEach(cat => {
      const src = getSignalSource(cat.id);
      let catTotal = 0;
      levels.forEach(lv => {
        const d = src[lv] || {};
        catTotal += (d.type1||[]).length + (d.type2||[]).length + (d.type3||[]).length;
      });
      const active = cat.id === spCategory;
      const bg = active ? '#21262d' : 'transparent';
      const clr = active ? '#58a6ff' : '#8b949e';
      const border = active ? '2px solid #58a6ff' : '2px solid transparent';
      h += `<button onclick="spCategory='${cat.id}';renderSignalsPanel()" style="padding:8px 18px;border:none;border-bottom:${border};background:${bg};color:${clr};cursor:pointer;font-size:14px;font-weight:600;border-radius:6px 6px 0 0">${cat.label} (${catTotal})</button>`;
    });
    h += '</div>';

    // Level 2 tabs: DF / 30F / 5F
    const currentSrc = getSignalSource(spCategory);
    h += '<div style="display:flex;gap:6px;margin-bottom:8px">';
    levels.forEach(lv => {
      const d = currentSrc[lv] || {};
      const total = (d.type1||[]).length + (d.type2||[]).length + (d.type3||[]).length;
      const active = lv === spLevel;
      const bg = active ? '#1c2333' : 'transparent';
      const clr = active ? '#c9d1d9' : '#6e7681';
      const border = active ? '1px solid #30363d' : '1px solid transparent';
      h += `<button onclick="spLevel='${lv}';renderSignalsPanel()" style="padding:5px 14px;border:${border};background:${bg};color:${clr};cursor:pointer;font-size:12px;border-radius:4px">${lv} (${total})</button>`;
    });
    h += '</div>';

    // Signal tables for current category + level
    const data = currentSrc[spLevel] || {};
    h += spTable('🔴 第一类买卖点（趋势背驰）', data.type1 || [], false, 'spT1Open', spT1Open);
    h += spTable('🟠 第二类买卖点（回调确认）', data.type2 || [], false, 'spT2Open', spT2Open);
    h += spTable('🔵 第三类买卖点（中枢突破）', data.type3 || [], true, 'spT3Open', spT3Open);
    h += '</div>';
  }
  h += '</div>';
  el.innerHTML = h;
}
renderSignalsPanel();
// ─── Initialize ───
async function init() {
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
    const isBroken = (idx.trend||'').includes('破坏');
    const isUp = !isBroken && (idx.trend||'').includes('上涨');
    const isDown = !isBroken && (idx.trend||'').includes('下跌');
    const isPanUp = !isBroken && !isUp && !isDown && (idx.trend||'').includes('盘整偏多');
    const isPanDn = !isBroken && !isUp && !isDown && (idx.trend||'').includes('盘整偏空');
    const isPan = !isBroken && !isUp && !isDown && !isPanUp && !isPanDn && (idx.trend||'').includes('盘整');
    const trendIcon = isBroken ? '<span style="color:#e3b341" title="DF趋势破坏">⚠</span>'
      : isUp ? '<span style="color:#f85149" title="DF上涨趋势">▲</span>'
      : isDown ? '<span style="color:#3fb950" title="DF下跌趋势">▼</span>'
      : isPanUp ? '<span style="color:#f0883e" title="DF盘整偏多">◆↑</span>'
      : isPanDn ? '<span style="color:#7ee787" title="DF盘整偏空">◆↓</span>'
      : isPan ? '<span style="color:#d29922" title="DF盘整">◆</span>'
      : '<span style="color:#8b949e" title="DF方向不明">—</span>';
    btn.innerHTML = trendIcon + ' ' + idx.index_name;
    btn.title = 'DF:' + (idx.trend||'-') + ' | ' + (idx.summary || '') + ' | 评分:' + idx.score;
    btn.dataset.code = idx.etf_code;
    btn.onclick = () => selectIndex(idx.etf_code);
    nav.appendChild(btn);
  });

  document.querySelectorAll('.level-btn').forEach(btn => {
    btn.onclick = () => selectLevel(btn.dataset.level);
  });

  if (INDEX_LIST.length > 0) {
    const first = INDEX_LIST[0];
    document.getElementById('current-asset-label').textContent = formatAssetLabel(first);
  }

  renderSignalsPanel();
  chart = echarts.init(document.getElementById('chart-container'));
  window.addEventListener('resize', () => chart.resize());
  await render();
}

let navExpanded = false;
function toggleNav() {
  navExpanded = !navExpanded;
  const nav = document.getElementById('index-nav');
  const btn = document.getElementById('toggle-nav-btn');
  nav.style.display = navExpanded ? '' : 'none';
  btn.textContent = navExpanded ? '收起标的 ▲' : '展开标的 ▼';
}

function formatAssetLabel(idx) {
  const suffix = idx.market === 'SH' ? '.SH' : (idx.market === 'SZ' ? '.SZ' : '');
  if (idx.type === 'stock') {
    return idx.index_name + ' (' + idx.etf_code + suffix + ')';
  }
  if (idx.index_code && idx.index_code !== idx.etf_code) {
    return idx.index_name + ' (' + idx.index_code + ' / ETF: ' + idx.etf_code + ')';
  }
  return idx.index_name + ' (' + idx.etf_code + suffix + ')';
}

function selectIndex(code) {
  currentIndex = code;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.code === code));
  const idx = INDEX_LIST.find(x => x.etf_code === code);
  if (idx) document.getElementById('current-asset-label').textContent = formatAssetLabel(idx);
  if (navExpanded) toggleNav();
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

  const env = idx.daily_env || {};
  const envLabel = env.label || '-';
  const envColor = env.color || '#8b949e';
  const envOk = env.m30_3b_ok;
  const envIcon = envOk ? '✅' : '❌';
  const envAdvice = env.advice || '';
  const envFactors = (env.factors || []).join(' · ');
  const envScoreVal = env.score !== undefined ? env.score : '-';

  const notesLabel = idx.notes ? `<span style="color:#8b949e;font-size:12px;margin-left:6px">${idx.notes}</span>` : '';

  bar.innerHTML = `
    <div class="concl-group">
      <span class="concl-label">评分</span>
      <span style="background:${scoreBg};color:${scoreClr};padding:3px 10px;border-radius:5px;font-weight:700;font-size:16px">${sc}</span>
      ${notesLabel}
    </div>
    <div class="concl-group" title="${envFactors}\n${envAdvice}">
      <span class="concl-label">DF环境</span>
      <span style="background:rgba(0,0,0,0.3);color:${envColor};padding:4px 12px;border-radius:6px;font-weight:700;font-size:14px;border:1px solid ${envColor}">${envIcon} ${envLabel}</span>
      <span style="font-size:12px;color:${envColor}">${envAdvice}</span>
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
  const tentTag = data.tentative > 0
    ? '<span style="color:#d29922;font-weight:bold" title="盘中数据，最后一根K线尚未确认">⚠ 盘中暂定</span>'
    : '';
  const vp = data.volume_profile || {};
  const vpIcons = {'active': '🔥活跃', 'normal': '➖正常', 'inactive': '❄️低迷'};
  const vpTrend = {'expanding': '放量', 'shrinking': '缩量', 'flat': '平稳'};
  const vpStr = vp.activity ? `<span title="近5日/MA20=${vp.ratio}">${vpIcons[vp.activity]||''} ${vpTrend[vp.trend]||''}</span>` : '';
  bar.innerHTML = `
    <span>K线 ${s.bars}</span><span>合并 ${s.merged}</span>
    <span>分型 ${s.fractals}</span><span>笔 ${s.strokes}</span>
    <span>线段 ${s.segments}</span><span>笔中枢 ${s.hubs}</span><span>段中枢 ${s.seg_hubs || 0}</span>
    <span>信号 ${s.bsp}</span>${tentTag}${vpStr ? '<span>│</span>' + vpStr : ''}
  `;
}



function renderChart(data) {
  const upColor = '#f85149';
  const downColor = '#3fb950';

  // Mark tentative (incomplete) bars with dashed border and reduced opacity
  if (data.tentative > 0 && data.kline.length > 0) {
    const lastIdx = data.kline.length - 1;
    const lastBar = data.kline[lastIdx];
    const isUp = lastBar[1] >= lastBar[0]; // close >= open
    data.kline[lastIdx] = {
      value: lastBar,
      itemStyle: {
        color: isUp ? 'rgba(248,81,73,0.35)' : 'rgba(63,185,80,0.35)',
        borderColor: isUp ? 'rgba(248,81,73,0.6)' : 'rgba(63,185,80,0.6)',
        borderType: 'dashed',
        borderWidth: 2,
      }
    };
  }

  // Hub direction color map: 1=up(red), -1=down(green), 0=consolidation(blue)
  const hubDirColors = {
    1:  { fill: 'rgba(248,81,73,0.06)',  border: 'rgba(248,81,73,0.40)' },
    '-1': { fill: 'rgba(63,185,80,0.06)',  border: 'rgba(63,185,80,0.40)' },
    0:  { fill: 'rgba(88,166,255,0.05)', border: 'rgba(88,166,255,0.35)' },
  };

  // Stroke lines as markLine data with index labels + volume trend + divergence color
  const strokeVolIcons = {'shrink': '↓', 'expand': '↑'};
  const strokeMarkData = data.strokes.map(s => {
    const volSuffix = strokeVolIcons[s.vol_trend] || '';
    const strokeColor = s.div ? (s.dir === 1 ? '#00d4aa' : '#ff6b9d') : '#f0883e';
    return [
      { coord: [data.dates[s.coords[0][0]], s.coords[0][1]] },
      { coord: [data.dates[s.coords[1][0]], s.coords[1][1]],
        lineStyle: { color: strokeColor, width: s.div ? 2.5 : 1.5 },
        label: { show: true, formatter: 'S' + (s.idx + 1) + volSuffix, fontSize: 12, fontWeight: 'bold', color: strokeColor,
                 position: 'middle', distance: -14 } },
    ];
  });

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
    label: { show: true, formatter: 'D' + (lb.idx + 1),
             fontSize: 13, fontWeight: 'bold',
             color: '#bc8cff',
             backgroundColor: 'rgba(13,17,23,0.85)',
             padding: [2, 6], borderRadius: 4,
             position: lb.dir === 1 ? 'insideRight' : 'insideLeft',
             distance: 0 },
  }));

  // Hub labels (stroke-level) with evolution type + volume trend + level + direction + sequence
  const evoColors = {'延伸': '#8b949e', '新生（上）': '#f85149', '新生（下）': '#3fb950', '扩展': '#d29922'};
  const volTrendIcons = {'shrink': '📉缩', 'expand': '📈放', 'flat': ''};
  const dirIcons = {'上': '↑', '下': '↓'};
  const evoColorsExt = Object.assign({}, evoColors, {'延伸升级': '#e3b341', '扩张': '#e3b341', '扩展合并': '#d29922', '扩张合并': '#d29922'});
  const hubLabelPts = data.hubs.map(h => {
    const midX = Math.round((h.x0 + Math.min(h.x1, data.dates.length - 1)) / 2);
    const evoTag = h.evo ? ' ' + h.evo : '';
    const volTag = volTrendIcons[h.vol_trend] || '';
    const mergedTag = h.is_merged ? '⬆合并' : '';
    const durTag = h.duration_bars > 0 ? h.duration_bars + 'K' : '';
    const dirTag = dirIcons[h.direction] || '';
    const seqTag = h.trend_seq >= 0 ? '#' + (h.trend_seq + 1) : '';
    const lvlTag = h.hub_level || '';
    const label = lvlTag + (h.idx + 1) + dirTag + seqTag + evoTag + (mergedTag ? ' ' + mergedTag : '') + (volTag ? ' ' + volTag : '') + (durTag ? ' ' + durTag : '');
    const evoClr = h.is_merged ? '#d29922' : (evoColorsExt[h.evo] || '#58a6ff');
    return {
      coord: [data.dates[midX], h.zg],
      symbol: 'circle', symbolSize: 1, itemStyle: { color: 'transparent' },
      label: { show: true, formatter: label,
               fontSize: 10, color: evoClr,
               backgroundColor: 'rgba(13,17,23,0.7)',
               padding: [1, 4], borderRadius: 2, position: 'top', distance: 5 },
    };
  });

  // Segment-level hub labels
  const segHubLabelPts = (data.seg_hubs || []).map(sh => {
    const midX = Math.round((sh.x0 + Math.min(sh.x1, data.dates.length - 1)) / 2);
    const evoTag = sh.evo ? ' ' + sh.evo : '';
    const dirTag = dirIcons[sh.direction] || '';
    const seqTag = sh.trend_seq >= 0 ? '#' + (sh.trend_seq + 1) : '';
    const lvlTag = sh.hub_level || '线段中枢';
    const label = lvlTag + (sh.idx + 1) + dirTag + seqTag + evoTag;
    const evoClr = evoColorsExt[sh.evo] || '#ffd700';
    return {
      coord: [data.dates[midX], sh.zg],
      symbol: 'circle', symbolSize: 1, itemStyle: { color: 'transparent' },
      label: { show: true, formatter: label,
               fontSize: 11, color: evoClr, fontWeight: 'bold',
               backgroundColor: 'rgba(13,17,23,0.85)',
               padding: [2, 5], borderRadius: 3, position: 'top', distance: 18 },
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
    const isT3 = p.type === '3B' || p.type === '3S';
    const effTier = (isT3 && p.status !== 'invalidated' && tier > 1) ? Math.max(tier - 1, 1) : tier;
    const prefix = isT3 ? '◆' : '#';
    if (effTier === 3) return prefix + p.bsp_idx;
    let text = prefix + p.bsp_idx + ' ' + (p.signal_level || p.label);
    if (effTier === 1) {
      if (p.conf) text += ' [' + (confIcons[p.conf] || p.conf) + ']';
      if (p.ranges && p.ranges.length >= 2) {
        const r0 = p.ranges[0], r1 = p.ranges[1];
        const ratio = r0.area > 0 ? (r1.area / r0.area).toFixed(2) : '-';
        text += '\n' + r1.label + '/' + r0.label + '=' + ratio;
      }
      if (isT3 && p.strength) {
        const strEmoji = {strongest: '🔥', strong: '💪', standard: '📌', weak: '⚠'}[p.strength] || '';
        text += '\n' + strEmoji + p.strength;
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
  function bspTooltipHtml(p) {
    const confMap = {'high': '🔴 高', 'medium': '🟡 中', 'low': '⚪ 低'};
    const strMap = {strongest: '🔥最强', strong: '💪强势', standard: '📌标准', weak: '⚠弱'};
    const statusMap = {'active': '✅ 有效', 'confirmed': '✅ 已确认', 'invalidated': '❌ 失效', 'pending': '⏳ 待确认'};
    const isT3 = p.type === '3B' || p.type === '3S';
    let h = '<div style="max-width:420px;font-size:13px;line-height:1.6">';
    const typeColor = p.is_buy ? '#f85149' : '#3fb950';
    h += '<div style="font-weight:bold;font-size:14px;color:' + typeColor + '">#' + p.bsp_idx + ' ' + p.label + '</div>';
    h += '<div style="color:#8b949e;margin:2px 0">日期: ' + (data.dates[p.idx] || '') + ' | 价格: ' + p.price.toFixed(3) + '</div>';

    // --- Type-3 buy/sell: hub context panel ---
    if (isT3 && p.hub_zg !== undefined) {
      const hubNum = (p.hub_idx !== undefined ? p.hub_idx + 1 : '?');
      const rankMap = {0: '下跌末端', 1: '首个中枢', 2: '第二中枢', 3: '第三中枢'};
      const rankLabel = p.hub_rank !== undefined ? (rankMap[p.hub_rank] || '第' + p.hub_rank + '中枢') : '-';
      const rankColor = p.hub_rank <= 1 ? '#3fb950' : (p.hub_rank === 2 ? '#d29922' : '#f85149');
      const hubW = p.hub_width || '-';
      const hubWColor = hubW >= 7 ? '#3fb950' : (hubW >= 4 ? '#d29922' : '#f85149');
      const hubWLabel = hubW >= 7 ? '充分' : (hubW >= 4 ? '一般' : '偏窄');
      const evo = p.hub_evo || '-';

      h += '<div style="margin:4px 0;padding:6px 8px;background:#0d1117;border:1px solid #30363d;border-radius:6px">';
      h += '<div style="font-size:12px;color:#58a6ff;font-weight:bold;margin-bottom:4px">📐 中枢信息</div>';
      h += '<table style="width:100%;border-collapse:collapse;font-size:12px">';
      h += '<tr>';
      h += '<td style="color:#8b949e;padding:1px 4px">中枢</td>';
      h += '<td style="color:#c9d1d9;padding:1px 4px">#' + hubNum + ' ' + evo + '</td>';
      h += '<td style="color:#8b949e;padding:1px 4px">位次</td>';
      h += '<td style="color:' + rankColor + ';padding:1px 4px;font-weight:bold">' + rankLabel + '</td>';
      h += '</tr><tr>';
      h += '<td style="color:#8b949e;padding:1px 4px">ZG</td>';
      h += '<td style="color:#f85149;padding:1px 4px;font-weight:bold">' + p.hub_zg.toFixed(3) + '</td>';
      h += '<td style="color:#8b949e;padding:1px 4px">ZD</td>';
      h += '<td style="color:#3fb950;padding:1px 4px">' + p.hub_zd.toFixed(3) + '</td>';
      h += '</tr><tr>';
      h += '<td style="color:#8b949e;padding:1px 4px">笔数</td>';
      h += '<td style="color:' + hubWColor + ';padding:1px 4px">' + hubW + '笔 (' + hubWLabel + ')</td>';
      h += '<td style="color:#8b949e;padding:1px 4px">宽度</td>';
      h += '<td style="color:#c9d1d9;padding:1px 4px">' + (p.hub_zg - p.hub_zd).toFixed(3) + '</td>';
      h += '</tr></table>';

      // Stop-loss / invalidation level
      if (p.inv_price > 0) {
        const invDist = p.is_buy
          ? ((p.price - p.inv_price) / p.price * 100).toFixed(1)
          : ((p.inv_price - p.price) / p.price * 100).toFixed(1);
        const invLabel = p.is_buy ? '止损位 (ZG)' : '止盈位 (ZD)';
        const invColor = p.is_buy ? '#f85149' : '#3fb950';
        h += '<div style="margin-top:4px;padding:3px 6px;background:#21262d;border-radius:4px;font-size:12px">';
        h += '<span style="color:#8b949e">' + invLabel + ': </span>';
        h += '<span style="color:' + invColor + ';font-weight:bold">' + p.inv_price.toFixed(3) + '</span>';
        h += ' <span style="color:#8b949e">(距' + invDist + '%)</span>';
        h += '</div>';
      }
      h += '</div>';
    }

    h += '<table style="width:100%;border-collapse:collapse;margin:4px 0">';
    h += '<tr><td style="color:#8b949e;padding:2px 6px 2px 0">强度</td><td>' + (strMap[p.strength] || p.strength || '-');
    if (p.str_score !== undefined && p.str_score !== null) h += ' <span style="color:#8b949e;font-size:11px">(' + p.str_score + '分)</span>';
    h += '</td>';
    h += '<td style="color:#8b949e;padding:2px 6px 2px 12px">置信度</td><td>' + (confMap[p.conf] || p.conf || '-');
    if (p.conf_score !== undefined && p.conf_score !== null) h += ' <span style="color:#8b949e;font-size:11px">(' + p.conf_score + '分)</span>';
    h += '</td></tr>';
    h += '<tr><td style="color:#8b949e;padding:2px 6px 2px 0">状态</td><td>' + (statusMap[p.status] || p.status) + '</td>';
    h += '<td style="color:#8b949e;padding:2px 6px 2px 12px">防狼</td><td>' + (p.wolf ? '<span style="color:#d29922">⚠ ' + p.wolf + '</span>' : '✓ 安全') + '</td></tr>';
    h += '</table>';
    function renderGradeTable(title, color, details) {
      if (!details || details.length === 0) return '';
      let t = '<div style="margin:4px 0 2px;color:' + color + ';font-size:12px;font-weight:bold">' + title + '</div>';
      t += '<table style="width:100%;border-collapse:collapse;margin:0 0 4px;font-size:12px">';
      t += '<tr style="border-bottom:1px solid #30363d"><th style="color:#8b949e;text-align:left;padding:2px 4px;font-weight:normal">维度</th><th style="color:#8b949e;text-align:left;padding:2px 4px;font-weight:normal">判断</th><th style="color:#8b949e;text-align:right;padding:2px 4px;font-weight:normal">分值</th></tr>';
      details.forEach(function(d) {
        const sc = d.score;
        const scColor = sc > 0 ? '#3fb950' : (sc < 0 ? '#f85149' : '#8b949e');
        const scStr = sc > 0 ? '+' + sc : String(sc);
        t += '<tr><td style="color:#d2a8ff;padding:1px 4px;white-space:nowrap">' + d.dim + '</td>';
        t += '<td style="color:#c9d1d9;padding:1px 4px">' + d.label + '</td>';
        t += '<td style="color:' + scColor + ';text-align:right;padding:1px 4px;font-weight:bold">' + scStr + '</td></tr>';
      });
      t += '</table>';
      return t;
    }
    h += renderGradeTable('📊 强度明细', '#58a6ff', p.str_details);
    h += renderGradeTable('🎯 置信度明细', '#d2a8ff', p.conf_details);
    if (p.ranges && p.ranges.length >= 2) {
      const r0 = p.ranges[0], r1 = p.ranges[1];
      const ratio = r0.area > 0 ? (r1.area / r0.area * 100).toFixed(1) : '-';
      h += '<div style="margin:3px 0;padding:3px 6px;background:#161b22;border-radius:4px">';
      h += '<span style="color:#58a6ff">' + r0.label + '=' + r0.area + '</span>';
      h += ' vs <span style="color:#f85149">' + r1.label + '=' + r1.area + '</span>';
      h += ' &nbsp;背驰比 <b>' + ratio + '%</b>';
      h += '</div>';
    }
    if (p.pos_advice) {
      h += '<div style="margin:3px 0;color:#f0883e">💰 ' + p.pos_advice + '</div>';
    }
    if (p.status === 'invalidated' && p.inv_reason) {
      h += '<div style="margin:3px 0;color:#da3633">失效原因: ' + p.inv_reason + '</div>';
    }

    // --- Type-3 buy/sell: risk assessment summary ---
    if (isT3 && p.strength !== 'weak') {
      h += '<div style="margin:4px 0;padding:5px 8px;background:#0d1117;border:1px solid #30363d;border-radius:6px;font-size:12px">';
      h += '<div style="color:#d29922;font-weight:bold;margin-bottom:2px">⚡ 风险提示</div>';
      const risks = [];
      if (p.hub_rank !== undefined && p.hub_rank >= 3) risks.push('趋势位置偏晚期（第' + p.hub_rank + '中枢）');
      if (p.hub_width !== undefined && p.hub_width <= 3) risks.push('中枢偏窄（' + p.hub_width + '笔），筹码交换不充分');
      if (p.wolf) risks.push('MACD防狼：' + p.wolf);
      const goods = [];
      if (p.hub_rank !== undefined && p.hub_rank <= 1) goods.push(p.hub_rank === 0 ? '下跌末端三买，转势确认' : '首个中枢三买，空间最大');
      if (p.hub_width !== undefined && p.hub_width >= 7) goods.push('中枢构建充分（' + p.hub_width + '笔）');
      if (!p.wolf) goods.push('MACD环境安全');
      if (goods.length > 0) {
        h += '<div style="color:#3fb950;line-height:1.5">';
        goods.forEach(function(g) { h += '✅ ' + g + '<br>'; });
        h += '</div>';
      }
      if (risks.length > 0) {
        h += '<div style="color:#f85149;line-height:1.5">';
        risks.forEach(function(r) { h += '⚠ ' + r + '<br>'; });
        h += '</div>';
      }
      h += '</div>';
    }

    if (p.fractal_bars && p.fractal_bars.length > 1) {
      const barLabels = p.fractal_bars.map(function(bi) { return 'K' + (data.dates[bi] || '').substring(5).replace(':00', ''); });
      h += '<div style="margin:3px 0;padding:3px 6px;background:#161b22;border-radius:4px;font-size:11px;color:#d2a8ff">';
      h += '📐 分型中间 = ' + barLabels.join('+') + ' 包含合并';
      h += '</div>';
    }
    h += '<div style="margin:4px 0 0;color:#8b949e;font-size:11px;border-top:1px solid #30363d;padding-top:4px">' + (p.desc || '') + '</div>';
    h += '</div>';
    return h;
  }
  function buildPoints(list, isBuy) {
    const baseColor = isBuy ? upColor : downColor;
    const pos = isBuy ? 'bottom' : 'top';
    const rot = isBuy ? 0 : 180;
    return list.map((p, i) => {
      const tier = bspTier(p);
      const inv = p.status === 'invalidated';
      const pending = p.status === 'pending';
      const isT3 = p.type === '3B' || p.type === '3S';
      const effTier = (isT3 && !inv && tier > 1) ? Math.max(tier - 1, 1) : tier;
      const sz = effTier === 1 ? 12 : (effTier === 2 ? 8 : 6);
      const fs = effTier === 1 ? 10 : (effTier === 2 ? 8 : 7);
      const lh = effTier === 1 ? 13 : 10;
      const prevClose = i > 0 && (p.idx - list[i-1].idx) < 8;
      const baseDist = effTier === 1 ? 10 : (effTier === 2 ? 6 : 4);
      const dist = prevClose && (i % 2 === 1) ? baseDist + 18 : baseDist;
      const color = inv ? '#484f58' : (pending ? '#d29922' : baseColor);
      const statusSuffix = inv ? '\n❌失效' : (pending ? '\n⏳待确认' : '');
      const labelText = bspLabel(p) + statusSuffix;
      const style = { color: color, opacity: inv ? 0.4 : 1 };
      if (isT3 && !inv) {
        style.borderColor = '#ffd700';
        style.borderWidth = 2;
      }
      return {
        coord: [data.dates[p.idx], p.price],
        value: p.label,
        symbol: isT3 ? 'diamond' : 'triangle',
        symbolSize: inv ? Math.max(sz - 2, 4) : (isT3 ? sz + 2 : sz),
        symbolRotate: isT3 ? 0 : rot,
        itemStyle: style,
        label: { show: true, formatter: labelText, position: pos,
                 fontSize: fs, color: color,
                 lineHeight: lh, align: 'center', distance: dist },
        _bp: p,
      };
    });
  }
  const buyPoints = buildPoints(sortedBuys, true);
  const sellPoints = buildPoints(sortedSells, false);

  // a+A+b+B+c / A+a+c structure labels on K-line chart
  // Show structure areas + MACD areas for the same set of recent signals
  const structAreas = [];
  const structLabels = [];
  const hubColorPC = '#ffd700';
  const hubFillPC = 'rgba(255,215,0,0.08)';
  const bspWithStruct = data.bsp.filter(p => (p.type === '1B' || p.type === '1S') && p.structure && p.structure.length > 0);
  const visibleBspIdx = new Set(bspWithStruct.map(p => p.idx));
  bspWithStruct.forEach(p => {
    const tag_prefix = '#' + p.bsp_idx + ' ';
    p.structure.forEach((st, si) => {
      const x0 = data.dates[st.x0], x1 = data.dates[st.x1];
      if (st.zg !== undefined) {
        structAreas.push([
          { xAxis: x0, yAxis: st.zd,
            itemStyle: { color: hubFillPC,
                         borderColor: hubColorPC,
                         borderWidth: 1, borderType: 'dashed' } },
          { xAxis: x1, yAxis: st.zg },
        ]);
        const midX = data.dates[Math.round((st.x0 + st.x1) / 2)];
        const kMid = data.kline[Math.round((st.x0 + st.x1) / 2)];
        const yVal = kMid ? Math.max(kMid[1], kMid[2], kMid[3], kMid[0]) : 0;
        structLabels.push({
          coord: [midX, yVal],
          symbol: 'circle', symbolSize: 1,
          itemStyle: { color: 'transparent' },
          label: { show: true, formatter: tag_prefix + st.tag,
            fontSize: 14, fontWeight: 'bold', fontStyle: 'italic',
            color: hubColorPC, position: 'top', distance: 15,
            textShadowColor: '#000', textShadowBlur: 3 },
        });
      } else {
        const isFirst = (si === 0);
        const isLast = (si === p.structure.length - 1);
        const bClr = isFirst ? '#58a6ff' : (isLast ? '#f85149' : '#8b949e');
        const fClr = isFirst ? 'rgba(88,166,255,0.10)' : (isLast ? 'rgba(248,81,73,0.10)' : 'rgba(139,148,158,0.06)');
        structAreas.push([
          { xAxis: x0,
            itemStyle: { color: fClr, borderColor: bClr,
                         borderWidth: 1, borderType: 'dashed' } },
          { xAxis: x1 },
        ]);
        const midX = data.dates[Math.round((st.x0 + st.x1) / 2)];
        const kMid = data.kline[Math.round((st.x0 + st.x1) / 2)];
        const yVal = kMid ? Math.max(kMid[1], kMid[2], kMid[3], kMid[0]) : 0;
        structLabels.push({
          coord: [midX, yVal],
          symbol: 'circle', symbolSize: 1,
          itemStyle: { color: 'transparent' },
          label: { show: true, formatter: tag_prefix + st.tag,
            fontSize: 14, fontWeight: 'bold', fontStyle: 'italic',
            color: bClr, position: 'top', distance: 15,
            textShadowColor: '#000', textShadowBlur: 3 },
        });
      }
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
    dataZoom: (() => {
      const MIN_BARS = 120;
      const hubs = data.hubs || [];
      const need2Hub = hubs.length >= 2 ? (data.dates.length - hubs[hubs.length - 2].x0 + 10) : MIN_BARS;
      const VISIBLE_BARS = Math.max(MIN_BARS, need2Hub);
      const total = data.dates.length;
      const zoomStart = total > VISIBLE_BARS ? Math.max(0, 100 - (VISIBLE_BARS / total * 100)) : 0;
      return [
        { type: 'inside', xAxisIndex: [0, 1, 2], start: zoomStart, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 8, height: 16,
          borderColor: '#30363d', fillerColor: 'rgba(88,166,255,0.1)',
          textStyle: { color: '#8b949e' } },
      ];
    })(),
    grid: [
      { left: 60, right: 30, top: 20, bottom: '24%' },
      { left: 60, right: 30, top: '78%', bottom: '11%' },
      { left: 60, right: 30, top: '91%', bottom: 36 },
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
      { type: 'category', data: data.dates, gridIndex: 2, boundaryGap: true,
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
      { scale: true, gridIndex: 2,
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
          data: [...buyPoints, ...sellPoints, ...hubLabelPts, ...segHubLabelPts, ...structLabels],
          animation: false,
          tooltip: {
            show: true,
            trigger: 'item',
            backgroundColor: '#161b22',
            borderColor: '#30363d',
            textStyle: { color: '#c9d1d9', fontSize: 12 },
            formatter: function(params) {
              const bp = params.data && params.data._bp;
              if (!bp) return null;
              return bspTooltipHtml(bp);
            },
          },
        },
        markArea: {
          silent: true,
          animation: false,
          data: (() => {
            const hubAreas = data.hubs.map(h => {
              const dc = hubDirColors[h.dir] || hubDirColors[0];
              return [
                { xAxis: data.dates[h.x0], yAxis: h.zd,
                  itemStyle: {
                    color: dc.fill,
                    borderColor: dc.border,
                    borderWidth: 1,
                    borderType: 'dashed',
                  } },
                { xAxis: data.dates[Math.min(h.x1, data.dates.length - 1)], yAxis: h.zg },
              ];
            });
            const segHubAreas = (data.seg_hubs || []).map(sh => {
              return [
                { xAxis: data.dates[sh.x0], yAxis: sh.zd,
                  itemStyle: {
                    color: 'rgba(255,215,0,0.06)',
                    borderColor: 'rgba(255,215,0,0.5)',
                    borderWidth: 2,
                    borderType: 'solid',
                  } },
                { xAxis: data.dates[Math.min(sh.x1, data.dates.length - 1)], yAxis: sh.zg },
              ];
            });
            return [...segHubAreas, ...hubAreas, ...structAreas];
          })(),
          label: { show: false },
        },
      },
      // MA5
      {
        name: 'MA5',
        type: 'line',
        data: data.ma5,
        xAxisIndex: 0, yAxisIndex: 0,
        symbol: 'none',
        lineStyle: { color: '#ffd700', width: 1.2 },
        connectNulls: false,
        z: 2,
      },
      // MA10
      {
        name: 'MA10',
        type: 'line',
        data: data.ma10,
        xAxisIndex: 0, yAxisIndex: 0,
        symbol: 'none',
        lineStyle: { color: '#58a6ff', width: 1.2 },
        connectNulls: false,
        z: 2,
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
      // Volume
      {
        name: '成交量',
        type: 'bar',
        data: data.volumes.map((v, i) => ({
          value: v,
          itemStyle: { color: data.kline[i][1] >= data.kline[i][0] ? upColor : downColor },
        })),
        xAxisIndex: 2, yAxisIndex: 2,
        barWidth: '60%',
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
        mb = 1250 if level_key == "daily" else 0
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
        )
        syn_dict = _synthesis_to_dict(syn)
        syn_text = f"{syn.direction_alignment} | {syn.overall_bias}"

    return {
        "etf_code": etf_code,
        "echarts": echarts_items,
        "synthesis": syn_dict,
        "syn_text": syn_text,
    }


def run_analysis_pipeline(data_dir: str = None,
                          max_workers: int = None) -> dict:
    """Run chanlun analysis on all indices and return shared results.

    Supports multiprocess parallelism for analyze() calls.
    The returned dict can be passed to generate_dashboard() and
    generate_mobile_dashboard() to avoid redundant computation.

    Returns dict with keys: all_data, synthesis_data, index_list,
    latest_data_time, indices.
    """
    if data_dir is None:
        data_dir = os.path.join(_PROJECT_ROOT, "data")

    indices = load_index_watchlist()
    levels_cfg = [("daily", "daily.csv", "DF"),
                  ("30min", "30min.csv", "30F"),
                  ("5min", "5min.csv", "5F")]

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
                mb = 1250 if level_key == "daily" else 0
                echarts_data = _result_to_echarts_data(result, max_bars=mb)
                all_data[f"{idx.etf_code}_{level_key}"] = echarts_data
                print(f"OK ({result.trend}, {len(result.buy_sell_points)} signals)")

            if "daily" in level_results:
                syn = synthesize_multi_level(
                    level_results["daily"],
                    level_results.get("30min"),
                    level_results.get("5min"),
                )
                synthesis_data[idx.etf_code] = _synthesis_to_dict(syn)
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
# Generate HTML
# ════════════════════════════════════════════════════════════════════

def generate_dashboard(data_dir: str = None,
                       output_path: str = None,
                       cache: dict = None) -> str:
    """Generate HTML dashboard. Reuses pre-computed analysis if cache is provided.

    Args:
        data_dir: directory containing ETF CSV data (default: PROJECT_ROOT/data)
        output_path: output HTML path (default: PROJECT_ROOT/reports/dashboard.html)
        cache: pre-computed results from run_analysis_pipeline() to avoid redundant analysis

    Returns:
        path to the generated HTML file
    """
    if data_dir is None:
        data_dir = os.path.join(_PROJECT_ROOT, "data")
    if output_path is None:
        output_path = os.path.join(_PROJECT_ROOT, "reports", "dashboard.html")

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
    snap_count = _inject_snapshot_bsp(all_data)
    if snap_count:
        print(f"  Injected {snap_count} snapshot BSP markers into chart data")

    # Collect global signals (1B/1S/2B/2S/3B/3S) across all indices
    level_labels = {"daily": "DF", "30min": "30F", "5min": "5F"}
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
                "level_key": level_key,
                "type": p["type"],
                "label": p["label"],
                "price": p["price"],
                "conf": p.get("conf", ""),
                "conf_score": p.get("conf_score"),
                "strength": p.get("strength", ""),
                "str_score": p.get("str_score"),
                "pos_advice": p.get("pos_advice", ""),
                "desc": p.get("desc", ""),
                "wolf": p.get("wolf", ""),
                "status": p.get("status", "active"),
                "inv_reason": p.get("inv_reason", ""),
                "hub_rank": p.get("hub_rank", -1),
                "signal_level": p.get("signal_level", ""),
            }
            if p.get("ranges") and len(p["ranges"]) >= 2:
                r0, r1 = p["ranges"][0], p["ranges"][1]
                ratio_val = r1["area"] / r0["area"] if r0["area"] > 0 else 0
                entry["area_cmp"] = f"{r1['label']}/{r0['label']}={ratio_val:.2f}"
            else:
                entry["area_cmp"] = ""
            global_signals.append(entry)
    global_signals.sort(key=lambda x: x["dt"], reverse=True)
    global_signals = update_signal_snapshots(global_signals)
    type_limits = {"type1": 10, "type2": 5, "type3": 20}
    levels = ["DF", "30F", "5F"]

    # Build type map: etf_code -> "stock" / "broad" / "sector"
    idx_type_map = {i.etf_code: i.type for i in indices}

    # Split into stock signals vs ETF signals (broad+sector)
    stock_signals_by_level: dict[str, dict[str, list]] = {
        lv: {"type1": [], "type2": [], "type3": []} for lv in levels
    }
    etf_signals_by_level: dict[str, dict[str, list]] = {
        lv: {"type1": [], "type2": [], "type3": []} for lv in levels
    }
    for s in global_signals:
        lv = s["level"]
        if lv not in stock_signals_by_level:
            continue
        if s["type"] in ("1B", "1S"):
            bucket = "type1"
        elif s["type"] in ("2B", "2S"):
            bucket = "type2"
        else:
            bucket = "type3"
        item_type = idx_type_map.get(s["etf_code"], "stock")
        if item_type == "stock":
            target = stock_signals_by_level
        else:
            target = etf_signals_by_level
        if len(target[lv][bucket]) < type_limits[bucket]:
            target[lv][bucket].append(s)

    # Build watchlist-specific signals (pre-filtered, generous limits)
    _wl_path_early = os.path.join(_PROJECT_ROOT, "config", "watchlist.json")
    _wl_codes_set: set[str] = set()
    if os.path.exists(_wl_path_early):
        with open(_wl_path_early, "r", encoding="utf-8") as wf:
            _wl_early = json.load(wf)
            _wl_codes_set = {item["etf_code"] for item in _wl_early.get("watchlist", [])}
    wl_type_limits = {"type1": 10, "type2": 5, "type3": 10}
    watchlist_signals_by_level: dict[str, dict[str, list]] = {
        lv: {"type1": [], "type2": [], "type3": []} for lv in levels
    }
    if _wl_codes_set:
        for s in global_signals:
            if s["etf_code"] not in _wl_codes_set:
                continue
            lv = s["level"]
            if lv not in watchlist_signals_by_level:
                continue
            if s["type"] in ("1B", "1S"):
                bucket = "type1"
            elif s["type"] in ("2B", "2S"):
                bucket = "type2"
            else:
                bucket = "type3"
            if len(watchlist_signals_by_level[lv][bucket]) < wl_type_limits[bucket]:
                watchlist_signals_by_level[lv][bucket].append(s)

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
    html = html.replace("__GLOBAL_SIGNALS_JSON__", json.dumps({"stock": stock_signals_by_level, "etf": etf_signals_by_level}, ensure_ascii=False))
    html = html.replace("__WATCHLIST_SIGNALS_JSON__", json.dumps(watchlist_signals_by_level, ensure_ascii=False))

    # Watchlist codes injection
    watchlist_codes = list(_wl_codes_set)
    html = html.replace("__WATCHLIST_CODES_JSON__", json.dumps(watchlist_codes, ensure_ascii=False))

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
    _inject_snapshot_bsp(all_data)

    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_time = latest_data_time or "-"

    data_out_dir = os.path.join(os.path.dirname(output_path), "data")
    os.makedirs(data_out_dir, exist_ok=True)
    for key, chart_data in all_data.items():
        fpath = os.path.join(data_out_dir, f"{key}.js")
        json_str = json.dumps(chart_data, ensure_ascii=False, separators=(",", ":"))
        with open(fpath, "w", encoding="utf-8") as df:
            df.write(f'DATA_CACHE["{key}"]={json_str};\n')

    # Generate live.js for delta deployment
    generate_live_js(data_out_dir, all_data)

    data_keys_json = json.dumps(sorted(all_data.keys()), ensure_ascii=False)
    index_list_json = json.dumps(index_list, ensure_ascii=False)
    synthesis_json = json.dumps(synthesis_data, ensure_ascii=False)

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
    level_labels_m = {"daily": "DF", "30min": "30F", "5min": "5F"}
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
                "signal_level": p.get("signal_level", ""),
            }
            if p.get("ranges") and len(p["ranges"]) >= 2:
                r0, r1 = p["ranges"][0], p["ranges"][1]
                ratio_val = r1["area"] / r0["area"] if r0["area"] > 0 else 0
                entry_m["area_cmp"] = f"{r1['label']}/{r0['label']}={ratio_val:.2f}"
            else:
                entry_m["area_cmp"] = ""
            mobile_global_signals.append(entry_m)
    mobile_global_signals.sort(key=lambda x: x["dt"], reverse=True)
    mobile_global_signals = update_signal_snapshots(mobile_global_signals)
    mobile_type_limits = {"type1": 30, "type2": 15, "type3": 60}
    mobile_levels = ["DF", "30F", "5F"]

    # Split mobile signals into stock vs ETF
    mobile_stock_by_level: dict[str, dict[str, list]] = {
        lv: {"type1": [], "type2": [], "type3": []} for lv in mobile_levels
    }
    mobile_etf_by_level: dict[str, dict[str, list]] = {
        lv: {"type1": [], "type2": [], "type3": []} for lv in mobile_levels
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
    mobile_global_signals_json = json.dumps({"stock": mobile_stock_by_level, "etf": mobile_etf_by_level}, ensure_ascii=False)

    # Build watchlist-specific signals for mobile (pre-filtered)
    mobile_wl_type_limits = {"type1": 30, "type2": 30, "type3": 30}
    mobile_wl_signals: dict[str, dict[str, list]] = {
        lv: {"type1": [], "type2": [], "type3": []} for lv in mobile_levels
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
    mobile_watchlist_signals_json = json.dumps(mobile_wl_signals, ensure_ascii=False)

    # ── Market Thermometer data ──
    thermo_levels = {"daily": "DF", "30min": "30F", "5min": "5F"}
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
<div class="subtitle">移动版 · 数据 {data_time} · 生成 {gen_time} · DF→30F→5F</div>

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
             oninput="filterIdxTabs(this.value)">
    </div>
    <div class="idx-tabs" id="idxTabs">
      {idx_tabs_html}
    </div>
  </div>
  <div class="level-tabs" id="levelTabs">
    <div class="level-tab" onclick="switchLevel('daily')">DF</div>
    <div class="level-tab active" onclick="switchLevel('30min')">30F</div>
    <div class="level-tab" onclick="switchLevel('5min')">5F</div>
  </div>
  <div class="info-bar" id="infoBar"></div>
  <div id="loadingOverlay" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(13,17,23,0.7);z-index:999;align-items:center;justify-content:center"><span style="color:#58a6ff;font-size:15px">加载数据中...</span></div>
  <div class="chart-area"><canvas id="klineCanvas" height="320"></canvas></div>
  <div class="legend">
    <div class="legend-item"><div class="legend-color" style="background:#f85149"></div>阳线</div>
    <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div>阴线</div>
    <div class="legend-item"><div class="legend-color" style="background:#ffd700"></div>MA5</div>
    <div class="legend-item"><div class="legend-color" style="background:#58a6ff"></div>MA10</div>
    <div class="legend-item"><div class="legend-color" style="background:#f0883e"></div>笔</div>
    <div class="legend-item"><div class="legend-color" style="background:#79c0ff"></div>上涨背驰笔</div>
    <div class="legend-item"><div class="legend-color" style="background:#d2a8ff"></div>下跌背驰笔</div>
    <div class="legend-item"><div class="legend-color" style="background:#bc8cff"></div>线段</div>
    <div class="legend-item"><div class="legend-color" style="background:rgba(248,81,73,0.4)"></div>上涨枢(↓↑↓)</div>
    <div class="legend-item"><div class="legend-color" style="background:rgba(63,185,80,0.4)"></div>下跌枢(↑↓↑)</div>
    <div class="legend-item"><div class="legend-color" style="background:rgba(88,166,255,0.4)"></div>未定枢</div>
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

<div id="marketThermo" style="margin-top:8px;margin-bottom:8px"></div>

</div>

<script>
var DATA_CACHE = {{}};
var LIVE_DATA = null;
const DATA_KEYS = {data_keys_json};
const INDEX_LIST = {index_list_json};
const SYNTHESIS = {synthesis_json};
const SIGNAL_DATA = {mobile_global_signals_json};
const WATCHLIST_SIGNALS = {mobile_watchlist_signals_json};
const WATCHLIST_CODES = {watchlist_codes_json};
const MARKET_THERMO = {market_thermo_json};

function applyLiveDelta(key, base) {{
  if (!LIVE_DATA || !LIVE_DATA[key]) return base;
  const live = LIVE_DATA[key];
  const arrFields = ['dates','kline','volumes','macd_hist','dif','dea','ma5','ma10'];
  const replaceFields = ['bsp','strokes','segments','seg_labels','hubs',
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

function loadChartData(key) {{
  return new Promise(resolve => {{
    if (DATA_CACHE[key]) {{ resolve(DATA_CACHE[key]); return; }}
    const s = document.createElement('script');
    s.src = 'data/' + key + '.js';
    s.onload = () => {{
      let d = DATA_CACHE[key] || null;
      if (d) {{
        DATA_CACHE['_base_' + key] = d;
        d = applyLiveDelta(key, d);
        DATA_CACHE[key] = d;
      }}
      resolve(d);
    }};
    s.onerror = () => resolve(null);
    document.head.appendChild(s);
  }});
}}

// Load live.js at startup for delta merge
let _liveReady = false;
(function() {{
  const ls = document.createElement('script');
  ls.src = 'data/live.js';
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

let mgsTab = '30F';
let mgsExpanded = true;
let mgsPage = 1;
const mgsPageSize = {{t1: 10, t2: 5, t3: 20}};
let mgsT1Open = false;
let mgsT2Open = false;
let mgsT3Open = true;
let mgsCat = 'stock';
function renderMobileGlobalSignals() {{
  const el = document.getElementById('mobileGlobalSignals');
  const levels = ['DF', '30F', '5F'];
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
    const _mgsSnap = s.source === 'snapshot';
    const bg = _mgsSnap ? '#1a1510' : (i % 2 === 0 ? '#0d1117' : '#161b22');
    const tc = tClrs[s.type] || '#c9d1d9';
    let confStr = (confIcons[s.conf] || '') + (s.conf === 'high' ? '高' : s.conf === 'medium' ? '中' : s.conf === 'low' ? '低' : '');
    if (s.conf_score !== undefined && s.conf_score !== null) confStr += '<span style="color:#8b949e;font-size:9px">(' + s.conf_score + ')</span>';
    let strStr = strMap[s.strength] || s.strength || '-';
    if (s.str_score !== undefined && s.str_score !== null) strStr += '<span style="color:#8b949e;font-size:9px">(' + s.str_score + ')</span>';
    const dtShort = s.dt ? s.dt.substring(5) : '-';
    const inv = s.status === 'invalidated';
    const pending = s.status === 'pending';
    const rowOpacity = inv ? 'opacity:0.45;' : (_mgsSnap ? 'opacity:0.75;' : '');
    const strike = inv ? 'text-decoration:line-through;' : '';
    const mGsBuyType = ['1B','2B','3B','PB'].includes(s.type);
    const mGsConfClr = mGsBuyType ? '#f85149' : '#3fb950';
    const statusTag = _mgsSnap ? '<span style="font-size:9px;color:#d29922;margin-left:2px">📸</span>' : (inv ? '<span style="font-size:9px;color:#da3633;margin-left:2px">✗</span>' : (pending ? '<span style="font-size:9px;color:#d29922;margin-left:2px">⏳</span>' : '<span style="font-size:9px;color:' + mGsConfClr + ';margin-left:2px">✓</span>'));
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
    let r = `<tr style="background:${{bg}};border-bottom:1px solid #21262d;${{rowOpacity}}">`;
    r += `<td style="padding:3px 4px;font-family:monospace;font-size:10px;white-space:nowrap;${{strike}}">${{dtShort}}</td>`;
    r += `<td style="padding:3px 4px;font-weight:600;${{strike}}">${{mTrendIcon}} <a href="javascript:void(0)" onclick="switchIndex('${{s.etf_code}}');switchLevel('${{s.level_key||'daily'}}')" style="color:#58a6ff;text-decoration:none">${{s.etf_name}}</a></td>`;
    r += `<td style="padding:3px 4px;text-align:center;font-weight:bold;color:${{tc}};${{strike}}">${{s.label}}${{statusTag}}</td>`;
    r += `<td style="padding:3px 4px;text-align:center;font-size:9px;color:#e3b341;${{strike}}">${{s.signal_level || '-'}}</td>`;
    r += `<td style="padding:3px 4px;text-align:center;font-size:10px">${{strStr}}</td>`;
    r += `<td style="padding:3px 4px;text-align:center;font-size:10px">${{confStr}}</td>`;
    if (isType3) {{
      const rk = s.hub_rank;
      const rkL = {{0:'⓪',1:'①',2:'②',3:'③'}};
      const rkC = {{0:'#f0883e',1:'#3fb950',2:'#d29922',3:'#8b949e'}};
      let rkS = '-', rkClr = '#8b949e';
      if (rk !== undefined && rk >= 0) {{
        rkS = rkL[rk] || '⑤+';
        rkClr = rkC[rk] || (rk <= 5 ? '#da3633' : '#6e7681');
      }}
      r += `<td style="padding:3px 4px;text-align:center;font-size:10px;font-weight:600;color:${{rkClr}}">${{rkS}}</td>`;
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
      t += '<thead><tr style="background:#21262d;color:#8b949e;font-size:10px">';
      t += '<th style="padding:4px;text-align:left">时间</th>';
      t += '<th style="padding:4px;text-align:left">标的</th>';
      t += '<th style="padding:4px;text-align:center">类型</th>';
      t += '<th style="padding:4px;text-align:center">级别</th>';
      t += '<th style="padding:4px;text-align:center">强度</th>';
      t += '<th style="padding:4px;text-align:center">置信</th>';
      if (isType3) t += '<th style="padding:4px;text-align:center">位次</th>';
      t += '</tr></thead><tbody>';
      const cols = isType3 ? 7 : 6;
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
  const gMaxItems = Math.max(gAllT1.length, gAllT2.length, gAllT3.length);
  const gMaxPageSize = Math.max(mgsPageSize.t1, mgsPageSize.t2, mgsPageSize.t3);
  const gTotalPages = Math.min(3, Math.max(1, Math.ceil(gMaxItems / gMaxPageSize)));
  if (mgsPage > gTotalPages) mgsPage = gTotalPages;
  const gs1 = (mgsPage - 1) * mgsPageSize.t1;
  const gs2 = (mgsPage - 1) * mgsPageSize.t2;
  const gs3 = (mgsPage - 1) * mgsPageSize.t3;
  const gT1 = gAllT1.slice(gs1, gs1 + mgsPageSize.t1);
  const gT2 = gAllT2.slice(gs2, gs2 + mgsPageSize.t2);
  const gT3 = gAllT3.slice(gs3, gs3 + mgsPageSize.t3);
  h += mgsTable('🔴 第一类买卖点', gT1, false, 'mgsT1Open', mgsT1Open);
  h += mgsTable('🟠 第二类买卖点', gT2, false, 'mgsT2Open', mgsT2Open);
  h += mgsTable('🔵 第三类买卖点', gT3, true, 'mgsT3Open', mgsT3Open);
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

  bar.innerHTML = `
    <span style="background:${{scoreBg}};color:${{scoreClr}};padding:1px 6px;border-radius:3px;font-weight:700">${{sc}}</span>
    <span style="color:${{mEnvColor}};font-size:11px" title="${{mEnvAdvice}}">${{mEnvAdvice || '-'}}</span>
    <span style="color:${{actionColor}};font-size:11px;font-weight:600">${{actionText}}</span>${{tentTag}}
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
  const mHubDirClr = {{1: {{f:'rgba(248,81,73,0.10)', s:'rgba(248,81,73,0.50)'}}, '-1': {{f:'rgba(63,185,80,0.10)', s:'rgba(63,185,80,0.50)'}}, 0: {{f:'rgba(88,166,255,0.08)', s:'rgba(88,166,255,0.4)'}}}};
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
    ctx.beginPath(); ctx.moveTo(x0, scaleY(h.zg)); ctx.lineTo(x1, scaleY(h.zg)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x0, scaleY(h.zd)); ctx.lineTo(x1, scaleY(h.zd)); ctx.stroke();
    ctx.setLineDash([]);
    const evoClr = evoColors[h.evo] || '#58a6ff';
    const mVolIcons = {{'shrink': '📉', 'expand': '📈'}};
    const volMark = mVolIcons[h.vol_trend] || '';
    const dirIcon = h.direction === '上' ? '↑' : (h.direction === '下' ? '↓' : '');
    const seqLabel = h.trend_seq >= 0 ? '#' + (h.trend_seq + 1) : '';
    const lvlTag = h.hub_level || '';
    ctx.fillStyle = evoClr; ctx.font = '8px sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(lvlTag + (h.idx + 1) + dirIcon + seqLabel + (h.evo ? ' ' + h.evo : '') + volMark, x1 + 2, scaleY(h.zg) + 8);
    ctx.fillStyle = '#58a6ff';
    ctx.fillText('ZG=' + h.zg.toFixed(2), x1 + 2, scaleY(h.zg) + 17);
    ctx.fillText('ZD=' + h.zd.toFixed(2), x1 + 2, scaleY(h.zd) + 10);
  }});

  // Segment-level hubs (线段中枢, gold outline, drawn behind stroke hubs)
  (data.seg_hubs || []).forEach(sh => {{
    if (sh.x1 < viewStart || sh.x0 >= viewEnd) return;
    const x0 = scaleX(Math.max(sh.x0, viewStart)) - cw / 2;
    const x1 = scaleX(Math.min(sh.x1, viewEnd - 1)) + cw / 2;
    ctx.fillStyle = 'rgba(255,215,0,0.04)';
    ctx.fillRect(x0, scaleY(sh.zg), x1 - x0, scaleY(sh.zd) - scaleY(sh.zg));
    ctx.strokeStyle = 'rgba(255,215,0,0.5)'; ctx.lineWidth = 1.5;
    ctx.setLineDash([]);
    ctx.strokeRect(x0, scaleY(sh.zg), x1 - x0, scaleY(sh.zd) - scaleY(sh.zg));
    const dirIcon = sh.direction === '上' ? '↑' : (sh.direction === '下' ? '↓' : '');
    const seqLabel = sh.trend_seq >= 0 ? '#' + (sh.trend_seq + 1) : '';
    const lvlTag = sh.hub_level || '线段中枢';
    ctx.fillStyle = '#ffd700'; ctx.font = 'bold 9px sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(lvlTag + (sh.idx + 1) + dirIcon + seqLabel + (sh.evo ? ' ' + sh.evo : ''), x0 + 2, scaleY(sh.zg) - 3);
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
    ctx.fillText('D' + (lb.idx + 1), scaleX(lb.x), scaleY(lb.y) - 8);
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
    ctx.fillText('S' + (s.idx + 1) + volSuf, mx, (scaleY(s.coords[0][1]) + scaleY(s.coords[1][1])) / 2 - 6);
  }});

  // Buy/Sell markers
  bspHitAreas = [];
  data.bsp.forEach(p => {{
    if (p.idx < viewStart || p.idx >= viewEnd) return;
    const x = scaleX(p.idx);
    const y = scaleY(p.price);
    const mIsInv = p.status === 'invalidated';
    const mIsPending = p.status === 'pending';
    const mIsT3 = p.type === '3B' || p.type === '3S';
    const triColor = mIsInv ? '#484f58' : (mIsPending ? '#f0883e' : (p.is_buy ? '#f85149' : '#3fb950'));
    const markerSize = mIsT3 && !mIsInv ? 8 : (mIsPending ? 7 : 6);
    ctx.globalAlpha = mIsInv ? 0.4 : 1.0;
    ctx.beginPath();
    if (mIsT3 && !mIsInv) {{
      ctx.moveTo(x, y + (p.is_buy ? 10 : -10) - markerSize);
      ctx.lineTo(x + markerSize, y + (p.is_buy ? 10 : -10));
      ctx.lineTo(x, y + (p.is_buy ? 10 : -10) + markerSize);
      ctx.lineTo(x - markerSize, y + (p.is_buy ? 10 : -10));
      ctx.closePath();
      ctx.fillStyle = triColor; ctx.fill();
      ctx.strokeStyle = '#ffd700'; ctx.lineWidth = 1.5; ctx.stroke();
    }} else if (p.is_buy) {{
      ctx.moveTo(x, y + 10); ctx.lineTo(x - 6, y + 18); ctx.lineTo(x + 6, y + 18); ctx.closePath();
      ctx.fillStyle = triColor; ctx.fill();
    }} else {{
      ctx.moveTo(x, y - 10); ctx.lineTo(x - 6, y - 18); ctx.lineTo(x + 6, y - 18); ctx.closePath();
      ctx.fillStyle = triColor; ctx.fill();
    }}
    bspHitAreas.push({{cx: x, cy: p.is_buy ? y + 14 : y - 14, bp: p}});
    ctx.fillStyle = mIsInv ? '#484f58' : (mIsPending ? '#d29922' : (p.is_buy ? '#f85149' : '#3fb950'));
    ctx.globalAlpha = mIsInv ? 0.4 : 1.0;
    ctx.font = mIsT3 ? 'bold 9px sans-serif' : 'bold 8px sans-serif'; ctx.textAlign = 'center';
    const mConfIcons = {{'high': '🔴', 'medium': '🟡', 'low': '⚪'}};
    const mPrefix = mIsT3 ? '◆' : '#';
    let bspText = mPrefix + p.bsp_idx + ' ' + p.label.substring(0, 2);
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

  // a+A+b+B+c+C+...+f structure areas and labels (only for 1B/1S trend divergence)
  const hubColor = '#ffd700';
  const hubFill = 'rgba(255,215,0,0.10)';
  data.bsp.filter(p => p.type === '1B' || p.type === '1S').forEach(p => {{
    if (!p.structure || !p.structure.length) return;
    p.structure.forEach((st, si) => {{
      if (st.x1 < viewStart || st.x0 >= viewEnd) return;
      const sx0 = scaleX(Math.max(st.x0, viewStart));
      const sx1 = scaleX(Math.min(st.x1, viewEnd - 1));
      const w = sx1 - sx0;
      if (w < 2) return;
      if (st.zg !== undefined) {{
        // Hub (A/B/C/D/E...): gold rectangle with ZG-ZD bounds
        const y0 = scaleY(st.zg), y1 = scaleY(st.zd);
        ctx.fillStyle = hubFill;
        ctx.fillRect(sx0, y0, w, y1 - y0);
        ctx.strokeStyle = hubColor;
        ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
        ctx.strokeRect(sx0, y0, w, y1 - y0);
        ctx.setLineDash([]);
        const mx = (sx0 + sx1) / 2;
        ctx.font = 'bold italic 14px sans-serif'; ctx.textAlign = 'center';
        ctx.fillStyle = hubColor;
        ctx.shadowColor = '#000'; ctx.shadowBlur = 3;
        ctx.fillText(st.tag, mx, y0 - 4);
        ctx.shadowBlur = 0;
      }} else {{
        // Segments: first=blue(a), last=red(divergence comparison), middle=gray
        const isFirst = (si === 0);
        const isLast = (si === p.structure.length - 1);
        const borderClr = isFirst ? '#58a6ff' : (isLast ? '#f85149' : '#8b949e');
        const fillClr = isFirst ? 'rgba(88,166,255,0.12)' : (isLast ? 'rgba(248,81,73,0.12)' : 'rgba(139,148,158,0.06)');
        ctx.fillStyle = fillClr;
        ctx.fillRect(sx0, pad.t, w, H - pad.t - pad.b);
        ctx.strokeStyle = borderClr;
        ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
        ctx.strokeRect(sx0, pad.t, w, H - pad.t - pad.b);
        ctx.setLineDash([]);
        const mx = (sx0 + sx1) / 2;
        ctx.font = 'bold italic 14px sans-serif'; ctx.textAlign = 'center';
        ctx.fillStyle = borderClr;
        ctx.shadowColor = '#000'; ctx.shadowBlur = 3;
        ctx.fillText(st.tag, mx, pad.t + 16);
        ctx.shadowBlur = 0;
      }}
    }});
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
  const levels = ['DF', '30F', '5F'];

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
