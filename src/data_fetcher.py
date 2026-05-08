"""
Data fetcher for Chanlun Trading System v2.

Fetches K-line data for index ETFs at three timeframes:
  - Daily   (direction level)
  - 30-min  (operation level)
  - 5-min   (precision level)

Primary: Sina Finance API (stable, no IP restrictions, up to 2000 bars)
Fallback: East Money API (richer fields, may block non-browser IPs)
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode
from urllib.request import urlopen, Request

try:
    import baostock as bs
    _HAS_BAOSTOCK = True
except ImportError:
    _HAS_BAOSTOCK = False

_TZ_CHINA = timezone(timedelta(hours=8))


# ════════════════════════════════════════════════════════════════════
# Data Structures
# ════════════════════════════════════════════════════════════════════

@dataclass
class KlineBar:
    """Single K-line bar, unified format across all data sources."""
    datetime: str
    open: float
    close: float
    high: float
    low: float
    volume: int
    amount: float = 0.0
    change_pct: float = 0.0
    change: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class IndexConfig:
    """Configuration for a single index."""
    index_code: str
    index_name: str
    etf_code: str
    etf_name: str
    market: str
    category: str
    weight: float
    type: str = "broad"
    notes: str = ""


@dataclass
class FetchResult:
    """Result of a data fetch operation for one index, all timeframes."""
    index_cfg: IndexConfig
    daily: list[KlineBar] = field(default_factory=list)
    min30: list[KlineBar] = field(default_factory=list)
    min5: list[KlineBar] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return bool(self.daily) and bool(self.min30) and bool(self.min5)


# ════════════════════════════════════════════════════════════════════
# Symbol Resolution
# ════════════════════════════════════════════════════════════════════

def _sina_symbol(code: str, market: str = "") -> str:
    """Convert ETF/stock code to Sina format (sh/sz + code)."""
    if market:
        m = market.lower()
        return f"{'sh' if m == 'sh' else 'sz'}{code}"
    if code.startswith(("6", "5")):
        return f"sh{code}"
    if code.startswith(("0", "3", "1")):
        return f"sz{code}"
    return f"sh{code}"


def _eastmoney_secid(code: str, market: str = "") -> str:
    """Convert ETF/stock code to East Money secid format."""
    if market:
        return f"{'1' if market.upper() == 'SH' else '0'}.{code}"
    if code.startswith(("6", "5")):
        return f"1.{code}"
    if code.startswith(("0", "3", "1")):
        return f"0.{code}"
    return f"1.{code}"


# Sina uses "scale" parameter: 240=daily, 60=60min, 30=30min, 15=15min, 5=5min
PERIOD_MAP = {
    "daily": {"sina_scale": "240", "em_klt": "101"},
    "30min": {"sina_scale": "30", "em_klt": "30"},
    "5min":  {"sina_scale": "5",  "em_klt": "5"},
}


# ════════════════════════════════════════════════════════════════════
# Sina Finance API (Primary)
# ════════════════════════════════════════════════════════════════════

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _fetch_sina(sina_sym: str, scale: str, datalen: int = 1500,
                max_retries: int = 3) -> list[KlineBar]:
    """Fetch K-line from Sina Finance API."""
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?"
        f"symbol={sina_sym}&scale={scale}&ma=no&datalen={datalen}"
    )

    raw = None
    for attempt in range(max_retries):
        try:
            req = Request(url, headers=_HEADERS)
            with urlopen(req, timeout=30) as resp:
                raw = json.loads(resp.read().decode())
            break
        except Exception as e:
            wait = 2 ** attempt
            if attempt < max_retries - 1:
                print(f"    [Sina] retry {attempt+1}/{max_retries} in {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"    [Sina] FAILED after {max_retries} attempts: {e}")
                return []

    if not raw:
        return []

    bars = []
    prev_close = None
    for item in raw:
        o = float(item["open"])
        c = float(item["close"])
        h = float(item["high"])
        l = float(item["low"])
        v = int(item["volume"])

        change = (c - prev_close) if prev_close else 0.0
        change_pct = (change / prev_close * 100) if prev_close else 0.0

        dt_str = item["day"]
        if scale == "240":
            dt_str = dt_str.split(" ")[0] if " " in dt_str else dt_str

        bars.append(KlineBar(
            datetime=dt_str,
            open=o, close=c, high=h, low=l,
            volume=v, amount=0.0,
            change_pct=round(change_pct, 4),
            change=round(change, 4),
        ))
        prev_close = c

    return bars


# ════════════════════════════════════════════════════════════════════
# East Money API (Fallback)
# ════════════════════════════════════════════════════════════════════

def _fetch_eastmoney(secid: str, klt: str, beg: str, end: str,
                     max_retries: int = 3) -> list[KlineBar]:
    """Fetch K-line from East Money API."""
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": klt,
        "fqt": "1",
        "secid": secid,
        "beg": beg,
        "end": end,
        "lmt": "10000",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{urlencode(params)}"
    headers = {**_HEADERS, "Referer": "https://quote.eastmoney.com"}

    data = None
    for attempt in range(max_retries):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            break
        except Exception as e:
            wait = 2 ** attempt
            if attempt < max_retries - 1:
                print(f"    [EastMoney] retry {attempt+1}/{max_retries} in {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"    [EastMoney] FAILED after {max_retries} attempts: {e}")
                return []

    if not data or not data.get("data") or not data["data"].get("klines"):
        return []

    bars = []
    for line in data["data"]["klines"]:
        p = line.split(",")
        bars.append(KlineBar(
            datetime=p[0],
            open=float(p[1]), close=float(p[2]),
            high=float(p[3]), low=float(p[4]),
            volume=int(p[5]),
            amount=float(p[6]) if p[6] else 0.0,
            change_pct=float(p[8]) if p[8] else 0.0,
            change=float(p[9]) if p[9] else 0.0,
        ))

    return bars


# ════════════════════════════════════════════════════════════════════
# Real-time Quote (Sina hq API) — for today's provisional bar
# ════════════════════════════════════════════════════════════════════

def _fetch_realtime_bar(sina_sym: str) -> Optional[KlineBar]:
    """Fetch real-time quote from Sina hq API and build today's provisional bar.
    Returns None if market has no data today (e.g. weekend/holiday).
    """
    url = f"https://hq.sinajs.cn/list={sina_sym}"
    headers = {**_HEADERS, "Referer": "https://finance.sina.com.cn"}
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
    except Exception:
        return None

    # Parse: var hq_str_shXXXXXX="name,open,prev_close,cur,...,date,time,status"
    if '="' not in raw:
        return None
    content = raw.split('="')[1].rstrip('";\n')
    if not content:
        return None

    parts = content.split(",")
    if len(parts) < 32:
        return None

    try:
        open_price = float(parts[1])
        cur_price = float(parts[3])
        high = float(parts[4])
        low = float(parts[5])
        volume = int(float(parts[8]))
        amount = float(parts[9])
        date_str = parts[30]  # YYYY-MM-DD
    except (ValueError, IndexError):
        return None

    if open_price <= 0 or cur_price <= 0:
        return None

    prev_close = float(parts[2]) if parts[2] else 0.0
    change = (cur_price - prev_close) if prev_close else 0.0
    change_pct = (change / prev_close * 100) if prev_close else 0.0

    return KlineBar(
        datetime=date_str,
        open=open_price, close=cur_price,
        high=high, low=low,
        volume=volume, amount=amount,
        change_pct=round(change_pct, 4),
        change=round(change, 4),
    )


# ════════════════════════════════════════════════════════════════════
# BaoStock API (Fallback for individual stocks)
# ════════════════════════════════════════════════════════════════════

_BS_FREQ_MAP = {"daily": "d", "30min": "30", "5min": "5"}
_bs_logged_in = False
_bs_lock = threading.Lock()


def _bs_login():
    """Thread-safe baostock login (reuses session)."""
    global _bs_logged_in
    with _bs_lock:
        if not _bs_logged_in:
            bs.login()
            _bs_logged_in = True


def _bs_symbol(code: str, market: str = "") -> str:
    """Convert code to baostock format (sh./sz. prefix)."""
    if market:
        return f"{'sh' if market.upper() == 'SH' else 'sz'}.{code}"
    if code.startswith(("6", "5")):
        return f"sh.{code}"
    if code.startswith(("0", "3", "1")):
        return f"sz.{code}"
    return f"sh.{code}"


def _fetch_baostock(code: str, period: str, market: str = "",
                    beg: str = "") -> list[KlineBar]:
    """Fetch K-line from BaoStock (good for individual stocks, weak for ETFs)."""
    if not _HAS_BAOSTOCK:
        return []

    freq = _BS_FREQ_MAP.get(period)
    if not freq:
        return []

    _bs_login()
    bs_sym = _bs_symbol(code, market)
    start_date = f"{beg[:4]}-{beg[4:6]}-{beg[6:8]}" if beg else "2020-01-01"
    end_date = datetime.now().strftime("%Y-%m-%d")

    if freq == "d":
        fields = "date,open,high,low,close,volume,amount,pctChg"
    else:
        fields = "date,time,open,high,low,close,volume,amount"

    try:
        rs = bs.query_history_k_data_plus(
            bs_sym, fields,
            start_date=start_date, end_date=end_date,
            frequency=freq, adjustflag="2",
        )
        if rs.error_code != "0":
            print(f"    [BaoStock] error: {rs.error_msg}")
            return []
    except Exception as e:
        print(f"    [BaoStock] exception: {e}")
        return []

    bars = []
    prev_close = None
    while rs.next():
        row = rs.get_row_data()
        try:
            o = float(row[2] if freq != "d" else row[1])
            h = float(row[3] if freq != "d" else row[2])
            l = float(row[4] if freq != "d" else row[3])
            c = float(row[5] if freq != "d" else row[4])
            v = int(float(row[6] if freq != "d" else row[5]))
            amt = float(row[7] if freq != "d" else row[6]) if len(row) > (7 if freq != "d" else 6) else 0.0
        except (ValueError, IndexError):
            continue

        if o <= 0 or c <= 0:
            continue

        if freq == "d":
            dt_str = row[0]
            pct = float(row[7]) if len(row) > 7 and row[7] else 0.0
            change = (c - prev_close) if prev_close else 0.0
        else:
            raw_time = row[1]  # e.g. "20260508100000000"
            dt_str = f"{raw_time[:4]}-{raw_time[4:6]}-{raw_time[6:8]} {raw_time[8:10]}:{raw_time[10:12]}"
            pct = 0.0
            change = (c - prev_close) if prev_close else 0.0
            if prev_close:
                pct = change / prev_close * 100

        bars.append(KlineBar(
            datetime=dt_str,
            open=o, close=c, high=h, low=l,
            volume=v, amount=amt,
            change_pct=round(pct, 4),
            change=round(change, 4),
        ))
        prev_close = c

    return bars


# ════════════════════════════════════════════════════════════════════
# Unified Fetch API
# ════════════════════════════════════════════════════════════════════

def _is_etf(code: str) -> bool:
    """Heuristic: ETF codes typically start with 1/5."""
    return code.startswith(("5", "1"))


def fetch_kline(code: str, period: str, market: str = "",
                beg: str = "", datalen: int = 1500) -> list[KlineBar]:
    """Fetch K-line data for an ETF or stock.

    Data source priority:
      1. Sina Finance API (primary, good for ETFs and stocks)
      2. East Money API (fallback)
      3. BaoStock (fallback for individual stocks only; weak ETF coverage)

    Args:
        code: ETF/stock code, e.g. "510300"
        period: one of "daily", "30min", "5min"
        market: "SH" or "SZ" (auto-detect if empty)
        beg: start date YYYYMMDD (filter applied after fetch)
        datalen: max bars for Sina (default 1500)
    """
    cfg = PERIOD_MAP.get(period)
    if not cfg:
        raise ValueError(f"Unknown period: {period}. Use: {list(PERIOD_MAP.keys())}")

    sina_sym = _sina_symbol(code, market)
    bars = _fetch_sina(sina_sym, cfg["sina_scale"], datalen=datalen)

    if not bars:
        print(f"    Sina empty for {sina_sym}/{period}, trying EastMoney...")
        end = datetime.now().strftime("%Y%m%d")
        secid = _eastmoney_secid(code, market)
        bars = _fetch_eastmoney(secid, cfg["em_klt"], beg or "20250101", end)

    if not bars and not _is_etf(code):
        print(f"    EastMoney also empty for {code}/{period}, trying BaoStock...")
        bars = _fetch_baostock(code, period, market, beg)

    # For daily K-line from Sina, append today's provisional bar if missing
    if bars and period == "daily":
        today_str = datetime.now(_TZ_CHINA).strftime("%Y-%m-%d")
        last_date = bars[-1].datetime.split(" ")[0]
        if last_date < today_str:
            rt_bar = _fetch_realtime_bar(sina_sym)
            if rt_bar and rt_bar.datetime == today_str:
                bars.append(rt_bar)

    if beg and bars:
        beg_iso = f"{beg[:4]}-{beg[4:6]}-{beg[6:8]}"
        bars = [b for b in bars if b.datetime >= beg_iso]

    return bars


# ════════════════════════════════════════════════════════════════════
# Configuration Loader
# ════════════════════════════════════════════════════════════════════

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_index_watchlist(path: str = None) -> list[IndexConfig]:
    """Load index watchlist from config JSON."""
    if path is None:
        path = os.path.join(_PROJECT_ROOT, "config", "index_watchlist.json")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    indices = []
    for item in cfg["indices"]:
        indices.append(IndexConfig(
            index_code=item["index_code"],
            index_name=item["index_name"],
            etf_code=item["etf_code"],
            etf_name=item["etf_name"],
            market=item["market"],
            category=item["category"],
            weight=item["weight"],
            type=item.get("type", "broad"),
            notes=item.get("notes", ""),
        ))
    return indices


def load_settings(path: str = None) -> dict:
    """Load system settings from config JSON."""
    if path is None:
        path = os.path.join(_PROJECT_ROOT, "config", "index_watchlist.json")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("settings", {})


# ════════════════════════════════════════════════════════════════════
# Batch Fetch for All Indices
# ════════════════════════════════════════════════════════════════════

_print_lock = threading.Lock()


def _fetch_one_index(idx: IndexConfig, seq: int, total: int,
                     beg: str, datalen_daily: int, datalen_intraday: int,
                     delay: float) -> FetchResult:
    """Fetch all 3 timeframes for a single index (runs in a worker thread)."""
    result = FetchResult(index_cfg=idx)
    bar_counts = {}

    for period, label in [("daily", "日线"), ("30min", "30分钟"), ("5min", "5分钟")]:
        try:
            dl = datalen_daily if period == "daily" else datalen_intraday
            bars = fetch_kline(
                idx.etf_code, period,
                market=idx.market, beg=beg, datalen=dl,
            )
            if period == "daily":
                result.daily = bars
            elif period == "30min":
                result.min30 = bars
            else:
                result.min5 = bars
            bar_counts[label] = len(bars)
        except Exception as e:
            msg = f"{idx.etf_name} {label}: {e}"
            result.errors.append(msg)
            bar_counts[label] = f"ERR"

        if delay > 0:
            time.sleep(delay)

    summary = " | ".join(f"{k} {v}" for k, v in bar_counts.items())
    with _print_lock:
        print(f"  [{seq}/{total}] {idx.etf_name} {idx.etf_code}: {summary}")

    return result


def fetch_all_indices(indices: list[IndexConfig] = None,
                      beg: str = None,
                      datalen_daily: int = None,
                      datalen_intraday: int = None,
                      delay: float = 0.2,
                      max_workers: int = 8) -> list[FetchResult]:
    """Fetch daily + 30min + 5min data for all indices (parallelized).

    Args:
        indices: list of IndexConfig (loads from config if None)
        beg: start date for filtering (reads from config if None)
        datalen_daily: max bars for daily Sina (reads from config if None)
        datalen_intraday: max bars for intraday Sina (reads from config if None)
        delay: seconds between API calls within each worker thread
        max_workers: number of concurrent download threads (default 8)
    """
    if indices is None:
        indices = load_index_watchlist()
    settings = load_settings()
    if beg is None:
        beg = settings.get("data_start", "20220101")
    if datalen_daily is None:
        datalen_daily = settings.get("datalen_daily", 2000)
    if datalen_intraday is None:
        datalen_intraday = settings.get("datalen_intraday", 2000)

    total = len(indices)
    print(f"\n并发拉取 {total} 个标的（{max_workers} 线程，间隔 {delay}s）...")

    ordered_results: list[FetchResult | None] = [None] * total

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for i, idx in enumerate(indices):
            fut = executor.submit(
                _fetch_one_index, idx, i + 1, total,
                beg, datalen_daily, datalen_intraday, delay,
            )
            futures[fut] = i

        for fut in as_completed(futures):
            idx_pos = futures[fut]
            try:
                ordered_results[idx_pos] = fut.result()
            except Exception as e:
                idx = indices[idx_pos]
                with _print_lock:
                    print(f"  [{idx_pos+1}/{total}] {idx.etf_name} FATAL: {e}")
                ordered_results[idx_pos] = FetchResult(
                    index_cfg=idx, errors=[str(e)],
                )

    return [r for r in ordered_results if r is not None]


# ════════════════════════════════════════════════════════════════════
# Data Persistence (CSV)
# ════════════════════════════════════════════════════════════════════

_CSV_HEADER = "datetime,open,close,high,low,volume,amount,change_pct,change"


def _bars_to_csv(bars: list[KlineBar]) -> str:
    """Convert bars to CSV string."""
    lines = [_CSV_HEADER]
    for b in bars:
        lines.append(
            f"{b.datetime},{b.open},{b.close},{b.high},{b.low},"
            f"{b.volume},{b.amount},{b.change_pct},{b.change}"
        )
    return "\n".join(lines)


def _bars_to_md_table(bars: list[KlineBar], title: str, period_label: str) -> str:
    """Convert bars to a Markdown report with summary and data table."""
    if not bars:
        return f"# {title} {period_label}数据\n\n> 无数据\n"

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]

    max_h = max(highs)
    min_l = min(lows)
    max_h_dt = bars[highs.index(max_h)].datetime
    min_l_dt = bars[lows.index(min_l)].datetime
    total_chg = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0

    lines = [
        f"# {title} {period_label}数据",
        "",
        f"> 数据区间：{bars[0].datetime} ~ {bars[-1].datetime}，共 {len(bars)} 根K线",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 数据总览",
        "",
        f"- **区间最高价**：{max_h}（{max_h_dt}）",
        f"- **区间最低价**：{min_l}（{min_l_dt}）",
        f"- **最新收盘价**：{closes[-1]}",
        f"- **区间涨跌幅**：{total_chg:+.2f}%",
        f"- **K线数量**：{len(bars)}",
        "",
        "## K线数据表",
        "",
        "| 时间 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 涨跌幅% |",
        "|------|------|------|------|------|--------|---------|",
    ]

    for b in bars:
        lines.append(
            f"| {b.datetime} | {b.open} | {b.close} | {b.high} | {b.low} "
            f"| {b.volume} | {b.change_pct} |"
        )

    return "\n".join(lines)


def save_fetch_results(results: list[FetchResult],
                       base_dir: str = None,
                       fmt: str = "csv") -> dict:
    """Save all fetch results to disk.

    Args:
        results: list of FetchResult from fetch_all_indices()
        base_dir: output directory (defaults to PROJECT_ROOT/data)
        fmt: "csv" for machine consumption, "md" for human-readable

    Returns:
        dict mapping index_code -> {daily: path, min30: path, min5: path}
    """
    if base_dir is None:
        base_dir = os.path.join(_PROJECT_ROOT, "data")

    paths = {}

    for res in results:
        idx = res.index_cfg
        idx_dir = os.path.join(base_dir, f"{idx.etf_code}_{idx.etf_name}")
        os.makedirs(idx_dir, exist_ok=True)

        idx_paths = {}
        for period, bars, label in [
            ("daily", res.daily, "日线"),
            ("30min", res.min30, "30分钟"),
            ("5min", res.min5, "5分钟"),
        ]:
            if not bars:
                continue

            if fmt == "csv":
                fname = f"{period}.csv"
                content = _bars_to_csv(bars)
            else:
                fname = f"{label}数据.md"
                content = _bars_to_md_table(bars, idx.etf_name, label)

            fpath = os.path.join(idx_dir, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
            idx_paths[period] = fpath

        paths[idx.etf_code] = idx_paths
        print(f"  Saved {idx.etf_name} -> {idx_dir}/")

    return paths


# ════════════════════════════════════════════════════════════════════
# Summary Report
# ════════════════════════════════════════════════════════════════════

def print_fetch_summary(results: list[FetchResult]):
    """Print a summary table of fetch results."""
    print("\n" + "=" * 72)
    print("数据拉取汇总")
    print("=" * 72)
    print(f"{'指数':<10} {'ETF':<12} {'日线':>6} {'30分钟':>8} {'5分钟':>7} {'状态':<6}")
    print("-" * 72)

    for res in results:
        idx = res.index_cfg
        status = "✓ 完整" if res.is_complete else "✗ 缺失"
        print(
            f"{idx.index_name:<10} {idx.etf_code:<12} "
            f"{len(res.daily):>6} {len(res.min30):>8} {len(res.min5):>7} "
            f"{status:<6}"
        )
        for err in res.errors:
            print(f"  ⚠ {err}")

    ok = sum(1 for r in results if r.is_complete)
    print("-" * 72)
    print(f"总计：{ok}/{len(results)} 个指数数据完整")
    print("=" * 72)


# ════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ════════════════════════════════════════════════════════════════════

def main():
    """Standalone data fetch for all indices."""
    import argparse

    parser = argparse.ArgumentParser(description="Chanlun Trading System - Data Fetcher")
    parser.add_argument("--beg", default="20250101", help="Start date YYYYMMDD (default: 20250101)")
    parser.add_argument("--format", choices=["csv", "md"], default="csv",
                        help="Output format (default: csv)")
    parser.add_argument("--outdir", default=None,
                        help="Output directory (default: PROJECT_ROOT/data)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Delay between API calls in seconds (default: 1.0)")
    args = parser.parse_args()

    print("=" * 72)
    print("缠论交易系统 v2 — 数据拉取")
    print(f"级别：日线（方向）→ 30分钟（买卖点）→ 5分钟（择时）")
    print(f"起始日期：{args.beg}")
    print(f"输出格式：{args.format}")
    print("=" * 72)

    indices = load_index_watchlist()
    print(f"\n加载 {len(indices)} 个指数标的：")
    for idx in indices:
        print(f"  {idx.index_name} ({idx.etf_name} {idx.etf_code}) [{idx.category}]")

    results = fetch_all_indices(
        indices=indices, beg=args.beg, delay=args.delay,
    )

    print_fetch_summary(results)

    print(f"\n保存数据（格式：{args.format}）...")
    paths = save_fetch_results(results, base_dir=args.outdir, fmt=args.format)

    print("\n数据拉取完成。")
    return results


if __name__ == "__main__":
    main()
