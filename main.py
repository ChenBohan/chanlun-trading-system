#!/usr/bin/env python3
"""
Chanlun Trading System v2 — Unified Entry Point.

Three-level analysis: Daily (direction) → 30min (buy/sell points) → 5min (timing).

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
    print(f"级别：DF（方向）→ 30F（买卖点）→ 5F（择时）")
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
    """Generate both PC and mobile HTML dashboards from existing data."""
    from src.visualize import generate_dashboard, generate_mobile_dashboard, run_analysis_pipeline

    print("=" * 60)
    print("缠论交易系统 v2 — 可视化仪表盘生成")
    print("=" * 60)

    cache = run_analysis_pipeline(data_dir=args.data_dir)
    generate_dashboard(cache=cache)
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
        ("daily", "daily.csv", "DF"),
        ("30min", "30min.csv", "30F"),
        ("5min", "5min.csv", "5F"),
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
            )
            lines.append(f"=== {idx.etf_code}_{idx.etf_name} 多级别联立 ===")
            lines.append(format_synthesis_report(syn))
            lines.append("")
            print(f"  → 联立: {syn.direction_alignment} | {syn.overall_bias}")

    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nBatch report saved to: {output}")
    print(f"Total lines: {len(lines)}")


def cmd_run(args):
    """Full pipeline: fetch → analyze → dashboard."""
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
        run_analysis_pipeline,
    )

    if getattr(args, 'source', None):
        _df.DATA_SOURCE_PRIMARY = args.source

    print("=" * 60)
    print(f"缠论交易系统 v2 — 完整流水线  |  数据源：{_df.DATA_SOURCE_PRIMARY}")
    print("=" * 60)

    t_start = _time.perf_counter()

    # Step 1: Fetch (Tencent bulk, fast)
    print("\n[Step 1/4] 数据拉取...")
    t0 = _time.perf_counter()
    indices = load_index_watchlist()
    results = fetch_all_indices(
        indices=indices, beg=args.beg,
        delay=args.delay, max_workers=args.workers,
        force=args.force,
    )
    print_fetch_summary(results)

    # Step 1b: Sina supplement for shallow data (skip during trading hours)
    if not getattr(args, 'no_supplement', False):
        from src.data_fetcher import _is_cn_market_closed
        if _is_cn_market_closed():
            print("\n[Step 1b/4] Sina 深度补充（日线 + 分钟线）...")
            supplement_daily_with_sina(results)
            supplement_intraday_with_sina(results)
        else:
            print("\n[Step 1b/4] Sina 深度补充... 盘中跳过（收盘后自动执行）")

    save_fetch_results(results, fmt="csv")
    t_fetch = _time.perf_counter() - t0

    # Step 2: Analyze all
    print("\n[Step 2/4] 缠论分析（多进程）...")
    t0 = _time.perf_counter()
    analyze_workers = min(args.analyze_workers, len(indices))
    cache = run_analysis_pipeline(max_workers=analyze_workers)
    t_analyze = _time.perf_counter() - t0

    # Step 3: Mobile dashboard (reuses analysis cache)
    print("\n[Step 3/4] 生成移动版仪表盘...")
    t0 = _time.perf_counter()
    generate_mobile_dashboard(cache=cache)
    t_mobile = _time.perf_counter() - t0

    t_total = _time.perf_counter() - t_start
    print(f"\n完整流水线执行完毕。")
    print(f"  数据拉取: {t_fetch:.1f}s | 缠论分析: {t_analyze:.1f}s | "
          f"移动仪表盘: {t_mobile:.1f}s | 总计: {t_total:.1f}s")


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
                           choices=["daily", "30min", "5min"],
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
