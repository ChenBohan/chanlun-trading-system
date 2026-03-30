"""
Index Trading System — Chanlun-based broad-market index analysis and rotation.

Orchestrates: data fetching, Chanlun analysis, rotation scoring, signal tracking,
and comprehensive report generation for A-share broad-based indices and ETFs.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

from fetch_data import fetch_kline, fetch_index_kline, daily_to_md, min30_to_md
from chanlun_engine import (
    analyze_level, synthesize_multilevel, generate_html_chart,
    compute_macd, parse_klines,
)

BASE_DIR = os.path.join(PROJECT_ROOT, "indices")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "index_watchlist.json")
REPORT_DIR = os.path.join(BASE_DIR, "reports")


def _load_config() -> dict:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def _load_json(path: str, default=None):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default if default is not None else {}


def _save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _append_jsonl(path: str, record: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: str) -> list:
    records = []
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def _ensure_index_dirs(index_code: str, index_name: str) -> str:
    idx_dir = os.path.join(BASE_DIR, f"{index_code}_{index_name}")
    for sub in ["data", "analysis", "tracking"]:
        os.makedirs(os.path.join(idx_dir, sub), exist_ok=True)
    return idx_dir


# ─── Data Fetching ───

def fetch_all_index_data(config: dict) -> Dict[str, dict]:
    """Fetch daily + 30min K-line data for all configured indices."""
    settings = config["settings"]
    beg = settings["data_start"]
    end = datetime.now().strftime("%Y%m%d")
    results = {}

    for idx_info in config["indices"]:
        code = idx_info["index_code"]
        name = idx_info["index_name"]
        etf_code = idx_info["etf_code"]
        etf_name = idx_info["etf_name"]
        idx_dir = _ensure_index_dirs(code, name)

        print(f"\n--- {name}（{code}）/ {etf_name}（{etf_code}）---")

        print(f"  Fetching index daily data...")
        idx_daily = fetch_index_kline(code, "101", beg, end)
        print(f"    -> {len(idx_daily)} daily records")

        print(f"  Fetching ETF daily data...")
        etf_daily = fetch_kline(etf_code, "101", beg, end)
        print(f"    -> {len(etf_daily)} daily records")

        print(f"  Fetching ETF 30min data...")
        etf_30min = fetch_kline(etf_code, "30", beg, end)
        print(f"    -> {len(etf_30min)} 30min records")

        if etf_daily:
            title = f"{name}ETF（{etf_code}）"
            md = daily_to_md(etf_daily, title=title)
            path = os.path.join(idx_dir, "data", "日线数据.md")
            with open(path, 'w', encoding='utf-8') as f:
                f.write(md)

        if etf_30min:
            title = f"{name}ETF（{etf_code}）"
            md = min30_to_md(etf_30min, title=title)
            path = os.path.join(idx_dir, "data", "30分钟线数据.md")
            with open(path, 'w', encoding='utf-8') as f:
                f.write(md)

        results[code] = {
            "info": idx_info,
            "idx_daily": idx_daily,
            "etf_daily": etf_daily,
            "etf_30min": etf_30min,
        }

    return results


# ─── Chanlun Analysis per Index ───

def analyze_single_index(code: str, name: str, etf_code: str, etf_name: str) -> dict:
    """Run Chanlun multi-level analysis on one index's ETF data."""
    idx_dir = os.path.join(BASE_DIR, f"{code}_{name}")
    daily_path = os.path.join(idx_dir, "data", "日线数据.md")
    min30_path = os.path.join(idx_dir, "data", "30分钟线数据.md")

    if not os.path.exists(daily_path):
        print(f"  [SKIP] No daily data for {name}")
        return {}

    title = f"{name}（{code}）/ {etf_name}（{etf_code}）"
    daily_result = analyze_level(daily_path, "日线")
    min30_result = analyze_level(min30_path, "30分钟") if os.path.exists(min30_path) else {}

    md_report = synthesize_multilevel(daily_result, min30_result, title=title)
    md_path = os.path.join(idx_dir, "analysis", "缠论买卖点分析.md")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md_report)

    html_chart = generate_html_chart(daily_result, min30_result, title=title)
    html_path = os.path.join(idx_dir, "analysis", "缠论分析图.html")
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_chart)

    return {
        "daily": daily_result,
        "min30": min30_result,
        "md_path": md_path,
        "html_path": html_path,
    }


