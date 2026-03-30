"""
Fetch K-line data from East Money API for stocks, indices, and ETFs.
Output: markdown files for Chanlun technical analysis.
"""

import json
import os
import sys
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.parse import urlencode


def _resolve_secid(symbol: str) -> str:
    """Resolve symbol to East Money secid format.
    
    - SH stocks (6xxxxx): 1.symbol
    - SZ stocks (0xxxxx, 3xxxxx): 0.symbol
    - SH indices (000xxx with SH context): 1.symbol
    - SZ indices (399xxx): 0.symbol
    - SH ETFs (51xxxx, 58xxxx): 1.symbol
    - SZ ETFs (15xxxx): 0.symbol
    """
    if symbol.startswith("6") or symbol.startswith("5"):
        return f"1.{symbol}"
    if symbol.startswith("0") or symbol.startswith("3") or symbol.startswith("1"):
        return f"0.{symbol}"
    return f"1.{symbol}"


def fetch_kline(symbol: str, period: str, beg: str, end: str,
                secid_override: str = None) -> list:
    """
    Fetch K-line data from East Money API.
    period: 101=daily, 30=30min, 60=60min, 5=5min
    beg/end: YYYYMMDD
    secid_override: directly specify secid (e.g. "1.000300" for index)
    """
    secid = secid_override or _resolve_secid(symbol)

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
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})

    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    if not data.get("data") or not data["data"].get("klines"):
        print(f"Warning: no data returned for {secid} period={period}")
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


def fetch_index_kline(index_code: str, period: str, beg: str, end: str) -> list:
    """Fetch index K-line data. Handles SH/SZ index secid resolution."""
    if index_code.startswith("399"):
        secid = f"0.{index_code}"
    else:
        secid = f"1.{index_code}"
    return fetch_kline(index_code, period, beg, end, secid_override=secid)


def daily_to_md(rows: list, title: str = "中芯国际（688981.SH）") -> str:
    lines = [
        f"# {title} 日线数据",
        "",
        f"> 数据区间：{rows[0]['datetime']} ~ {rows[-1]['datetime']}，共 {len(rows)} 个交易日",
        f"> 数据来源：东方财富 | 复权方式：前复权 | 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
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
    lines = [
        f"# {title} 30分钟线数据",
        "",
        f"> 数据区间：{rows[0]['datetime']} ~ {rows[-1]['datetime']}，共 {len(rows)} 根K线",
        f"> 数据来源：东方财富 | 复权方式：前复权 | 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
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

    readme = os.path.join(base_dir, "README.md")
    with open(readme, "w", encoding="utf-8") as f:
        f.write("\n".join([
            "# 中芯国际（688981.SH）缠论分析工作区",
            "",
            "## 文件结构",
            "",
            f"- `日线数据.md` — 日线K线数据（{len(daily)} 条）",
            f"- `30分钟线数据.md` — 30分钟K线数据（{len(min30)} 条）",
            "",
            "## 数据信息",
            "",
            f"- **股票代码**：688981.SH（科创板）",
            f"- **数据区间**：2025-06-20 ~ {datetime.now().strftime('%Y-%m-%d')}",
            f"- **复权方式**：前复权",
            f"- **数据来源**：东方财富",
            "",
            "## 缠论分析计划",
            "",
            "1. **日线级别**：确定大级别走势类型（趋势/盘整）",
            "2. **30分钟级别**：精确标注分型、笔、线段、中枢",
            "3. **多级别联立**：日线定方向，30分钟找买卖点",
            "",
            "## 分析步骤",
            "",
            "1. 顶底分型识别",
            "2. 笔的划分（相邻分型间无包含关系的K线 ≥1）",
            "3. 线段的划分（至少3笔）",
            "4. 中枢的确定（至少3段重叠区间）",
            "5. 背驰判断（MACD面积 / 成交量配合）",
            "6. 买卖点标注",
        ]))

    print(f"\nDone! All files written to {base_dir}/")


if __name__ == "__main__":
    main()
