"""
Data fetcher for Chanlun Trading System v2.

Fetches K-line data for index ETFs at three timeframes:
  - Daily   (direction level)
  - 30-min  (operation level)
  - 5-min   (precision level)

Data source priority is configurable via DATA_SOURCE_PRIMARY:
  "tencent"    (default) — Tencent Finance (ifzq.gtimg.cn), stable, no IP blocking
  "eastmoney"            — more accurate close prices (includes auction)
  "sina"                 — historical option, currently IP-blocked (HTTP 456)
Fallback chain: tencent → eastmoney → sina → BaoStock (stocks only).
"""

from __future__ import annotations

import json
import os
import random
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

# ── Data source configuration ─────────────────────────────────────
# Switch primary source: "tencent", "eastmoney", or "sina"
# Fallback chain tries remaining sources in order.
DATA_SOURCE_PRIMARY = "tencent"


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

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
]


def _random_headers(referer: str = "") -> dict:
    """Build randomized browser-like HTTP headers."""
    h = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    if referer:
        h["Referer"] = referer
    return h


_HEADERS = _random_headers()

_sina_last_call = 0.0
_sina_lock = threading.Lock()
_SINA_MIN_INTERVAL = 0.35


def _fetch_sina(sina_sym: str, scale: str, datalen: int = 1500,
                max_retries: int = 3) -> list[KlineBar]:
    """Fetch K-line from Sina Finance API."""
    global _sina_last_call
    with _sina_lock:
        elapsed = time.monotonic() - _sina_last_call
        if elapsed < _SINA_MIN_INTERVAL:
            time.sleep(_SINA_MIN_INTERVAL - elapsed)
        _sina_last_call = time.monotonic()

    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?"
        f"symbol={sina_sym}&scale={scale}&ma=no&datalen={datalen}"
    )

    raw = None
    for attempt in range(max_retries):
        try:
            req = Request(url, headers=_random_headers("https://finance.sina.com.cn"))
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
# Rate-limit guard: minimum 0.15s between consecutive EM requests
# ════════════════════════════════════════════════════════════════════

_em_last_call = 0.0
_em_lock = threading.Lock()
_EM_MIN_INTERVAL = 0.15


def _fetch_eastmoney(secid: str, klt: str, beg: str, end: str,
                     max_retries: int = 3) -> list[KlineBar]:
    """Fetch K-line from East Money API."""
    global _em_last_call
    with _em_lock:
        elapsed = time.monotonic() - _em_last_call
        if elapsed < _EM_MIN_INTERVAL:
            time.sleep(_EM_MIN_INTERVAL - elapsed)
        _em_last_call = time.monotonic()
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

    data = None
    for attempt in range(max_retries):
        try:
            req = Request(url, headers=_random_headers("https://quote.eastmoney.com"))
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
        dt = p[0]
        # EM intraday returns "YYYY-MM-DD HH:MM"; normalize to "HH:MM:00"
        if " " in dt and dt.count(":") == 1:
            dt += ":00"
        bars.append(KlineBar(
            datetime=dt,
            open=float(p[1]), close=float(p[2]),
            high=float(p[3]), low=float(p[4]),
            volume=int(p[5]),
            amount=float(p[6]) if p[6] else 0.0,
            change_pct=float(p[8]) if p[8] else 0.0,
            change=float(p[9]) if p[9] else 0.0,
        ))

    return bars


# ════════════════════════════════════════════════════════════════════
# Tencent Finance API (Primary)
# Daily:   web.ifzq.gtimg.cn  — up to ~800 bars, qfq (forward-adjusted)
# Intra:   ifzq.gtimg.cn      — up to ~320 bars for m30/m5
# ════════════════════════════════════════════════════════════════════

_TENCENT_PERIOD_MAP = {
    "daily": {"url_type": "daily", "key": "qfqday"},
    "30min": {"url_type": "minute", "param": "m30", "key": "m30"},
    "5min":  {"url_type": "minute", "param": "m5",  "key": "m5"},
}