def analyze_all_indices(config: dict) -> Dict[str, dict]:
    """Run Chanlun analysis for all configured indices."""
    results = {}
    for idx_info in config["indices"]:
        code = idx_info["index_code"]
        name = idx_info["index_name"]
        etf_code = idx_info["etf_code"]
        etf_name = idx_info["etf_name"]

        print(f"\n{'='*60}")
        print(f"  Analyzing: {name}（{code}）")
        print(f"{'='*60}")

        result = analyze_single_index(code, name, etf_code, etf_name)
        if result:
            results[code] = result

    return results


# ─── Signal Tracking ───

class IndexSignalTracker:

    def __init__(self, index_code: str, index_name: str):
        idx_dir = _ensure_index_dirs(index_code, index_name)
        self.signals_path = os.path.join(idx_dir, "tracking", "signals.jsonl")

    def record_signal(self, date: str, signal_type: str, price: float,
                      level: str, confidence: str, description: str):
        record = {
            "timestamp": datetime.now().isoformat(),
            "date": date, "signal": signal_type, "price": price,
            "level": level, "confidence": confidence,
            "description": description,
            "status": "active", "outcome": None,
        }
        _append_jsonl(self.signals_path, record)
        print(f"  [Signal] {signal_type} @ {date} price={price:.2f} [{confidence}]")

    def get_signals(self) -> list:
        return _read_jsonl(self.signals_path)

    def get_active_signals(self) -> list:
        return [s for s in self.get_signals() if s.get("status") == "active"]


def record_index_signals(config: dict, analysis_results: Dict[str, dict]):
    """Extract buy/sell points from analysis and record as signals."""
    for idx_info in config["indices"]:
        code = idx_info["index_code"]
        name = idx_info["index_name"]

        result = analysis_results.get(code, {})
        if not result:
            continue

        tracker = IndexSignalTracker(code, name)
        existing = {(s['date'], s['signal']) for s in tracker.get_signals()}

        for level_key in ("daily", "min30"):
            level_result = result.get(level_key, {})
            for p in level_result.get('buy_sell_points', []):
                key = (p.date, p.type)
                if key not in existing:
                    tracker.record_signal(p.date, p.type, p.price,
                                          p.level, p.confidence, p.description)


# ─── Rotation Analysis ───

def compute_rotation_scores(config: dict, data_results: Dict[str, dict]) -> List[dict]:
    """
    Score each index for rotation ranking based on:
    1. Short-term momentum (20-day return)
    2. MACD trend strength
    3. Chanlun trend alignment
    4. Volume trend
    """
    settings = config["settings"]
    window = settings.get("rotation_window_days", 20)
    scores = []

    for idx_info in config["indices"]:
        code = idx_info["index_code"]
        name = idx_info["index_name"]
        data = data_results.get(code, {})
        etf_daily = data.get("etf_daily", [])

        if len(etf_daily) < window + 5:
            scores.append({
                "code": code, "name": name,
                "total_score": 0, "rank": 0,
                "momentum": 0, "macd_score": 0,
                "volume_score": 0, "category": idx_info["category"],
                "latest_price": 0, "change_pct_window": 0,
                "reason": "insufficient data",
            })
            continue

        recent = etf_daily[-window:]
        closes = [float(r["close"]) for r in recent]
        volumes = [int(r["volume"]) for r in recent]

        window_return = (closes[-1] - closes[0]) / closes[0] * 100
        momentum_score = min(max(window_return * 2, -20), 20)

        mid = window // 2
        vol_recent = sum(volumes[mid:]) / len(volumes[mid:])
        vol_early = sum(volumes[:mid]) / len(volumes[:mid])
        vol_ratio = vol_recent / vol_early if vol_early > 0 else 1.0
        volume_score = min((vol_ratio - 1.0) * 10, 10) if vol_ratio > 1.0 else max((vol_ratio - 1.0) * 10, -10)

        ema12 = closes[0]
        ema26 = closes[0]
        dif_vals = []
        for c in closes:
            ema12 = ema12 * (1 - 2/13) + c * 2/13
            ema26 = ema26 * (1 - 2/27) + c * 2/27
            dif_vals.append(ema12 - ema26)

        macd_score = 0
        if dif_vals[-1] > 0:
            macd_score += 5
        if len(dif_vals) >= 2 and dif_vals[-1] > dif_vals[-2]:
            macd_score += 5
        if dif_vals[-1] < 0:
            macd_score -= 5
        if len(dif_vals) >= 2 and dif_vals[-1] < dif_vals[-2]:
            macd_score -= 3

        total = (momentum_score * 0.4 + macd_score * 0.3 + volume_score * 0.3) * idx_info["weight"]

        scores.append({
            "code": code, "name": name,
            "total_score": round(total, 2),
            "momentum": round(momentum_score, 2),
            "macd_score": round(macd_score, 2),
            "volume_score": round(volume_score, 2),
            "category": idx_info["category"],
            "latest_price": closes[-1],
            "change_pct_window": round(window_return, 2),
            "dif_latest": round(dif_vals[-1], 4) if dif_vals else 0,
            "reason": "ok",
        })

    scores.sort(key=lambda x: x["total_score"], reverse=True)
    for i, s in enumerate(scores):
        s["rank"] = i + 1

    return scores


