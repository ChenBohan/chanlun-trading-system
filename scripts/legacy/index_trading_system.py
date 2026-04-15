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
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPTS_DIR))
sys.path.insert(0, SCRIPTS_DIR)

from fetch_data import (
    fetch_kline, fetch_index_kline, daily_to_md, min30_to_md,
    min120_to_md, _aggregate_to_120min,
)
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
    """Fetch daily + 120min + 30min K-line data for all configured indices.

    120-minute data is synthesized from 60-minute bars (Sina/EastMoney don't
    natively support 120min period).
    """
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

        is_cross_market = not code.isdigit()
        print(f"  Fetching index daily data...")
        if is_cross_market:
            idx_daily = []
            print(f"    -> Cross-market index, skipping (will use ETF data)")
        else:
            idx_daily = fetch_index_kline(code, "101", beg, end)
            print(f"    -> {len(idx_daily)} daily records")

        print(f"  Fetching ETF daily data...")
        etf_daily = fetch_kline(etf_code, "101", beg, end)
        print(f"    -> {len(etf_daily)} daily records")

        print(f"  Fetching ETF 60min data (for 120min aggregation)...")
        etf_60min = fetch_kline(etf_code, "60", beg, end, datalen=1023)
        print(f"    -> {len(etf_60min)} 60min records")
        etf_120min = _aggregate_to_120min(etf_60min) if etf_60min else []
        print(f"    -> aggregated to {len(etf_120min)} 120min records")

        print(f"  Fetching ETF 30min data...")
        etf_30min = fetch_kline(etf_code, "30", beg, end, datalen=1023)
        print(f"    -> {len(etf_30min)} 30min records")

        title = f"{name}ETF（{etf_code}）"

        if etf_daily:
            md = daily_to_md(etf_daily, title=title)
            path = os.path.join(idx_dir, "data", "日线数据.md")
            with open(path, 'w', encoding='utf-8') as f:
                f.write(md)

        if etf_120min:
            md = min120_to_md(etf_120min, title=title)
            path = os.path.join(idx_dir, "data", "120分钟线数据.md")
            with open(path, 'w', encoding='utf-8') as f:
                f.write(md)

        if etf_30min:
            md = min30_to_md(etf_30min, title=title)
            path = os.path.join(idx_dir, "data", "30分钟线数据.md")
            with open(path, 'w', encoding='utf-8') as f:
                f.write(md)

        results[code] = {
            "info": idx_info,
            "idx_daily": idx_daily,
            "etf_daily": etf_daily,
            "etf_120min": etf_120min,
            "etf_30min": etf_30min,
        }

    return results


# ─── Chanlun Analysis per Index ───

def analyze_single_index(code: str, name: str, etf_code: str, etf_name: str) -> dict:
    """Run Chanlun three-level analysis on one index's ETF data.

    Levels: daily (direction) -> 120min (operation) -> 30min (precision).
    """
    idx_dir = os.path.join(BASE_DIR, f"{code}_{name}")
    daily_path = os.path.join(idx_dir, "data", "日线数据.md")
    min120_path = os.path.join(idx_dir, "data", "120分钟线数据.md")
    min30_path = os.path.join(idx_dir, "data", "30分钟线数据.md")

    if not os.path.exists(daily_path):
        print(f"  [SKIP] No daily data for {name}")
        return {}

    title = f"{name}（{code}）/ {etf_name}（{etf_code}）"
    daily_result = analyze_level(daily_path, "日线")
    min120_result = analyze_level(min120_path, "120分钟") if os.path.exists(min120_path) else {}
    min30_result = analyze_level(min30_path, "30分钟") if os.path.exists(min30_path) else {}

    md_report = synthesize_multilevel(daily_result, min30_result, title=title,
                                       min120=min120_result)
    html_chart = generate_html_chart(daily_result, min30_result, title=title,
                                      min120=min120_result)

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
        "min120": min120_result,
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

        for level_key in ("daily", "min120", "min30"):
            level_result = result.get(level_key, {})
            for p in level_result.get('buy_sell_points', []):
                key = (p.date, p.type)
                if key not in existing:
                    tracker.record_signal(p.date, p.type, p.price,
                                          p.level, p.confidence, p.description)


# ─── Market Regime Detection (图解缠论3·§3.2) ───

