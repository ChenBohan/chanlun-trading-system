"""
Fetch K-line data for stocks, indices, and ETFs.
Primary source: Sina Finance API (no IP restrictions, supports up to 2000 bars)
Fallback: East Money API (may be blocked from cloud server IPs)
Output: markdown files for Chanlun technical analysis.
"""

import json
import os
import sys
import time
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.parse import urlencode


# ─── Symbol Resolution ───

def _resolve_sina_symbol(symbol: str, market: str = None) -> str:
    """Resolve symbol to Sina format: sh/sz + code."""
    if market:
        return f"{market.lower()}{symbol}"
    if symbol.startswith("6") or symbol.startswith("5"):
        return f"sh{symbol}"
    if symbol.startswith("0") or symbol.startswith("3") or symbol.startswith("1"):
        return f"sz{symbol}"
    return f"sh{symbol}"


def _resolve_secid(symbol: str) -> str:
    """Resolve symbol to East Money secid format (fallback)."""
    if symbol.startswith("6") or symbol.startswith("5"):
        return f"1.{symbol}"
    if symbol.startswith("0") or symbol.startswith("3") or symbol.startswith("1"):
        return f"0.{symbol}"
    return f"1.{symbol}"


_PERIOD_TO_SINA_SCALE = {"101": "240", "30": "30", "60": "60", "15": "15", "5": "5"}


# ─── Sina Finance API (Primary) ───

def _fetch_sina(sina_symbol: str, scale: str, datalen: int = 1000,
                max_retries: int = 3) -> list:
    """Fetch K-line data from Sina Finance API.

    Returns list of dicts with keys matching the East Money format for
    downstream compatibility: datetime, open, close, high, low, volume,
    amount, amplitude, change_pct, change, turnover.
    """
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?"
        f"symbol={sina_symbol}&scale={scale}&ma=no&datalen={datalen}"
    )

    for attempt in range(max_retries):
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            })
            with urlopen(req, timeout=30) as resp:
                raw = json.loads(resp.read().decode())
            break
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"  Sina retry {attempt+1}/{max_retries} after {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"  Sina ERROR: Failed after {max_retries} attempts: {e}")
                return []

    if not raw:
        return []

    result = []
    prev_close = None
    for bar in raw:
        o = float(bar["open"])
        c = float(bar["close"])
        h = float(bar["high"])
        l = float(bar["low"])
        v = int(bar["volume"])

        amplitude = ((h - l) / prev_close * 100) if prev_close else 0.0
        change = (c - prev_close) if prev_close else 0.0
        change_pct = (change / prev_close * 100) if prev_close else 0.0

        dt_str = bar["day"]
        if scale == "240":
            dt_str = dt_str.split(" ")[0] if " " in dt_str else dt_str

        result.append({
            "datetime": dt_str,
            "open": bar["open"],
            "close": bar["close"],
            "high": bar["high"],
            "low": bar["low"],
            "volume": str(v),
            "amount": "0",
            "amplitude": f"{amplitude:.2f}",
            "change_pct": f"{change_pct:.2f}",
            "change": f"{change:.4f}",
            "turnover": "0",
        })
        prev_close = c

    return result


# ─── East Money API (Fallback) ───

def _fetch_eastmoney(secid: str, period: str, beg: str, end: str,
                     max_retries: int = 3) -> list:
    """Fetch K-line data from East Money API with retry."""
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": period,
        "fqt": "1",
        "secid": secid,
        "beg": beg,
        "end": end,
        "lmt": "10000",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }

    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{urlencode(params)}"

    for attempt in range(max_retries):
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://quote.eastmoney.com",
            })
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            break
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"  EastMoney retry {attempt+1}/{max_retries} after {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"  EastMoney ERROR: Failed after {max_retries} attempts: {e}")
                return []

    if not data.get("data") or not data["data"].get("klines"):
        return []

    result = []
    for line in data["data"]["klines"]:
        parts = line.split(",")
        result.append({
            "datetime": parts[0],
            "open": parts[1],
            "close": parts[2],
            "high": parts[3],
            "low": parts[4],
            "volume": parts[5],
            "amount": parts[6],
            "amplitude": parts[7],
            "change_pct": parts[8],
            "change": parts[9],
            "turnover": parts[10],
        })

    return result


# ─── Unified Public API ───

def fetch_kline(symbol: str, period: str, beg: str, end: str,
                secid_override: str = None, datalen: int = 1000) -> list:
    """Fetch K-line data. Tries Sina first, falls back to East Money.

    period: 101=daily, 30=30min, 60=60min, 5=5min
    beg/end: YYYYMMDD
    datalen: max number of bars for Sina API (default 1000)
    """
    sina_scale = _PERIOD_TO_SINA_SCALE.get(period)
    sina_symbol = _resolve_sina_symbol(symbol)

    if sina_scale:
        rows = _fetch_sina(sina_symbol, sina_scale, datalen=datalen)
        if rows:
            if beg:
                beg_dt = f"{beg[:4]}-{beg[4:6]}-{beg[6:8]}"
                rows = [r for r in rows if r["datetime"] >= beg_dt]
            return rows
        print(f"  Sina returned no data for {sina_symbol}, trying EastMoney...")

    secid = secid_override or _resolve_secid(symbol)
    return _fetch_eastmoney(secid, period, beg, end)


