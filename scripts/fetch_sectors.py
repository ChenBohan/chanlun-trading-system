#!/usr/bin/env python3
"""
Fetch comprehensive A-share industry and concept classifications
with top leader stocks for each category.

Data sources:
  1. East Money (push2 API) — more comprehensive (400+ concepts), but may rate-limit
  2. Sina Finance — stable fallback (84 industries, 175+ concepts)

Usage:
  python scripts/fetch_sectors.py                   # Auto: try EM, fallback to Sina
  python scripts/fetch_sectors.py --source sina      # Force Sina only
  python scripts/fetch_sectors.py --source eastmoney # Force East Money only
  python scripts/fetch_sectors.py --top 5            # Top 5 leaders per board
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen, Request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "config", "sector_classification.json")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# CSRC major industry categories (证监会行业大类)
CSRC_MAJOR_CATEGORIES = {
    "ZA": "农、林、牧、渔业",
    "ZB": "采矿业",
    "ZC": "制造业",
    "ZD": "电力、热力、燃气及水生产和供应业",
    "ZE": "建筑业",
    "ZF": "批发和零售业",
    "ZG": "交通运输、仓储和邮政业",
    "ZH": "住宿和餐饮业",
    "ZI": "信息传输、软件和信息技术服务业",
    "ZJ": "金融业",
    "ZK": "房地产业",
    "ZL": "租赁和商务服务业",
    "ZM": "科学研究和技术服务业",
    "ZN": "水利、环境和公共设施管理业",
    "ZO": "居民服务、修理和其他服务业",
    "ZP": "教育",
    "ZQ": "卫生和社会工作",
    "ZR": "文化、体育和娱乐业",
    "ZS": "综合",
}


# ════════════════════════════════════════════════════════════════════
# Sina Finance data fetching
# ════════════════════════════════════════════════════════════════════

def _sina_fetch_boards(param: str) -> dict:
    """Fetch board list: param='industry' or 'class' (concepts)."""
    url = f"https://money.finance.sina.com.cn/q/view/newFLJK.php?param={param}"
    headers = {**_HEADERS, "Referer": "https://finance.sina.com.cn"}
    req = Request(url, headers=headers)
    with urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("gbk")
    match = re.search(r"=\s*(\{.+\})", raw)
    if not match:
        return {}
    return json.loads(match.group(1))


def _sina_parse_entry(key: str, val: str) -> dict | None:
    """Parse Sina board entry: key,name,count,...,leader_code,...,leader_name."""
    parts = val.split(",")
    if len(parts) < 13:
        return None
    code = parts[8]
    market, clean_code = "", code
    if code.startswith("sh"):
        market, clean_code = "SH", code[2:]
    elif code.startswith("sz"):
        market, clean_code = "SZ", code[2:]

    return {
        "key": key,
        "name": parts[1],
        "stock_count": int(parts[2]) if parts[2].isdigit() else 0,
        "leader": {"code": clean_code, "market": market, "name": parts[12]},
    }


def _sina_fetch_top_stocks(node_key: str, top_n: int = 3) -> list[dict]:
    """Fetch top stocks sorted by market cap within a Sina board node."""
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData?"
        f"page=1&num={top_n}&sort=nmc&asc=0&node={node_key}&symbol=&_s_r_a=auto"
    )
    headers = {**_HEADERS, "Referer": "https://finance.sina.com.cn"}
    req = Request(url, headers=headers)
    with urlopen(req, timeout=15) as resp:
        raw = resp.read().decode()
    if not raw or raw[0] != "[":
        return []
    items = json.loads(raw)
    stocks = []
    for item in items:
        code = item.get("code", "")
        market = "SH" if code.startswith(("6", "5")) else "SZ"
        stocks.append({
            "code": code,
            "market": market,
            "name": item.get("name", ""),
            "market_cap_yi": round(item.get("nmc", 0) / 1e4, 1) if item.get("nmc") else 0,
        })
    return stocks


def fetch_from_sina(top_n: int = 3) -> dict:
    """Full Sina data fetch: industries + concepts + top stocks."""
    print("📡 Fetching from Sina Finance...")

    # Industries (证监会行业分类)
    print("  Fetching industry boards...")
    ind_raw = _sina_fetch_boards("industry")
    industries = []
    for key, val in ind_raw.items():
        entry = _sina_parse_entry(key, val)
        if entry:
            code_part = key.replace("hangye_", "")
            major_code = code_part[:2]
            entry["major_category"] = CSRC_MAJOR_CATEGORIES.get(major_code, "")
            entry["major_code"] = major_code
            industries.append(entry)
    print(f"    Got {len(industries)} industry boards")

    # Concepts
    print("  Fetching concept boards...")
    con_raw = _sina_fetch_boards("class")
    concepts = []
    for key, val in con_raw.items():
        entry = _sina_parse_entry(key, val)
        if entry:
            concepts.append(entry)
    print(f"    Got {len(concepts)} concept boards")

    # Top stocks for industries
    total = len(industries) + len(concepts)
    done = 0
    print(f"  Fetching top {top_n} stocks for {len(industries)} industries...")
    for ind in industries:
        try:
            ind["top_stocks"] = _sina_fetch_top_stocks(ind["key"], top_n)
        except Exception:
            ind["top_stocks"] = []
        done += 1
        if done % 30 == 0:
            print(f"    [{done}/{total}] processed")
        time.sleep(0.08)

    # Top stocks for concepts
    print(f"  Fetching top {top_n} stocks for {len(concepts)} concepts...")
    for con in concepts:
        try:
            con["top_stocks"] = _sina_fetch_top_stocks(con["key"], top_n)
        except Exception:
            con["top_stocks"] = []
        done += 1
        if done % 30 == 0:
            print(f"    [{done}/{total}] processed")
        time.sleep(0.08)

    return {
        "source": "sina_finance",
        "update_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "classification_type": "CSRC (证监会行业分类)",
        "major_categories": CSRC_MAJOR_CATEGORIES,
        "industries": sorted(industries, key=lambda x: x.get("key", "")),
        "concepts": sorted(concepts, key=lambda x: x.get("stock_count", 0), reverse=True),
    }


# ════════════════════════════════════════════════════════════════════
# East Money data fetching
# ════════════════════════════════════════════════════════════════════

def _em_fetch_boards(board_type: int, page_size: int = 500) -> list[dict]:
    """Fetch boards: board_type 2=industry, 3=concept."""
    params = {
        "pn": "1", "pz": str(page_size), "po": "1", "np": "1",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": f"m:90+t:{board_type}",
        "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f20",
    }
    url = f"https://push2.eastmoney.com/api/qt/clist/get?{urlencode(params)}"
    headers = {**_HEADERS, "Referer": "https://quote.eastmoney.com"}
    req = Request(url, headers=headers)
    with urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    return data.get("data", {}).get("diff", [])


def _em_fetch_board_stocks(board_code: str, top_n: int = 3) -> list[dict]:
    """Fetch top stocks in an EM board, sorted by market cap (f20)."""
    params = {
        "pn": "1", "pz": str(top_n), "po": "1", "np": "1",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fltt": "2", "invt": "2",
        "fid": "f20",
        "fs": f"b:{board_code}+f:!50",
        "fields": "f2,f3,f12,f14,f20",
    }
    url = f"https://push2.eastmoney.com/api/qt/clist/get?{urlencode(params)}"
    headers = {**_HEADERS, "Referer": "https://quote.eastmoney.com"}
    req = Request(url, headers=headers)
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    items = data.get("data", {}).get("diff", [])
    stocks = []
    for item in items:
        code = str(item.get("f12", ""))
        market = "SH" if code.startswith(("6", "5")) else "SZ"
        cap = item.get("f20", 0) or 0
        stocks.append({
            "code": code,
            "market": market,
            "name": item.get("f14", ""),
            "market_cap_yi": round(cap / 1e8, 1) if cap else 0,
        })
    return stocks


def fetch_from_eastmoney(top_n: int = 3) -> dict:
    """Full East Money data fetch: industries + concepts + top stocks."""
    print("📡 Fetching from East Money...")

    print("  Fetching industry boards...")
    ind_raw = _em_fetch_boards(2)
    industries = []
    for item in ind_raw:
        name = item.get("f14", "")
        industries.append({
            "key": item.get("f12", ""),
            "name": name,
            "stock_count": (item.get("f104", 0) or 0) + (item.get("f105", 0) or 0),
            "up_count": item.get("f104", 0),
            "down_count": item.get("f105", 0),
        })
    print(f"    Got {len(industries)} industry boards")

    time.sleep(0.2)

    print("  Fetching concept boards...")
    con_raw = _em_fetch_boards(3)
    concepts = []
    for item in con_raw:
        concepts.append({
            "key": item.get("f12", ""),
            "name": item.get("f14", ""),
            "stock_count": (item.get("f104", 0) or 0) + (item.get("f105", 0) or 0),
            "up_count": item.get("f104", 0),
            "down_count": item.get("f105", 0),
        })
    print(f"    Got {len(concepts)} concept boards")

    # Fetch top stocks
    total = len(industries) + len(concepts)
    done = 0
    print(f"  Fetching top {top_n} stocks for {len(industries)} industries...")
    for board in industries:
        try:
            board["top_stocks"] = _em_fetch_board_stocks(board["key"], top_n)
            time.sleep(0.15)
        except Exception:
            board["top_stocks"] = []
        done += 1
        if done % 30 == 0:
            print(f"    [{done}/{total}] processed")

    print(f"  Fetching top {top_n} stocks for {len(concepts)} concepts...")
    for board in concepts:
        try:
            board["top_stocks"] = _em_fetch_board_stocks(board["key"], top_n)
            time.sleep(0.15)
        except Exception:
            board["top_stocks"] = []
        done += 1
        if done % 30 == 0:
            print(f"    [{done}/{total}] processed")

    return {
        "source": "eastmoney",
        "update_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "classification_type": "EastMoney (东方财富板块分类)",
        "industries": sorted(industries, key=lambda x: x.get("stock_count", 0), reverse=True),
        "concepts": sorted(concepts, key=lambda x: x.get("stock_count", 0), reverse=True),
    }


# ════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════

def print_summary(data: dict):
    """Print a summary of fetched data."""
    ind = data.get("industries", [])
    con = data.get("concepts", [])
    src = data.get("source", "?")
    cls_type = data.get("classification_type", "")

    print(f"\n{'=' * 60}")
    print(f"A股板块分类数据  |  数据源: {src}")
    print(f"分类体系: {cls_type}")
    print(f"更新时间: {data.get('update_time', '?')}")
    print(f"{'=' * 60}")

    print(f"\n📊 行业板块: {len(ind)} 个")
    ind_with_top = sum(1 for i in ind if i.get("top_stocks"))
    print(f"   有龙头股数据: {ind_with_top} 个")

    # Show top industries by stock count
    sorted_ind = sorted(ind, key=lambda x: x.get("stock_count", 0), reverse=True)
    for i in sorted_ind[:8]:
        top = i.get("top_stocks", [])
        top_str = ", ".join(f"{s['name']}" for s in top[:3])
        major = i.get("major_category", "")
        prefix = f"[{major}] " if major else ""
        print(f"   {prefix}{i['name']:20s} {i.get('stock_count', 0):4d}只  Top: {top_str}")
    print(f"   ... (共 {len(ind)} 个)")

    print(f"\n💡 概念板块: {len(con)} 个")
    con_with_top = sum(1 for c in con if c.get("top_stocks"))
    print(f"   有龙头股数据: {con_with_top} 个")

    sorted_con = sorted(con, key=lambda x: x.get("stock_count", 0), reverse=True)
    for c in sorted_con[:8]:
        top = c.get("top_stocks", [])
        top_str = ", ".join(f"{s['name']}" for s in top[:3])
        print(f"   {c['name']:20s} {c.get('stock_count', 0):4d}只  Top: {top_str}")
    print(f"   ... (共 {len(con)} 个)")


def main():
    parser = argparse.ArgumentParser(description="获取A股行业/概念板块分类及龙头股")
    parser.add_argument("--source", choices=["auto", "sina", "eastmoney"],
                        default="auto", help="数据源 (默认: auto)")
    parser.add_argument("--top", type=int, default=3,
                        help="每个板块获取的龙头股数量 (默认: 3)")
    parser.add_argument("--output", default=OUTPUT_PATH,
                        help=f"输出JSON路径 (默认: {OUTPUT_PATH})")
    args = parser.parse_args()

    data = None

    if args.source in ("auto", "eastmoney"):
        try:
            data = fetch_from_eastmoney(top_n=args.top)
        except Exception as e:
            if args.source == "eastmoney":
                print(f"❌ East Money failed: {e}")
                sys.exit(1)
            print(f"⚠️  East Money failed ({e}), falling back to Sina...")

    if data is None and args.source in ("auto", "sina"):
        try:
            data = fetch_from_sina(top_n=args.top)
        except Exception as e:
            print(f"❌ Sina also failed: {e}")
            sys.exit(1)

    if data is None:
        print("❌ No data fetched")
        sys.exit(1)

    # Save
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print_summary(data)
    print(f"\n✅ Saved to: {args.output}")


if __name__ == "__main__":
    main()
