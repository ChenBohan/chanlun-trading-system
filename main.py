#!/usr/bin/env python3
"""
Chanlun Trading System v2 — Unified Entry Point.

Five-level analysis: Weekly (macro) → Daily (direction) → 30min (buy/sell points) → 5min (timing) → 1min (interval nesting, max 5 days).

Usage:
  python main.py fetch                    # Fetch K-line data for all indices
  python main.py analyze <csv_file>       # Analyze a single CSV file
  python main.py dashboard                # Generate HTML dashboard (fetch + analyze)
  python main.py run                      # Full pipeline: fetch → analyze → dashboard
"""

from __future__ import annotations

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)


def cmd_fetch(args):
    """Fetch K-line data for all configured indices."""
    from src.data_fetcher import (
        load_index_watchlist, fetch_all_indices,
        save_fetch_results, print_fetch_summary,
        supplement_daily_with_sina,
        supplement_intraday_with_sina,
        _is_cn_market_closed,
    )
    import src.data_fetcher as _df

    if getattr(args, 'source', None):
        _df.DATA_SOURCE_PRIMARY = args.source

    print("=" * 60)
    print("缠论交易系统 v2 — 数据拉取")
    print(f"级别：DF（方向）→ 30F（买卖点）→ 5F（择时）→ 1F（区间套）")
    print(f"起始日期：{args.beg}  |  数据源：{_df.DATA_SOURCE_PRIMARY}")
    print("=" * 60)

    indices = load_index_watchlist()
    print(f"\n加载 {len(indices)} 个指数标的：")
    for idx in indices:
        print(f"  {idx.index_name} ({idx.etf_name} {idx.etf_code}) [{idx.category}]")

    results = fetch_all_indices(
        indices=indices, beg=args.beg,
        delay=args.delay, max_workers=args.workers,
        force=getattr(args, 'force', False),
    )
    print_fetch_summary(results)

    if not getattr(args, 'no_supplement', False) and _is_cn_market_closed():
        print("\nSina 深度补充（日线 + 分钟线）...")
        supplement_daily_with_sina(results)
        supplement_intraday_with_sina(results)

    fmt = args.format
    print(f"\n保存数据（格式：{fmt}）...")
    save_fetch_results(results, fmt=fmt)
    print("数据拉取完成。")


def cmd_analyze(args):
    """Analyze a single CSV file with the Chanlun engine."""
    from src.chanlun_engine import analyze_from_csv, format_report

    if not os.path.exists(args.csv_file):
        print(f"File not found: {args.csv_file}", file=sys.stderr)
        sys.exit(1)

    result = analyze_from_csv(args.csv_file, args.level)
    print(format_report(result))


def cmd_dashboard(args):
    """Generate mobile HTML dashboard from existing data."""
    from src.visualize import generate_mobile_dashboard, run_analysis_pipeline

    print("=" * 60)
    print("缠论交易系统 v2 — 可视化仪表盘生成")
    print("=" * 60)

    cache = run_analysis_pipeline(data_dir=args.data_dir)
    generate_mobile_dashboard(data_dir=args.data_dir, output_path=args.output, cache=cache)


def cmd_batch(args):
    """Generate batch text report for all indices × all levels."""
    from src.data_fetcher import load_index_watchlist
    from src.chanlun_engine import (
        load_bars_from_csv, analyze, format_report,
        synthesize_multi_level, format_synthesis_report,
    )

    print("=" * 60)
    print("缠论交易系统 v2 — 批量文字报告")
    print("=" * 60)

    indices = load_index_watchlist()
    data_dir = args.data_dir or os.path.join(PROJECT_ROOT, "data")
    output = args.output or os.path.join(PROJECT_ROOT, "reports", "batch_report.txt")
    os.makedirs(os.path.dirname(output), exist_ok=True)

    levels = [
        ("weekly", "weekly.csv", "WF"),
        ("daily", "daily.csv", "DF"),
        ("30min", "30min.csv", "30F"),
        ("5min", "5min.csv", "5F"),
        ("1min", "1min.csv", "1F"),
    ]

    lines = []
    for idx in indices:
        etf_dir = os.path.join(data_dir, f"{idx.etf_code}_{idx.etf_name}")
        level_results = {}
        for level_key, csv_name, level_label in levels:
            csv_path = os.path.join(etf_dir, csv_name)
            if not os.path.exists(csv_path):
                print(f"  [SKIP] {idx.etf_name} {level_label}: {csv_path} not found")
                continue
            result = analyze(load_bars_from_csv(csv_path), level_label)
            level_results[level_key] = result
            lines.append(f"=== {idx.etf_code}_{idx.etf_name} {level_label} ===")
            lines.append(format_report(result))
            lines.append("")
            tc = result.trend_completion
            tc_status = tc.get("status", "?") if tc else "?"
            print(f"  [{idx.etf_name} {level_label}] {result.trend} | {tc_status} | "
                  f"{len(result.buy_sell_points)} signals")

        if "daily" in level_results:
            syn = synthesize_multi_level(
                level_results["daily"],
                level_results.get("30min"),
                level_results.get("5min"),
                weekly=level_results.get("weekly"),
            )
            lines.append(f"=== {idx.etf_code}_{idx.etf_name} 多级别联立 ===")
            lines.append(format_synthesis_report(syn))
            lines.append("")
            print(f"  → 联立: {syn.direction_alignment} | {syn.overall_bias}")

    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nBatch report saved to: {output}")
    print(f"Total lines: {len(lines)}")