_TENCENT_MAX_PER_REQ = 800
_TENCENT_MAX_MINUTE = 320


def _fetch_tencent_single(sina_sym: str, url: str, data_key: str,
                          url_type: str, max_retries: int = 3) -> list[KlineBar]:
    """Fetch one batch of K-line from Tencent Finance API."""
    raw_json = None
    for attempt in range(max_retries):
        try:
            req = Request(url, headers=_random_headers("https://web.ifzq.gtimg.cn"))
            with urlopen(req, timeout=30) as resp:
                raw_json = json.loads(resp.read().decode())
            break
        except Exception as e:
            wait = 2 ** attempt
            if attempt < max_retries - 1:
                print(f"    [Tencent] retry {attempt+1}/{max_retries} in {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"    [Tencent] FAILED after {max_retries} attempts: {e}")
                return []

    if not raw_json:
        return []

    data = raw_json.get("data", {})
    if isinstance(data, list):
        if not data:
            return []
        data = data[0] if isinstance(data[0], dict) else {}

    sym_data = data.get(sina_sym, {})
    klines = sym_data.get(data_key, [])

    if not klines and data_key == "qfqday":
        klines = sym_data.get("day", [])

    if not klines:
        return []

    bars = []
    prev_close = None
    for item in klines:
        if not isinstance(item, list) or len(item) < 6:
            continue

        if url_type == "daily":
            dt_str = item[0]
            o, c, h, l = float(item[1]), float(item[2]), float(item[3]), float(item[4])
            v = int(float(item[5]))
            amt = 0.0
        else:
            raw_dt = item[0]  # "202604281030"
            dt_str = f"{raw_dt[:4]}-{raw_dt[4:6]}-{raw_dt[6:8]} {raw_dt[8:10]}:{raw_dt[10:12]}:00"
            o, c, h, l = float(item[1]), float(item[2]), float(item[3]), float(item[4])
            v = int(float(item[5]))
            amt = float(item[7]) if len(item) > 7 and isinstance(item[7], (int, float, str)) and item[7] != {} else 0.0

        change = (c - prev_close) if prev_close else 0.0
        change_pct = (change / prev_close * 100) if prev_close else 0.0

        bars.append(KlineBar(
            datetime=dt_str,
            open=o, close=c, high=h, low=l,
            volume=v, amount=amt,
            change_pct=round(change_pct, 4),
            change=round(change, 4),
        ))
        prev_close = c

    return bars


# End-date boundaries for multi-window daily fetch (recent→old order).
# Each window fetches up to 800 bars ending BEFORE the boundary.
# Chosen to maximize coverage with minimal overlap.
_TENCENT_DAILY_WINDOWS = [
    None,          # window 0: beg ~ now        (latest ~3 yrs)
    "2023-06-01",  # window 1: beg ~ 2023-06    (fills gap: ~2020 to ~2023)
    "2020-06-01",  # window 2: beg ~ 2020-06    (fills gap: ~2017 to ~2020)
    "2017-06-01",  # window 3: beg ~ 2017-06    (older: ~2014 to ~2017)
    "2014-06-01",  # window 4: beg ~ 2014-06    (oldest: ~2011 to ~2014)
]


def _fetch_tencent(sina_sym: str, period: str, beg: str = "",
                   datalen: int = 800, max_retries: int = 3) -> list[KlineBar]:
    """Fetch K-line from Tencent Finance API.

    For daily data, uses multi-window fetch to get up to ~2400 bars
    (3 windows × 800 bars each) when datalen > 800.

    Args:
        sina_sym: symbol in sh/sz format (e.g. "sh510300")
        period: "daily", "30min", or "5min"
        beg: start date YYYYMMDD (only used for daily)
        datalen: desired total bars (daily auto-splits into windows)
        max_retries: retry count on failure
    """
    tcfg = _TENCENT_PERIOD_MAP.get(period)
    if not tcfg:
        return []

    if tcfg["url_type"] != "daily":
        dl = min(datalen, _TENCENT_MAX_MINUTE)
        url = (
            f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?"
            f"param={sina_sym},{tcfg['param']},,{dl}"
        )
        return _fetch_tencent_single(sina_sym, url, tcfg["key"], "minute", max_retries)

    # Daily: multi-window fetch to overcome 800-bar limit
    start_iso = f"{beg[:4]}-{beg[4:6]}-{beg[6:8]}" if beg else "2010-01-01"
    end_now = datetime.now(_TZ_CHINA).strftime("%Y-%m-%d")
    num_windows = max(1, min(len(_TENCENT_DAILY_WINDOWS), datalen // _TENCENT_MAX_PER_REQ + 1))

    merged = OrderedDict()
    data_key = tcfg["key"]

    for i in range(num_windows):
        end_iso = _TENCENT_DAILY_WINDOWS[i] or end_now
        url = (
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
            f"param={sina_sym},day,{start_iso},{end_iso},{_TENCENT_MAX_PER_REQ},qfq"
        )
        batch = _fetch_tencent_single(sina_sym, url, data_key, "daily", max_retries)
        for b in batch:
            merged[b.datetime] = b

        if len(batch) < _TENCENT_MAX_PER_REQ // 2:
            break

    result = list(merged.values())
    result.sort(key=lambda b: b.datetime)
    return result


# ════════════════════════════════════════════════════════════════════
# Real-time Quote (Sina hq API) — for today's provisional bar
# ════════════════════════════════════════════════════════════════════

def _fetch_realtime_bar(sina_sym: str) -> Optional[KlineBar]:
    """Fetch real-time quote from Sina hq API and build today's provisional bar.
    Returns None if market has no data today (e.g. weekend/holiday).
    """
    global _sina_last_call
    with _sina_lock:
        elapsed = time.monotonic() - _sina_last_call
        if elapsed < _SINA_MIN_INTERVAL:
            time.sleep(_SINA_MIN_INTERVAL - elapsed)
        _sina_last_call = time.monotonic()

    url = f"https://hq.sinajs.cn/list={sina_sym}"
    try:
        req = Request(url, headers=_random_headers("https://finance.sina.com.cn"))
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
        # Sina real-time returns volume in shares; convert to lots (手=100股)
        # to match Tencent historical K-line unit.
        volume = int(float(parts[8]) / 100)
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

    Data source order is controlled by DATA_SOURCE_PRIMARY.
    Fallback chain tries all sources: tencent → eastmoney → sina → BaoStock.

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
    secid = _eastmoney_secid(code, market)
    end_date = datetime.now().strftime("%Y%m%d")
    em_beg = beg or "20250101"
    source_used = None
    bars = []

    source_order = _build_source_order(DATA_SOURCE_PRIMARY, period)

    for source in source_order:
        if source == "tencent":
            bars = _fetch_tencent(sina_sym, period, beg=beg, datalen=datalen)
            if bars:
                source_used = "tencent"
                break
        elif source == "eastmoney":
            bars = _fetch_eastmoney(secid, cfg["em_klt"], em_beg, end_date)
            if bars:
                source_used = "eastmoney"
                break
        elif source == "sina":
            bars = _fetch_sina(sina_sym, cfg["sina_scale"], datalen=datalen)
            if bars:
                source_used = "sina"
                break

        if not bars:
            print(f"    {source} empty for {code}/{period}, trying next...")

    if not bars and not _is_etf(code):
        print(f"    All sources empty for {code}/{period}, trying BaoStock...")
        bars = _fetch_baostock(code, period, market, beg)
        if bars:
            source_used = "baostock"

    # Tencent and Sina daily K-line may NOT include today's in-progress bar;
    # supplement via real-time quote API when market is open.
    if bars and period == "daily" and source_used not in ("eastmoney",):
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


def _build_source_order(primary: str, period: str = "daily") -> list[str]:
    """Build the data source fallback chain.

    Tencent is always first (most stable, no IP blocking).
    Sina/EastMoney are fallbacks only — they rate-limit aggressively
    when hit with bulk requests (241 symbols × 3 periods every 5 min).
    """
    all_sources = ["tencent", "eastmoney", "sina"]
    if primary in all_sources:
        all_sources.remove(primary)
        return [primary] + all_sources
    return all_sources


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
# Incremental Fetch Helpers
# ════════════════════════════════════════════════════════════════════

def _csv_last_datetime(csv_path: str) -> Optional[str]:
    """Read last bar datetime from CSV file (fast: reads only tail)."""
    if not os.path.exists(csv_path):
        return None
    try:
        with open(csv_path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            pos = max(0, fsize - 512)
            f.seek(pos)
            tail = f.read().decode("utf-8", errors="ignore")
        lines = tail.strip().split("\n")
        for line in reversed(lines):
            line = line.strip()
            if line and not line.startswith("datetime"):
                return line.split(",")[0]
    except Exception:
        pass
    return None


def _is_cn_market_closed() -> bool:
    """Check if Chinese A-share market has closed for today's session."""
    now = datetime.now(_TZ_CHINA)
    if now.weekday() >= 5:
        return True
    return now.hour > 15 or (now.hour == 15 and now.minute >= 5)


def _latest_trading_date() -> str:
    """Estimate the latest trading date (YYYY-MM-DD). Not holiday-aware."""
    now = datetime.now(_TZ_CHINA)
    if now.weekday() >= 5:
        days_back = now.weekday() - 4
        d = now - timedelta(days=days_back)
    elif now.hour < 9 or (now.hour == 9 and now.minute < 30):
        d = now - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    else:
        d = now
    return d.strftime("%Y-%m-%d")


_SKIP = "skip"
_SMALL = "small"
_FULL = "full"

_SMALL_DATALEN_DAILY = 80
_SMALL_DATALEN_INTRADAY = 300


def _fetch_strategy(csv_path: str, period: str) -> str:
    """Determine fetch strategy based on existing CSV freshness.

    Returns _SKIP / _SMALL / _FULL.
    - SKIP: data already complete, no API call needed
    - SMALL: data is recent but may have incomplete last bar, fetch small window
    - FULL: no data or very old, full fetch needed
    """
    last_dt = _csv_last_datetime(csv_path)
    if not last_dt:
        return _FULL

    last_date = last_dt.split(" ")[0]
    latest_td = _latest_trading_date()
    market_closed = _is_cn_market_closed()

    if last_date == latest_td and market_closed:
        try:
            mtime = os.path.getmtime(csv_path)
            mdt = datetime.fromtimestamp(mtime, tz=_TZ_CHINA)
            if mdt.strftime("%Y-%m-%d") == latest_td and mdt.hour < 15:
                return _SMALL
        except Exception:
            pass
        return _SKIP

    if last_date >= latest_td:
        return _SMALL

    try:
        ld = datetime.strptime(last_date, "%Y-%m-%d")
        ltd = datetime.strptime(latest_td, "%Y-%m-%d")
        if (ltd - ld).days <= 5:
            return _SMALL
    except ValueError:
        pass
    return _FULL


def _merge_bars(existing_csv_path: str, new_bars: list[KlineBar]) -> list[KlineBar]:
    """Merge new bars into existing CSV data. New bars win on datetime collision."""
    if not os.path.exists(existing_csv_path) or not new_bars:
        return new_bars

    existing = []
    try:
        with open(existing_csv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("datetime"):
                    continue
                parts = line.split(",")
                if len(parts) >= 6:
                    existing.append(KlineBar(
                        datetime=parts[0],
                        open=float(parts[1]),
                        close=float(parts[2]),
                        high=float(parts[3]),
                        low=float(parts[4]),
                        volume=int(float(parts[5])),
                        amount=float(parts[6]) if len(parts) > 6 else 0.0,
                        change_pct=float(parts[7]) if len(parts) > 7 else 0.0,
                        change=float(parts[8]) if len(parts) > 8 else 0.0,
                    ))
    except Exception:
        return new_bars

    merged = OrderedDict()
    for b in existing:
        merged[b.datetime] = b
    for b in new_bars:
        merged[b.datetime] = b

    result = list(merged.values())
    result.sort(key=lambda b: b.datetime)

    daily_csv = ""
    if existing_csv_path:
        parent = os.path.dirname(existing_csv_path)
        daily_csv = os.path.join(parent, "daily.csv")

    return _normalize_volume_units(result, daily_csv_path=daily_csv)


def _normalize_volume_units(bars: list[KlineBar],
                            daily_csv_path: str = "") -> list[KlineBar]:
    """Fix volume unit discontinuity caused by Tencent API changes.

    Around 2026-03-19, the Tencent intraday API changed volume units.
    The exact unit ratio varies per symbol (e.g., daily/30m_sum can be 44x or 100x).
    Uses daily CSV data as ground truth to compute the precise scaling factor.
    Falls back to median comparison when daily data is unavailable.
    """
    if len(bars) < 20:
        return bars

    boundary = -1
    for i in range(1, len(bars)):
        if bars[i].amount > 0 and bars[i - 1].amount == 0:
            boundary = i
            break

    if boundary < 0:
        return bars

    scale = _calc_scale_from_daily(bars, boundary, daily_csv_path)

    if scale is None:
        scale = _calc_scale_from_median(bars, boundary)

    if scale is not None and scale > 0:
        for b in bars[:boundary]:
            b.volume = int(b.volume * scale)

    return bars


def _calc_scale_from_daily(bars: list[KlineBar], boundary: int,
                           daily_csv_path: str) -> float | None:
    """Compute volume scale factor using daily data as ground truth.

    Compares daily_vol / intraday_sum ratios for old (amount=0) and new (amount>0)
    eras. If they differ by >1.3x, returns the scale factor to normalize old data.
    """
    if not daily_csv_path or not os.path.exists(daily_csv_path):
        return None

    daily_vols = {}
    try:
        with open(daily_csv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("datetime"):
                    continue
                parts = line.split(",")
                if len(parts) >= 6:
                    d_vol = int(float(parts[5]))
                    if d_vol > 0:
                        daily_vols[parts[0].split(" ")[0]] = d_vol
    except Exception:
        return None

    if not daily_vols:
        return None

    from collections import defaultdict
    intra_sums_old: dict[str, int] = defaultdict(int)
    intra_sums_new: dict[str, int] = defaultdict(int)

    for i, b in enumerate(bars):
        day = b.datetime.split(" ")[0]
        if b.volume <= 0:
            continue
        if i < boundary:
            intra_sums_old[day] += b.volume
        else:
            intra_sums_new[day] += b.volume

    old_ratios = []
    for day, s in intra_sums_old.items():
        if day in daily_vols and s > 0:
            old_ratios.append(daily_vols[day] / s)

    new_ratios = []
    for day, s in intra_sums_new.items():
        if day in daily_vols and s > 0:
            new_ratios.append(daily_vols[day] / s)

    if len(old_ratios) < 3 or len(new_ratios) < 3:
        return None

    old_median = sorted(old_ratios)[len(old_ratios) // 2]
    new_median = sorted(new_ratios)[len(new_ratios) // 2]

    if old_median <= 0 or new_median <= 0:
        return None

    diff = max(old_median, new_median) / min(old_median, new_median)
    if diff < 1.3:
        return None

    return old_median / new_median


def _calc_scale_from_median(bars: list[KlineBar], boundary: int) -> float | None:
    """Fallback: compute scale from volume medians across the full old/new eras."""
    old_vols = [b.volume for b in bars[:boundary] if b.volume > 0]
    new_vols = [b.volume for b in bars[boundary:] if b.volume > 0]

    if len(old_vols) < 10 or len(new_vols) < 10:
        return None

    old_median = sorted(old_vols)[len(old_vols) // 2]
    new_median = sorted(new_vols)[len(new_vols) // 2]

    if old_median <= 0 or new_median <= 0:
        return None

    ratio = old_median / new_median
    if ratio < 1.5:
        return None

    return new_median / old_median


# ════════════════════════════════════════════════════════════════════
# Batch Fetch for All Indices
# ════════════════════════════════════════════════════════════════════

_print_lock = threading.Lock()


def _fetch_one_index(idx: IndexConfig, seq: int, total: int,
                     beg: str, datalen_daily: int, datalen_intraday: int,
                     delay: float, data_dir: str = "",
                     force: bool = False) -> FetchResult:
    """Fetch all 3 timeframes for a single index (runs in a worker thread).

    Supports incremental mode: checks existing CSV freshness and may
    skip or use reduced datalen for indices with up-to-date data.
    Set force=True to always do full fetch.
    """
    result = FetchResult(index_cfg=idx)
    bar_counts = {}
    skipped_all = True

    csv_names = {"daily": "daily.csv", "30min": "30min.csv", "5min": "5min.csv"}
    idx_dir = os.path.join(data_dir, f"{idx.etf_code}_{idx.etf_name}") if data_dir else ""

    for period, label in [("daily", "日线"), ("30min", "30分钟"), ("5min", "5分钟")]:
        csv_path = os.path.join(idx_dir, csv_names[period]) if idx_dir else ""

        strategy = _FULL if force else _fetch_strategy(csv_path, period)

        if strategy == _SKIP:
            bar_counts[label] = "✓"
            continue

        skipped_all = False
        try:
            if strategy == _SMALL:
                dl = _SMALL_DATALEN_DAILY if period == "daily" else _SMALL_DATALEN_INTRADAY
            else:
                dl = datalen_daily if period == "daily" else datalen_intraday
            bars = fetch_kline(
                idx.etf_code, period,
                market=idx.market, beg=beg, datalen=dl,
            )
            if csv_path and bars:
                bars = _merge_bars(csv_path, bars)
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
            bar_counts[label] = "ERR"

        if delay > 0 and not skipped_all:
            time.sleep(delay + random.uniform(0, delay * 0.5))

    tag = " [skip]" if skipped_all else (" [incr]" if not force and any(
        v == "✓" for v in bar_counts.values()) else "")
    summary = " | ".join(f"{k} {v}" for k, v in bar_counts.items())
    with _print_lock:
        print(f"  [{seq}/{total}] {idx.etf_name} {idx.etf_code}: {summary}{tag}")

    return result


def fetch_all_indices(indices: list[IndexConfig] = None,
                      beg: str = None,
                      datalen_daily: int = None,
                      datalen_intraday: int = None,
                      delay: float = 0.2,
                      max_workers: int = 8,
                      force: bool = False) -> list[FetchResult]:
    """Fetch daily + 30min + 5min data for all indices (parallelized).

    Args:
        indices: list of IndexConfig (loads from config if None)
        beg: start date for filtering (reads from config if None)
        datalen_daily: max bars for daily Sina (reads from config if None)
        datalen_intraday: max bars for intraday Sina (reads from config if None)
        delay: seconds between API calls within each worker thread
        max_workers: number of concurrent download threads (default 8)
        force: bypass incremental mode, always full fetch
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

    data_dir = os.path.join(_PROJECT_ROOT, "data")
    mode = "全量" if force else "增量"
    total = len(indices)
    print(f"\n并发拉取 {total} 个标的（{max_workers} 线程，间隔 {delay}s，{mode}模式）...")

    ordered_results: list[FetchResult | None] = [None] * total

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for i, idx in enumerate(indices):
            fut = executor.submit(
                _fetch_one_index, idx, i + 1, total,
                beg, datalen_daily, datalen_intraday, delay,
                data_dir, force,
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


def supplement_daily_with_sina(results: list[FetchResult],
                               data_dir: str = None,
                               min_bars: int = 2000,
                               datalen: int = 5000) -> int:
    """Supplement shallow daily data with Sina (serial, rate-limited).

    After the main Tencent bulk fetch, some ETFs/stocks may have <2000 daily
    bars because Tencent caps at ~800 per window. This function fills the gap
    by calling Sina one-by-one (serial, 1s delay) to get deeper history.

    Returns the number of indices supplemented.
    """
    if data_dir is None:
        data_dir = os.path.join(_PROJECT_ROOT, "data")

    candidates = []
    for res in results:
        daily_count = len(res.daily)
        if 0 < daily_count < min_bars:
            candidates.append(res)

    if not candidates:
        print(f"  Sina supplement: all {len(results)} indices have ≥{min_bars} daily bars, skip.")
        return 0

    print(f"\n  Sina supplement: {len(candidates)} indices with <{min_bars} daily bars, "
          f"fetching deeper history...")

    count = 0
    for i, res in enumerate(candidates):
        idx = res.index_cfg
        sina_sym = _sina_symbol(idx.etf_code, idx.market)

        time.sleep(1.0 + random.uniform(0, 0.5))

        bars = _fetch_sina(sina_sym, "240", datalen=datalen)
        if not bars:
            print(f"    [{i+1}/{len(candidates)}] {idx.etf_name}: Sina failed, skip")
            continue

        csv_path = os.path.join(data_dir, f"{idx.etf_code}_{idx.etf_name}", "daily.csv")
        merged = _merge_bars(csv_path, bars)

        before = len(res.daily)
        res.daily = merged
        gained = len(merged) - before
        count += 1
        print(f"    [{i+1}/{len(candidates)}] {idx.etf_name}: {before} → {len(merged)} "
              f"(+{gained} bars)")

    print(f"  Sina supplement done: {count}/{len(candidates)} indices enriched.")
    return count


def supplement_intraday_with_sina(results: list[FetchResult],
                                  data_dir: str = None,
                                  min_bars: int = 800,
                                  datalen: int = 1500) -> int:
    """Supplement shallow intraday (30min/5min) data with Sina.

    Tencent caps minute data at 320 bars (~40 trading days for 30min).
    Sina can return up to 1500 bars (~187 trading days for 30min).
    This function fills the gap by calling Sina one-by-one.

    Returns the total number of period-index pairs supplemented.
    """
    if data_dir is None:
        data_dir = os.path.join(_PROJECT_ROOT, "data")

    periods = [
        ("30min", "30", "min30", "30min.csv"),
        ("5min", "5", "min5", "5min.csv"),
    ]

    total_enriched = 0

    for period, sina_scale, attr_name, csv_name in periods:
        candidates = []
        for res in results:
            bars_list = getattr(res, attr_name, None) or []
            if 0 < len(bars_list) < min_bars:
                candidates.append(res)

        if not candidates:
            print(f"  Sina {period} supplement: all have ≥{min_bars} bars, skip.")
            continue

        print(f"\n  Sina {period} supplement: {len(candidates)} indices with "
              f"<{min_bars} bars, fetching deeper history...")

        count = 0
        for i, res in enumerate(candidates):
            idx = res.index_cfg
            sina_sym = _sina_symbol(idx.etf_code, idx.market)

            time.sleep(1.0 + random.uniform(0, 0.5))

            bars = _fetch_sina(sina_sym, sina_scale, datalen=datalen)
            if not bars:
                print(f"    [{i+1}/{len(candidates)}] {idx.etf_name} {period}: "
                      f"Sina failed, skip")
                continue

            csv_path = os.path.join(
                data_dir, f"{idx.etf_code}_{idx.etf_name}", csv_name)
            merged = _merge_bars(csv_path, bars)

            old_bars = getattr(res, attr_name, None) or []
            before = len(old_bars)
            setattr(res, attr_name, merged)
            gained = len(merged) - before
            count += 1
            print(f"    [{i+1}/{len(candidates)}] {idx.etf_name} {period}: "
                  f"{before} → {len(merged)} (+{gained} bars)")

        print(f"  Sina {period} supplement done: {count}/{len(candidates)} enriched.")
        total_enriched += count

    return total_enriched


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