def determine_market_regime(benchmark_analysis: dict) -> dict:
    """Determine overall market regime using the benchmark index (沪深300).

    Based on 图解缠论3 §3.2: rotation is most effective in ranging markets,
    less meaningful in strong bull (everything rises) or bear (everything falls).

    Returns dict with regime type and strategy adjustment.
    """
    daily = benchmark_analysis.get("daily", {})
    min30 = benchmark_analysis.get("min30", {})

    d_trend = daily.get("trend", "N/A")
    d_hubs = daily.get("hubs", [])
    d_klines = daily.get("klines", [])
    d_bsp = daily.get("buy_sell_points", [])
    m_trend = min30.get("trend", "N/A")

    regime = "震荡市"
    confidence = "medium"
    rotation_multiplier = 1.0
    strategy_note = ""

    has_sell = any("S" in p.type for p in d_bsp)
    has_buy = any("B" in p.type for p in d_bsp)

    if "上涨趋势" in d_trend and "上涨" in m_trend:
        regime = "牛市"
        confidence = "high"
        rotation_multiplier = 0.7
        strategy_note = "普涨格局，轮动收益有限；仍可抓主线弹性更大者"
    elif "上涨趋势" in d_trend:
        regime = "牛市"
        confidence = "medium"
        rotation_multiplier = 0.8
        strategy_note = "日线上涨但30分钟未共振，可能牛市初中期"
    elif "下跌趋势" in d_trend and "下跌" in m_trend:
        regime = "熊市"
        confidence = "high"
        rotation_multiplier = 0.3
        strategy_note = "泥沙俱下，轮动无意义；空仓为主，等待一买信号"
    elif "下跌趋势" in d_trend:
        regime = "熊市"
        confidence = "medium"
        rotation_multiplier = 0.5
        strategy_note = "日线下跌，30分钟有反弹；仅指数反弹级别操作"
    else:
        dif_above_zero = False
        if d_klines:
            dif_above_zero = d_klines[-1].dif > 0

        if len(d_hubs) >= 1:
            regime = "震荡市"
            confidence = "high" if len(d_hubs) >= 2 else "medium"
            rotation_multiplier = 1.0
            if dif_above_zero:
                strategy_note = "震荡偏强，MACD多头区间；轮动策略最有效"
            else:
                strategy_note = "震荡偏弱，MACD空头区间；轮动谨慎，集中主线"
        else:
            regime = "震荡市"
            confidence = "low"
            rotation_multiplier = 0.9
            strategy_note = "走势不明朗，等待中枢形成确认方向"

    return {
        "regime": regime,
        "confidence": confidence,
        "rotation_multiplier": rotation_multiplier,
        "strategy_note": strategy_note,
        "benchmark_trend": d_trend,
        "benchmark_min30_trend": m_trend,
    }


# ─── New Scoring Dimensions ───

def _bsp_progress_score(daily: dict, min30: dict) -> float:
    """Score based on buy/sell point completion progress (图解缠论2·§4.3).

    The core insight: indices that have completed more buy points in sequence
    (1B → 2B → 3B) are further along in their uptrend cycle and thus stronger.
    Those still in sell-point territory (1S → 2S → 3S) are weaker.

    谁先完成三买谁先拉主升 — the梯队 model.
    """
    daily_bsp = daily.get("buy_sell_points", [])
    min30_bsp = min30.get("buy_sell_points", [])

    PROGRESS_MAP = {
        "3B": 10,
        "2B": 7,
        "1B": 4,
        "PB": 2,   # consolidation divergence, weaker than trend 1B
        "PS": -2,
        "1S": -4,
        "2S": -7,
        "3S": -10,
    }

    score = 0.0
    all_bsp = daily_bsp + min30_bsp
    if not all_bsp:
        return 0.0

    latest_buy = None
    latest_sell = None
    for p in sorted(all_bsp, key=lambda x: x.date, reverse=True):
        if "B" in p.type and latest_buy is None:
            latest_buy = p
        if "S" in p.type and latest_sell is None:
            latest_sell = p
        if latest_buy and latest_sell:
            break

    if latest_buy:
        score += PROGRESS_MAP.get(latest_buy.type, 0) * (0.7 if latest_buy.level == "日线" else 0.3)
    if latest_sell:
        score += PROGRESS_MAP.get(latest_sell.type, 0) * (0.7 if latest_sell.level == "日线" else 0.3)

    buy_types_found = set()
    for p in daily_bsp:
        if "B" in p.type:
            buy_types_found.add(p.type)
    if "1B" in buy_types_found and "2B" in buy_types_found:
        score += 3.0
    if "2B" in buy_types_found and "3B" in buy_types_found:
        score += 5.0

    return max(min(score, 10), -10)


def _champion_penalty(code: str, rotation_history_path: str,
                      lookback: int = 4) -> float:
    """Penalize recent top performers (图解缠论3·§3.4).

    基钦周期 insight: 上一波涨幅最大的板块，下一轮往往偏弱.
    Check last N rotation snapshots; if this index was consistently #1,
    apply a small negative adjustment.
    """
    history = _read_jsonl(rotation_history_path)
    if len(history) < 2:
        return 0.0

    recent = history[-lookback:] if len(history) >= lookback else history

    top_count = 0
    bottom_count = 0
    for snapshot in recent:
        ranking = snapshot.get("ranking", [])
        if not ranking:
            continue
        if ranking[0].get("code") == code:
            top_count += 1
        if ranking[-1].get("code") == code:
            bottom_count += 1

    penalty = 0.0
    if top_count >= 3:
        penalty = -3.0
    elif top_count >= 2:
        penalty = -1.5

    if bottom_count >= 3:
        penalty = 2.0
    elif bottom_count >= 2:
        penalty = 1.0

    return penalty


def _anti_drop_score(klines: list, benchmark_klines: list, window: int = 10) -> float:
    """Reward indices that hold up during market drops (缠论辅导·§5.1).

    调整中关注逆市不跌品种作下一轮候选.
    Compare recent drawdown of this index vs benchmark.
    """
    if not klines or not benchmark_klines or len(klines) < window or len(benchmark_klines) < window:
        return 0.0

    idx_recent = klines[-window:]
    bm_recent = benchmark_klines[-window:]

    idx_change = (idx_recent[-1].close - idx_recent[0].close) / idx_recent[0].close
    bm_change = (bm_recent[-1].close - bm_recent[0].close) / bm_recent[0].close

    if bm_change >= 0:
        return 0.0

    relative_strength = idx_change - bm_change

    if relative_strength > 0.03:
        return 5.0
    elif relative_strength > 0.01:
        return 3.0
    elif relative_strength > 0:
        return 1.0
    elif relative_strength < -0.03:
        return -3.0
    return 0.0


