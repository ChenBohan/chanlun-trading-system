"""
Chanlun Trading System v2.

Modules:
  - data_fetcher:   K-line data fetching (Sina + EastMoney APIs)
  - chanlun_engine: Chanlun analysis pipeline (MACD → inclusion → fractal →
                    stroke → segment → hub → divergence → buy/sell points)
  - visualize:      ECharts HTML dashboard generation
"""

from .data_fetcher import (
    KlineBar,
    IndexConfig,
    FetchResult,
    fetch_kline,
    fetch_all_indices,
    load_index_watchlist,
    load_settings,
    save_fetch_results,
    print_fetch_summary,
)

from .chanlun_engine import (
    RawBar,
    MergedBar,
    Fractal,
    Stroke,
    Segment,
    Hub,
    SegHub,
    BuySellPoint,
    AnalysisResult,
    IntervalNest,
    MultiLevelSynthesis,
    load_bars_from_csv,
    analyze,
    analyze_from_csv,
    find_interval_nests,
    synthesize_multi_level,
    format_report,
    format_synthesis_report,
)

from .visualize import generate_dashboard, generate_mobile_dashboard, run_analysis_pipeline

__all__ = [
    # data_fetcher
    "KlineBar", "IndexConfig", "FetchResult",
    "fetch_kline", "fetch_all_indices",
    "load_index_watchlist", "load_settings",
    "save_fetch_results", "print_fetch_summary",
    # chanlun_engine
    "RawBar", "MergedBar", "Fractal", "Stroke", "Segment",
    "Hub", "SegHub", "BuySellPoint", "AnalysisResult",
    "IntervalNest", "MultiLevelSynthesis",
    "load_bars_from_csv", "analyze", "analyze_from_csv",
    "find_interval_nests", "synthesize_multi_level",
    "format_report", "format_synthesis_report",
    # visualize
    "generate_dashboard",
    "generate_mobile_dashboard",
    "run_analysis_pipeline",
]