def enrich_with_chanlun(scores: List[dict], analysis_results: Dict[str, dict]) -> List[dict]:
    """Add Chanlun analysis summary to rotation scores."""
    for s in scores:
        code = s["code"]
        result = analysis_results.get(code, {})
        daily = result.get("daily", {})

        s["trend"] = daily.get("trend", "N/A")
        s["hub_count"] = len(daily.get("hubs", []))
        s["stroke_count"] = len(daily.get("strokes", []))

        bsp = daily.get("buy_sell_points", [])
        s["buy_signals"] = [p.type for p in bsp if "B" in p.type]
        s["sell_signals"] = [p.type for p in bsp if "S" in p.type]

        if daily.get("klines"):
            lk = daily["klines"][-1]
            s["macd_dif"] = round(lk.dif, 4)
            s["macd_dea"] = round(lk.dea, 4)
            s["macd_bar"] = round(lk.macd, 4)

        hubs = daily.get("hubs", [])
        if hubs:
            last_hub = hubs[-1]
            s["last_hub_zg"] = round(last_hub.zg, 2)
            s["last_hub_zd"] = round(last_hub.zd, 2)
            if s.get("latest_price"):
                if s["latest_price"] > last_hub.zg:
                    s["hub_position"] = "above"
                elif s["latest_price"] < last_hub.zd:
                    s["hub_position"] = "below"
                else:
                    s["hub_position"] = "inside"

        chanlun_bonus = 0
        if s.get("buy_signals"):
            chanlun_bonus += 3 * len(s["buy_signals"])
        if s.get("sell_signals"):
            chanlun_bonus -= 3 * len(s["sell_signals"])
        if s.get("hub_position") == "above":
            chanlun_bonus += 2
        elif s.get("hub_position") == "below":
            chanlun_bonus -= 2
        if "上涨" in s.get("trend", ""):
            chanlun_bonus += 3
        elif "下跌" in s.get("trend", ""):
            chanlun_bonus -= 3

        s["chanlun_bonus"] = chanlun_bonus
        s["final_score"] = round(s["total_score"] + chanlun_bonus, 2)

    scores.sort(key=lambda x: x["final_score"], reverse=True)
    for i, s in enumerate(scores):
        s["final_rank"] = i + 1

    return scores


# ─── Report Generation ───