def _fetch_batch_realtime_prices(indices) -> dict:
    """Fetch real-time prices for all stocks via Sina batch quote API.

    Returns dict: {etf_code: latest_price} for stocks with valid quotes.
    """
    from urllib.request import Request, urlopen
    from src.data_fetcher import _sina_symbol

    prices = {}
    symbols = []
    code_map = {}  # sina_sym -> etf_code

    for idx in indices:
        sina_sym = _sina_symbol(idx.etf_code, idx.market)
        symbols.append(sina_sym)
        code_map[sina_sym] = idx.etf_code

    # Sina supports batch: up to ~800 symbols per request
    BATCH_SIZE = 500
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        url = f"https://hq.sinajs.cn/list={','.join(batch)}"
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("gbk")
            for line in raw.strip().split("\n"):
                if '="' not in line:
                    continue
                var_part = line.split("=")[0]
                sina_sym = var_part.split("_")[-1]
                content = line.split('="')[1].rstrip('";\n')
                if not content:
                    continue
                parts = content.split(",")
                if len(parts) < 4:
                    continue
                try:
                    cur_price = float(parts[3])
                    if cur_price > 0:
                        etf_code = code_map.get(sina_sym, "")
                        if etf_code:
                            prices[etf_code] = cur_price
                except (ValueError, IndexError):
                    continue
        except Exception:
            continue

    return prices


def _filter_by_ma250(indices, data_dir: str) -> tuple[list, dict]:
    """Filter indices by MA250: fetch real-time price, compare with MA250.

    ETFs (type "broad" or "sector") bypass the filter entirely.
    Only individual stocks (type "stock") are subject to MA250 filtering.

    Returns (filtered_indices, filter_stats) where filter_stats contains
    total_count, selected_count, and per-symbol details.
    """
    import csv as _csv

    MA_PERIOD = 250
    BYPASS_TYPES = {"broad", "sector"}

    # Separate ETFs and stocks
    etfs = [idx for idx in indices if getattr(idx, 'type', '') in BYPASS_TYPES]
    stocks = [idx for idx in indices if getattr(idx, 'type', '') not in BYPASS_TYPES]

    stats = {"total": len(indices), "selected": 0, "above": [], "below": [],
             "etf_bypass": len(etfs)}

    # Fetch real-time prices for all stocks
    print(f"  获取 {len(stocks)} 只个股实时行情...")
    realtime_prices = _fetch_batch_realtime_prices(stocks)
    print(f"  获取到 {len(realtime_prices)} 只实时价格")

    selected = list(etfs)

    for idx in stocks:
        # Use real-time price if available, otherwise fall back to disk data
        latest_price = realtime_prices.get(idx.etf_code)

        idx_dir = os.path.join(data_dir, f"{idx.etf_code}_{idx.etf_name}")
        csv_path = os.path.join(idx_dir, "daily.csv")
        if not os.path.exists(csv_path):
            selected.append(idx)
            continue

        closes = []
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                closes.append(float(row["close"]))

        if len(closes) < MA_PERIOD:
            selected.append(idx)
            continue

        # Use real-time price if available; otherwise use last close from CSV
        if latest_price is None:
            latest_price = closes[-1]

        ma250 = sum(closes[-MA_PERIOD:]) / MA_PERIOD

        if latest_price >= ma250:
            selected.append(idx)
            stats["above"].append((idx.etf_code, idx.etf_name, latest_price, ma250))
        else:
            stats["below"].append((idx.etf_code, idx.etf_name, latest_price, ma250))

    stats["selected"] = len(selected)
    return selected, stats


