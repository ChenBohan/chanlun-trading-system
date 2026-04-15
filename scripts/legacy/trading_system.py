"""
Trading System Manager - Central hub for portfolio tracking, signal recording,
fundamental data, capital flow, and analysis orchestration.
"""

import json
import os
import sys
from datetime import datetime
from typing import Optional, List, Dict
from urllib.request import urlopen, Request
from urllib.parse import urlencode

BASE_DIR = "/home/chenbohan/Documents/gp"
STOCKS_DIR = os.path.join(BASE_DIR, "stocks")
PORTFOLIO_DIR = os.path.join(BASE_DIR, "portfolio")
CONFIG_DIR = os.path.join(BASE_DIR, "config")


def _ensure_dirs(symbol: str, name: str):
    stock_dir = os.path.join(STOCKS_DIR, f"{symbol}_{name}")
    for sub in ["data", "fundamental", "fundamental/reports", "analysis", "tracking"]:
        os.makedirs(os.path.join(stock_dir, sub), exist_ok=True)
    os.makedirs(PORTFOLIO_DIR, exist_ok=True)
    return stock_dir


def _load_json(path: str, default=None):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default if default is not None else {}


def _save_json(path: str, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _append_jsonl(path: str, record: dict):
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


# ─── Portfolio & Position Tracking ───

class PortfolioManager:

    def __init__(self):
        self.holdings_path = os.path.join(PORTFOLIO_DIR, "holdings.json")
        self.pnl_path = os.path.join(PORTFOLIO_DIR, "pnl_history.jsonl")
        self.holdings = _load_json(self.holdings_path, {"positions": {}, "cash": 0, "total_invested": 0})

    def save(self):
        _save_json(self.holdings_path, self.holdings)

    def add_position(self, symbol: str, name: str, date: str, price: float,
                     shares: int, reason: str, signal_type: str = ""):
        stock_dir = _ensure_dirs(symbol, name)
        pos_path = os.path.join(stock_dir, "tracking", "position.json")
        position = _load_json(pos_path, {
            "symbol": symbol, "name": name,
            "entries": [], "current_shares": 0, "avg_cost": 0.0,
            "target_prices": {}, "stop_loss": {},
        })

        position["entries"].append({
            "date": date, "action": "buy", "price": price,
            "shares": shares, "reason": reason, "signal_type": signal_type,
        })

        old_shares = position["current_shares"]
        old_cost = position["avg_cost"]
        new_shares = old_shares + shares
        if new_shares > 0:
            position["avg_cost"] = (old_cost * old_shares + price * shares) / new_shares
        position["current_shares"] = new_shares

        _save_json(pos_path, position)

        self.holdings["positions"][symbol] = {
            "name": name, "shares": new_shares,
            "avg_cost": position["avg_cost"],
            "last_update": date,
        }
        self.save()

        self._log_journal(stock_dir, date, "buy", price, shares, reason, signal_type)
        print(f"  [Portfolio] BUY {shares} shares of {name} @ {price:.2f}")
        print(f"    -> Total: {new_shares} shares, avg cost: {position['avg_cost']:.2f}")
        return position

    def reduce_position(self, symbol: str, name: str, date: str, price: float,
                        shares: int, reason: str, signal_type: str = ""):
        stock_dir = _ensure_dirs(symbol, name)
        pos_path = os.path.join(stock_dir, "tracking", "position.json")
        position = _load_json(pos_path)
        if not position:
            print(f"  [Portfolio] No position for {symbol}")
            return None

        position["entries"].append({
            "date": date, "action": "sell", "price": price,
            "shares": shares, "reason": reason, "signal_type": signal_type,
        })

        sold_shares = min(shares, position["current_shares"])
        pnl = (price - position["avg_cost"]) * sold_shares
        position["current_shares"] -= sold_shares

        _save_json(pos_path, position)

        if position["current_shares"] > 0:
            self.holdings["positions"][symbol]["shares"] = position["current_shares"]
        else:
            self.holdings["positions"].pop(symbol, None)
        self.holdings["positions"].get(symbol, {}).update({"last_update": date})
        self.save()

        _append_jsonl(self.pnl_path, {
            "date": date, "symbol": symbol, "action": "sell",
            "price": price, "shares": sold_shares,
            "pnl": round(pnl, 2), "pnl_pct": round(pnl / (position["avg_cost"] * sold_shares) * 100, 2),
        })

        self._log_journal(stock_dir, date, "sell", price, sold_shares, reason, signal_type)
        print(f"  [Portfolio] SELL {sold_shares} shares of {name} @ {price:.2f}")
        print(f"    -> PnL: {pnl:+.2f} ({pnl / (position['avg_cost'] * sold_shares) * 100:+.2f}%)")
        print(f"    -> Remaining: {position['current_shares']} shares")
        return position

    def set_targets(self, symbol: str, name: str, targets: dict, stop_loss: dict):
        stock_dir = _ensure_dirs(symbol, name)
        pos_path = os.path.join(stock_dir, "tracking", "position.json")
        position = _load_json(pos_path)
        if position:
            position["target_prices"] = targets
            position["stop_loss"] = stop_loss
            _save_json(pos_path, position)

    def get_position(self, symbol: str, name: str) -> dict:
        stock_dir = os.path.join(STOCKS_DIR, f"{symbol}_{name}")
        pos_path = os.path.join(stock_dir, "tracking", "position.json")
        return _load_json(pos_path, {})

    def get_portfolio_summary(self, current_prices: dict = None) -> str:
        lines = ["## 持仓汇总", ""]
        positions = self.holdings.get("positions", {})
        if not positions:
            lines.append("当前无持仓。")
            return "\n".join(lines)

        lines.append("| 股票 | 持仓 | 成本 | 现价 | 浮盈/亏 | 浮盈% |")
        lines.append("|------|------|------|------|---------|-------|")

        total_cost = 0
        total_value = 0
        for sym, pos in positions.items():
            shares = pos["shares"]
            cost = pos["avg_cost"]
            current = current_prices.get(sym, cost) if current_prices else cost
            pnl = (current - cost) * shares
            pnl_pct = (current - cost) / cost * 100 if cost > 0 else 0
            total_cost += cost * shares
            total_value += current * shares
            lines.append(
                f"| {pos['name']}({sym}) | {shares}股 | {cost:.2f} | {current:.2f} | {pnl:+.2f} | {pnl_pct:+.2f}% |"
            )

        if total_cost > 0:
            total_pnl = total_value - total_cost
            total_pct = total_pnl / total_cost * 100
            lines.append(f"| **合计** | | | | **{total_pnl:+.2f}** | **{total_pct:+.2f}%** |")

        return "\n".join(lines)

    def _log_journal(self, stock_dir: str, date: str, action: str, price: float,
                     shares: int, reason: str, signal_type: str):
        journal_path = os.path.join(stock_dir, "tracking", "journal.md")
        now = datetime.now().strftime("%H:%M")
        entry = f"\n## {date} {now} {'买入' if action == 'buy' else '卖出'}\n\n"
        entry += f"- **操作**：{'买入' if action == 'buy' else '卖出'} {shares} 股 @ {price:.2f}\n"
        if signal_type:
            entry += f"- **信号**：{signal_type}\n"
        entry += f"- **依据**：{reason}\n"
        entry += "\n"

        with open(journal_path, 'a', encoding='utf-8') as f:
            if os.path.getsize(journal_path) == 0 if os.path.exists(journal_path) else True:
                f.write("# 操作日志\n\n")
            f.write(entry)


# ─── Signal Tracking ───

class SignalTracker:

    def __init__(self, symbol: str, name: str):
        self.stock_dir = _ensure_dirs(symbol, name)
        self.signals_path = os.path.join(self.stock_dir, "tracking", "signals.jsonl")

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
        print(f"  [Signal] Recorded: {signal_type} @ {date} price={price:.2f} [{confidence}]")

    def get_signals(self) -> list:
        return _read_jsonl(self.signals_path)

    def get_active_signals(self) -> list:
        return [s for s in self.get_signals() if s.get("status") == "active"]

    def update_signal_status(self, date: str, signal_type: str, status: str,
                             outcome: str = None, exit_price: float = None):
        signals = self.get_signals()
        updated = []
        for s in signals:
            if s["date"] == date and s["signal"] == signal_type:
                s["status"] = status
                if outcome:
                    s["outcome"] = outcome
                if exit_price:
                    s["exit_price"] = exit_price
            updated.append(s)

        with open(self.signals_path, 'w', encoding='utf-8') as f:
            for s in updated:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")

    def get_signal_stats(self) -> dict:
        signals = self.get_signals()
        total = len(signals)
        completed = [s for s in signals if s.get("outcome")]
        wins = [s for s in completed if s.get("outcome") == "profit"]
        losses = [s for s in completed if s.get("outcome") == "loss"]
        return {
            "total": total,
            "active": len([s for s in signals if s.get("status") == "active"]),
            "completed": len(completed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(completed) * 100 if completed else 0,
        }


# ─── Fundamental Data Fetcher ───

def fetch_fundamental(symbol: str) -> dict:
    """Fetch basic fundamental data from East Money API."""
    secid = f"1.{symbol}" if symbol.startswith("6") else f"0.{symbol}"
    fields = "f57,f58,f43,f116,f117,f173,f183,f184,f185,f186,f187,f188,f135,f136,f137"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={fields}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
        d = raw.get("data", {})
        if not d:
            return {}

        total_cap = d.get("f116", 0) or 0
        revenue = d.get("f183", 0) or 0
        net_margin = d.get("f187", 0) or 0
        net_profit = revenue * net_margin / 100 if net_margin else 0
        pe_ttm = total_cap / net_profit if net_profit > 0 else 0

        return {
            "symbol": d.get("f57", symbol),
            "name": d.get("f58", ""),
            "latest_price": (d.get("f43", 0) or 0) / 100,
            "total_market_cap": total_cap,
            "float_market_cap": d.get("f117", 0) or 0,
            "pb": d.get("f173", 0) or 0,
            "pe_ttm": round(pe_ttm, 2),
            "revenue": revenue,
            "revenue_yoy": d.get("f184", 0) or 0,
            "gross_margin": d.get("f185", 0) or 0,
            "operating_margin": d.get("f186", 0) or 0,
            "net_margin": net_margin,
            "roe": d.get("f188", 0) or 0,
            "main_inflow": d.get("f135", 0) or 0,
            "main_outflow": d.get("f136", 0) or 0,
            "main_net": d.get("f137", 0) or 0,
            "fetch_time": datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"  [Fundamental] Error: {e}")
        return {}


def fetch_capital_flow(symbol: str) -> dict:
    """Fetch recent capital flow data from East Money."""
    secid = f"1.{symbol}" if symbol.startswith("6") else f"0.{symbol}"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f135,f136,f137,f138,f139,f140,f141,f142,f143,f144,f145,f146"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
        d = raw.get("data", {})
        if not d:
            return {}
        return {
            "main_inflow": d.get("f135", 0),
            "main_outflow": d.get("f136", 0),
            "main_net": d.get("f137", 0),
            "super_large_inflow": d.get("f138", 0),
            "super_large_outflow": d.get("f139", 0),
            "large_inflow": d.get("f140", 0),
            "large_outflow": d.get("f141", 0),
            "medium_inflow": d.get("f142", 0),
            "medium_outflow": d.get("f143", 0),
            "small_inflow": d.get("f144", 0),
            "small_outflow": d.get("f145", 0),
            "fetch_time": datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"  [CapitalFlow] Error: {e}")
        return {}


def fetch_north_flow() -> dict:
    """Fetch northbound capital flow (沪深港通)."""
    url = "https://push2.eastmoney.com/api/qt/kamtbs.wss?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
        s2n = raw.get("data", {}).get("s2n", [])
        if not s2n:
            return {}
        latest = s2n[-1].split(",") if s2n else []
        if len(latest) >= 4:
            return {
                "date": latest[0],
                "hgt_net": float(latest[1]) if latest[1] != "-" else 0,
                "sgt_net": float(latest[2]) if latest[2] != "-" else 0,
                "total_net": float(latest[3]) if latest[3] != "-" else 0,
                "fetch_time": datetime.now().isoformat(),
            }
        return {}
    except Exception as e:
        print(f"  [NorthFlow] Error: {e}")
        return {}


# ─── Stock Profile ───

def create_stock_profile(symbol: str, name: str, industry: str = "", notes: str = ""):
    stock_dir = _ensure_dirs(symbol, name)
    profile_path = os.path.join(stock_dir, "profile.json")
    fundamental = fetch_fundamental(symbol)

    profile = {
        "symbol": symbol,
        "name": name,
        "market": "SH" if symbol.startswith("6") else "SZ",
        "industry": industry,
        "notes": notes,
        "fundamental": fundamental,
        "created": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
    }
    _save_json(profile_path, profile)
    print(f"  [Profile] Created for {name}({symbol})")
    if fundamental:
        cap_yi = fundamental.get('total_market_cap', 0) / 1e8
        rev_yi = fundamental.get('revenue', 0) / 1e8
        print(f"    PE(TTM): {fundamental.get('pe_ttm', 'N/A'):.1f}x")
        print(f"    PB: {fundamental.get('pb', 'N/A')}x")
        print(f"    Total Cap: {cap_yi:.0f} 亿")
        print(f"    Revenue: {rev_yi:.0f} 亿 (YoY: {fundamental.get('revenue_yoy', 0):.1f}%)")
        print(f"    Gross Margin: {fundamental.get('gross_margin', 0):.1f}%")
        print(f"    Net Margin: {fundamental.get('net_margin', 0):.1f}%")
        print(f"    ROE: {fundamental.get('roe', 0):.1f}%")
    return profile


# ─── Analysis History ───

def record_analysis_snapshot(symbol: str, name: str, analysis_result: dict):
    stock_dir = _ensure_dirs(symbol, name)
    history_path = os.path.join(stock_dir, "analysis", "history.jsonl")

    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "trend": analysis_result.get("trend", ""),
        "hub_count": len(analysis_result.get("hubs", [])),
        "stroke_count": len(analysis_result.get("strokes", [])),
        "buy_sell_points": [
            {"type": p.type, "date": p.date, "price": p.price, "confidence": p.confidence}
            for p in analysis_result.get("buy_sell_points", [])
        ],
        "latest_price": analysis_result["klines"][-1].close if analysis_result.get("klines") else 0,
        "macd_dif": analysis_result["klines"][-1].dif if analysis_result.get("klines") else 0,
        "macd_dea": analysis_result["klines"][-1].dea if analysis_result.get("klines") else 0,
    }
    _append_jsonl(history_path, snapshot)


# ─── Comprehensive Daily Report ───

def generate_daily_report(symbol: str, name: str) -> str:
    stock_dir = os.path.join(STOCKS_DIR, f"{symbol}_{name}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    profile = _load_json(os.path.join(stock_dir, "profile.json"), {})
    position = _load_json(os.path.join(stock_dir, "tracking", "position.json"), {})
    signals = _read_jsonl(os.path.join(stock_dir, "tracking", "signals.jsonl"))
    analysis_history = _read_jsonl(os.path.join(stock_dir, "analysis", "history.jsonl"))

    lines = [
        f"# {name}（{symbol}）每日跟踪报告",
        "",
        f"> 生成时间：{now}",
        "",
        "---",
        "",
    ]

    if profile.get("fundamental"):
        fund = profile["fundamental"]
        cap_yi = fund.get('total_market_cap', 0) / 1e8 if fund.get('total_market_cap') else 0
        rev_yi = fund.get('revenue', 0) / 1e8 if fund.get('revenue') else 0
        lines.extend([
            "## 基本面快照",
            "",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| PE(TTM) | {fund.get('pe_ttm', 0):.1f}x |",
            f"| PB | {fund.get('pb', 0)}x |",
            f"| 总市值 | {cap_yi:.0f} 亿 |",
            f"| 营收(年) | {rev_yi:.0f} 亿 |",
            f"| 营收增速 | {fund.get('revenue_yoy', 0):.1f}% |",
            f"| 毛利率 | {fund.get('gross_margin', 0):.1f}% |",
            f"| 净利率 | {fund.get('net_margin', 0):.1f}% |",
            f"| ROE | {fund.get('roe', 0):.1f}% |",
            f"| 主力净流入 | {fund.get('main_net', 0)/1e4:.0f} 万 |",
            "",
        ])

    if position and position.get("current_shares", 0) > 0:
        lines.extend([
            "## 持仓状态",
            "",
            f"- **持仓数量**：{position['current_shares']} 股",
            f"- **平均成本**：{position['avg_cost']:.2f}",
        ])
        if position.get("target_prices"):
            lines.append(f"- **目标价位**：")
            for k, v in position["target_prices"].items():
                lines.append(f"  - {k}：{v}")
        if position.get("stop_loss"):
            sl = position["stop_loss"]
            lines.append(f"- **止损位**：{sl.get('price', 'N/A')}（{sl.get('reason', '')}）")
        lines.append("")
    else:
        lines.extend(["## 持仓状态", "", "当前无持仓。", ""])

    active_signals = [s for s in signals if s.get("status") == "active"]
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
            "## 信号历史",
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

def full_update(symbol: str = "688981", name: str = "中芯国际"):
    """Run full update: fetch data, analyze, record signals, generate reports."""
    print("=" * 60)
    print(f"  {name}（{symbol}）完整更新")
    print("=" * 60)

    stock_dir = _ensure_dirs(symbol, name)

    print("\n[1/5] Fetching fundamental data...")
    profile = create_stock_profile(symbol, name, industry="半导体")

    print("\n[2/5] Fetching capital flow...")
    cap_flow = fetch_capital_flow(symbol)
    if cap_flow:
        main_net_yi = cap_flow.get('main_net', 0) / 10000
        print(f"    Main net flow: {main_net_yi:.2f} 万")
        fund_path = os.path.join(stock_dir, "fundamental", "capital_flow.jsonl")
        _append_jsonl(fund_path, cap_flow)

    north = fetch_north_flow()
    if north:
        print(f"    North net flow: {north.get('total_net', 0) / 100000000:.2f} 亿")

    print("\n[3/5] Fetching K-line data...")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    from fetch_data import fetch_kline, daily_to_md, min30_to_md

    beg = "20250620"
    end = datetime.now().strftime("%Y%m%d")

    daily_raw = fetch_kline(symbol, "101", beg, end)
    print(f"    Daily: {len(daily_raw)} records")
    if daily_raw:
        md = daily_to_md(daily_raw)
        with open(os.path.join(stock_dir, "data", "日线数据.md"), 'w', encoding='utf-8') as f:
            f.write(md)

    min30_raw = fetch_kline(symbol, "30", beg, end, datalen=1023)
    print(f"    30-min: {len(min30_raw)} records")
    if min30_raw:
        md = min30_to_md(min30_raw)
        with open(os.path.join(stock_dir, "data", "30分钟线数据.md"), 'w', encoding='utf-8') as f:
            f.write(md)

    print("\n[4/5] Running Chanlun analysis...")
    from chanlun_engine import analyze_level, synthesize_multilevel, generate_html_chart

    daily_data_path = os.path.join(stock_dir, "data", "日线数据.md")
    min30_data_path = os.path.join(stock_dir, "data", "30分钟线数据.md")

    daily_result = analyze_level(daily_data_path, "日线")
    min30_result = analyze_level(min30_data_path, "30分钟")

    md_report = synthesize_multilevel(daily_result, min30_result)
    md_path = os.path.join(stock_dir, "analysis", "缠论买卖点分析.md")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md_report)

    html_report = generate_html_chart(daily_result, min30_result)
    html_path = os.path.join(stock_dir, "analysis", "缠论分析图.html")
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_report)

    record_analysis_snapshot(symbol, name, daily_result)

    print("\n[5/5] Recording signals & generating tracking report...")
    tracker = SignalTracker(symbol, name)
    existing_signals = {(s['date'], s['signal']) for s in tracker.get_signals()}

    for p in daily_result.get('buy_sell_points', []):
        key = (p.date, p.type)
        if key not in existing_signals:
            tracker.record_signal(p.date, p.type, p.price, p.level, p.confidence, p.description)

    for p in min30_result.get('buy_sell_points', []):
        key = (p.date, p.type)
        if key not in existing_signals:
            tracker.record_signal(p.date, p.type, p.price, p.level, p.confidence, p.description)

    tracking_report = generate_daily_report(symbol, name)
    tracking_path = os.path.join(stock_dir, "tracking", "daily_report.md")
    with open(tracking_path, 'w', encoding='utf-8') as f:
        f.write(tracking_report)

    print(f"\n{'='*60}")
    print("  Output files:")
    print(f"  - Chanlun analysis: {md_path}")
    print(f"  - Chanlun chart:    {html_path}")
    print(f"  - Tracking report:  {tracking_path}")
    print(f"{'='*60}")
    print("\nDone!")


if __name__ == "__main__":
    full_update()