def generate_rotation_report(scores: List[dict], config: dict) -> str:
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    settings = config["settings"]
    window = settings.get("rotation_window_days", 20)

    lines = [
        "# A股宽基指数轮动分析报告",
        "",
        f"> 生成时间：{now}",
        f"> 动量窗口：{window} 个交易日",
        f"> 覆盖指数：{len(scores)} 只宽基指数及对应ETF",
        "",
        "---",
        "",
        "## 一、轮动排名总表",
        "",
        "| 排名 | 指数 | 类别 | 最新价 | 窗口涨幅 | 动量分 | MACD分 | 量能分 | 缠论加分 | 综合分 | 走势类型 |",
        "|------|------|------|--------|----------|--------|--------|--------|----------|--------|----------|",
    ]

    for s in scores:
        trend = s.get("trend", "N/A")
        lines.append(
            f"| {s['final_rank']} | {s['name']} | {s['category']} "
            f"| {s['latest_price']:.3f} | {s['change_pct_window']:+.2f}% "
            f"| {s['momentum']:.1f} | {s['macd_score']:.1f} | {s['volume_score']:.1f} "
            f"| {s.get('chanlun_bonus', 0):+.1f} | **{s['final_score']:.1f}** | {trend} |"
        )

    lines.extend(["", "---", ""])

    lines.extend([
        "## 二、各指数缠论摘要",
        "",
    ])

    for s in scores:
        emoji = "🟢" if s["final_score"] > 0 else "🔴" if s["final_score"] < -5 else "🟡"
        hub_pos = {"above": "中枢上方", "below": "中枢下方", "inside": "中枢内部"}.get(s.get("hub_position", ""), "N/A")

        lines.extend([
            f"### {emoji} {s['name']}（{s['code']}）",
            "",
            f"- **走势类型**：{s.get('trend', 'N/A')}",
            f"- **中枢数**：{s.get('hub_count', 0)}，笔数：{s.get('stroke_count', 0)}",
            f"- **价格位置**：{hub_pos}",
        ])

        if s.get("last_hub_zg"):
            lines.append(f"- **最近中枢**：ZD={s.get('last_hub_zd', 0):.2f} ~ ZG={s.get('last_hub_zg', 0):.2f}")
        if s.get("macd_dif") is not None:
            lines.append(f"- **MACD**：DIF={s.get('macd_dif', 0):.4f}，DEA={s.get('macd_dea', 0):.4f}，柱={'红' if s.get('macd_bar', 0) > 0 else '绿'}柱")

        buys = s.get("buy_signals", [])
        sells = s.get("sell_signals", [])
        if buys:
            lines.append(f"- **买点信号**：{', '.join(buys)}")
        if sells:
            lines.append(f"- **卖点信号**：{', '.join(sells)}")
        if not buys and not sells:
            lines.append("- **买卖点**：无新信号")

        lines.append("")

    lines.extend(["---", ""])

    lines.extend([
        "## 三、轮动操作建议",
        "",
    ])

    top_indices = [s for s in scores if s["final_score"] > 0]
    bottom_indices = [s for s in scores if s["final_score"] < -3]

    if top_indices:
        lines.append("### 3.1 推荐关注（综合分 > 0）")
        lines.append("")
        for s in top_indices:
            idx_info = next((i for i in config["indices"] if i["index_code"] == s["code"]), {})
            etf_info = f"{idx_info.get('etf_name', '')}（{idx_info.get('etf_code', '')}）"
            lines.append(f"- **{s['name']}** → 对应ETF：{etf_info}")
            if "上涨" in s.get("trend", ""):
                lines.append(f"  - 处于上涨趋势，可持仓或择机加仓")
            elif s.get("buy_signals"):
                lines.append(f"  - 出现买点信号 {s['buy_signals']}，可考虑建仓")
            elif s.get("hub_position") == "above":
                lines.append(f"  - 价格在中枢上方运行，关注回试确认（三买）")
            else:
                lines.append(f"  - 动量向上，关注缠论买点确认后介入")
        lines.append("")

    if bottom_indices:
        lines.append("### 3.2 回避或减仓（综合分 < -3）")
        lines.append("")
        for s in bottom_indices:
            lines.append(f"- **{s['name']}**：{s.get('trend', 'N/A')}，综合分 {s['final_score']:.1f}")
        lines.append("")

    lines.extend([
        "### 3.3 轮动策略要点",
        "",
        "1. **核心原则**：选择处于上涨趋势且动量最强的指数ETF配置",
        "2. **缠论确认**：单纯动量不够，需要缠论买卖点确认",
        "3. **大小盘轮动**：大盘价值（沪深300/上证50）与中小盘成长（中证500/1000）通常交替领涨",
        "4. **仓位管理**：",
        "   - 综合分 > 5 且有买点：可配置 40%~60%",
        "   - 综合分 0~5：轻仓观察 20%~30%",
        "   - 综合分 < 0：空仓观望",
        "5. **调仓频率**：建议每周末分析一次，月度调仓",
        "6. **止损规则**：跌破最近中枢下沿 ZD 减半仓，跌破前低清仓",
        "",
        "---",
        "",
        "> **重要声明**：本分析基于缠论技术方法和动量轮动模型，不构成投资建议。",
        "> 指数ETF投资也有风险，请根据自身风险承受能力做出决策。",
    ])

    return "\n".join(lines)