def cmd_run(args):
    """Full pipeline: daily fetch → MA250 filter → fetch filtered → analyze → dashboard."""
    import time as _time
    from src.data_fetcher import (
        load_index_watchlist, fetch_all_indices,
        save_fetch_results, print_fetch_summary,
        supplement_daily_with_sina,
        supplement_intraday_with_sina,
    )
    import src.data_fetcher as _df
    from src.visualize import (
        generate_mobile_dashboard,
        generate_live_js_from_cache,
        run_analysis_pipeline,
    )

    if getattr(args, 'source', None):
        _df.DATA_SOURCE_PRIMARY = args.source

    from src.data_fetcher import _is_cn_market_closed
    is_full_mode = getattr(args, 'full', False) or _is_cn_market_closed()
    mode_label = "全量(盘后)" if is_full_mode else "增量(盘中)"

    print("=" * 60)
    print(f"缠论交易系统 v2 — 完整流水线  |  数据源：{_df.DATA_SOURCE_PRIMARY}")
    print(f"模式：{mode_label}")
    print("=" * 60)

    t_start = _time.perf_counter()
    indices = load_index_watchlist()
    data_dir = os.path.join(PROJECT_ROOT, "data")

    # Step 1: Fetch data for ALL stocks (needed for MA250 calculation)
    # Weekly data only in full mode (after market close)
    if is_full_mode:
        step1_periods = ["weekly", "daily"]
        step1_label = "周线+日线"
    else:
        step1_periods = ["daily"]
        step1_label = "日线"
    print(f"\n[Step 1/5] 拉取全量{step1_label}数据（{len(indices)} 标的）...")
    t0 = _time.perf_counter()
    daily_results = fetch_all_indices(
        indices=indices, beg=args.beg,
        delay=args.delay, max_workers=args.workers,
        force=args.force,
        periods=step1_periods,
    )
    save_fetch_results(daily_results, fmt="csv")
    t_daily = _time.perf_counter() - t0
    print(f"  {step1_label}拉取完成: {t_daily:.1f}s")

    # Step 2: MA250 filter (real-time price vs MA250 from daily data)
    print("\n[Step 2/5] 年线过滤（实时价格 vs MA250）...")
    filtered_indices, filter_stats = _filter_by_ma250(indices, data_dir)
    print(f"  总股票池: {filter_stats['total']} | "
          f"ETF直通: {filter_stats.get('etf_bypass', 0)} | "
          f"个股年线上方: {len(filter_stats['above'])} | "
          f"个股年线下方（排除）: {len(filter_stats['below'])} | "
          f"入选: {filter_stats['selected']}")

    # Step 3: Fetch 30min + 5min + 1min for filtered stocks only
    print(f"\n[Step 3/5] 拉取入选标的分钟线（{filter_stats['selected']} 标的）...")
    t0 = _time.perf_counter()
    intraday_results = fetch_all_indices(
        indices=filtered_indices, beg=args.beg,
        delay=args.delay, max_workers=args.workers,
        force=args.force,
        periods=["30min", "5min", "1min"],
    )
    save_fetch_results(intraday_results, fmt="csv")
    t_intraday = _time.perf_counter() - t0

    # Step 3b: Sina supplement for shallow data (only in full mode)
    if not getattr(args, 'no_supplement', False) and is_full_mode:
        print(f"\n[Step 3b/5] Sina 深度补充...")
        supplement_daily_with_sina(daily_results)
        supplement_intraday_with_sina(intraday_results)
        print("  保存补充后的数据...")
        save_fetch_results(daily_results, fmt="csv")
        save_fetch_results(intraday_results, fmt="csv")
    elif not is_full_mode:
        print(f"\n[Step 3b/5] Sina 深度补充... 盘中模式跳过")

    # Step 4: Analyze filtered pool
    print(f"\n[Step 4/5] 缠论分析（{filter_stats['selected']} 标的，多进程）...")
    t0 = _time.perf_counter()
    analyze_workers = min(args.analyze_workers, len(filtered_indices))
    cache = run_analysis_pipeline(
        max_workers=analyze_workers,
        indices_override=filtered_indices,
    )
    cache["filter_stats"] = filter_stats
    t_analyze = _time.perf_counter() - t0

    # Step 5: Generate output
    t0 = _time.perf_counter()
    if is_full_mode:
        # Full mode: rewrite all .js files, save baseline, remove live.js
        print("\n[Step 5/5] 全量模式 — 重写主数据文件 + 仪表盘...")
        generate_mobile_dashboard(cache=cache)
        t_output = _time.perf_counter() - t0
        print(f"  主.js文件 + baseline 已更新，live.js 已清理")
    else:
        # Intraday mode: only generate live.js (delta from last full deploy baseline)
        print("\n[Step 5/5] 盘中模式 — 生成 live.js（增量delta）...")
        live_path = generate_live_js_from_cache(cache=cache)
        t_output = _time.perf_counter() - t0
        if live_path:
            size_kb = os.path.getsize(live_path) / 1024
            print(f"  live.js 已生成: {size_kb:.0f} KB")
        else:
            print("  无 baseline，回退到全量模式...")
            t0b = _time.perf_counter()
            generate_mobile_dashboard(cache=cache)
            t_output += _time.perf_counter() - t0b

    t_total = _time.perf_counter() - t_start
    print(f"\n完整流水线执行完毕。（{mode_label}）")
    print(f"  日线拉取: {t_daily:.1f}s | 分钟线拉取: {t_intraday:.1f}s | "
          f"缠论分析: {t_analyze:.1f}s | 输出: {t_output:.1f}s | 总计: {t_total:.1f}s")