def _ma_strength_score(klines: list) -> float:
    """Score based on price position relative to key moving averages
    (土匪注解·§4.5 + 图解缠论2·§4.2).

    Classify by which MA the price has conquered:
    Above MA250 → strongest; below MA5 → weakest.
    Uses simple MA periods: 5, 10, 20, 60, 120, 250.
    """
    if not klines or len(klines) < 5:
        return 0.0

    latest_close = klines[-1].close

    ma_periods = [5, 10, 20, 60, 120, 250]
    ma_scores = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]

    score = 0.0
    for period, pts in zip(ma_periods, ma_scores):
        if len(klines) >= period:
            ma_val = sum(k.close for k in klines[-period:]) / period
            if latest_close > ma_val:
                score += pts
            else:
                score -= pts

    ma5_rising = False
    if len(klines) >= 10:
        ma5_now = sum(k.close for k in klines[-5:]) / 5
        ma5_prev = sum(k.close for k in klines[-10:-5]) / 5
        ma5_rising = ma5_now > ma5_prev
        if ma5_rising:
            score += 1.5
        else:
            score -= 1.0

    return max(min(score, 10), -10)


# ─── Pure Chanlun Rotation Scoring (v3: 9 dimensions) ───

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
        "PB": 3,  "PS": -3,  # consolidation divergence, not trend reversal
    }
    total = 0.0
    for p in buy_sell_points:
        bsp_type = p.type if hasattr(p, 'type') else p.get('type', '')
        total += TYPE_WEIGHT.get(bsp_type, 0)
    return max(min(total, 10), -10)


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


def _three_level_resonance_score(daily_trend: str, min120_trend: str, min30_trend: str,
                                  min120_bsp: list, min30_bsp: list) -> float:
    """Three-level resonance scoring (日线→120分钟→30分钟).

    All three aligned → strongest signal; contradictions → caution.
    """
    d_up = "上涨" in daily_trend
    d_down = "下跌" in daily_trend
    h_up = "上涨" in min120_trend
    h_down = "下跌" in min120_trend
    m_up = "上涨" in min30_trend
    m_down = "下跌" in min30_trend

    if d_up and h_up and m_up:
        return 10.0
    if d_down and h_down and m_down:
        return -10.0
    if d_up and h_up and m_down:
        has_m_buy = any("B" in (p.type if hasattr(p, 'type') else p.get('type', ''))
                        for p in min30_bsp)
        return 5.0 if has_m_buy else 3.0
    if d_up and h_up:
        return 7.0
    if d_up and h_down:
        has_h_buy = any("B" in (p.type if hasattr(p, 'type') else p.get('type', ''))
                        for p in min120_bsp)
        return 2.0 if has_h_buy else -2.0
    if d_down and h_up:
        return -5.0
    if d_down and h_down:
        return -8.0
    if d_up:
        return 3.0
    if d_down:
        return -5.0
    return 0.0