def generate_single_index_report(code: str, name: str, analysis: dict,
                                 score: dict, config: dict) -> str:
    """Generate individual index tracking report."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    idx_info = next((i for i in config["indices"] if i["index_code"] == code), {})
    tracker = IndexSignalTracker(code, name)
    signals = tracker.get_signals()
    active_signals = [s for s in signals if s.get("status") == "active"]

    lines = [
        f"# {name}（{code}）每日跟踪报告",
        "",
        f"> 生成时间：{now}",
        f"> ETF：{idx_info.get('etf_name', '')}（{idx_info.get('etf_code', '')}）",
        "",
        "---",
        "",
        "## 基本信息",
        "",
        f"- **类别**：{idx_info.get('category', '')}",
        f"- **说明**：{idx_info.get('notes', '')}",
        f"- **轮动排名**：第 {score.get('final_rank', 'N/A')} 名",
        f"- **综合得分**：{score.get('final_score', 0):.1f}",
        f"- **窗口涨幅**：{score.get('change_pct_window', 0):+.2f}%",
        "",
    ]

    if active_signals:
        lines.extend([
            "## 活跃信号",
            "",
            "| 日期 | 类型 | 价格 | 级别 | 置信度 |",
            "|------|------|------|------|--------|",
        ])
        for s in active_signals:
            lines.append(f"| {s['date']} | {s['signal']} | {s['price']:.2f} | {s['level']} | {s['confidence']} |")
        lines.append("")

    if signals:
        lines.extend([
            "## 信号历史（最近20条）",
            "",
            "| 日期 | 类型 | 价格 | 状态 | 结果 |",
            "|------|------|------|------|------|",
        ])
        for s in sorted(signals, key=lambda x: x.get("date", ""), reverse=True)[:20]:
            outcome = s.get("outcome", "-") or "-"
            lines.append(f"| {s['date']} | {s['signal']} | {s['price']:.2f} | {s['status']} | {outcome} |")
        lines.append("")

    lines.extend(["---", "", "> 本报告自动生成，配合缠论分析报告使用。"])
    return "\n".join(lines)


# ─── Main Orchestrator ───

def full_update():
    """Run complete index trading system update."""
    print("=" * 70)
    print("  A股宽基指数缠论交易系统 — 完整更新")
    print("=" * 70)

    config = _load_config()
    os.makedirs(REPORT_DIR, exist_ok=True)

    print("\n[1/5] Fetching data for all indices and ETFs...")
    data_results = fetch_all_index_data(config)

    print("\n[2/5] Running Chanlun analysis on all indices...")
    analysis_results = analyze_all_indices(config)

    print("\n[3/5] Recording buy/sell point signals...")
    record_index_signals(config, analysis_results)

    print("\n[4/5] Computing rotation scores...")
    scores = compute_rotation_scores(config, data_results)
    scores = enrich_with_chanlun(scores, analysis_results)

    rotation_path = os.path.join(REPORT_DIR, "rotation_scores.json")
    _save_json(rotation_path, {
        "timestamp": datetime.now().isoformat(),
        "scores": scores,
    })

    print("\n  Rotation ranking:")
    for s in scores:
        emoji = "🟢" if s["final_score"] > 0 else "🔴" if s["final_score"] < -5 else "🟡"
        print(f"    {emoji} #{s['final_rank']} {s['name']}: score={s['final_score']:.1f}, "
              f"momentum={s['change_pct_window']:+.2f}%, trend={s.get('trend', 'N/A')}")

    print("\n[5/5] Generating reports...")
    rotation_report = generate_rotation_report(scores, config)
    rotation_md_path = os.path.join(REPORT_DIR, "指数轮动分析报告.md")
    with open(rotation_md_path, 'w', encoding='utf-8') as f:
        f.write(rotation_report)

    for idx_info in config["indices"]:
        code = idx_info["index_code"]
        name = idx_info["index_name"]
        analysis = analysis_results.get(code, {})
        score = next((s for s in scores if s["code"] == code), {})
        idx_dir = os.path.join(BASE_DIR, f"{code}_{name}")

        report = generate_single_index_report(code, name, analysis, score, config)
        report_path = os.path.join(idx_dir, "tracking", "daily_report.md")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)

    _append_jsonl(os.path.join(REPORT_DIR, "rotation_history.jsonl"), {
        "timestamp": datetime.now().isoformat(),
        "ranking": [{"rank": s["final_rank"], "code": s["code"],
                      "name": s["name"], "score": s["final_score"]}
                     for s in scores],
    })

    print(f"\n{'='*70}")
    print("  Output files:")
    print(f"  - Rotation report:  {rotation_md_path}")
    print(f"  - Rotation scores:  {rotation_path}")
    for idx_info in config["indices"]:
        code = idx_info["index_code"]
        name = idx_info["index_name"]
        idx_dir = os.path.join(BASE_DIR, f"{code}_{name}")
        print(f"  - {name}: {idx_dir}/analysis/")
    print(f"{'='*70}")
    print("\nDone!")


if __name__ == "__main__":
    full_update()