def main():
    parser = argparse.ArgumentParser(
        description="缠论交易系统 v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # fetch
    p_fetch = sub.add_parser("fetch", help="拉取K线数据")
    p_fetch.add_argument("--beg", default="20100101", help="起始日期 YYYYMMDD")
    p_fetch.add_argument("--format", choices=["csv", "md"], default="csv",
                         help="输出格式 (默认: csv)")
    p_fetch.add_argument("--delay", type=float, default=0.2,
                         help="API 调用间隔秒数 (默认: 0.2)")
    p_fetch.add_argument("--workers", type=int, default=8,
                         help="并发线程数 (默认: 8)")
    p_fetch.add_argument("--force", action="store_true",
                         help="跳过增量检查，强制全量拉取")
    p_fetch.add_argument("--source", choices=["sina", "eastmoney"], default=None,
                         help="主数据源 (默认: 代码中的配置)")

    # analyze
    p_analyze = sub.add_parser("analyze", help="分析单个CSV文件")
    p_analyze.add_argument("csv_file", help="K线CSV文件路径")
    p_analyze.add_argument("--level", default="daily",
                           choices=["daily", "30min", "5min", "1min"],
                           help="分析级别 (默认: daily)")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="生成移动版HTML仪表盘")
    p_dash.add_argument("--data-dir", default=None, help="数据目录")
    p_dash.add_argument("--output", default=None, help="输出HTML路径")

    # batch
    p_batch = sub.add_parser("batch", help="生成批量文字报告")
    p_batch.add_argument("--data-dir", default=None, help="数据目录")
    p_batch.add_argument("--output", default=None, help="输出文件路径")

    # run
    p_run = sub.add_parser("run", help="完整流水线: 拉取 → 分析 → 仪表盘")
    p_run.add_argument("--beg", default="20100101", help="起始日期 YYYYMMDD")
    p_run.add_argument("--delay", type=float, default=0.2,
                        help="API 调用间隔秒数 (默认: 0.2)")
    p_run.add_argument("--workers", type=int, default=8,
                        help="数据拉取并发线程数 (默认: 8)")
    _default_analyze = max(4, (os.cpu_count() or 4) // 2)
    p_run.add_argument("--analyze-workers", type=int, default=_default_analyze,
                        dest="analyze_workers",
                        help=f"分析阶段并行进程数 (默认: {_default_analyze}, 0=串行)")
    p_run.add_argument("--force", action="store_true",
                        help="跳过增量检查，强制全量拉取")
    p_run.add_argument("--source", choices=["sina", "eastmoney"], default=None,
                        help="主数据源 (默认: 代码中的配置)")
    p_run.add_argument("--full", action="store_true",
                        help="强制全量模式（重写主.js+baseline，默认收盘后自动触发）")

    # backfill
    p_backfill = sub.add_parser("backfill", help="回填历史信号快照")
    p_backfill.add_argument("--data-dir", default=None, help="数据目录")
    p_backfill.add_argument("--workers", type=int,
                            default=max(4, (os.cpu_count() or 4) // 2),
                            help="并行进程数")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    def cmd_backfill(a):
        from src.visualize import backfill_signal_snapshots
        print("=" * 60)
        print("缠论交易系统 v2 — 历史信号快照回填")
        print("=" * 60)
        backfill_signal_snapshots(data_dir=a.data_dir, max_workers=a.workers)

    dispatch = {
        "fetch": cmd_fetch,
        "analyze": cmd_analyze,
        "batch": cmd_batch,
        "dashboard": cmd_dashboard,
        "run": cmd_run,
        "backfill": cmd_backfill,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
