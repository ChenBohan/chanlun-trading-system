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
        etf_30min = fetch_kline(etf_code, "30", beg, end, datalen=1023)
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
    html_chart = generate_html_chart(daily_result, min30_result, title=title)

    today_str = datetime.now().strftime('%Y%m%d')
    analysis_dir = os.path.join(idx_dir, "analysis")
    history_dir = os.path.join(analysis_dir, today_str)
    os.makedirs(history_dir, exist_ok=True)

    for fpath, content in [
        (os.path.join(analysis_dir, "缠论买卖点分析.md"), md_report),
        (os.path.join(analysis_dir, "缠论分析图.html"), html_chart),
        (os.path.join(history_dir, "缠论买卖点分析.md"), md_report),
        (os.path.join(history_dir, "缠论分析图.html"), html_chart),
    ]:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(content)

    return {
        "daily": daily_result,
        "min30": min30_result,
        "md_path": os.path.join(analysis_dir, "缠论买卖点分析.md"),
        "html_path": os.path.join(analysis_dir, "缠论分析图.html"),
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


# ─── Pure Chanlun Rotation Scoring ───

def _trend_score(trend: str) -> float:
    """Map trend classification to score. Pure Chanlun: 走势类型判定."""
    if "上涨" in trend:
        return 10.0
    if "下跌" in trend:
        return -10.0
    if "上方" in trend:
        return 5.0
    if "下方" in trend:
        return -5.0
    return 0.0


def _signal_score(buy_sell_points: list) -> float:
    """Score based on buy/sell point types. Pure Chanlun: 三类买卖点."""
    TYPE_WEIGHT = {
        "1B": 10, "1S": -10,
        "2B": 7,  "2S": -7,
        "3B": 5,  "3S": -5,
    }
    total = 0.0
    for p in buy_sell_points:
        bsp_type = p.type if hasattr(p, 'type') else p.get('type', '')
        w = TYPE_WEIGHT.get(bsp_type, 0)
        if "盘整" in (p.label if hasattr(p, 'label') else p.get('label', '')):
            w = int(w * 0.6)
        total += w
    return max(min(total, 20), -20)


def _macd_dynamics_score(klines: list) -> float:
    """MACD-based scoring using Chanlun's 防狼术 and bar direction.

    Per 108课 §三 and 土匪注解版: DIF above/below 0-axis determines
    the macro trend direction; bar expansion/contraction indicates
    momentum acceleration.
    """
    if not klines:
        return 0.0
    last = klines[-1]
    dif = last.dif if hasattr(last, 'dif') else 0.0
    bar = last.macd if hasattr(last, 'macd') else 0.0

    score = 0.0
    score += 5.0 if dif > 0 else -5.0

    if len(klines) >= 2:
        prev_bar = klines[-2].macd if hasattr(klines[-2], 'macd') else 0.0
        if bar > 0 and bar > prev_bar:
            score += 3.0
        elif bar < 0 and bar < prev_bar:
            score -= 3.0
    return score


def _hub_structure_score(hubs: list, latest_price: float) -> float:
    """Score based on price position relative to hubs and hub progression.

    Pure Chanlun: 中枢位置 + 中枢上移/下移 (§1.4-1.5).
    """
    if not hubs:
        return 0.0
    last_hub = hubs[-1]
    score = 0.0

    if latest_price > last_hub.zg:
        score += 5.0
    elif latest_price < last_hub.zd:
        score -= 5.0

    if len(hubs) >= 2:
        prev = hubs[-2]
        if last_hub.zd > prev.zg:
            score += 5.0
        elif last_hub.zg < prev.zd:
            score -= 5.0
    return score


def _resonance_score(daily_trend: str, min30_trend: str,
                     min30_bsp: list) -> float:
    """Multi-level resonance scoring.

    Pure Chanlun: 级别共振 (图解缠论3, 缠论辅导).
    Aligned levels amplify confidence; contradictions demand caution.
    """
    d_up = "上涨" in daily_trend
    d_down = "下跌" in daily_trend
    m_up = "上涨" in min30_trend
    m_down = "下跌" in min30_trend

    if d_up and m_up:
        return 10.0
    if d_down and m_down:
        return -10.0
    if d_up and m_down:
        has_buy = any(("B" in (p.type if hasattr(p, 'type') else p.get('type', '')))
                      for p in min30_bsp)
        return 3.0 if has_buy else -2.0
    if d_down and m_up:
        return -3.0
    if d_up:
        return 5.0
    if d_down:
        return -5.0
    return 0.0


def compute_chanlun_scores(config: dict, data_results: Dict[str, dict],
                           analysis_results: Dict[str, dict]) -> List[dict]:
    """Pure Chanlun rotation scoring across 5 dimensions.

    All dimensions derive from Chanlun theory:
      1. 走势类型 (Trend Classification)   - weight 20%
      2. 买卖点信号 (Signal Strength)      - weight 25%
      3. MACD动力学 (防狼术+背驰)          - weight 20%
      4. 中枢结构 (Hub Structure)          - weight 15%
      5. 级别共振 (Multi-level Resonance)  - weight 20%
    """
    WEIGHTS = {
        "trend": 0.20,
        "signal": 0.25,
        "macd": 0.20,
        "hub": 0.15,
        "resonance": 0.20,
    }
    scores = []

    for idx_info in config["indices"]:
        code = idx_info["index_code"]
        name = idx_info["index_name"]
        data = data_results.get(code, {})
        analysis = analysis_results.get(code, {})
        etf_daily = data.get("etf_daily", [])

        daily = analysis.get("daily", {})
        min30 = analysis.get("min30", {})

        if not daily:
            scores.append({
                "code": code, "name": name,
                "final_score": 0, "rank": 0,
                "d1_trend": 0, "d2_signal": 0, "d3_macd": 0,
                "d4_hub": 0, "d5_resonance": 0,
                "category": idx_info["category"],
                "latest_price": 0, "reason": "insufficient data",
            })
            continue

        latest_price = 0.0
        if daily.get("klines"):
            latest_price = daily["klines"][-1].close
        elif etf_daily:
            latest_price = float(etf_daily[-1]["close"])

        daily_trend = daily.get("trend", "N/A")
        min30_trend = min30.get("trend", "N/A")
        daily_bsp = daily.get("buy_sell_points", [])
        min30_bsp = min30.get("buy_sell_points", [])
        all_bsp = daily_bsp + min30_bsp

        d1 = _trend_score(daily_trend) * 0.6 + _trend_score(min30_trend) * 0.4
        d2 = _signal_score(all_bsp)
        d3 = (_macd_dynamics_score(daily.get("klines", [])) * 0.6 +
              _macd_dynamics_score(min30.get("klines", [])) * 0.4)
        d4 = _hub_structure_score(daily.get("hubs", []), latest_price)
        d5 = _resonance_score(daily_trend, min30_trend, min30_bsp)

        raw_score = (d1 * WEIGHTS["trend"] +
                     d2 * WEIGHTS["signal"] +
                     d3 * WEIGHTS["macd"] +
                     d4 * WEIGHTS["hub"] +
                     d5 * WEIGHTS["resonance"])
        final = round(raw_score * idx_info["weight"], 2)

        hubs = daily.get("hubs", [])
        hub_position = ""
        if hubs and latest_price:
            if latest_price > hubs[-1].zg:
                hub_position = "above"
            elif latest_price < hubs[-1].zd:
                hub_position = "below"
            else:
                hub_position = "inside"

        entry = {
            "code": code, "name": name,
            "final_score": final,
            "d1_trend": round(d1, 2),
            "d2_signal": round(d2, 2),
            "d3_macd": round(d3, 2),
            "d4_hub": round(d4, 2),
            "d5_resonance": round(d5, 2),
            "category": idx_info["category"],
            "latest_price": round(latest_price, 4),
            "trend": daily_trend,
            "min30_trend": min30_trend,
            "hub_count": len(hubs),
            "stroke_count": len(daily.get("strokes", [])),
            "buy_signals": [p.type for p in daily_bsp if "B" in p.type],
            "sell_signals": [p.type for p in daily_bsp if "S" in p.type],
            "min30_buy_signals": [p.type for p in min30_bsp if "B" in p.type],
            "min30_sell_signals": [p.type for p in min30_bsp if "S" in p.type],
            "hub_position": hub_position,
            "reason": "ok",
        }

        if daily.get("klines"):
            lk = daily["klines"][-1]
            entry["macd_dif"] = round(lk.dif, 4)
            entry["macd_dea"] = round(lk.dea, 4)
            entry["macd_bar"] = round(lk.macd, 4)

        if hubs:
            entry["last_hub_zg"] = round(hubs[-1].zg, 2)
            entry["last_hub_zd"] = round(hubs[-1].zd, 2)

        scores.append(entry)

    scores.sort(key=lambda x: x["final_score"], reverse=True)
    for i, s in enumerate(scores):
        s["final_rank"] = i + 1

    return scores


# ─── Report Generation ───

def generate_rotation_report(scores: List[dict], config: dict) -> str:
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    lines = [
        "# A股宽基指数轮动分析报告",
        "",
        f"> 生成时间：{now}",
        f"> 评分体系：纯缠论五维度（走势类型·买卖点·MACD动力学·中枢结构·级别共振）",
        f"> 覆盖指数：{len(scores)} 只宽基指数及对应ETF",
        f"> 理论基础：缠论（缠中说禅技术分析理论），详见 [`docs/指数轮动交易系统逻辑.md`](../../docs/指数轮动交易系统逻辑.md)",
        "",
        "---",
        "",
        "## 一、轮动排名总表",
        "",
        "| 排名 | 指数 | 类别 | 最新价 | 走势类型 | 信号分 | MACD分 | 中枢分 | 共振分 | 综合分 | 日线走势 |",
        "|------|------|------|--------|----------|--------|--------|--------|--------|--------|----------|",
    ]

    for s in scores:
        trend = s.get("trend", "N/A")
        lines.append(
            f"| {s['final_rank']} | {s['name']} | {s['category']} "
            f"| {s['latest_price']:.3f} | {s['d1_trend']:+.1f} "
            f"| {s['d2_signal']:+.1f} | {s['d3_macd']:+.1f} | {s['d4_hub']:+.1f} "
            f"| {s['d5_resonance']:+.1f} | **{s['final_score']:.1f}** | {trend} |"
        )

    lines.extend([
        "",
        "> **评分维度说明**：走势类型(20%) — 日线+30分钟趋势判定 | "
        "信号分(25%) — 三类买卖点强度 | MACD分(20%) — 防狼术+柱状体方向 | "
        "中枢分(15%) — 价格相对中枢位置+中枢上移 | 共振分(20%) — 多级别共振/矛盾",
        "",
        "---",
        "",
    ])

    lines.extend([
        "## 二、各指数缠论摘要",
        "",
    ])

    for s in scores:
        emoji = "🟢" if s["final_score"] > 0 else "🔴" if s["final_score"] < -5 else "🟡"
        hub_pos = {"above": "中枢上方", "below": "中枢下方", "inside": "中枢内部"}.get(s.get("hub_position", ""), "N/A")
        min30_trend = s.get("min30_trend", "N/A")

        lines.extend([
            f"### {emoji} {s['name']}（{s['code']}）",
            "",
            f"- **日线走势**：{s.get('trend', 'N/A')}（中枢{s.get('hub_count', 0)}个，笔{s.get('stroke_count', 0)}根）",
            f"- **30分钟走势**：{min30_trend}",
            f"- **价格位置**：{hub_pos}",
        ])

        if s.get("last_hub_zg"):
            lines.append(f"- **最近中枢**：ZD={s.get('last_hub_zd', 0):.2f} ~ ZG={s.get('last_hub_zg', 0):.2f}")
        if s.get("macd_dif") is not None:
            bar_str = "红" if s.get("macd_bar", 0) > 0 else "绿"
            wolf_str = "✅ 0轴上方" if s.get("macd_dif", 0) > 0 else "⚠️ 0轴下方（防狼术警告）"
            lines.append(f"- **MACD**：DIF={s.get('macd_dif', 0):.4f}，DEA={s.get('macd_dea', 0):.4f}，{bar_str}柱 | {wolf_str}")

        buys = s.get("buy_signals", [])
        sells = s.get("sell_signals", [])
        m_buys = s.get("min30_buy_signals", [])
        m_sells = s.get("min30_sell_signals", [])

        if buys or m_buys:
            parts = []
            if buys:
                parts.append(f"日线 {', '.join(buys)}")
            if m_buys:
                parts.append(f"30分钟 {', '.join(m_buys)}")
            lines.append(f"- **买点信号**：{' | '.join(parts)}")
        if sells or m_sells:
            parts = []
            if sells:
                parts.append(f"日线 {', '.join(sells)}")
            if m_sells:
                parts.append(f"30分钟 {', '.join(m_sells)}")
            lines.append(f"- **卖点信号**：{' | '.join(parts)}")
        if not buys and not sells and not m_buys and not m_sells:
            lines.append("- **买卖点**：无新信号")

        d_up = "上涨" in s.get("trend", "")
        m_up = "上涨" in min30_trend
        m_down = "下跌" in min30_trend
        if d_up and m_up:
            lines.append("- **级别共振**：✅ 日线+30分钟同向上涨")
        elif d_up and m_down:
            lines.append("- **级别共振**：⚠️ 日线上涨 vs 30分钟下跌（级别矛盾）")

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
            if "上涨" in s.get("trend", "") and "上涨" in s.get("min30_trend", ""):
                lines.append(f"  - ✅ 双级别上涨共振，可持仓或择机加仓")
            elif s.get("buy_signals") or s.get("min30_buy_signals"):
                all_buys = s.get("buy_signals", []) + s.get("min30_buy_signals", [])
                lines.append(f"  - 出现买点信号 {all_buys}，可考虑建仓")
            elif s.get("hub_position") == "above":
                lines.append(f"  - 价格在中枢上方运行，关注回试确认（三买）")
            else:
                lines.append(f"  - 缠论结构偏多，关注买卖点确认后介入")
        lines.append("")

    if bottom_indices:
        lines.append("### 3.2 回避或减仓（综合分 < -3）")
        lines.append("")
        for s in bottom_indices:
            lines.append(f"- **{s['name']}**：{s.get('trend', 'N/A')}，综合分 {s['final_score']:.1f}")
        lines.append("")

    lines.extend([
        "### 3.3 缠论轮动策略要点",
        "",
        "1. **核心原则**：选择双级别走势共振且买点信号最强的指数ETF",
        "2. **防狼术**：日线 MACD DIF 在 0 轴下方时不做多（空头主导）",
        "3. **买卖点确认**：必须有明确的缠论买点（一买/二买/三买）才能建仓",
        "4. **仓位管理**（基于缠论信号）：",
        "   - 双级别共振 + 买点确认：40%~60%",
        "   - 单级别买点 + 另一级别中性：20%~30%",
        "   - 无买点或卖点出现：空仓观望",
        "5. **止损规则**（基于中枢结构）：",
        "   - 跌破最近中枢下沿 ZD → 减半仓",
        "   - 三卖信号出现 → 必须清仓",
        "   - 跌破前低 → 清仓",
        "6. **调仓频率**：建议每周末分析一次，月度调仓",
        "",
        "---",
        "",
        "> **重要声明**：本分析完全基于缠论技术方法，不构成投资建议。",
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


# ─── Tracking Log ───

def _update_tracking_log(scores: List[dict], config: dict,
                         analysis_results: dict, daily_dir: str):
    """Append today's summary to the persistent tracking.md file.

    Same-day runs overwrite the day's section; different days append a new section.
    Records: ranking, conclusions, key signals, and current status for trend tracking.
    """
    tracking_path = os.path.join(REPORT_DIR, "tracking.md")
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    date_header = f"## {today_str}"

    ranking_lines = []
    for s in scores:
        emoji = "🟢" if s["final_score"] > 0 else "🔴" if s["final_score"] < -5 else "🟡"
        ranking_lines.append(
            f"| {s['final_rank']} | {emoji} {s['name']} | {s['latest_price']:.3f} "
            f"| {s['final_score']:.1f} | {s.get('trend', 'N/A')} |"
        )

    signals_summary = []
    for s in scores:
        buys = s.get("buy_signals", [])
        sells = s.get("sell_signals", [])
        if buys or sells:
            parts = []
            if buys:
                parts.append(f"买: {','.join(buys)}")
            if sells:
                parts.append(f"卖: {','.join(sells)}")
            signals_summary.append(f"- **{s['name']}**：{' | '.join(parts)}")

    level_resonance = []
    for s in scores:
        code = s["code"]
        analysis = analysis_results.get(code, {})
        daily_trend = s.get("trend", "N/A")
        min30_trend = analysis.get("min30", {}).get("trend", "N/A")
        if "上涨" in daily_trend and "上涨" in min30_trend:
            level_resonance.append(f"- **{s['name']}** ✅ 日线+30分钟同向上涨")
        elif "上涨" in daily_trend and "下跌" in min30_trend:
            level_resonance.append(f"- **{s['name']}** ⚠️ 日线上涨 vs 30分钟下跌")

    section = [
        date_header,
        "",
        f"> 更新时间：{now.strftime('%H:%M')}",
        "",
        "### 排名",
        "",
        "| 排名 | 指数 | 价格 | 综合分 | 走势类型 |",
        "|------|------|------|--------|---------|",
    ] + ranking_lines + [
        "",
        "### 关键信号",
        "",
    ] + (signals_summary if signals_summary else ["- 无新信号"]) + [
        "",
        "### 级别共振",
        "",
    ] + (level_resonance if level_resonance else ["- 无共振数据"]) + [
        "",
        "### 结论与计划",
        "",
        "<!-- 请在此处补充当日分析结论、操作计划和走势推演 -->",
        "",
        "---",
        "",
    ]

    new_section = "\n".join(section)

    if os.path.exists(tracking_path):
        with open(tracking_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if date_header in content:
            start = content.index(date_header)
            next_h2 = content.find("\n## ", start + len(date_header))
            if next_h2 == -1:
                content = content[:start] + new_section
            else:
                content = content[:start] + new_section + content[next_h2 + 1:]
            with open(tracking_path, 'w', encoding='utf-8') as f:
                f.write(content)
        else:
            with open(tracking_path, 'a', encoding='utf-8') as f:
                f.write(new_section)
    else:
        header = "\n".join([
            "# 指数轮动交易跟踪日志",
            "",
            "> 持续记录每日分析结论、操作计划和走势推演",
            "> 每次运行 `index_trading_system.py` 自动更新排名和信号",
            "> 结论和计划部分由人工或AI补充",
            "",
            "---",
            "",
        ])
        with open(tracking_path, 'w', encoding='utf-8') as f:
            f.write(header + new_section)

    print(f"  Tracking log updated: {tracking_path}")


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

    print("\n[4/5] Computing pure Chanlun rotation scores...")
    scores = compute_chanlun_scores(config, data_results, analysis_results)

    rotation_path = os.path.join(REPORT_DIR, "rotation_scores.json")
    _save_json(rotation_path, {
        "timestamp": datetime.now().isoformat(),
        "scores": scores,
    })

    print("\n  Rotation ranking (pure Chanlun):")
    for s in scores:
        emoji = "🟢" if s["final_score"] > 0 else "🔴" if s["final_score"] < -5 else "🟡"
        print(f"    {emoji} #{s['final_rank']} {s['name']}: score={s['final_score']:.1f}, "
              f"trend={s.get('trend', 'N/A')}, "
              f"[走势={s['d1_trend']:.1f} 信号={s['d2_signal']:.1f} "
              f"MACD={s['d3_macd']:.1f} 中枢={s['d4_hub']:.1f} 共振={s['d5_resonance']:.1f}]")

    print("\n[5/5] Generating reports...")

    today_str = datetime.now().strftime('%Y%m%d')
    daily_dir = os.path.join(REPORT_DIR, today_str)
    os.makedirs(daily_dir, exist_ok=True)

    rotation_report = generate_rotation_report(scores, config)
    daily_report_path = os.path.join(daily_dir, "指数轮动分析报告.md")
    with open(daily_report_path, 'w', encoding='utf-8') as f:
        f.write(rotation_report)

    latest_link = os.path.join(REPORT_DIR, "指数轮动分析报告.md")
    with open(latest_link, 'w', encoding='utf-8') as f:
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

    _update_tracking_log(scores, config, analysis_results, daily_dir)

    print(f"\n{'='*70}")
    print("  Output files:")
    print(f"  - Daily report dir: {daily_dir}/")
    print(f"  - Rotation report:  {daily_report_path}")
    print(f"  - Latest link:      {latest_link}")
    print(f"  - Rotation scores:  {rotation_path}")
    print(f"  - Tracking log:     {os.path.join(REPORT_DIR, 'tracking.md')}")
    for idx_info in config["indices"]:
        code = idx_info["index_code"]
        name = idx_info["index_name"]
        idx_dir = os.path.join(BASE_DIR, f"{code}_{name}")
        print(f"  - {name}: {idx_dir}/analysis/")
    print(f"{'='*70}")
    print("\nDone!")


if __name__ == "__main__":
    full_update()