def compute_chanlun_scores(config: dict, data_results: Dict[str, dict],
                           analysis_results: Dict[str, dict],
                           market_regime: Optional[dict] = None) -> List[dict]:
    """Pure Chanlun rotation scoring across 9 dimensions (v3).

    Three-level system: daily (direction) -> 120min (operation) -> 30min (precision).

    Core dimensions (Chanlun theory, 75% total):
      1. 走势类型 (Trend Classification)       - weight 15%
      2. 买卖点信号 (Signal Strength)          - weight 18%
      3. MACD动力学 (防狼术+背驰)              - weight 15%
      4. 中枢结构 (Hub Structure)              - weight 12%
      5. 级别共振 (Three-level Resonance)      - weight 15%

    Theoretical enhancement dimensions (25% total):
      6. 买卖点进度 (BSP Progress, 图解缠论2·§4.3)  - weight 10%
      7. 逆市抗跌 (Anti-drop, 缠论辅导·§5.1)       - weight 5%
      8. 均线强弱 (MA Strength, 土匪注解·§4.5)      - weight 5%
      9. 冠军修正 (Champion Adj, 图解缠论3·§3.4)    - weight 5%

    Market regime (图解缠论3·§3.2) modulates final score interpretation.
    """
    WEIGHTS = {
        "trend": 0.15,
        "signal": 0.18,
        "macd": 0.15,
        "hub": 0.12,
        "resonance": 0.15,
        "bsp_progress": 0.10,
        "anti_drop": 0.05,
        "ma_strength": 0.05,
        "champion_adj": 0.05,
    }
    scores = []

    benchmark_code = "000300"
    benchmark_klines = []
    bm_analysis = analysis_results.get(benchmark_code, {})
    if bm_analysis.get("daily", {}).get("klines"):
        benchmark_klines = bm_analysis["daily"]["klines"]

    rotation_history_path = os.path.join(REPORT_DIR, "rotation_history.jsonl")
    rotation_mult = market_regime.get("rotation_multiplier", 1.0) if market_regime else 1.0

    for idx_info in config["indices"]:
        code = idx_info["index_code"]
        name = idx_info["index_name"]
        data = data_results.get(code, {})
        analysis = analysis_results.get(code, {})
        etf_daily = data.get("etf_daily", [])

        daily = analysis.get("daily", {})
        min120 = analysis.get("min120", {})
        min30 = analysis.get("min30", {})

        if not daily:
            scores.append({
                "code": code, "name": name,
                "final_score": 0, "rank": 0,
                "d1_trend": 0, "d2_signal": 0, "d3_macd": 0,
                "d4_hub": 0, "d5_resonance": 0,
                "d6_bsp_progress": 0, "d7_anti_drop": 0,
                "d8_ma_strength": 0, "d9_champion_adj": 0,
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
        min120_trend = min120.get("trend", "N/A")
        min30_trend = min30.get("trend", "N/A")
        daily_bsp = daily.get("buy_sell_points", [])
        min120_bsp = min120.get("buy_sell_points", [])
        min30_bsp = min30.get("buy_sell_points", [])
        all_bsp = daily_bsp + min120_bsp + min30_bsp

        has_120 = bool(min120.get("klines"))

        if has_120:
            d1 = (_trend_score(daily_trend) * 0.4 +
                  _trend_score(min120_trend) * 0.35 +
                  _trend_score(min30_trend) * 0.25)
        else:
            d1 = _trend_score(daily_trend) * 0.6 + _trend_score(min30_trend) * 0.4

        d2 = _signal_score(all_bsp)

        if has_120:
            d3 = (_macd_dynamics_score(daily.get("klines", [])) * 0.35 +
                  _macd_dynamics_score(min120.get("klines", [])) * 0.35 +
                  _macd_dynamics_score(min30.get("klines", [])) * 0.30)
        else:
            d3 = (_macd_dynamics_score(daily.get("klines", [])) * 0.6 +
                  _macd_dynamics_score(min30.get("klines", [])) * 0.4)

        d4 = _hub_structure_score(daily.get("hubs", []), latest_price)
        if has_120:
            d4_120 = _hub_structure_score(min120.get("hubs", []), latest_price)
            d4 = d4 * 0.6 + d4_120 * 0.4

        d5 = _three_level_resonance_score(daily_trend, min120_trend, min30_trend,
                                           min120_bsp, min30_bsp) if has_120 else \
             _resonance_score(daily_trend, min30_trend, min30_bsp)

        d6 = _bsp_progress_score(daily, min120 if has_120 else min30)
        d7 = _anti_drop_score(daily.get("klines", []), benchmark_klines)
        d8 = _ma_strength_score(daily.get("klines", []))
        d9 = _champion_penalty(code, rotation_history_path)

        raw_score = (d1 * WEIGHTS["trend"] +
                     d2 * WEIGHTS["signal"] +
                     d3 * WEIGHTS["macd"] +
                     d4 * WEIGHTS["hub"] +
                     d5 * WEIGHTS["resonance"] +
                     d6 * WEIGHTS["bsp_progress"] +
                     d7 * WEIGHTS["anti_drop"] +
                     d8 * WEIGHTS["ma_strength"] +
                     d9 * WEIGHTS["champion_adj"])
        final = round(raw_score * idx_info["weight"] * rotation_mult, 2)

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
            "d6_bsp_progress": round(d6, 2),
            "d7_anti_drop": round(d7, 2),
            "d8_ma_strength": round(d8, 2),
            "d9_champion_adj": round(d9, 2),
            "category": idx_info["category"],
            "latest_price": round(latest_price, 4),
            "trend": daily_trend,
            "min120_trend": min120_trend,
            "min30_trend": min30_trend,
            "hub_count": len(hubs),
            "stroke_count": len(daily.get("strokes", [])),
            "buy_signals": [p.type for p in daily_bsp if "B" in p.type],
            "sell_signals": [p.type for p in daily_bsp if "S" in p.type],
            "min120_buy_signals": [p.type for p in min120_bsp if "B" in p.type],
            "min120_sell_signals": [p.type for p in min120_bsp if "S" in p.type],
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
            entry["latest_daily_date"] = lk.date

        if min120.get("klines"):
            lk120 = min120["klines"][-1]
            entry["min120_macd_dif"] = round(lk120.dif, 4)

        if min30.get("klines"):
            entry["latest_30min_date"] = min30["klines"][-1].date

        if hubs:
            entry["last_hub_zg"] = round(hubs[-1].zg, 2)
            entry["last_hub_zd"] = round(hubs[-1].zd, 2)

        scores.append(entry)

    scores.sort(key=lambda x: x["final_score"], reverse=True)
    for i, s in enumerate(scores):
        s["final_rank"] = i + 1

    return scores


# ─── Report Generation ───

def _generate_executive_summary(scores: List[dict], config: dict,
                                market_regime: Optional[dict] = None) -> str:
    """Generate executive summary with key conclusions at the top of the report."""
    lines = []
    regime = market_regime.get("regime", "N/A") if market_regime else "N/A"
    r_emoji = {"牛市": "🐂", "熊市": "🐻", "震荡市": "🔄"}.get(regime, "❓")

    top_pick = scores[0] if scores else None
    positive = [s for s in scores if s["final_score"] > 0]
    negative = [s for s in scores if s["final_score"] < -3]
    a_shares = [s for s in scores if s["code"][0].isdigit()]
    cross_mkt = [s for s in scores if not s["code"][0].isdigit()]

    resonance = [s for s in scores
                 if "上涨" in s.get("trend", "") and "上涨" in s.get("min120_trend", "")
                 and "上涨" in s.get("min30_trend", "")]
    two_level_resonance = [s for s in scores
                           if "上涨" in s.get("trend", "") and "上涨" in s.get("min120_trend", "")
                           and s not in resonance]
    level_conflict = [s for s in scores
                      if "上涨" in s.get("trend", "") and "下跌" in s.get("min120_trend", "")]
    wolf_safe = [s for s in scores if s.get("macd_dif", -1) > 0]
    wolf_warn = [s for s in scores if s.get("macd_dif", -1) <= 0]

    lines.append("## 📋 核心结论")
    lines.append("")
    lines.append(f"**行情性质**：{r_emoji} {regime}")
    if market_regime:
        lines.append(f"（{market_regime.get('strategy_note', '')}）")
    lines.append("")

    lines.append("### 操作建议")
    lines.append("")
    lines.append("| 指数 | 综合分 | 走势 | 建议 |")
    lines.append("|------|--------|------|------|")

    for s in scores:
        trend_short = s.get("trend", "N/A")[:4]
        d_up = "上涨" in s.get("trend", "")
        h_up = "上涨" in s.get("min120_trend", "")
        h_down = "下跌" in s.get("min120_trend", "")
        m_up = "上涨" in s.get("min30_trend", "")
        dif_above = s.get("macd_dif", -1) > 0

        idx_info = next((i for i in config["indices"] if i["index_code"] == s["code"]), {})
        etf = idx_info.get("etf_code", "")

        if d_up and h_up and m_up and dif_above:
            advice = f"✅ 三级共振，可加仓 {etf}"
        elif d_up and h_up and m_up:
            advice = f"✅ 三级共振，关注MACD {etf}"
        elif d_up and h_up:
            advice = f"✅ 日线+120分共振 {etf}"
        elif d_up and h_down and s["final_score"] > 0:
            advice = f"🟡 等120分钟下跌结束 {etf}"
        elif d_up and h_down:
            advice = f"⏳ 观望，日线vs120分矛盾"
        elif "下跌" in s.get("trend", ""):
            advice = "❌ 空仓回避"
        else:
            advice = "⏳ 观望"

        lines.append(f"| {s['name']} | **{s['final_score']:+.1f}** | {trend_short} | {advice} |")

    lines.append("")

    lines.append("### 关键判断")
    lines.append("")
    if resonance:
        names = "、".join(s["name"] for s in resonance)
        lines.append(f"1. **三级共振**：{names}（日线+120分钟+30分钟同向上涨）")
    if two_level_resonance:
        names = "、".join(s["name"] for s in two_level_resonance)
        lines.append(f"1.5. **双级共振**：{names}（日线+120分钟上涨，30分钟回调中）")
    if level_conflict:
        names = "、".join(s["name"] for s in level_conflict)
        lines.append(f"2. **级别矛盾**：{names}（日线上涨但120分钟下跌）")
    if wolf_warn:
        names = "、".join(s["name"] for s in wolf_warn)
        lines.append(f"3. **防狼术警告**：{names}（MACD DIF在0轴下方）")
    if negative:
        names = "、".join(s["name"] for s in negative)
        lines.append(f"4. **回避标的**：{names}（综合分低于-3）")

    if a_shares and cross_mkt:
        a_avg = sum(s["final_score"] for s in a_shares) / len(a_shares)
        c_avg = sum(s["final_score"] for s in cross_mkt) / len(cross_mkt)
        if a_avg > c_avg + 2:
            lines.append(f"5. **A股 vs 跨市场**：A股整体优于跨市场（A股均分{a_avg:+.1f} vs 跨市场{c_avg:+.1f}）")

    large = [s for s in a_shares if s.get("category", "").startswith("大盘")]
    small = [s for s in a_shares if "成长" in s.get("category", "") or "创新" in s.get("category", "")]
    if large and small:
        large_avg = sum(s["final_score"] for s in large) / len(large)
        small_avg = sum(s["final_score"] for s in small) / len(small)
        if small_avg > large_avg + 1:
            lines.append(f"6. **大小盘轮动**：中小盘强于大盘（中小盘均分{small_avg:+.1f} vs 大盘{large_avg:+.1f}）")
        elif large_avg > small_avg + 1:
            lines.append(f"6. **大小盘轮动**：大盘强于中小盘（大盘均分{large_avg:+.1f} vs 中小盘{small_avg:+.1f}）")

    lines.append("")
    return "\n".join(lines)


def generate_rotation_report(scores: List[dict], config: dict,
                             market_regime: Optional[dict] = None) -> str:
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    regime_text = ""
    if market_regime:
        r = market_regime
        emoji = {"牛市": "🐂", "熊市": "🐻", "震荡市": "🔄"}.get(r["regime"], "❓")
        regime_text = f"\n> **行情性质判断**：{emoji} **{r['regime']}**（置信度：{r['confidence']}）\n> {r['strategy_note']}\n"

    executive_summary = _generate_executive_summary(scores, config, market_regime)

    daily_dates = [s.get("latest_daily_date", "") for s in scores if s.get("latest_daily_date")]
    min30_dates = [s.get("latest_30min_date", "") for s in scores if s.get("latest_30min_date")]
    latest_daily = max(daily_dates) if daily_dates else "N/A"
    latest_30min = max(min30_dates) if min30_dates else "N/A"

    lines = [
        "# A股宽基指数轮动分析报告",
        "",
        f"> 生成时间：{now}",
        f"> 数据截止：日线 {latest_daily} ｜ 30分钟线 {latest_30min}",
        f"> 评分体系：纯缠论九维度 v3（三级联立：日线→120分钟→30分钟）",
        f"> 覆盖指数：{len(scores)} 只指数及对应ETF",
        f"> 理论基础：缠论（缠中说禅技术分析理论），详见 [`docs/指数轮动交易系统逻辑.md`](../../docs/指数轮动交易系统逻辑.md)",
        regime_text,
        "---",
        "",
        executive_summary,
        "---",
        "",
        "## 一、轮动排名总表",
        "",
        "### 1.1 综合排名",
        "",
        "| 排名 | 指数 | 类别 | 最新价 | 综合分 | 日线走势 |",
        "|------|------|------|--------|--------|----------|",
    ]

    for s in scores:
        trend = s.get("trend", "N/A")
        emoji = "🟢" if s["final_score"] > 0 else "🔴" if s["final_score"] < -3 else "🟡"
        lines.append(
            f"| {s['final_rank']} | {emoji} {s['name']} | {s['category']} "
            f"| {s['latest_price']:.3f} | **{s['final_score']:.1f}** | {trend} |"
        )

    lines.extend([
        "",
        "### 1.2 九维度评分明细",
        "",
        "| 指数 | 走势 | 信号 | MACD | 中枢 | 共振 | 买点进度 | 抗跌 | 均线 | 冠军修正 |",
        "|------|------|------|------|------|------|---------|------|------|---------|",
    ])

    for s in scores:
        lines.append(
            f"| {s['name']} | {s['d1_trend']:+.1f} "
            f"| {s['d2_signal']:+.1f} | {s['d3_macd']:+.1f} | {s['d4_hub']:+.1f} "
            f"| {s['d5_resonance']:+.1f} | {s.get('d6_bsp_progress', 0):+.1f} "
            f"| {s.get('d7_anti_drop', 0):+.1f} | {s.get('d8_ma_strength', 0):+.1f} "
            f"| {s.get('d9_champion_adj', 0):+.1f} |"
        )

    lines.extend([
        "",
        "> **评分维度说明（v3 九维度）**：",
        "> - **核心5维（75%）**：走势类型(15%) · 买卖点信号(18%) · MACD动力学(15%) · 中枢结构(12%) · 级别共振(15%)",
        "> - **理论增强4维（25%）**：买卖点进度(10%,图解缠论2) · 逆市抗跌(5%,缠论辅导) · 均线强弱(5%,土匪注解) · 冠军修正(5%,图解缠论3)",
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
        min120_trend = s.get("min120_trend", "N/A")
        min30_trend = s.get("min30_trend", "N/A")

        lines.extend([
            f"### {emoji} {s['name']}（{s['code']}）",
            "",
            f"- **日线走势**（定方向）：{s.get('trend', 'N/A')}（中枢{s.get('hub_count', 0)}个，笔{s.get('stroke_count', 0)}根）",
            f"- **120分钟走势**（定买卖点）：{min120_trend}",
            f"- **30分钟走势**（精确择时）：{min30_trend}",
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
        h_buys = s.get("min120_buy_signals", [])
        h_sells = s.get("min120_sell_signals", [])
        m_buys = s.get("min30_buy_signals", [])
        m_sells = s.get("min30_sell_signals", [])

        if buys or h_buys or m_buys:
            parts = []
            if buys:
                parts.append(f"日线 {', '.join(buys)}")
            if h_buys:
                parts.append(f"**120分钟 {', '.join(h_buys)}**")
            if m_buys:
                parts.append(f"30分钟 {', '.join(m_buys)}")
            lines.append(f"- **买点信号**：{' | '.join(parts)}")
        if sells or h_sells or m_sells:
            parts = []
            if sells:
                parts.append(f"日线 {', '.join(sells)}")
            if h_sells:
                parts.append(f"**120分钟 {', '.join(h_sells)}**")
            if m_sells:
                parts.append(f"30分钟 {', '.join(m_sells)}")
            lines.append(f"- **卖点信号**：{' | '.join(parts)}")
        if not buys and not sells and not h_buys and not h_sells and not m_buys and not m_sells:
            lines.append("- **买卖点**：无新信号")

        d_up = "上涨" in s.get("trend", "")
        h_up = "上涨" in min120_trend
        h_down = "下跌" in min120_trend
        m_up = "上涨" in min30_trend
        if d_up and h_up and m_up:
            lines.append("- **级别共振**：✅ 三级共振（日线+120分钟+30分钟同向上涨）")
        elif d_up and h_up:
            lines.append("- **级别共振**：✅ 日线+120分钟共振上涨")
        elif d_up and h_down:
            lines.append("- **级别共振**：⚠️ 日线上涨 vs 120分钟下跌（等120分钟回调结束）")

        lines.append("")

    lines.extend(["---", ""])

    lines.extend([
        "## 三、轮动操作建议",
        "",
    ])

    top_indices = [s for s in scores if s["final_score"] > 0]
    bottom_indices = [s for s in scores if s["final_score"] < -3]

    lines.append("### 3.1 各指数操作判断（日线定方向，120分钟定买卖点，30分钟精确择时）")
    lines.append("")
    lines.append("| 指数 | ETF | 日线信号 | 120分钟信号 | 30分钟信号 | MACD | 操作判断 | 仓位 |")
    lines.append("|------|-----|---------|-----------|-----------|------|---------|------|")

    for s in scores:
        idx_info = next((i for i in config["indices"] if i["index_code"] == s["code"]), {})
        etf = idx_info.get("etf_code", "")
        d_buys = s.get("buy_signals", [])
        d_sells = s.get("sell_signals", [])
        h_buys = s.get("min120_buy_signals", [])
        h_sells = s.get("min120_sell_signals", [])
        m_buys = s.get("min30_buy_signals", [])
        m_sells = s.get("min30_sell_signals", [])
        d_up = "上涨" in s.get("trend", "")
        h_up = "上涨" in s.get("min120_trend", "")
        h_down = "下跌" in s.get("min120_trend", "")
        m_up = "上涨" in s.get("min30_trend", "")
        m_down = "下跌" in s.get("min30_trend", "")
        dif_above = s.get("macd_dif", -1) > 0
        has_d_sell = bool(d_sells)
        has_h_sell = bool(h_sells)

        d_sig = ", ".join(d_buys + d_sells) if (d_buys or d_sells) else "无"
        h_sig = ", ".join(h_buys + h_sells) if (h_buys or h_sells) else "无"
        m_sig = ", ".join(m_buys + m_sells) if (m_buys or m_sells) else "无"
        macd_str = "✅0轴上" if dif_above else "⚠️0轴下"

        if not d_up and "上涨" not in s.get("trend", ""):
            advice = "❌ 空仓回避"
            pos = "0%"
        elif d_up and h_up and m_up and h_buys and m_buys and dif_above:
            advice = "✅ 三级共振+买点+MACD确认，加仓"
            pos = "40-60%"
        elif d_up and h_up and h_buys and m_buys:
            advice = "✅ 120分买点+30分确认，买入"
            pos = "30-40%"
        elif d_up and h_buys and m_down:
            advice = "⏳ 120分钟买点，等30分钟企稳再入"
            pos = "0%→20%"
        elif d_up and h_buys and not dif_above:
            advice = "⏳ 120分钟买点，等DIF回0轴"
            pos = "0%→10%"
        elif d_up and (has_d_sell or has_h_sell) and h_down:
            advice = "⚠️ 卖点+120分下跌，减仓/清仓"
            pos = "→0%"
        elif d_up and has_h_sell:
            advice = "⚠️ 120分钟卖点，减仓观望"
            pos = "10-20%"
        elif d_up and h_up and m_up:
            advice = "🟡 三级共振，持仓等买卖点"
            pos = "维持"
        elif d_up and h_up:
            advice = "🟡 日线+120分共振，30分回调中"
            pos = "维持"
        elif d_up and h_down:
            advice = "⏳ 等120分钟下跌结束"
            pos = "维持/减"
        else:
            advice = "⏳ 观望"
            pos = "0%"

        lines.append(
            f"| {s['name']} | {etf} | {d_sig} | {h_sig} | {m_sig} | {macd_str} | {advice} | {pos} |"
        )

    lines.append("")

    if bottom_indices:
        lines.append("### 3.2 回避标的（综合分 < -3）")
        lines.append("")
        for s in bottom_indices:
            lines.append(f"- **{s['name']}**：{s.get('trend', 'N/A')}，综合分 {s['final_score']:.1f}")
        lines.append("")

    lines.extend([
        "### 3.3 三级联立操作规则（日线→120分钟→30分钟）",
        "",
        "> **方向级别 = 日线** | **操作级别 = 120分钟** | **择时级别 = 30分钟** | 频率 = 半天一次",
        "",
        "**级别倍率**：30分钟 ×4→ 120分钟 ×2→ 日线（符合缠论标准递推关系）",
        "",
        "**买入（三步确认）**：",
        "",
        "| 信号组合 | 操作 | 仓位 | 置信度 |",
        "|---------|------|------|--------|",
        "| 日线向上 + 120分钟买点 + 30分钟买点 | 买入 | 40-60% | 高 |",
        "| 日线向上 + 120分钟买点 + 30分钟中性 | 轻仓买入 | 20-30% | 中 |",
        "| 日线向上 + 120分钟买点 + 30分钟下跌 | 等30分钟企稳 | 0%→20% | 低 |",
        "| 日线向上 + 120分钟无信号 | **不操作** | 维持 | - |",
        "| 仅30分钟买点，120分钟无信号 | **不买** | 0% | - |",
        "",
        "**卖出（信号即执行）**：",
        "",
        "| 信号 | 操作 | 理由 |",
        "|------|------|------|",
        "| 120分钟一卖（1S） | 至少减仓50% | 操作级别背驰 |",
        "| 120分钟三卖（3S） | 必须清仓 | 操作级别跌破中枢 |",
        "| 日线一卖（1S） | 清仓 | 方向级别背驰，宁卖早勿卖晚 |",
        "| 日线三卖（3S） | 必须清仓 | 方向级别趋势破坏 |",
        "| 30分钟连续卖点 | 关注120分钟是否跟进 | 小级别走弱预警 |",
        "",
        "**防狼术叠加**：日线 MACD DIF 在0轴下方时，即使120分钟有买点也需降低仓位（标准仓位减半）或等DIF回0轴。",
        "",
        "**止损规则**：",
        "- 跌破120分钟最近中枢下沿 ZD → 减半仓",
        "- 120分钟三卖信号 → 必须清仓",
        "- 跌破日线中枢下沿 → 清仓",
        "",
        "**口诀**：日线定方向，120分钟定买卖，30分钟定价位。120分钟说卖立刻走，日线说卖全清仓。",
        "",
        "**调仓频率**：每半天（午盘/收盘后）检查120分钟和30分钟信号，每周末做一次日线完整分析。",
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

    daily_date = score.get("latest_daily_date", "N/A")
    min30_date = score.get("latest_30min_date", "N/A")

    lines = [
        f"# {name}（{code}）每日跟踪报告",
        "",
        f"> 生成时间：{now}",
        f"> 数据截止：日线 {daily_date} ｜ 30分钟线 {min30_date}",
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
        min120_trend = analysis.get("min120", {}).get("trend", "N/A")
        min30_trend = analysis.get("min30", {}).get("trend", "N/A")
        if "上涨" in daily_trend and "上涨" in min120_trend and "上涨" in min30_trend:
            level_resonance.append(f"- **{s['name']}** ✅ 三级共振（日线+120分钟+30分钟同向上涨）")
        elif "上涨" in daily_trend and "上涨" in min120_trend:
            level_resonance.append(f"- **{s['name']}** ✅ 日线+120分钟共振上涨")
        elif "上涨" in daily_trend and "下跌" in min120_trend:
            level_resonance.append(f"- **{s['name']}** ⚠️ 日线上涨 vs 120分钟下跌")

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
    """Run complete index trading system update (v3: 9 dimensions + market regime)."""
    print("=" * 70)
    print("  A股宽基指数缠论交易系统 v3 — 完整更新")
    print("  (三级联立：日线→120分钟→30分钟 | 九维度评分)")
    print("=" * 70)

    config = _load_config()
    os.makedirs(REPORT_DIR, exist_ok=True)

    print("\n[1/6] Fetching data for all indices and ETFs...")
    data_results = fetch_all_index_data(config)

    print("\n[2/6] Running Chanlun analysis on all indices...")
    analysis_results = analyze_all_indices(config)

    print("\n[3/6] Determining market regime (图解缠论3·§3.2)...")
    benchmark_analysis = analysis_results.get("000300", {})
    market_regime = determine_market_regime(benchmark_analysis)
    emoji = {"牛市": "🐂", "熊市": "🐻", "震荡市": "🔄"}.get(market_regime["regime"], "❓")
    print(f"  {emoji} Market regime: {market_regime['regime']} "
          f"(confidence={market_regime['confidence']}, "
          f"rotation_mult={market_regime['rotation_multiplier']:.1f})")
    print(f"  Strategy: {market_regime['strategy_note']}")

    print("\n[4/6] Recording buy/sell point signals...")
    record_index_signals(config, analysis_results)

    print("\n[5/6] Computing Chanlun v3 rotation scores (9 dimensions)...")
    scores = compute_chanlun_scores(config, data_results, analysis_results,
                                    market_regime=market_regime)

    rotation_path = os.path.join(REPORT_DIR, "rotation_scores.json")
    _save_json(rotation_path, {
        "timestamp": datetime.now().isoformat(),
        "version": "v3",
        "market_regime": market_regime,
        "scores": scores,
    })

    print("\n  Rotation ranking (Chanlun v3, 9 dimensions):")
    for s in scores:
        emoji = "🟢" if s["final_score"] > 0 else "🔴" if s["final_score"] < -5 else "🟡"
        print(f"    {emoji} #{s['final_rank']} {s['name']}: score={s['final_score']:.1f}, "
              f"trend={s.get('trend', 'N/A')}")
        print(f"       [走势={s['d1_trend']:.1f} 信号={s['d2_signal']:.1f} "
              f"MACD={s['d3_macd']:.1f} 中枢={s['d4_hub']:.1f} 共振={s['d5_resonance']:.1f}]")
        print(f"       [买点进度={s.get('d6_bsp_progress', 0):.1f} "
              f"抗跌={s.get('d7_anti_drop', 0):.1f} "
              f"均线={s.get('d8_ma_strength', 0):.1f} "
              f"冠军修正={s.get('d9_champion_adj', 0):.1f}]")

    print("\n[6/6] Generating reports...")

    today_str = datetime.now().strftime('%Y%m%d')
    daily_dir = os.path.join(REPORT_DIR, today_str)
    os.makedirs(daily_dir, exist_ok=True)

    rotation_report = generate_rotation_report(scores, config,
                                                market_regime=market_regime)
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

    print("\n[6.5/6] Building standalone HTML reports...")
    try:
        from build_standalone_html import build_standalone_html
        build_standalone_html(today_str)
    except Exception as e:
        print(f"  [WARN] HTML build failed: {e}")

    print(f"\n{'='*70}")
    print("  Output files:")
    print(f"  - Daily report dir: {daily_dir}/")
    print(f"  - Rotation report:  {daily_report_path}")
    print(f"  - Latest link:      {latest_link}")
    print(f"  - Rotation scores:  {rotation_path}")
    print(f"  - Tracking log:     {os.path.join(REPORT_DIR, 'tracking.md')}")
    print(f"  - HTML chart:       {daily_dir}/指数轮动分析图.html")
    print(f"  - Mobile HTML:      {daily_dir}/指数轮动分析报告-移动版.html")
    for idx_info in config["indices"]:
        code = idx_info["index_code"]
        name = idx_info["index_name"]
        idx_dir = os.path.join(BASE_DIR, f"{code}_{name}")
        print(f"  - {name}: {idx_dir}/analysis/")
    print(f"{'='*70}")
    print(f"\n  Market regime: {market_regime['regime']} | "
          f"Scoring: v3 (9 dimensions, 3-level: daily→120min→30min)")
    print("\nDone!")


if __name__ == "__main__":
    full_update()