def fetch_index_kline(index_code: str, period: str, beg: str, end: str,
                      datalen: int = 1000) -> list:
    """Fetch index K-line data."""
    if index_code.startswith("399"):
        sina_sym = f"sz{index_code}"
        secid = f"0.{index_code}"
    else:
        sina_sym = f"sh{index_code}"
        secid = f"1.{index_code}"

    sina_scale = _PERIOD_TO_SINA_SCALE.get(period)
    if sina_scale:
        rows = _fetch_sina(sina_sym, sina_scale, datalen=datalen)
        if rows:
            if beg:
                beg_dt = f"{beg[:4]}-{beg[4:6]}-{beg[6:8]}"
                rows = [r for r in rows if r["datetime"] >= beg_dt]
            return rows
        print(f"  Sina returned no data for {sina_sym}, trying EastMoney...")

    return _fetch_eastmoney(secid, period, beg, end)


# ─── Markdown Output ───

def daily_to_md(rows: list, title: str = "中芯国际（688981.SH）") -> str:
    source = "新浪财经/东方财富"
    lines = [
        f"# {title} 日线数据",
        "",
        f"> 数据区间：{rows[0]['datetime']} ~ {rows[-1]['datetime']}，共 {len(rows)} 个交易日",
        f"> 数据来源：{source} | 复权方式：前复权 | 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 数据总览",
        "",
        _compute_summary(rows),
        "",
        "## K线数据表",
        "",
        "| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量(手) | 成交额(元) | 振幅% | 涨跌幅% | 涨跌额 | 换手率% |",
        "|------|------|------|------|------|-----------|-----------|-------|---------|--------|---------|",
    ]

    for r in rows:
        lines.append(
            f"| {r['datetime']} | {r['open']} | {r['close']} | {r['high']} | {r['low']} "
            f"| {r['volume']} | {r['amount']} | {r['amplitude']} | {r['change_pct']} | {r['change']} | {r['turnover']} |"
        )

    lines.append("")
    lines.append(_chanlun_prep_section(rows, "daily"))
    return "\n".join(lines)


def min30_to_md(rows: list, title: str = "中芯国际（688981.SH）") -> str:
    source = "新浪财经/东方财富"
    lines = [
        f"# {title} 30分钟线数据",
        "",
        f"> 数据区间：{rows[0]['datetime']} ~ {rows[-1]['datetime']}，共 {len(rows)} 根K线",
        f"> 数据来源：{source} | 复权方式：前复权 | 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 数据总览",
        "",
        _compute_summary(rows),
        "",
        "## K线数据表",
        "",
        "| 时间 | 开盘 | 收盘 | 最高 | 最低 | 成交量(手) | 成交额(元) | 振幅% | 涨跌幅% | 涨跌额 | 换手率% |",
        "|------|------|------|------|------|-----------|-----------|-------|---------|--------|---------|",
    ]

    for r in rows:
        lines.append(
            f"| {r['datetime']} | {r['open']} | {r['close']} | {r['high']} | {r['low']} "
            f"| {r['volume']} | {r['amount']} | {r['amplitude']} | {r['change_pct']} | {r['change']} | {r['turnover']} |"
        )

    lines.append("")
    lines.append(_chanlun_prep_section(rows, "30min"))
    return "\n".join(lines)


def _compute_summary(rows: list) -> str:
    closes = [float(r["close"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    volumes = [int(r["volume"]) for r in rows]

    max_high = max(highs)
    min_low = min(lows)
    max_high_date = rows[highs.index(max_high)]["datetime"]
    min_low_date = rows[lows.index(min_low)]["datetime"]

    latest = closes[-1]
    first = closes[0]
    total_change = (latest - first) / first * 100

    avg_vol = sum(volumes) / len(volumes)

    return "\n".join([
        f"- **区间最高价**：{max_high}（{max_high_date}）",
        f"- **区间最低价**：{min_low}（{min_low_date}）",
        f"- **最新收盘价**：{latest}",
        f"- **区间涨跌幅**：{total_change:+.2f}%",
        f"- **日均成交量**：{avg_vol:,.0f} 手",
        f"- **K线数量**：{len(rows)}",
    ])


def _chanlun_prep_section(rows: list, freq: str) -> str:
    """Generate Chanlun analysis preparation section."""
    closes = [float(r["close"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]

    lines = [
        "## 缠论分析准备",
        "",
        f"### {freq} 级别关键信息",
        "",
        f"- **K线总数**：{len(rows)}（满足缠论分析最低要求 ≥30）" if len(rows) >= 30 else f"- **K线总数**：{len(rows)}（⚠️ 不足30根，缠论分析精度受限）",
        f"- **价格区间**：{min(lows):.2f} ~ {max(highs):.2f}",
        f"- **最新价格**：{closes[-1]:.2f}",
        "",
        "### 缠论分析待标注项",
        "",
        "- [ ] 顶分型 / 底分型标注",
        "- [ ] 笔的划分",
        "- [ ] 线段的划分",
        "- [ ] 中枢的确定",
        "- [ ] 背驰判断（MACD辅助）",
        "- [ ] 买卖点标注（一买/二买/三买）",
        "",
        "### 数据格式说明",
        "",
        "每根K线包含：时间、开盘、收盘、最高、最低、成交量、成交额、振幅、涨跌幅、涨跌额、换手率。",
        "缠论核心使用：**最高价、最低价**（用于分型/笔/线段）+ **成交量**（辅助判断背驰）。",
    ]
    return "\n".join(lines)


def _aggregate_to_120min(min60_data: list) -> list:
    """Aggregate 60-minute K-lines into 120-minute K-lines.

    A-share has 4 x 60min bars/day -> 2 x 120min bars/day (morning + afternoon).
    Pairs consecutive bars within each trading day.
    """
    from collections import OrderedDict

    daily_groups = OrderedDict()
    for bar in min60_data:
        dt = bar["datetime"]
        date_part = dt.split(" ")[0] if " " in dt else dt[:10]
        daily_groups.setdefault(date_part, []).append(bar)

    result = []
    for _date, bars in daily_groups.items():
        i = 0
        while i + 1 < len(bars):
            bar1 = bars[i]
            bar2 = bars[i + 1]
            vol = int(bar1["volume"]) + int(bar2["volume"])
            try:
                amt = float(bar1["amount"]) + float(bar2["amount"])
            except (ValueError, TypeError):
                amt = 0
            merged = {
                "datetime": bar2["datetime"],
                "open": bar1["open"],
                "close": bar2["close"],
                "high": str(max(float(bar1["high"]), float(bar2["high"]))),
                "low": str(min(float(bar1["low"]), float(bar2["low"]))),
                "volume": str(vol),
                "amount": str(int(amt)),
                "amplitude": "0",
                "change_pct": "0",
                "change": "0",
                "turnover": "0",
            }
            result.append(merged)
            i += 2

    for j in range(len(result)):
        if j > 0:
            prev_c = float(result[j - 1]["close"])
            c = float(result[j]["close"])
            h = float(result[j]["high"])
            l = float(result[j]["low"])
            if prev_c > 0:
                result[j]["amplitude"] = f"{(h - l) / prev_c * 100:.2f}"
                result[j]["change"] = f"{c - prev_c:.4f}"
                result[j]["change_pct"] = f"{(c - prev_c) / prev_c * 100:.2f}"

    return result


def min120_to_md(rows: list, title: str = "ETF") -> str:
    source = "新浪财经/东方财富（由60分钟合成）"
    lines = [
        f"# {title} 120分钟线数据",
        "",
        f"> 数据区间：{rows[0]['datetime']} ~ {rows[-1]['datetime']}，共 {len(rows)} 根K线",
        f"> 数据来源：{source} | 复权方式：前复权 | 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 数据总览",
        "",
        _compute_summary(rows),
        "",
        "## K线数据表",
        "",
        "| 时间 | 开盘 | 收盘 | 最高 | 最低 | 成交量(手) | 成交额(元) | 振幅% | 涨跌幅% | 涨跌额 | 换手率% |",
        "|------|------|------|------|------|-----------|-----------|-------|---------|--------|---------|",
    ]

    for r in rows:
        lines.append(
            f"| {r['datetime']} | {r['open']} | {r['close']} | {r['high']} | {r['low']} "
            f"| {r['volume']} | {r['amount']} | {r['amplitude']} | {r['change_pct']} | {r['change']} | {r['turnover']} |"
        )

    lines.append("")
    lines.append(_chanlun_prep_section(rows, "120min"))
    return "\n".join(lines)


def main():
    symbol = "688981"
    beg = "20250620"
    end = datetime.now().strftime("%Y%m%d")

    base_dir = "/home/chenbohan/Documents/gp/688981_中芯国际"
    os.makedirs(base_dir, exist_ok=True)

    print(f"[1/2] Fetching daily data for {symbol} ({beg} ~ {end})...")
    daily = fetch_kline(symbol, "101", beg, end)
    print(f"  -> Got {len(daily)} daily records")

    if daily:
        md = daily_to_md(daily)
        path = os.path.join(base_dir, "日线数据.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"  -> Written to {path}")

    print(f"[2/2] Fetching 30-min data for {symbol} ({beg} ~ {end})...")
    min30 = fetch_kline(symbol, "30", beg, end)
    print(f"  -> Got {len(min30)} 30-min records")

    if min30:
        md = min30_to_md(min30)
        path = os.path.join(base_dir, "30分钟线数据.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"  -> Written to {path}")

    print(f"\nDone! All files written to {base_dir}/")


if __name__ == "__main__":
    main()
