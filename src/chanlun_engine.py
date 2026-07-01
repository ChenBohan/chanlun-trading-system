"""
Chanlun (缠论) Analysis Engine v2.

Complete pipeline: K-line → MACD → Inclusion → Fractal → Stroke → Segment →
Hub → Trend → Divergence → Buy/Sell Points.

Theory sources:
  - 缠论108课操作框架 (knowledge/缠论108课操作框架.md)
  - 图解缠论操作框架 (knowledge/图解缠论操作框架.md)
  - 缠论解析操作框架 (knowledge/缠论解析操作框架.md)
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Optional


# ════════════════════════════════════════════════════════════════════
# 1. Data Structures
# ════════════════════════════════════════════════════════════════════

@dataclass
class RawBar:
    """Raw K-line bar loaded from CSV."""
    idx: int
    dt: str
    open: float
    close: float
    high: float
    low: float
    volume: int
    # MACD fields (computed later)
    ema12: float = 0.0
    ema26: float = 0.0
    dif: float = 0.0
    dea: float = 0.0
    macd_hist: float = 0.0   # histogram = 2*(DIF-DEA)


@dataclass
class MergedBar:
    """K-line bar after inclusion processing (包含处理)."""
    idx: int
    high: float
    low: float
    start_raw: int        # first raw bar index
    end_raw: int          # last raw bar index
    direction: int        # 1=up merge, -1=down merge, 0=initial
    dates: list[str] = field(default_factory=list)
    high_dt: str = ""     # dt of the raw bar that achieved the high price
    low_dt: str = ""      # dt of the raw bar that achieved the low price


@dataclass
class Fractal:
    """Top or bottom fractal (顶分型 / 底分型)."""
    type: str             # "top" or "bottom"
    mk_idx: int           # index in merged bars
    high: float
    low: float
    dt: str               # date/time of the middle bar


@dataclass
class Stroke:
    """A stroke (笔) connecting adjacent top↔bottom fractals."""
    idx: int
    start: Fractal
    end: Fractal
    direction: int        # 1=up (bottom→top), -1=down (top→bottom)
    mk_span: int          # merged bar count between start/end
    macd_area: float = 0.0
    dif_extreme: float = 0.0   # max DIF (up) or min DIF (down) within stroke
    hist_peak: float = 0.0     # max |histogram| bar within stroke
    avg_volume: float = 0.0    # average volume of bars within stroke
    total_volume: int = 0      # total volume of bars within stroke
    volume_trend: str = ""     # "shrink" / "expand" / "flat"
    divergence: bool = False   # True if weaker than previous same-direction stroke


@dataclass
class Segment:
    """A segment (线段) composed of at least 3 strokes with overlap."""
    idx: int
    start_stroke: Stroke
    end_stroke: Stroke
    direction: int        # 1=up, -1=down
    strokes: list[Stroke] = field(default_factory=list)

    @property
    def high(self) -> float:
        return max(max(s.start.high, s.end.high) for s in self.strokes)

    @property
    def low(self) -> float:
        return min(min(s.start.low, s.end.low) for s in self.strokes)

    @property
    def start_dt(self) -> str:
        return self.strokes[0].start.dt

    @property
    def end_dt(self) -> str:
        return self.strokes[-1].end.dt


@dataclass
class Hub:
    """A hub/pivot (中枢).

    Per 缠论动力学十一讲 P06: in a structure A+B+C+D+E, the hub is BCD;
    A (entry stroke) does NOT belong to the hub range.
    """
    idx: int
    zg: float             # upper bound (min of highs)
    zd: float             # lower bound (max of lows)
    gg: float             # highest price in hub range
    dd: float             # lowest price in hub range
    strokes: list[Stroke] = field(default_factory=list)
    entry_stroke: Stroke | None = None  # A segment: enters the hub zone, not part of [ZD,ZG]
    evolution_type: str = ""   # "延伸"/"新生（上）"/"新生（下）"/"扩展"/""
    avg_volume: float = 0.0    # average volume across all strokes in hub
    volume_trend: str = ""     # "shrink"=蓄势 / "expand"=分歧加剧 / "flat"
    hub_level: str = ""        # level name, e.g. "30F中枢" (assigned by classify_hub_evolution)
    duration_bars: int = 0     # number of K-bars the hub spans
    is_merged: bool = False    # True if this hub resulted from expansion merge
    direction: str = ""        # "上" / "下" / "" (position vs prev hub in same trend)
    trend_seq: int = -1        # sequence number within same-direction chain (0-indexed)

    @property
    def start_dt(self) -> str:
        return self.strokes[0].start.dt if self.strokes else ""

    @property
    def end_dt(self) -> str:
        return self.strokes[-1].end.dt if self.strokes else ""

    @property
    def context_direction(self) -> int:
        """Hub context direction inferred from the entry stroke.

        Theory (缠论解析 场景B / 缠论动力学十一讲 P06):
          - 上涨趋势中的中枢: entry stroke is UP, hub is 下-上-下
          - 下跌趋势中的中枢: entry stroke is DOWN, hub is 上-下-上

        Returns:
            1  = hub formed in uptrend context (entry goes up)
           -1  = hub formed in downtrend context (entry goes down)
            0  = unknown
        """
        if self.entry_stroke:
            return self.entry_stroke.direction
        if not self.strokes:
            return 0
        first_dir = self.strokes[0].direction
        return -first_dir


@dataclass
class BuySellPoint:
    """A buy or sell point (买卖点)."""
    type: str             # "1B","2B","3B","1S","2S","3S","PB","PS"
    label: str
    dt: str
    price: float
    description: str
    level: str            # "daily","30min","5min"
    confidence: str       # "high","medium","low"
    hub_idx: int = -1
    stroke_idx: int = -1  # index of the stroke where the signal occurs
    seg_idx: int = -1     # index of the segment containing the signal
    area_ranges: list = field(default_factory=list)
    structure: list = field(default_factory=list)
    # a+A+b+B+c structure for trend divergence (趋势背驰)
    # Each: {"tag": "a"|"A"|"b"|"B"|"c", "start_dt", "end_dt", opt "zg","zd"}
    # Each entry: {"label": str, "start_dt": str, "end_dt": str, "area": float}
    wolf_warning: str = ""  # 防狼术 warning (empty = safe, otherwise = warning text)
    macd_zone: str = ""     # "above_zero" / "below_zero" / "near_zero"
    strength: str = ""      # "strongest" / "strong" / "standard" / "weak"
    strength_score: int = 0  # raw strength score (operational value)
    strength_details: list = field(default_factory=list)
    # [{"dim": str, "label": str, "score": int}, ...]
    conf_score: int = 0      # raw confidence score (pattern certainty)
    conf_details: list = field(default_factory=list)
    # [{"dim": str, "label": str, "score": int}, ...]
    position_advice: str = ""  # position sizing advice (e.g. "轻仓试探1/3", "满仓")
    idx: int = -1  # sequential index, assigned in analyze()
    invalidation_price: float = 0.0  # price that invalidates this signal
    status: str = "active"  # "active" / "invalidated"
    signal_level: str = ""  # Chanlun theoretical level, e.g. "30F三买"
    invalidation_reason: str = ""  # reason for invalidation
    trend_hub_rank: int = -1  # for 3B/3S: hub position in trend (0=end-of-opposite, 1=1st, ...); -1=N/A


@dataclass
class SegHub:
    """A hub built from segments (线段中枢), one level above stroke hubs.

    Same entry-segment convention as Hub: entry segment is stored separately.
    """
    idx: int
    zg: float
    zd: float
    gg: float
    dd: float
    segments: list[Segment] = field(default_factory=list)
    entry_segment: Segment | None = None
    evolution_type: str = ""
    direction: str = ""       # "上" / "下" / "" (determined by position vs prev hub)
    trend_seq: int = -1       # sequence number within same-direction trend (0-indexed)
    hub_level: str = ""        # level name, e.g. "DF中枢" (assigned by classify_seg_hub_evolution)

    @property
    def context_direction(self) -> int:
        """Hub context direction inferred from entry segment direction."""
        if self.entry_segment:
            return self.entry_segment.direction
        if not self.segments:
            return 0
        return -self.segments[0].direction

    @property
    def start_dt(self) -> str:
        if self.segments:
            return self.segments[0].strokes[0].start.dt
        return ""

    @property
    def end_dt(self) -> str:
        if self.segments:
            return self.segments[-1].strokes[-1].end.dt
        return ""


@dataclass
class AnalysisResult:
    """Complete analysis output for one timeframe."""
    level: str
    raw_bars: list[RawBar] = field(default_factory=list)
    merged_bars: list[MergedBar] = field(default_factory=list)
    fractals: list[Fractal] = field(default_factory=list)
    strokes: list[Stroke] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    hubs: list[Hub] = field(default_factory=list)
    seg_hubs: list[SegHub] = field(default_factory=list)
    trend: str = ""
    merged_hubs: list[Hub] = field(default_factory=list)
    merged_seg_hubs: list[SegHub] = field(default_factory=list)
    divergences: list[dict] = field(default_factory=list)
    buy_sell_points: list[BuySellPoint] = field(default_factory=list)
    seg_buy_sell_points: list[BuySellPoint] = field(default_factory=list)
    position_vs_hub: str = ""
    hub_position_detail: dict = field(default_factory=dict)
    trend_completion: dict = field(default_factory=dict)
    # {"status": "进行中"|"疑似完成"|"已确认完成",
    #  "reason": str, "confidence": "high"|"medium"|"low",
    #  "stage": str (描述当前阶段)}
    macd_diagnostics: dict = field(default_factory=dict)
    # Additional MACD-derived diagnostics:
    #   "area_2x_estimates": list of potential divergence estimates
    #   "double_pullback_warnings": list of double 0-axis pullback patterns
    #   "ma_area_divergences": list of MA-area cross-verified divergences
    volume_profile: dict = field(default_factory=dict)
    # {"activity": "active"|"normal"|"inactive",
    #  "recent_avg": float, "ma20_avg": float, "ratio": float,
    #  "trend": "expanding"|"shrinking"|"flat"}
    pending_3b: list = field(default_factory=list)
    # List of Pending3B objects: hubs that have been broken upward but
    # pullback is not yet complete (or still in progress).


@dataclass
class Pending3B:
    """A hub that has been broken upward but pullback confirmation is pending.

    States:
      "突破等待回抽" — breakout completed, no pullback stroke started yet
      "回抽进行中"   — pullback stroke is in progress (current bar in down-stroke)
      "回抽已至ZG附近" — pullback is near ZG, high tension zone
    """
    hub_idx: int
    hub_zg: float
    hub_zd: float
    hub_strokes: int = 0
    breakout_dt: str = ""
    breakout_high: float = 0.0
    breakout_pct: float = 0.0
    current_low: float = 0.0
    margin_to_zg: float = 0.0
    margin_pct: float = 0.0
    status: str = ""
    level: str = ""
    stop_loss: float = 0.0
    hub_rank: int = 1
    note: str = ""


@dataclass
class IntervalNest:
    """A nested interval chain from large to small timeframes (区间套).

    Each layer narrows the time window and provides a more precise entry point.
    Theory: 108课 §四 / 缠论解析 场景D / 图解缠论2 §1.2
    """
    big_level: str               # e.g. "daily"
    big_signal_type: str         # "trend" or "consolidation"
    big_signal_dt: str
    big_time_range: tuple[str, str]  # (start_dt, end_dt) of divergence segment
    big_direction: int           # 1=up divergence, -1=down divergence
    mid_level: str = ""          # e.g. "30min"
    mid_signal_dt: str = ""
    mid_time_range: tuple[str, str] = ("", "")
    small_level: str = ""        # e.g. "5min"
    small_signal_dt: str = ""
    precision_price: float = 0.0  # most precise entry/exit price
    precision_dt: str = ""       # most precise datetime
    depth: int = 1               # how many levels deep (1=big only, 2=+mid, 3=+small)
    note: str = ""


@dataclass
class ThreeBuyConfirmation:
    """Sub-level divergence confirmation for a Type-3 Buy signal (三买次级别确认).

    Theory: 三买筛选与评价体系 §七 — 30min三买的回抽段是5min下跌走势，
    用区间套在5min内部找该下跌走势的结束点（5min一买）= 30min三买精确入场。
    """
    source_level: str = ""
    source_3b_dt: str = ""
    source_3b_price: float = 0.0
    source_3b_hub_idx: int = -1
    source_3b_strength: str = ""
    pullback_start_dt: str = ""
    pullback_end_dt: str = ""
    sub_level: str = ""
    confirmed: bool = False
    confirmation_type: str = ""
    confirmation_dt: str = ""
    confirmation_price: float = 0.0
    daily_env: str = ""
    overall_status: str = ""
    note: str = ""
    sub_divergences: list = field(default_factory=list)
    sub_buy_signals: list = field(default_factory=list)


@dataclass
class MultiLevelSynthesis:
    """Multi-level synthesis result (多级别联立分析).

    Combines daily (direction) + 30min (buy/sell points) + 5min (timing)
    to produce resonance markers and context-enriched signals.
    """
    level_summary: list[dict] = field(default_factory=list)
    direction_alignment: str = ""
    resonance_signals: list[dict] = field(default_factory=list)
    enriched_signals: list[dict] = field(default_factory=list)
    interval_nests: list[IntervalNest] = field(default_factory=list)
    three_buy_confirmations: list[ThreeBuyConfirmation] = field(default_factory=list)
    overall_bias: str = ""
    action_advice: str = ""
    summary: str = ""


# ════════════════════════════════════════════════════════════════════
# 2. Data Loading
# ════════════════════════════════════════════════════════════════════

def load_bars_from_csv(filepath: str) -> list[RawBar]:
    """Load K-line bars from CSV file produced by data_fetcher."""
    bars = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            bars.append(RawBar(
                idx=i,
                dt=row["datetime"],
                open=float(row["open"]),
                close=float(row["close"]),
                high=float(row["high"]),
                low=float(row["low"]),
                volume=int(float(row["volume"])),
            ))
    return bars


# ════════════════════════════════════════════════════════════════════
# 3. MACD Calculation
# ════════════════════════════════════════════════════════════════════

def compute_macd(bars: list[RawBar], short: int = 12, long: int = 26,
                 signal: int = 9):
    """Compute MACD (DIF, DEA, histogram) in-place on raw bars."""
    if not bars:
        return
    bars[0].ema12 = bars[0].close
    bars[0].ema26 = bars[0].close

    a_s = 2.0 / (short + 1)
    a_l = 2.0 / (long + 1)
    a_sig = 2.0 / (signal + 1)

    for i in range(1, len(bars)):
        b = bars[i]
        p = bars[i - 1]
        b.ema12 = p.ema12 * (1 - a_s) + b.close * a_s
        b.ema26 = p.ema26 * (1 - a_l) + b.close * a_l
        b.dif = b.ema12 - b.ema26
        b.dea = p.dea * (1 - a_sig) + b.dif * a_sig
        b.macd_hist = 2 * (b.dif - b.dea)


def compute_macd_alt(bars: list[RawBar], short: int = 24, long: int = 52,
                     signal: int = 18) -> list[tuple[float, float, float]]:
    """Compute MACD with alternative parameters (e.g. doubled).

    Returns list of (dif, dea, hist) tuples, one per bar.
    Does NOT modify bars in-place.
    Theory: 土匪注解 §3.1 — when histogram bars are fragmented across
    zero crossings, doubling parameters produces more continuous area.
    """
    if not bars:
        return []
    a_s = 2.0 / (short + 1)
    a_l = 2.0 / (long + 1)
    a_sig = 2.0 / (signal + 1)

    ema_s = bars[0].close
    ema_l = bars[0].close
    dea = 0.0
    result = [(0.0, 0.0, 0.0)]
    for i in range(1, len(bars)):
        c = bars[i].close
        ema_s = ema_s * (1 - a_s) + c * a_s
        ema_l = ema_l * (1 - a_l) + c * a_l
        dif = ema_s - ema_l
        dea = dea * (1 - a_sig) + dif * a_sig
        hist = 2 * (dif - dea)
        result.append((dif, dea, hist))
    return result


def compute_ma(bars: list[RawBar], periods: list[int] | None = None):
    """Compute simple moving averages in-place on raw bars.

    Adds ma5 and ma10 attributes. Used for MA-area cross-verification
    of divergence (缠论解析 场景C / 108课详解 §三).
    """
    if periods is None:
        periods = [5, 10]
    for p in periods:
        attr = f"ma{p}"
        for i, b in enumerate(bars):
            if not hasattr(b, attr):
                object.__setattr__(b, attr, 0.0)
            if i < p - 1:
                object.__setattr__(b, attr, b.close)
            else:
                avg = sum(bars[j].close for j in range(i - p + 1, i + 1)) / p
                object.__setattr__(b, attr, avg)


# ════════════════════════════════════════════════════════════════════
# 4. Inclusion Processing (包含处理)
#
# Rules (缠论108课 §1.1, 缠论解析 场景A):
#   - Process left-to-right, only adjacent pairs
#   - Up direction: new_high = max, new_low = max (向上取高高低高)
#   - Down direction: new_high = min, new_low = min (向下取低低高低)
#   - Direction determined by comparing with the *previous* non-included bar
# ════════════════════════════════════════════════════════════════════

def inclusion_processing(bars: list[RawBar]) -> list[MergedBar]:
    """Merge bars with inclusion relationships."""
    if not bars:
        return []

    merged = [MergedBar(
        idx=0, high=bars[0].high, low=bars[0].low,
        start_raw=0, end_raw=0, direction=0,
        dates=[bars[0].dt],
        high_dt=bars[0].dt, low_dt=bars[0].dt,
    )]

    for i in range(1, len(bars)):
        b = bars[i]
        last = merged[-1]

        has_inclusion = (
            (last.high >= b.high and last.low <= b.low) or
            (b.high >= last.high and b.low <= last.low)
        )

        if has_inclusion:
            if len(merged) >= 2:
                direction = 1 if last.high > merged[-2].high else -1
            else:
                direction = 1 if b.close >= b.open else -1

            if direction == 1:
                if b.high > last.high:
                    last.high_dt = b.dt
                last.high = max(last.high, b.high)
                if b.low > last.low:
                    last.low_dt = b.dt
                last.low = max(last.low, b.low)
            else:
                if b.high < last.high:
                    last.high_dt = b.dt
                last.high = min(last.high, b.high)
                if b.low < last.low:
                    last.low_dt = b.dt
                last.low = min(last.low, b.low)
            last.end_raw = i
            last.dates.append(b.dt)
            last.direction = direction
        else:
            merged.append(MergedBar(
                idx=len(merged),
                high=b.high, low=b.low,
                start_raw=i, end_raw=i,
                direction=1 if b.high > last.high else -1,
                dates=[b.dt],
                high_dt=b.dt, low_dt=b.dt,
            ))

    for i, m in enumerate(merged):
        m.idx = i
    return merged


# ════════════════════════════════════════════════════════════════════
# 5. Fractal Identification (分型识别)
#
# Rules (108课 §1.1):
#   - Top fractal: middle bar's high > left's high AND > right's high,
#                  middle bar's low > left's low AND > right's low
#   - Bottom fractal: symmetric
#   - Must operate on *merged* bars (after inclusion processing)
# ════════════════════════════════════════════════════════════════════

def find_fractals(merged: list[MergedBar]) -> list[Fractal]:
    """Identify top and bottom fractals on merged bars."""
    fractals = []
    for i in range(1, len(merged) - 1):
        prev, curr, nxt = merged[i - 1], merged[i], merged[i + 1]
        if (curr.high > prev.high and curr.high > nxt.high and
                curr.low > prev.low and curr.low > nxt.low):
            fractals.append(Fractal(
                type="top", mk_idx=i,
                high=curr.high, low=curr.low,
                dt=curr.high_dt or curr.dates[0],
            ))
        elif (curr.low < prev.low and curr.low < nxt.low and
              curr.high < prev.high and curr.high < nxt.high):
            fractals.append(Fractal(
                type="bottom", mk_idx=i,
                high=curr.high, low=curr.low,
                dt=curr.low_dt or curr.dates[0],
            ))
    return fractals


# ════════════════════════════════════════════════════════════════════
# 6. Stroke Construction (笔划分)
#
# Rules (108课 §1.2, 缠论解析 场景A):
#   - A stroke connects adjacent top↔bottom fractals
#   - Between top and bottom fractals, at least 1 independent K-line
#     (not shared with either fractal's 3-bar group) → mk_gap >= 4
#   - Same-type fractals: keep the extreme (top→higher, bottom→lower)
#   - Up stroke (bottom→top): end.high > start.high
#   - Down stroke (top→bottom): end.low < start.low
# ════════════════════════════════════════════════════════════════════

def find_strokes(fractals: list[Fractal], merged: list[MergedBar]) -> list[Stroke]:
    """Build strokes from fractal sequence."""
    if len(fractals) < 2:
        return []

    valid = [fractals[0]]
    for f in fractals[1:]:
        last = valid[-1]
        if f.type == last.type:
            if f.type == "top" and f.high >= last.high:
                valid[-1] = f
            elif f.type == "bottom" and f.low <= last.low:
                valid[-1] = f
        else:
            gap = abs(f.mk_idx - last.mk_idx)
            if gap >= 4:
                ok = True
                if last.type == "bottom" and f.type == "top":
                    ok = f.high > last.high
                elif last.type == "top" and f.type == "bottom":
                    ok = f.low < last.low
                if ok:
                    valid.append(f)
                else:
                    if f.type == last.type:
                        if f.type == "top" and f.high >= last.high:
                            valid[-1] = f
                        elif f.type == "bottom" and f.low <= last.low:
                            valid[-1] = f
            else:
                if f.type == "top" and f.high > last.high and last.type == "top":
                    valid[-1] = f
                elif f.type == "bottom" and f.low < last.low and last.type == "bottom":
                    valid[-1] = f

    strokes = []
    for i in range(1, len(valid)):
        s, e = valid[i - 1], valid[i]
        if s.type == e.type:
            continue
        direction = 1 if e.type == "top" else -1
        strokes.append(Stroke(
            idx=len(strokes), start=s, end=e,
            direction=direction,
            mk_span=abs(e.mk_idx - s.mk_idx),
        ))
    strokes = _fix_stroke_extreme_violations(strokes, fractals)
    return strokes


def _fix_stroke_extreme_violations(
    strokes: list[Stroke], fractals: list[Fractal],
) -> list[Stroke]:
    """Fix strokes where an intermediate fractal exceeds the stroke's start.

    An up stroke from BOT(A) to TOP(B) is violated when an intermediate
    bottom fractal C has C.low < A.low.  This means the previous down
    stroke should extend to C and the up stroke should start from C.
    Similarly for down strokes with intermediate higher tops.

    Only adjusts endpoints; stroke count stays the same.
    Gap >= 4 is required for the adjusted up/down stroke.
    """
    _MIN_GAP = 4
    changed = True
    while changed:
        changed = False
        for i, s in enumerate(strokes):
            start_mk = s.start.mk_idx
            end_mk = s.end.mk_idx
            worst = None
            if s.direction == 1:
                for f in fractals:
                    if (f.type == "bottom" and start_mk < f.mk_idx < end_mk
                            and f.low < s.start.low):
                        if worst is None or f.low < worst.low:
                            worst = f
            else:
                for f in fractals:
                    if (f.type == "top" and start_mk < f.mk_idx < end_mk
                            and f.high > s.start.high):
                        if worst is None or f.high > worst.high:
                            worst = f
            if worst is None:
                continue
            new_gap = abs(end_mk - worst.mk_idx)
            if new_gap < _MIN_GAP:
                continue
            if i > 0:
                prev = strokes[i - 1]
                strokes[i - 1] = Stroke(
                    idx=prev.idx, start=prev.start, end=worst,
                    direction=prev.direction,
                    mk_span=abs(worst.mk_idx - prev.start.mk_idx),
                )
            strokes[i] = Stroke(
                idx=s.idx, start=worst, end=s.end,
                direction=s.direction,
                mk_span=new_gap,
            )
            changed = True
            break
    return strokes


# ════════════════════════════════════════════════════════════════════
# 7. MACD Area for Strokes
# ════════════════════════════════════════════════════════════════════

def compute_stroke_macd(strokes: list[Stroke], bars: list[RawBar],
                        merged: list[MergedBar]):
    """Compute MACD and volume metrics for each stroke.

    For each stroke, computes:
      - macd_area:   histogram area (same-direction; fallback to absolute)
      - dif_extreme: max DIF for up-strokes, min DIF for down-strokes
      - hist_peak:   maximum |histogram| bar value within the stroke
      - avg_volume:  average volume of bars within stroke
      - total_volume: total volume of bars within stroke
      - volume_trend: "shrink"/"expand"/"flat" based on first/second half comparison
    """
    dt_to_idx = {b.dt: b.idx for b in bars}

    for s in strokes:
        si = dt_to_idx.get(s.start.dt, 0)
        ei = dt_to_idx.get(s.end.dt, len(bars) - 1)
        dir_area = 0.0
        abs_area = 0.0
        peak_hist = 0.0
        dif_vals = []
        volumes = []
        for ki in range(si, min(ei + 1, len(bars))):
            v = bars[ki].macd_hist
            abs_area += abs(v)
            if abs(v) > peak_hist:
                peak_hist = abs(v)
            dif_vals.append(bars[ki].dif)
            volumes.append(bars[ki].volume)
            if (s.direction == 1 and v > 0) or (s.direction == -1 and v < 0):
                dir_area += abs(v)
        s.macd_area = dir_area if dir_area > 0 else abs_area
        s.hist_peak = peak_hist
        if dif_vals:
            s.dif_extreme = max(dif_vals) if s.direction == 1 else min(dif_vals)
        else:
            s.dif_extreme = 0.0

        # Volume metrics
        if volumes:
            s.total_volume = sum(volumes)
            s.avg_volume = s.total_volume / len(volumes)
            mid = len(volumes) // 2
            if mid >= 2:
                first_half = sum(volumes[:mid]) / mid
                second_half = sum(volumes[mid:]) / (len(volumes) - mid)
                if first_half > 0:
                    ratio = second_half / first_half
                    if ratio < 0.7:
                        s.volume_trend = "shrink"
                    elif ratio > 1.3:
                        s.volume_trend = "expand"
                    else:
                        s.volume_trend = "flat"
                else:
                    s.volume_trend = "flat"
            else:
                s.volume_trend = "flat"


def mark_stroke_divergence(strokes: list['Stroke']):
    """Compare each stroke with previous same-direction stroke by macd_area.

    If current stroke's macd_area < previous same-direction stroke's macd_area,
    mark it as divergence (力度背驰).
    """
    last_up: 'Stroke | None' = None
    last_down: 'Stroke | None' = None
    for s in strokes:
        if s.direction == 1:
            if last_up is not None and s.macd_area < last_up.macd_area * 0.95:
                s.divergence = True
            last_up = s
        else:
            if last_down is not None and s.macd_area < last_down.macd_area * 0.95:
                s.divergence = True
            last_down = s


# ════════════════════════════════════════════════════════════════════
# 8. Segment Construction (线段划分)
#
# Strict implementation per 108课第67课:
#   - Characteristic sequence = counter-direction strokes, each
#     element represented by its full price range (high, low)
#   - Apply inclusion processing (包含处理) to get standard sequence
#   - Up segment ends at top fractal in standard char sequence
#   - Down segment ends at bottom fractal in standard char sequence
#   - Fractal requires 3 elements: middle is extreme (highest/lowest)
# ════════════════════════════════════════════════════════════════════

def _stroke_high_low(s: Stroke) -> tuple[float, float]:
    """Full price range of a stroke."""
    return max(s.start.high, s.end.high), min(s.start.low, s.end.low)


def _process_char_seq_inclusion(
    elements: list[tuple[float, float, Stroke]],
    seg_dir: int,
) -> list[tuple[float, float, Stroke, Stroke]]:
    """Apply inclusion processing on characteristic sequence elements.

    Treats each element as a K-line with (high, low). When two adjacent
    elements have an inclusion relationship, merge following the local
    trend direction (same rule as K-line inclusion).

    Returns 4-tuples: (high, low, first_stroke, last_stroke) where
    first/last track the original stroke range of merged elements.
    """
    if len(elements) < 2:
        return [(h, l, s, s) for h, l, s in elements]

    result: list[tuple[float, float, Stroke, Stroke]] = [
        (elements[0][0], elements[0][1], elements[0][2], elements[0][2])
    ]
    for i in range(1, len(elements)):
        h_prev, l_prev, s_first, s_last = result[-1]
        h_cur, l_cur, s_cur = elements[i]

        has_inclusion = ((h_prev >= h_cur and l_prev <= l_cur) or
                         (h_cur >= h_prev and l_cur <= l_prev))

        if has_inclusion:
            if len(result) >= 2:
                local_up = result[-1][0] >= result[-2][0]
            else:
                local_up = (seg_dir == 1)

            if local_up:
                merged = (max(h_prev, h_cur), max(l_prev, l_cur),
                          s_first, s_cur)
            else:
                merged = (min(h_prev, h_cur), min(l_prev, l_cur),
                          s_first, s_cur)
            result[-1] = merged
        else:
            result.append((h_cur, l_cur, s_cur, s_cur))

    return result


_CHAR_GAP_MIN_RATIO = 0.003  # 0.3% — ignore micro-gaps from inclusion merge


def _has_char_gap(A: tuple[float, float, Stroke, Stroke],
                   B: tuple[float, float, Stroke, Stroke]) -> bool:
    """Check if two characteristic sequence elements have a meaningful gap.

    Inclusion processing can create tiny artificial gaps when the merged
    low/high barely exceeds the neighbor's high/low.  A 0.3% minimum
    ensures only structurally significant gaps trigger type-2 handling.
    """
    ref = (A[0] + B[0]) / 2 if (A[0] + B[0]) > 0 else 1.0
    threshold = ref * _CHAR_GAP_MIN_RATIO
    if A[1] > B[0]:
        return (A[1] - B[0]) > threshold
    if B[1] > A[0]:
        return (B[1] - A[0]) > threshold
    return False


def _find_char_fractal(
    std_seq: list[tuple[float, float, Stroke, Stroke]],
    seg_dir: int,
    skip_ids: set[int] | None = None,
) -> tuple[Stroke, Stroke, bool] | None:
    """Find the first top/bottom fractal in the standard char sequence.

    Returns (first_stroke, last_stroke, has_gap) where the strokes
    define the range of the fractal's middle element (may span multiple
    original strokes due to inclusion merge), and has_gap indicates
    whether the first two elements have a gap (67课第二种情况).
    Returns None if no fractal is found.

    skip_ids: object ids of strokes to skip (for rejected type-2 fractals).
    """
    if len(std_seq) < 3:
        return None

    for k in range(1, len(std_seq) - 1):
        A, B, C = std_seq[k - 1], std_seq[k], std_seq[k + 1]
        if skip_ids and id(B[2]) in skip_ids:
            continue
        if seg_dir == 1:
            if (B[0] > A[0] and B[0] > C[0] and
                    B[1] > A[1] and B[1] > C[1]):
                return (B[2], B[3], _has_char_gap(A, B))
        else:
            if (B[1] < A[1] and B[1] < C[1] and
                    B[0] < A[0] and B[0] < C[0]):
                return (B[2], B[3], _has_char_gap(A, B))
    return None


def _check_reverse_fractal(
    all_strokes: list[Stroke],
    from_stroke_idx: int,
    seg_dir: int,
) -> bool:
    """67课第二种情况: check if the reverse sequence confirms the segment break.

    When a gap exists between the first two elements of a characteristic
    sequence fractal, the reverse sequence starting from the fractal
    must form the opposite fractal to confirm the segment termination.

    Per 67课: 第二个序列中的分型不分第一二种情况，只要有分型就可以。
    """
    rev_dir = -seg_dir
    rev_char_dir = seg_dir

    remaining = [s for s in all_strokes if s.idx >= from_stroke_idx]
    if len(remaining) < 3:
        return False

    char_elements: list[tuple[float, float, Stroke]] = []
    for s in remaining:
        if s.direction == rev_char_dir:
            char_elements.append((*_stroke_high_low(s), s))
            std_seq = _process_char_seq_inclusion(char_elements, rev_dir)
            result = _find_char_fractal(std_seq, rev_dir)
            if result is not None:
                return True

    return False


_STRUCT_BREAK_RATIO = 0.03  # 3% — fallback when char sequence produces no fractal


def find_segments(strokes: list[Stroke]) -> list[Segment]:
    """Build segments using the standard characteristic sequence method.

    Per 108课第67课: extract counter-direction strokes as the
    characteristic sequence, apply inclusion processing, then detect
    top/bottom fractals to determine segment boundaries.

    Two cases for segment termination:
      Type 1: fractal's first two elements have no gap -> ends directly.
      Type 2: fractal's first two elements have a gap -> needs reverse
              sequence confirmation (opposite fractal must appear).

    Fallback: when no fractal is found but the price exceeds the segment's
    starting extreme by >3% in the counter direction, the segment is
    truncated at the directional extreme.  This prevents runaway segments
    where the char sequence method cannot produce a fractal due to
    monotonic counter-moves.
    """
    if len(strokes) < 3:
        return []

    segments = []
    seg_start = 0

    while seg_start < len(strokes) - 2:
        s0, s1, s2 = strokes[seg_start], strokes[seg_start + 1], strokes[seg_start + 2]

        h0, l0 = _stroke_high_low(s0)
        h2, l2 = _stroke_high_low(s2)

        if max(l0, l2) >= min(h0, h2):
            seg_start += 1
            continue

        seg_dir = s0.direction
        char_dir = -seg_dir
        seg_strokes = [s0, s1, s2]

        char_elements: list[tuple[float, float, Stroke]] = []
        for s in seg_strokes:
            if s.direction == char_dir:
                char_elements.append((*_stroke_high_low(s), s))

        if seg_dir == -1:
            seg_start_ref = max(s0.start.high, s0.end.high)
        else:
            seg_start_ref = min(s0.start.low, s0.end.low)

        found_end = False
        j = seg_start + 3
        rejected_ids: set[int] = set()
        break_trunc_idx: int | None = None

        while j < len(strokes):
            cur = strokes[j]
            seg_strokes.append(cur)

            if cur.direction == char_dir:
                char_elements.append((*_stroke_high_low(cur), cur))

            std_seq = _process_char_seq_inclusion(char_elements, seg_dir)
            result = _find_char_fractal(std_seq, seg_dir, rejected_ids)

            if result is not None:
                frac_first, frac_last, has_gap = result

                if has_gap:
                    if not _check_reverse_fractal(
                            strokes, frac_first.idx, seg_dir):
                        rejected_ids.add(id(frac_first))
                        j += 1
                        continue

                # Truncate at the extreme within the fractal middle
                # element's span.  When elements are merged by inclusion,
                # the span covers frac_first..frac_last (and any
                # connecting strokes in between).
                first_pos = None
                last_pos = None
                for ki, s in enumerate(seg_strokes):
                    if s is frac_first:
                        first_pos = ki
                    if s is frac_last:
                        last_pos = ki

                if first_pos is not None:
                    if last_pos is not None and last_pos > first_pos:
                        span = seg_strokes[first_pos:last_pos + 1]
                        if seg_dir == -1:
                            _, eidx = min(
                                (min(s.start.low, s.end.low), ki)
                                for ki, s in enumerate(span)
                            )
                        else:
                            _, eidx = max(
                                (max(s.start.high, s.end.high), ki)
                                for ki, s in enumerate(span)
                            )
                        cut = first_pos + eidx + 1
                    else:
                        cut = first_pos
                    seg_strokes = seg_strokes[:cut]
                found_end = True
                break

            if break_trunc_idx is None and len(seg_strokes) > 3:
                cur_h = max(cur.start.high, cur.end.high)
                cur_l = min(cur.start.low, cur.end.low)
                exceeded = (
                    (seg_dir == -1 and cur_h > seg_start_ref * (1 + _STRUCT_BREAK_RATIO))
                    or
                    (seg_dir == 1 and cur_l < seg_start_ref * (1 - _STRUCT_BREAK_RATIO))
                )
                if exceeded:
                    candidates = seg_strokes[:-1]
                    if seg_dir == -1:
                        _, eidx = min(
                            (min(s.start.low, s.end.low), i)
                            for i, s in enumerate(candidates)
                        )
                    else:
                        _, eidx = max(
                            (max(s.start.high, s.end.high), i)
                            for i, s in enumerate(candidates)
                        )
                    eidx = max(eidx, 2)
                    if eidx % 2 == 1:
                        eidx = max(eidx - 1, 2)
                    break_trunc_idx = eidx
                    break

            j += 1

        if break_trunc_idx is not None:
            seg_strokes = seg_strokes[:break_trunc_idx + 1]

        # 78课: segment must end on a same-direction stroke (odd count)
        if len(seg_strokes) % 2 == 0:
            seg_strokes = seg_strokes[:-1]

        if len(seg_strokes) >= 3:
            segments.append(Segment(
                idx=len(segments),
                start_stroke=seg_strokes[0],
                end_stroke=seg_strokes[-1],
                direction=seg_dir,
                strokes=list(seg_strokes),
            ))

        seg_start += len(seg_strokes)
        if seg_start >= len(strokes):
            break

    return segments


# ════════════════════════════════════════════════════════════════════
# 9. Hub Construction (中枢构建)
#
# Per 缠论动力学十一讲 P06:
#   Structure: A + B + C + D + E
#     A = entry stroke (进入段, NOT part of hub)
#     B, C, D = three overlapping strokes → hub range [ZD, ZG]
#     E = exit stroke (离开段)
#   "中枢前面的上涨和下跌是否属于中枢区间，答案是否定的"
#
#   ZG = min(highs of B,C,D), ZD = max(lows of B,C,D)
#   Valid when ZD < ZG
#   Extension: subsequent strokes crossing ZD~ZG extend the hub
#   GG = highest price, DD = lowest price in hub strokes
# ════════════════════════════════════════════════════════════════════

def _stroke_range(s: Stroke) -> tuple[float, float]:
    """Get (high, low) price range of a stroke."""
    return max(s.start.high, s.end.high), min(s.start.low, s.end.low)


_HUB_MIN_WIDTH_RATIO = 0.005  # 0.5% — skip degenerate hubs with negligible overlap


def find_hubs(strokes: list[Stroke]) -> list[Hub]:
    """Build hubs from stroke sequence.

    Structure per 缠论动力学十一讲 P06: A + B + C + D + E
      A = entry stroke (enters the hub zone from OUTSIDE, NOT part of [ZD, ZG])
      B, C, D = hub core (three overlapping strokes define the range)
      E = exit stroke (first stroke leaving the hub zone)

    Entry pen validation (进入笔方向性验证):
      The entry pen must ENTER the zone [ZD, ZG] from outside:
      - Up entry (dir=1): starts below ZD, reaches into/above zone
      - Down entry (dir=-1): starts above ZG, reaches into/below zone
      This ensures the entry pen is directional — it brings price INTO the
      center zone, rather than being an arbitrary preceding pen.
    """
    if len(strokes) < 4:
        return []

    hubs = []
    i = 0

    while i < len(strokes) - 3:
        # strokes[i] = entry candidate (A), strokes[i+1..i+3] = hub (B, C, D)
        ranges = [_stroke_range(strokes[k]) for k in range(i + 1, i + 4)]
        zg = min(r[0] for r in ranges)
        zd = max(r[1] for r in ranges)

        if zd >= zg:
            i += 1
            continue

        if zd > 0 and (zg - zd) / zd < _HUB_MIN_WIDTH_RATIO:
            i += 1
            continue

        # Validate entry pen: must enter zone [ZD, ZG] from outside
        entry = strokes[i]
        entry_h, entry_l = _stroke_range(entry)
        entry_valid = False
        if entry.direction == 1:
            # Up pen entering from below: starts below ZD, reaches zone
            entry_valid = entry_l < zd and entry_h >= zd
        else:
            # Down pen entering from above: starts above ZG, reaches zone
            entry_valid = entry_h > zg and entry_l <= zg

        if not entry_valid:
            i += 1
            continue

        hub_strokes = list(strokes[i + 1:i + 4])
        j = i + 4

        while j < len(strokes):
            sj_h, sj_l = _stroke_range(strokes[j])
            if sj_l <= zg and sj_h >= zd:
                hub_strokes.append(strokes[j])
                j += 1
            else:
                break

        gg = max(_stroke_range(s)[0] for s in hub_strokes)
        dd = min(_stroke_range(s)[1] for s in hub_strokes)

        hubs.append(Hub(
            idx=len(hubs), zg=zg, zd=zd, gg=gg, dd=dd,
            strokes=hub_strokes,
            entry_stroke=entry,
        ))
        # j = exit stroke (E); reuse as entry (A) of the next hub.
        # Per 缠论: the departure stroke of one hub becomes the approach
        # stroke of the next — skipping it (j+1) loses valid hubs when
        # strokes are tight (e.g. 天孚通信 S194-S197 after Hub 20).
        i = j

    return hubs


def _segment_range(seg: Segment) -> tuple[float, float]:
    """Get (high, low) price range of a segment."""
    h = max(max(s.start.high, s.end.high) for s in seg.strokes)
    l = min(min(s.start.low, s.end.low) for s in seg.strokes)
    return h, l


def find_seg_hubs(segments: list[Segment]) -> list[SegHub]:
    """Build hubs from segment sequence (线段中枢).

    Same convention as stroke hubs: A(entry) + B,C,D(core) + E(exit).

    Entry segment validation (same principle as stroke hubs):
      The entry segment must ENTER the zone [ZD, ZG] from outside:
      - Up entry (dir=1): starts below ZD, reaches into/above zone
      - Down entry (dir=-1): starts above ZG, reaches into/below zone
    """
    if len(segments) < 4:
        return []

    hubs = []
    i = 0

    while i < len(segments) - 3:
        ranges = [_segment_range(segments[k]) for k in range(i + 1, i + 4)]
        zg = min(r[0] for r in ranges)
        zd = max(r[1] for r in ranges)

        if zd >= zg:
            i += 1
            continue

        # Validate entry segment: must enter zone [ZD, ZG] from outside
        entry = segments[i]
        entry_h, entry_l = _segment_range(entry)
        entry_valid = False
        if entry.direction == 1:
            entry_valid = entry_l < zd and entry_h >= zd
        else:
            entry_valid = entry_h > zg and entry_l <= zg

        if not entry_valid:
            i += 1
            continue

        hub_segs = list(segments[i + 1:i + 4])
        j = i + 4

        while j < len(segments):
            sj_h, sj_l = _segment_range(segments[j])
            if sj_l <= zg and sj_h >= zd:
                hub_segs.append(segments[j])
                j += 1
            else:
                break

        gg = max(_segment_range(s)[0] for s in hub_segs)
        dd = min(_segment_range(s)[1] for s in hub_segs)

        hubs.append(SegHub(
            idx=len(hubs), zg=zg, zd=zd, gg=gg, dd=dd,
            segments=hub_segs,
            entry_segment=entry,
        ))
        # Exit segment at j; reuse as entry of next hub (same as stroke hubs)
        i = j

    return hubs


# ════════════════════════════════════════════════════════════════════
# 9b. Hub Evolution Classification (中枢演化分类)
#
# Theory (108课 §1.4 / 缠论解析 场景B / 图解缠论2 §2.2-2.4):
#   - 延伸: hub oscillation beyond initial 3 strokes (swing-trade zone)
#   - 新生: adjacent hubs with non-overlapping [ZD,ZG] → trend continuation
#   - 扩展: adjacent hubs with overlapping [ZD,ZG] → upgrade to larger hub
#
# Note: Using CORE range [ZD,ZG] for overlap — NOT oscillation [DD,GG].
# DD/GG includes extreme stroke excursions far beyond the hub core,
# especially in extended hubs (延伸), causing false cascade merges that
# collapse all hubs into one degenerate hub and destroy trend structure.
# ════════════════════════════════════════════════════════════════════

_EXTENSION_THRESHOLD = 5  # strokes needed to classify as "延伸"
_UPGRADE_EXTENSION_THRESHOLD = 9  # 延伸达9段 → 升级为更高级别

# Hub level hierarchy (中枢级别体系)
#
# Theory (108课 §1.4):
#   "三个1分钟走势重叠 ≈ 5分钟中枢；三个5分钟走势重叠 ≈ 30分钟中枢"
#
# When analysing a given K-line period, strokes are sub-level movements
# and segments are current-level movements.  So:
#   stroke hub  → level = one below the K-line period
#   segment hub → level = same as the K-line period
#
# Upgrade paths:
#   延伸9段 → hub level +1  (3 sub-hubs within one extended hub)
#   扩张     → hub level +1  (2 same-direction hubs, GG/DD overlap)

# Standard Chanlun level sequence (缠论新课程 §一):
#   1F → 5F → 30F → DF → WF → MF → QF
_LEVEL_ORDER = [
    "1F", "5F", "30F", "DF", "WF", "MF", "QF",
]

# Stroke hub: building blocks are strokes → sub-level movements
_STROKE_HUB_LEVEL = {
    "5min":  "1F",
    "15min": "5F",
    "30min": "5F",
    "60min": "30F",
    "daily": "30F",
    "weekly": "DF",
}

# Segment hub: building blocks are segments → current-level movements
_SEGMENT_HUB_LEVEL = {
    "5min":  "5F",
    "15min": "30F",
    "30min": "30F",
    "60min": "DF",
    "daily": "DF",
    "weekly": "WF",
}


def _level_up(level_name: str) -> str:
    """Return the next higher level name, or the input if already at top."""
    try:
        idx = _LEVEL_ORDER.index(level_name)
        if idx + 1 < len(_LEVEL_ORDER):
            return _LEVEL_ORDER[idx + 1]
    except ValueError:
        pass
    return level_name


def get_hub_level_name(analysis_level: str, hub_type: str) -> str:
    """Return the theoretical level name for a hub.

    Args:
        analysis_level: K-line period ("daily", "30min", "5min", etc.)
        hub_type: "stroke" or "segment"
    """
    if hub_type == "stroke":
        return _STROKE_HUB_LEVEL.get(analysis_level, "30F")
    else:
        return _SEGMENT_HUB_LEVEL.get(analysis_level, "DF")


def classify_hub_evolution(hubs: list[Hub], analysis_level: str = "daily"):
    """Classify each hub's evolution type and level in-place.

    For a single hub: extended oscillation if strokes > threshold.
    For adjacent pairs: new birth vs expansion based on range overlap.

    Level upgrades (108课 §1.4 / 缠论新课程 / 缠论动力学十一讲):
      - 延伸9段: 3 sub-hubs overlap within one extended hub → level +1
      - 扩张: 2 same-direction hubs with GG/DD overlap → level +1

    Uses CORE range [ZD, ZG] for overlap detection. Chan Theory defines
    a trend as two hubs whose cores don't overlap. DD/GG would misclassify
    many valid trends as expansion due to extreme stroke excursions.
    """
    if not hubs:
        return

    base_level = get_hub_level_name(analysis_level, "stroke")

    for h in hubs:
        h.hub_level = base_level + "中枢"
        if len(h.strokes) >= _EXTENSION_THRESHOLD:
            h.evolution_type = "延伸"
        if len(h.strokes) >= _UPGRADE_EXTENSION_THRESHOLD:
            upgraded = _level_up(base_level)
            h.hub_level = upgraded + "中枢"
            h.evolution_type = "延伸升级"

    for i in range(1, len(hubs)):
        prev, curr = hubs[i - 1], hubs[i]

        core_overlap = curr.zd <= prev.zg and curr.zg >= prev.zd

        if core_overlap:
            curr.evolution_type = "扩展"
        else:
            gg_dd_overlap = (curr.dd <= prev.gg and curr.gg >= prev.dd)
            same_direction = _hubs_same_direction(prev, curr)
            if gg_dd_overlap and same_direction:
                upgraded = _level_up(base_level)
                curr.evolution_type = "扩张"
                prev.hub_level = upgraded + "中枢"
                curr.hub_level = upgraded + "中枢"
            elif curr.zd > prev.zg:
                curr.evolution_type = "新生（上）"
            else:
                curr.evolution_type = "新生（下）"


def _hubs_same_direction(a, b) -> bool:
    """Check if two hubs trend in the same direction based on context."""
    a_dir = getattr(a, 'context_direction', 0)
    b_dir = getattr(b, 'context_direction', 0)
    if a_dir != 0 and b_dir != 0:
        return a_dir == b_dir
    a_mid = (a.zg + a.zd) / 2
    b_mid = (b.zg + b.zd) / 2
    return (b_mid > a_mid) or (b_mid < a_mid)


def classify_seg_hub_evolution(seg_hubs: list[SegHub], analysis_level: str = "daily"):
    """Same logic for segment-level hubs."""
    if not seg_hubs:
        return

    base_level = get_hub_level_name(analysis_level, "segment")

    for h in seg_hubs:
        h.hub_level = base_level + "中枢"
        if len(h.segments) >= _EXTENSION_THRESHOLD:
            h.evolution_type = "延伸"
        if len(h.segments) >= _UPGRADE_EXTENSION_THRESHOLD:
            upgraded = _level_up(base_level)
            h.hub_level = upgraded + "中枢"
            h.evolution_type = "延伸升级"

    for i in range(1, len(seg_hubs)):
        prev, curr = seg_hubs[i - 1], seg_hubs[i]
        core_overlap = curr.zd <= prev.zg and curr.zg >= prev.zd

        if core_overlap:
            curr.evolution_type = "扩展"
        else:
            gg_dd_overlap = (curr.dd <= prev.gg and curr.gg >= prev.dd)
            prev_mid = (prev.zg + prev.zd) / 2
            curr_mid = (curr.zg + curr.zd) / 2
            same_direction = (curr_mid > prev_mid) or (curr_mid < prev_mid)
            if gg_dd_overlap and same_direction:
                upgraded = _level_up(base_level)
                curr.evolution_type = "扩张"
                prev.hub_level = upgraded + "中枢"
                curr.hub_level = upgraded + "中枢"
            elif curr.zg > prev.zg:
                curr.evolution_type = "新生（上）"
            else:
                curr.evolution_type = "新生（下）"


def assign_hub_direction_and_sequence(hubs: list[Hub]):
    """Assign direction ('上'/'下') and trend_seq to each hub.

    Direction: determined by the entry stroke direction.
      - Entry stroke UP (dir=1) → hub direction = "上" (uptrend context)
      - Entry stroke DOWN (dir=-1) → hub direction = "下" (downtrend context)

    Sequence: consecutive same-direction hubs form a chain numbered from 0.

    Theory (108课 §1.5 / 缠论动力学十一讲 P06):
      - 上涨趋势中的中枢: entry stroke is UP → "上"
      - 下跌趋势中的中枢: entry stroke is DOWN → "下"
    """
    if not hubs:
        return

    seq = 0
    prev_dir = ""
    for i, h in enumerate(hubs):
        entry_dir = h.context_direction
        if entry_dir == 1:
            d = "上"
        elif entry_dir == -1:
            d = "下"
        else:
            d = prev_dir if prev_dir else ""

        h.direction = d
        if i == 0:
            seq = 0
        elif d == prev_dir and d != "":
            seq += 1
        else:
            seq = 0
        h.trend_seq = seq
        prev_dir = d


def assign_seg_hub_direction_and_sequence(seg_hubs: list[SegHub]):
    """Same direction/sequence logic for segment-level hubs (entry segment direction)."""
    if not seg_hubs:
        return

    seq = 0
    prev_dir = ""
    for i, h in enumerate(seg_hubs):
        entry_dir = h.context_direction
        if entry_dir == 1:
            d = "上"
        elif entry_dir == -1:
            d = "下"
        else:
            d = prev_dir if prev_dir else ""

        h.direction = d
        if i == 0:
            seq = 0
        elif d == prev_dir and d != "":
            seq += 1
        else:
            seq = 0
        h.trend_seq = seq
        prev_dir = d


# ════════════════════════════════════════════════════════════════════
# 9c. Hub Merge After Expansion (扩展中枢合并)
#
# When adjacent hubs are classified as "扩展", they should be merged
# into a single larger-level hub for trend determination purposes.
# (108课 §1.4 / 图解缠论2 §2.4 / 缠论解析 场景B)
# ════════════════════════════════════════════════════════════════════

def merge_expanded_hubs(hubs: list[Hub], analysis_level: str = "daily") -> list[Hub]:
    """Merge adjacent hubs where the later one is classified as expansion.

    Returns a new list where expanded hubs are combined into larger hubs.
    Original hub list is not modified.
    """
    if len(hubs) <= 1:
        return list(hubs)

    base_level = get_hub_level_name(analysis_level, "stroke")
    merged_level = _level_up(base_level) + "中枢"

    merged: list[Hub] = []
    for h in hubs:
        if merged and h.evolution_type == "扩展":
            prev = merged[-1]
            all_strokes = prev.strokes + h.strokes
            new_zg = min(prev.zg, h.zg)
            new_zd = max(prev.zd, h.zd)
            merged[-1] = Hub(
                idx=prev.idx,
                zg=new_zg,
                zd=new_zd,
                gg=max(prev.gg, h.gg),
                dd=min(prev.dd, h.dd),
                strokes=all_strokes,
                entry_stroke=prev.entry_stroke,
                evolution_type="扩展合并",
                is_merged=True,
                hub_level=merged_level,
            )
        elif merged and h.evolution_type == "扩张":
            prev = merged[-1]
            all_strokes = prev.strokes + h.strokes
            merged[-1] = Hub(
                idx=prev.idx,
                zg=min(prev.zg, h.zg),
                zd=max(prev.zd, h.zd),
                gg=max(prev.gg, h.gg),
                dd=min(prev.dd, h.dd),
                strokes=all_strokes,
                entry_stroke=prev.entry_stroke,
                evolution_type="扩张合并",
                is_merged=True,
                hub_level=merged_level,
            )
        else:
            merged.append(Hub(
                idx=len(merged),
                zg=h.zg, zd=h.zd, gg=h.gg, dd=h.dd,
                strokes=list(h.strokes),
                entry_stroke=h.entry_stroke,
                evolution_type=h.evolution_type,
                hub_level=h.hub_level,
            ))

    for i, h in enumerate(merged):
        h.idx = i
    return merged


def merge_expanded_seg_hubs(seg_hubs: list[SegHub], analysis_level: str = "daily") -> list[SegHub]:
    """Same merge logic for segment-level hubs."""
    if len(seg_hubs) <= 1:
        return list(seg_hubs)

    base_level = get_hub_level_name(analysis_level, "segment")
    merged_level = _level_up(base_level) + "中枢"

    merged: list[SegHub] = []
    for h in seg_hubs:
        if merged and h.evolution_type in ("扩展", "扩张"):
            prev = merged[-1]
            all_segs = prev.segments + h.segments
            merged[-1] = SegHub(
                idx=prev.idx,
                zg=min(prev.zg, h.zg),
                zd=max(prev.zd, h.zd),
                gg=max(prev.gg, h.gg),
                dd=min(prev.dd, h.dd),
                segments=all_segs,
                entry_segment=prev.entry_segment,
                evolution_type=h.evolution_type + "合并",
                hub_level=merged_level,
            )
        else:
            merged.append(SegHub(
                idx=len(merged),
                zg=h.zg, zd=h.zd, gg=h.gg, dd=h.dd,
                segments=list(h.segments),
                entry_segment=h.entry_segment,
                evolution_type=h.evolution_type,
                hub_level=h.hub_level,
            ))

    for i, h in enumerate(merged):
        h.idx = i
    return merged


# ════════════════════════════════════════════════════════════════════
# 10. Trend Type (走势类型)
#
# Rules (108课 §1.5):
#   - 0 hubs: follow last stroke direction
#   - 1 hub: check price vs hub position
#   - ≥2 hubs: check hub shift direction
# ════════════════════════════════════════════════════════════════════

def determine_trend(hubs: list[Hub], strokes: list[Stroke]) -> str:
    """Determine current trend type based on hub structure AND current price position.

    Two-dimensional trend determination:
      1. Position shifts (primary): relative position of consecutive hubs
      2. Context direction (secondary): hub.context_direction from first stroke

    Theory (108课 §1.5 + 缠论解析 场景B):
      - 盘整: contains only 1 hub
      - 上涨趋势: ≥2 consecutive non-overlapping hubs shifting upward,
                   confirmed by context_direction == 1 (下-上-下 pattern)
      - 下跌趋势: ≥2 consecutive non-overlapping hubs shifting downward,
                   confirmed by context_direction == -1 (上-下-上 pattern)
    """
    if not hubs:
        if strokes:
            return "上涨" if strokes[-1].direction == 1 else "下跌"
        return "无数据"

    if len(hubs) == 1:
        hub = hubs[0]
        if not strokes:
            return "盘整"
        last = strokes[-1]
        last_price = last.end.high if last.direction == 1 else last.end.low
        if last_price > hub.gg:
            return "中枢上方运行"
        elif last_price < hub.dd:
            return "中枢下方运行"
        return "盘整"

    # --- Dimension 1: trailing consecutive same-direction position shifts ---
    trailing_dir = 0   # 1=up, -1=down
    trailing_count = 0

    for i in range(len(hubs) - 1, 0, -1):
        curr, prev = hubs[i], hubs[i - 1]
        if curr.zd > prev.zg:
            shift = 1
        elif curr.zg < prev.zd:
            shift = -1
        else:
            shift = 0

        if trailing_count == 0:
            if shift != 0:
                trailing_dir = shift
                trailing_count = 1
            else:
                break
        elif shift == trailing_dir:
            trailing_count += 1
        else:
            break

    # --- Dimension 2: context_direction consistency of recent hubs ---
    # In established uptrend: hubs formed by 下-上-下 → context_direction == 1
    # In established downtrend: hubs formed by 上-下-上 → context_direction == -1
    _CTX_WINDOW = 3
    recent_n = min(_CTX_WINDOW, len(hubs))
    recent_ctx = [h.context_direction for h in hubs[-recent_n:]]
    ctx_up = sum(1 for c in recent_ctx if c == 1)
    ctx_dn = sum(1 for c in recent_ctx if c == -1)
    ctx_signal = 0
    if ctx_up == recent_n:
        ctx_signal = 1   # all hubs in uptrend context
    elif ctx_dn == recent_n:
        ctx_signal = -1  # all hubs in downtrend context

    # --- Combine: position shifts + context direction ---
    if trailing_count >= 2 and trailing_dir == 1:
        struct_trend = "上涨趋势"
    elif trailing_count >= 2 and trailing_dir == -1:
        struct_trend = "下跌趋势"
    elif trailing_count == 1 and trailing_dir == 1:
        # 2 hubs with 1 upward shift — check context for confirmation
        if ctx_signal == 1:
            struct_trend = "上涨趋势"  # context confirms trend early
        else:
            struct_trend = "中枢上方运行"
    elif trailing_count == 1 and trailing_dir == -1:
        if ctx_signal == -1:
            struct_trend = "下跌趋势"  # context confirms trend early
        else:
            struct_trend = "中枢下方运行"
    else:
        # No clear position shifts — context_direction may reveal direction
        if ctx_signal == 1 and trailing_count == 0:
            struct_trend = "盘整偏多"
        elif ctx_signal == -1 and trailing_count == 0:
            struct_trend = "盘整偏空"
        else:
            struct_trend = "盘整"

    # Override: if current price has broken below/above the last hub,
    # the structural trend is no longer valid
    h_last = hubs[-1]
    if strokes:
        last = strokes[-1]
        last_price = last.end.high if last.direction == 1 else last.end.low
        if "上涨" in struct_trend and last_price < h_last.dd:
            return "上涨趋势破坏"
        if "下跌" in struct_trend and last_price > h_last.gg:
            return "下跌趋势破坏"

    return struct_trend


def assess_trend_completion(
    trend: str,
    hubs: list[Hub],
    strokes: list[Stroke],
    divergences: list[dict],
    buy_sell_points: list[BuySellPoint],
) -> dict:
    """Assess whether the current trend is complete, in-progress, or suspect.

    Theory basis:
      - 108课 §1.6 "走势终完美": every trend type will eventually complete
      - 图解缠论2 §1.1: completion = sub-level divergence + structural confirmation
      - Trend (上涨/下跌): completed by trend divergence at the end
      - Consolidation (盘整): completed by breakout (3B/3S) or consolidation divergence

    Returns dict with: status, reason, confidence, stage
    """
    result = {"status": "进行中", "reason": "", "confidence": "medium", "stage": ""}

    if not strokes:
        result["stage"] = "无数据"
        return result

    last_stroke = strokes[-1]
    num_hubs = len(hubs)

    recent_divs = [d for d in divergences
                   if d["dt"] == last_stroke.end.dt
                   or (len(strokes) >= 2 and d["dt"] == strokes[-2].end.dt)]
    recent_t1 = [p for p in buy_sell_points
                 if p.type in ("1B", "1S")
                 and (p.dt == last_stroke.end.dt
                      or (len(strokes) >= 2 and p.dt == strokes[-2].end.dt))]

    if "趋势" in trend:
        is_up = "上涨" in trend
        result["stage"] = f"{'上涨' if is_up else '下跌'}趋势, {num_hubs}个中枢"

        trend_divs = [d for d in recent_divs if d["type"] == "trend"]
        if trend_divs:
            latest_div = trend_divs[-1]
            div_dir_match = (is_up and latest_div["direction"] == 1) or \
                            (not is_up and latest_div["direction"] == -1)
            if div_dir_match:
                if recent_t1:
                    result["status"] = "已确认完成"
                    result["reason"] = f"趋势背驰已确认，产生{'一卖' if is_up else '一买'}信号"
                    result["confidence"] = "high"
                else:
                    result["status"] = "疑似完成"
                    result["reason"] = "趋势背驰出现，等待买卖点确认"
                    result["confidence"] = "medium"
                return result

        consol_divs = [d for d in recent_divs if d["type"] == "consolidation"]
        if consol_divs:
            result["status"] = "疑似完成"
            result["reason"] = "盘整背驰出现，趋势动能衰减"
            result["confidence"] = "low"
            return result

        if num_hubs >= 3:
            result["stage"] += "（多中枢，趋势成熟）"
            result["reason"] = "3+中枢，趋势进入后期阶段"
        elif num_hubs == 2:
            result["reason"] = "标准趋势结构，观察c段力度"

    elif "盘整" in trend:
        result["stage"] = f"盘整, {num_hubs}个中枢"

        recent_3b = [p for p in buy_sell_points if p.type == "3B"
                     and (p.dt == last_stroke.end.dt
                          or (len(strokes) >= 2 and p.dt == strokes[-2].end.dt))]
        recent_3s = [p for p in buy_sell_points if p.type == "3S"
                     and (p.dt == last_stroke.end.dt
                          or (len(strokes) >= 2 and p.dt == strokes[-2].end.dt))]

        if recent_3b or recent_3s:
            result["status"] = "已确认完成"
            direction = "向上突破(三买)" if recent_3b else "向下突破(三卖)"
            result["reason"] = f"中枢{direction}，盘整结束"
            result["confidence"] = "high"
            return result

        consol_divs = [d for d in recent_divs if d["type"] == "consolidation"]
        if consol_divs:
            result["status"] = "疑似完成"
            result["reason"] = "盘整背驰出现，可能转向"
            result["confidence"] = "medium"
            return result

        if num_hubs >= 1 and hubs[-1].evolution_type == "延伸":
            ext_count = len(hubs[-1].strokes)
            result["reason"] = f"中枢延伸中（{ext_count}笔），震荡为主"

    elif "中枢" in trend:
        is_above = "上方" in trend
        result["stage"] = f"中枢{'上方' if is_above else '下方'}运行"

        if recent_divs:
            result["status"] = "疑似完成"
            result["reason"] = "背驰出现，可能回归中枢"
            result["confidence"] = "medium"
            return result

        result["reason"] = "等待形成新中枢或回归"

    return result


# ════════════════════════════════════════════════════════════════════
# 11. Divergence Detection (背驰判断)
#
# Rules (108课 §三, 缠论解析 场景C):
#   Trend divergence: a+A+...+B+c structure, c_area < a_area
#   Consolidation divergence: same hub, consecutive exits weaken
# ════════════════════════════════════════════════════════════════════

def _macd_hist_area(bars: list, start_dt: str, end_dt: str,
                    direction: int, dt_idx: dict) -> float:
    """Sum MACD histogram area in a bar range for a given trend direction.

    For downtrend (direction=-1): sum abs(histogram) where histogram < 0
    For uptrend  (direction= 1): sum histogram where histogram > 0
    """
    si = dt_idx.get(start_dt)
    ei = dt_idx.get(end_dt)
    if si is None or ei is None or si >= ei:
        return 0.0
    area = 0.0
    for k in range(si, ei + 1):
        h = bars[k].macd_hist
        if direction == -1 and h < 0:
            area += abs(h)
        elif direction == 1 and h > 0:
            area += h
    return area


def check_trend_divergence(strokes: list[Stroke], hubs: list[Hub],
                           bars: list = None) -> list[dict]:
    """Detect trend divergence (趋势背驰).

    Groups consecutive same-direction hub shifts, then compares the
    segment before the first hub (a) with segment after the last hub (c).

    MACD area is computed from raw bar histogram over the full interval
    between hub exit and next hub entry (not just same-direction strokes).
    """
    divergences = []
    if len(hubs) < 2:
        return divergences

    dt_idx: dict = {}
    if bars:
        dt_idx = {b.dt: i for i, b in enumerate(bars)}

    i = 0
    while i < len(hubs) - 1:
        h0, h1 = hubs[i], hubs[i + 1]
        is_down = h1.zd < h0.zd and h1.zg < h0.zg
        is_up = h1.zd > h0.zd and h1.zg > h0.zg
        if not is_down and not is_up:
            i += 1
            continue

        trend_dir = -1 if is_down else 1

        j = i + 1
        while j + 1 < len(hubs):
            nxt, cur = hubs[j + 1], hubs[j]
            same = ((nxt.zd < cur.zd and nxt.zg < cur.zg) if trend_dir == -1
                    else (nxt.zd > cur.zd and nxt.zg > cur.zg))
            if same:
                j += 1
            else:
                break

        first_hub = hubs[i]
        last_hub = hubs[j]

        # a-segment: from exit of previous hub to entry of first hub.
        # Bar range: previous hub's last stroke end → first hub's first
        # core stroke start.
        a_start_dt = (hubs[i - 1].strokes[-1].end.dt if i > 0
                      else bars[0].dt if bars else None)
        a_end_dt = first_hub.strokes[0].start.dt

        # c-segment: from exit of last hub to entry of next hub (or data end).
        c_start_dt = last_hub.strokes[-1].end.dt
        c_end_dt = (hubs[j + 1].strokes[0].start.dt if j + 1 < len(hubs)
                    else bars[-1].dt if bars else None)

        # Keep stroke-based seg_a/seg_c for DIF, volume, structure, etc.
        a_start_idx = hubs[i - 1].strokes[-1].idx if i > 0 else 0
        first_hub_start = first_hub.strokes[0].idx

        all_in_a = [s for s in strokes
                    if a_start_idx <= s.idx < first_hub_start]
        if len(all_in_a) >= 3:
            last_hidden_end = -1
            k = 0
            while k < len(all_in_a) - 2:
                s1, s2, s3 = all_in_a[k], all_in_a[k + 1], all_in_a[k + 2]
                highs = [max(x.start.high, x.end.high) for x in (s1, s2, s3)]
                lows = [min(x.start.low, x.end.low) for x in (s1, s2, s3)]
                zg, zd = min(highs), max(lows)
                if zg > zd:
                    end_k = k + 2
                    while end_k + 1 < len(all_in_a):
                        ns = all_in_a[end_k + 1]
                        nh = max(ns.start.high, ns.end.high)
                        nl = min(ns.start.low, ns.end.low)
                        new_zg, new_zd = min(zg, nh), max(zd, nl)
                        if new_zg > new_zd:
                            zg, zd = new_zg, new_zd
                            end_k += 1
                        else:
                            break
                    last_hidden_end = end_k
                    k = end_k + 1
                else:
                    k += 1
            if last_hidden_end >= 0:
                a_start_idx = all_in_a[last_hidden_end].idx
                a_start_dt = all_in_a[last_hidden_end].start.dt

        seg_a = [s for s in strokes
                 if a_start_idx <= s.idx < first_hub_start
                 and s.direction == trend_dir]

        c_end_idx = hubs[j + 1].strokes[0].idx if j + 1 < len(hubs) else strokes[-1].idx + 1
        seg_c = [s for s in strokes
                 if s.idx > last_hub.strokes[-1].idx
                 and s.idx < c_end_idx
                 and s.direction == trend_dir]

        if seg_a and seg_c:
            if trend_dir == -1:
                c_extreme = min(s.end.low for s in seg_c)
                if c_extreme >= last_hub.zd:
                    i = j + 1
                    continue
            else:
                c_extreme = max(s.end.high for s in seg_c)
                if c_extreme <= last_hub.zg:
                    i = j + 1
                    continue

            # Bar-level MACD histogram area (full interval, not just
            # same-direction strokes).
            if bars and a_start_dt and a_end_dt and c_start_dt and c_end_dt:
                a_area = _macd_hist_area(bars, a_start_dt, a_end_dt,
                                         trend_dir, dt_idx)
                c_area = _macd_hist_area(bars, c_start_dt, c_end_dt,
                                         trend_dir, dt_idx)
            else:
                a_area = sum(s.macd_area for s in seg_a)
                c_area = sum(s.macd_area for s in seg_c)

            if a_area > 0 and c_area < a_area:
                area_ratio = c_area / a_area
                area_diverged = area_ratio < 0.9
                # DIF extreme comparison
                if trend_dir == -1:
                    a_dif = min(s.dif_extreme for s in seg_a)
                    c_dif = min(s.dif_extreme for s in seg_c)
                    dif_diverged = c_dif > a_dif
                else:
                    a_dif = max(s.dif_extreme for s in seg_a)
                    c_dif = max(s.dif_extreme for s in seg_c)
                    dif_diverged = c_dif < a_dif
                # Histogram peak comparison
                a_peak = max(s.hist_peak for s in seg_a)
                c_peak = max(s.hist_peak for s in seg_c)
                hist_peak_diverged = c_peak < a_peak

                # Volume decline comparison (P3)
                a_avg_vol = (sum(s.avg_volume for s in seg_a) / len(seg_a)
                             if seg_a else 0)
                c_avg_vol = (sum(s.avg_volume for s in seg_c) / len(seg_c)
                             if seg_c else 0)
                vol_diverged = (c_avg_vol < a_avg_vol * 0.8
                                if a_avg_vol > 0 else False)

                dims = sum([area_diverged, dif_diverged, hist_peak_diverged])
                if dims < 2:
                    i = j + 1
                    continue
                # Volume divergence boosts confidence but not required for trigger
                if vol_diverged and dims == 2:
                    div_confidence = "high"
                else:
                    div_confidence = "high" if dims == 3 else "medium"

                trigger = seg_c[-1]
                hub_tags = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                seg_tags = "abcdefghijklmnopqrstuvwxyz"
                trend_hubs = hubs[i:j + 1]
                structure = [
                    {"tag": seg_tags[0], "start_dt": seg_a[0].start.dt,
                     "end_dt": seg_a[-1].end.dt},
                ]
                for hi, th in enumerate(trend_hubs):
                    h_tag = hub_tags[hi] if hi < len(hub_tags) else f"H{hi}"
                    structure.append(
                        {"tag": h_tag,
                         "start_dt": th.strokes[0].start.dt,
                         "end_dt": th.strokes[-1].end.dt,
                         "zg": th.zg, "zd": th.zd})
                    if hi < len(trend_hubs) - 1:
                        nxt_hub = trend_hubs[hi + 1]
                        s_tag = seg_tags[hi + 1] if hi + 1 < len(seg_tags) else f"s{hi+1}"
                        structure.append(
                            {"tag": s_tag,
                             "start_dt": th.strokes[-1].end.dt,
                             "end_dt": nxt_hub.strokes[0].start.dt})
                final_seg_idx = len(trend_hubs)
                final_tag = seg_tags[final_seg_idx] if final_seg_idx < len(seg_tags) else f"s{final_seg_idx}"
                structure.append(
                    {"tag": final_tag, "start_dt": seg_c[0].start.dt,
                     "end_dt": seg_c[-1].end.dt})
                divergences.append({
                    "type": "trend",
                    "direction": trend_dir,
                    "dt": trigger.end.dt,
                    "a_area": round(a_area, 2),
                    "c_area": round(c_area, 2),
                    "ratio": round(c_area / a_area, 4),
                    "area_diverged": area_diverged,
                    "dif_diverged": dif_diverged,
                    "a_dif": round(a_dif, 4),
                    "c_dif": round(c_dif, 4),
                    "hist_peak_diverged": hist_peak_diverged,
                    "a_hist_peak": round(a_peak, 4),
                    "c_hist_peak": round(c_peak, 4),
                    "div_dims": dims,
                    "div_confidence": div_confidence,
                    "hub_count": j - i + 1,
                    "hub_idx": last_hub.idx,
                    "price": trigger.end.low if trend_dir == -1 else trigger.end.high,
                    "a_start_dt": seg_a[0].start.dt,
                    "a_end_dt": seg_a[-1].end.dt,
                    "a_stroke_range": (seg_a[0].idx, seg_a[-1].idx),
                    "c_start_dt": seg_c[0].start.dt,
                    "c_end_dt": seg_c[-1].end.dt,
                    "c_stroke_range": (seg_c[0].idx, seg_c[-1].idx),
                    "vol_diverged": vol_diverged,
                    "structure": structure,
                })

        i = j + 1

    return divergences


def check_consolidation_divergence(strokes: list[Stroke],
                                   hubs: list[Hub]) -> list[dict]:
    """Detect consolidation divergence (盘整背驰).

    Compares consecutive same-direction exits from a hub.
    """
    divergences = []
    all_hub_stroke_ids = set()
    for h in hubs:
        for s in h.strokes:
            all_hub_stroke_ids.add(s.idx)

    for hub in hubs:
        relevant = list(hub.strokes)
        hub_end_idx = hub.strokes[-1].idx
        extra = 0
        for s in strokes:
            if s.idx > hub_end_idx and s.idx not in all_hub_stroke_ids:
                relevant.append(s)
                extra += 1
                if extra >= 6:
                    break

        exit_up = sorted(
            [s for s in relevant
             if s.direction == 1 and max(s.start.high, s.end.high) >= hub.zg],
            key=lambda s: s.idx,
        )
        exit_down = sorted(
            [s for s in relevant
             if s.direction == -1 and min(s.start.low, s.end.low) <= hub.zd],
            key=lambda s: s.idx,
        )

        for exits in [exit_up, exit_down]:
            for k in range(1, len(exits)):
                prev_e, curr_e = exits[k - 1], exits[k]
                area_ratio = (curr_e.macd_area / prev_e.macd_area
                              if prev_e.macd_area > 0 else 1.0)
                if prev_e.macd_area > 0 and area_ratio < 0.9:
                    area_diverged = True
                elif prev_e.macd_area > 0 and curr_e.macd_area < prev_e.macd_area:
                    area_diverged = False  # ratio 0.9~1.0: too weak

                if prev_e.macd_area > 0 and curr_e.macd_area < prev_e.macd_area:
                    # DIF extreme comparison
                    if curr_e.direction == -1:
                        dif_diverged = curr_e.dif_extreme > prev_e.dif_extreme
                    else:
                        dif_diverged = curr_e.dif_extreme < prev_e.dif_extreme
                    # Histogram peak comparison
                    hist_peak_diverged = curr_e.hist_peak < prev_e.hist_peak

                    # Volume decline (P3)
                    vol_diverged = (curr_e.avg_volume < prev_e.avg_volume * 0.8
                                    if prev_e.avg_volume > 0 else False)

                    dims = sum([area_diverged, dif_diverged, hist_peak_diverged])
                    if dims < 2:
                        continue
                    if vol_diverged and dims == 2:
                        div_confidence = "high"
                    else:
                        div_confidence = "high" if dims == 3 else "medium"

                    divergences.append({
                        "type": "consolidation",
                        "direction": curr_e.direction,
                        "dt": curr_e.end.dt,
                        "prev_area": round(prev_e.macd_area, 2),
                        "curr_area": round(curr_e.macd_area, 2),
                        "ratio": round(curr_e.macd_area / prev_e.macd_area, 4),
                        "area_diverged": area_diverged,
                        "dif_diverged": dif_diverged,
                        "prev_dif": round(prev_e.dif_extreme, 4),
                        "curr_dif": round(curr_e.dif_extreme, 4),
                        "hist_peak_diverged": hist_peak_diverged,
                        "prev_hist_peak": round(prev_e.hist_peak, 4),
                        "curr_hist_peak": round(curr_e.hist_peak, 4),
                        "div_dims": dims,
                        "div_confidence": div_confidence,
                        "vol_diverged": vol_diverged,
                        "hub_idx": hub.idx,
                        "price": (curr_e.end.low if curr_e.direction == -1
                                  else curr_e.end.high),
                        "prev_stroke_idx": prev_e.idx,
                        "curr_stroke_idx": curr_e.idx,
                        "prev_start_dt": prev_e.start.dt,
                        "prev_end_dt": prev_e.end.dt,
                        "curr_start_dt": curr_e.start.dt,
                        "curr_end_dt": curr_e.end.dt,
                    })

    return divergences


# ════════════════════════════════════════════════════════════════════
# 11b. Advanced MACD Diagnostics
#
# Knowledge-base requirements not covered by basic divergence detection:
#   - Area ×2 estimation (108课 §三: 已见面积×2预估)
#   - MA-area cross-verification (缠论解析 场景C)
#   - MACD parameter doubling (土匪注解 §3.1)
#   - Double 0-axis pullback detection (108课详解 §三)
# ════════════════════════════════════════════════════════════════════

def estimate_area_2x(strokes: list[Stroke], hubs: list[Hub]) -> list[dict]:
    """Estimate potential divergence using area×2 for incomplete c segments.

    Per 108课 §三: when the last stroke's histogram bars are still extending
    but slowing down, use current_area × 2 as the estimated final area.
    If estimated_c_area < a_area, flag as potential divergence.

    This is useful for early warning before the c segment completes.
    """
    estimates = []
    if len(hubs) < 2:
        return estimates

    i = 0
    while i < len(hubs) - 1:
        h0, h1 = hubs[i], hubs[i + 1]
        is_down = h1.zd < h0.zd and h1.zg < h0.zg
        is_up = h1.zd > h0.zd and h1.zg > h0.zg
        if not is_down and not is_up:
            i += 1
            continue

        trend_dir = -1 if is_down else 1
        j = i + 1
        while j + 1 < len(hubs):
            nxt, cur = hubs[j + 1], hubs[j]
            same = ((nxt.zd < cur.zd and nxt.zg < cur.zg) if trend_dir == -1
                    else (nxt.zd > cur.zd and nxt.zg > cur.zg))
            if same:
                j += 1
            else:
                break

        first_hub, last_hub = hubs[i], hubs[j]
        a_low = hubs[i - 1].strokes[-1].idx if i > 0 else 0
        fhs = first_hub.strokes[0].idx
        all_in_a2 = [s for s in strokes if a_low <= s.idx < fhs]
        eff_a_start = a_low
        if len(all_in_a2) >= 3:
            last_he = -1
            kk = 0
            while kk < len(all_in_a2) - 2:
                s1, s2, s3 = all_in_a2[kk], all_in_a2[kk+1], all_in_a2[kk+2]
                hs = [max(x.start.high, x.end.high) for x in (s1, s2, s3)]
                ls = [min(x.start.low, x.end.low) for x in (s1, s2, s3)]
                zg2, zd2 = min(hs), max(ls)
                if zg2 > zd2:
                    ek = kk + 2
                    while ek + 1 < len(all_in_a2):
                        ns = all_in_a2[ek + 1]
                        nh = max(ns.start.high, ns.end.high)
                        nl = min(ns.start.low, ns.end.low)
                        nzg, nzd = min(zg2, nh), max(zd2, nl)
                        if nzg > nzd:
                            zg2, zd2 = nzg, nzd
                            ek += 1
                        else:
                            break
                    last_he = ek
                    kk = ek + 1
                else:
                    kk += 1
            if last_he >= 0:
                eff_a_start = all_in_a2[last_he].idx

        seg_a = [s for s in strokes
                 if eff_a_start <= s.idx < fhs and s.direction == trend_dir]
        seg_c = [s for s in strokes
                 if s.idx > last_hub.strokes[-1].idx and s.direction == trend_dir]

        if seg_a and seg_c:
            a_area = sum(s.macd_area for s in seg_a)
            c_area = sum(s.macd_area for s in seg_c)
            c_area_2x = c_area * 2

            is_last_stroke = seg_c[-1].idx == strokes[-1].idx or \
                             seg_c[-1].idx >= strokes[-1].idx - 1
            if a_area > 0 and is_last_stroke:
                if c_area < a_area:
                    status = "已背驰"
                elif c_area_2x < a_area:
                    status = "预估背驰（面积×2仍不足）"
                elif c_area_2x < a_area * 1.2:
                    status = "接近背驰（面积×2刚好或略超）"
                else:
                    status = "力度充足"
                estimates.append({
                    "status": status,
                    "a_area": round(a_area, 2),
                    "c_area_current": round(c_area, 2),
                    "c_area_2x": round(c_area_2x, 2),
                    "ratio_current": round(c_area / a_area, 4) if a_area else 0,
                    "ratio_2x": round(c_area_2x / a_area, 4) if a_area else 0,
                    "direction": trend_dir,
                    "c_start_dt": seg_c[0].start.dt,
                    "c_end_dt": seg_c[-1].end.dt,
                    "hub_idx": last_hub.idx,
                })
        i = j + 1

    return estimates


def compute_ma_area_divergence(
    strokes: list[Stroke], hubs: list[Hub], bars: list[RawBar],
) -> list[dict]:
    """Cross-verify divergence using MA5/MA10 enclosed area.

    Per 缠论解析 场景C: the area between short MA and long MA
    (between two "kisses") can serve as an independent divergence measure.
    Computes the area between MA5 and MA10 within each stroke's range.
    """
    if not bars or not hasattr(bars[0], 'ma5'):
        compute_ma(bars)

    dt_to_idx = {b.dt: b.idx for b in bars}
    divergences = []

    if len(hubs) < 2:
        return divergences

    def _stroke_ma_area(stroke: Stroke) -> float:
        si = dt_to_idx.get(stroke.start.dt, 0)
        ei = dt_to_idx.get(stroke.end.dt, len(bars) - 1)
        area = 0.0
        for ki in range(si, min(ei + 1, len(bars))):
            b = bars[ki]
            area += abs(getattr(b, 'ma5', 0) - getattr(b, 'ma10', 0))
        return area

    i = 0
    while i < len(hubs) - 1:
        h0, h1 = hubs[i], hubs[i + 1]
        is_down = h1.zd < h0.zd and h1.zg < h0.zg
        is_up = h1.zd > h0.zd and h1.zg > h0.zg
        if not is_down and not is_up:
            i += 1
            continue

        trend_dir = -1 if is_down else 1
        j = i + 1
        while j + 1 < len(hubs):
            nxt, cur = hubs[j + 1], hubs[j]
            same = ((nxt.zd < cur.zd and nxt.zg < cur.zg) if trend_dir == -1
                    else (nxt.zd > cur.zd and nxt.zg > cur.zg))
            if same:
                j += 1
            else:
                break

        first_hub, last_hub = hubs[i], hubs[j]
        seg_a = [s for s in strokes
                 if s.idx < first_hub.strokes[0].idx and s.direction == trend_dir]
        seg_c = [s for s in strokes
                 if s.idx > last_hub.strokes[-1].idx and s.direction == trend_dir]

        if seg_a and seg_c:
            a_ma_area = sum(_stroke_ma_area(s) for s in seg_a)
            c_ma_area = sum(_stroke_ma_area(s) for s in seg_c)
            if a_ma_area > 0:
                ma_diverged = c_ma_area < a_ma_area
                divergences.append({
                    "type": "ma_area",
                    "direction": trend_dir,
                    "a_ma_area": round(a_ma_area, 4),
                    "c_ma_area": round(c_ma_area, 4),
                    "ratio": round(c_ma_area / a_ma_area, 4),
                    "ma_diverged": ma_diverged,
                    "a_start_dt": seg_a[0].start.dt,
                    "c_end_dt": seg_c[-1].end.dt,
                    "hub_idx": last_hub.idx,
                })
        i = j + 1

    return divergences


def compute_doubled_macd_area(
    strokes: list[Stroke], hubs: list[Hub], bars: list[RawBar],
) -> list[dict]:
    """Re-verify divergence using doubled MACD parameters (24,52,18).

    Per 土匪注解 §3.1: when histogram bars are fragmented (not contiguous),
    doubling MACD parameters can produce more continuous bars for area comparison.
    Returns additional divergence data for the same a/c structure.
    """
    alt = compute_macd_alt(bars)
    dt_to_idx = {b.dt: b.idx for b in bars}
    results = []

    if len(hubs) < 2:
        return results

    def _seg_alt_area(seg: list[Stroke], direction: int) -> float:
        area = 0.0
        for s in seg:
            si = dt_to_idx.get(s.start.dt, 0)
            ei = dt_to_idx.get(s.end.dt, len(bars) - 1)
            for ki in range(si, min(ei + 1, len(bars))):
                _, _, hist = alt[ki]
                if (direction == 1 and hist > 0) or (direction == -1 and hist < 0):
                    area += abs(hist)
        return area

    i = 0
    while i < len(hubs) - 1:
        h0, h1 = hubs[i], hubs[i + 1]
        is_down = h1.zd < h0.zd and h1.zg < h0.zg
        is_up = h1.zd > h0.zd and h1.zg > h0.zg
        if not is_down and not is_up:
            i += 1
            continue

        trend_dir = -1 if is_down else 1
        j = i + 1
        while j + 1 < len(hubs):
            nxt, cur = hubs[j + 1], hubs[j]
            same = ((nxt.zd < cur.zd and nxt.zg < cur.zg) if trend_dir == -1
                    else (nxt.zd > cur.zd and nxt.zg > cur.zg))
            if same:
                j += 1
            else:
                break

        first_hub, last_hub = hubs[i], hubs[j]
        seg_a = [s for s in strokes
                 if s.idx < first_hub.strokes[0].idx and s.direction == trend_dir]
        seg_c = [s for s in strokes
                 if s.idx > last_hub.strokes[-1].idx and s.direction == trend_dir]

        if seg_a and seg_c:
            a_alt = _seg_alt_area(seg_a, trend_dir)
            c_alt = _seg_alt_area(seg_c, trend_dir)
            if a_alt > 0:
                results.append({
                    "type": "doubled_macd",
                    "direction": trend_dir,
                    "params": "(24,52,18)",
                    "a_area_alt": round(a_alt, 2),
                    "c_area_alt": round(c_alt, 2),
                    "ratio_alt": round(c_alt / a_alt, 4),
                    "alt_diverged": c_alt < a_alt,
                    "hub_idx": last_hub.idx,
                })
        i = j + 1

    return results


def detect_double_pullback_zero(bars: list[RawBar]) -> list[dict]:
    """Detect double 0-axis pullback patterns in DIF.

    Per 108课详解 §三 / 土匪注解 §3.2: when DIF pulls back toward the 0-axis
    twice and fails to cross, it often signals a type-3 buy/sell setup.

    Detects:
      - From above: DIF dips toward 0 twice without crossing → bearish warning
      - From below: DIF rises toward 0 twice without crossing → bullish potential
    """
    if len(bars) < 20:
        return []

    warnings = []
    near_zero_pct = 0.15
    # Smooth DIF into segments above/below zero
    segments: list[dict] = []
    seg_start = 0
    above = bars[0].dif > 0

    for i in range(1, len(bars)):
        now_above = bars[i].dif > 0
        if now_above != above:
            segments.append({
                "start": seg_start, "end": i - 1,
                "above": above,
                "min_dif": min(bars[k].dif for k in range(seg_start, i)),
                "max_dif": max(bars[k].dif for k in range(seg_start, i)),
            })
            seg_start = i
            above = now_above
    segments.append({
        "start": seg_start, "end": len(bars) - 1,
        "above": above,
        "min_dif": min(bars[k].dif for k in range(seg_start, len(bars))),
        "max_dif": max(bars[k].dif for k in range(seg_start, len(bars))),
    })

    # Look for near-zero touches within above-zero or below-zero segments
    for seg in segments:
        if seg["end"] - seg["start"] < 5:
            continue
        span = seg["end"] - seg["start"] + 1
        dif_range = seg["max_dif"] - seg["min_dif"]
        if dif_range < 1e-6:
            continue

        near_zero_threshold = dif_range * near_zero_pct
        pullback_dates = []

        if seg["above"]:
            for k in range(seg["start"], seg["end"] + 1):
                if bars[k].dif < near_zero_threshold and bars[k].dif > 0:
                    if not pullback_dates or k - pullback_dates[-1][-1] > 3:
                        pullback_dates.append([k])
                    else:
                        pullback_dates[-1].append(k)
        else:
            for k in range(seg["start"], seg["end"] + 1):
                if bars[k].dif > -near_zero_threshold and bars[k].dif < 0:
                    if not pullback_dates or k - pullback_dates[-1][-1] > 3:
                        pullback_dates.append([k])
                    else:
                        pullback_dates[-1].append(k)

        if len(pullback_dates) >= 2:
            first_touch = pullback_dates[0][0]
            second_touch = pullback_dates[1][0]
            if seg["above"]:
                warnings.append({
                    "type": "double_pullback_from_above",
                    "direction": "bearish",
                    "description": "DIF两次接近0轴但未下穿，空头尝试失败→注意后续三卖",
                    "first_touch_dt": bars[first_touch].dt,
                    "second_touch_dt": bars[second_touch].dt,
                    "first_dif": round(bars[first_touch].dif, 4),
                    "second_dif": round(bars[second_touch].dif, 4),
                })
            else:
                warnings.append({
                    "type": "double_pullback_from_below",
                    "direction": "bullish",
                    "description": "DIF两次接近0轴但未上穿，多头尝试失败→注意后续三买",
                    "first_touch_dt": bars[first_touch].dt,
                    "second_touch_dt": bars[second_touch].dt,
                    "first_dif": round(bars[first_touch].dif, 4),
                    "second_dif": round(bars[second_touch].dif, 4),
                })

    return warnings


# ════════════════════════════════════════════════════════════════════
# 12. Buy/Sell Point Identification (三类买卖点)
#
# Rules (108课 §二, 图解缠论 §二, 缠论解析 场景C):
#   Type 1: Trend divergence → 1B/1S
#   Type 2: First pullback/rally after Type 1 doesn't break extreme
#   Type 3: After leaving hub, pullback doesn't break ZG(buy)/ZD(sell)
#   Consolidation: PB/PS from consolidation divergence
# ════════════════════════════════════════════════════════════════════

_STRENGTH_ZH_ALL = {
    "strongest": "最强", "strong": "强势",
    "standard": "标准", "weak": "弱",
}


def _grade_type1(div: dict):
    """Grade Type 1 buy/sell point: strength (value) + confidence (certainty).

    Strength = operational value if signal is correct:
      area_ratio, hub_count
    Confidence = certainty that the divergence is real:
      div_dims, DIF convergence, area_ratio margin
    """
    tags: list[str] = []
    str_score = 0
    str_details: list[dict] = []
    conf_score = 0
    conf_details: list[dict] = []

    dims = div.get("div_dims", 2)
    ratio = div.get("ratio", 1.0)
    hub_count = div.get("hub_count", 1)

    # === Strength dimensions ===
    # S1: divergence magnitude
    s1 = 0
    if ratio < 0.4:
        s1 = 3; s1_l = f"强背驰({ratio:.2f})"; tags.append("强背驰")
    elif ratio < 0.6:
        s1 = 2; s1_l = f"较强({ratio:.2f})"
    elif ratio < 0.75:
        s1 = 1; s1_l = f"一般({ratio:.2f})"
    else:
        s1 = -1; s1_l = f"弱背驰({ratio:.2f})"; tags.append("弱背驰")
    str_score += s1
    str_details.append({"dim": "背驰强度", "label": s1_l, "score": s1})

    # S2: hub count (more hubs = bigger move expected)
    s2 = 0
    if hub_count >= 3:
        s2 = 3; s2_l = f"{hub_count}中枢趋势"; tags.append(s2_l)
    elif hub_count == 2:
        s2 = 2; s2_l = "双中枢趋势"; tags.append("双中枢趋势")
    else:
        s2 = -1; s2_l = "单中枢"; tags.append("单中枢")
    str_score += s2
    str_details.append({"dim": "中枢数量", "label": s2_l, "score": s2})

    # === Confidence dimensions ===
    # C1: divergence dimensions
    c1 = 0
    if dims == 3:
        c1 = 3; c1_l = "三维背驰"; tags.append("三维背驰")
    else:
        c1 = 1; c1_l = "二维背驰"; tags.append("二维背驰")
    conf_score += c1
    conf_details.append({"dim": "背驰维度", "label": c1_l, "score": c1})

    # C2: DIF convergence
    a_dif = abs(div.get("a_dif", 0))
    c_dif = abs(div.get("c_dif", 0))
    if a_dif > 0:
        dif_r = c_dif / a_dif
        if dif_r < 0.5:
            c2 = 2; c2_l = f"DIF强收敛({dif_r:.2f})"; tags.append("DIF强收敛")
        elif dif_r < 0.8:
            c2 = 1; c2_l = f"DIF收敛({dif_r:.2f})"
        else:
            c2 = 0; c2_l = f"DIF一般({dif_r:.2f})"
        conf_score += c2
        conf_details.append({"dim": "DIF收敛", "label": c2_l, "score": c2})

    # C3: area ratio certainty (marginal divergence = uncertain)
    c3 = 0
    if ratio < 0.5:
        c3 = 2; c3_l = f"明确背驰({ratio:.2f})"
    elif ratio < 0.7:
        c3 = 1; c3_l = f"较明确({ratio:.2f})"
    elif ratio >= 0.85:
        c3 = -2; c3_l = f"边缘背驰({ratio:.2f})"; tags.append("边缘背驰⚠")
    else:
        c3 = 0; c3_l = f"尚可({ratio:.2f})"
    conf_score += c3
    conf_details.append({"dim": "背驰确定性", "label": c3_l, "score": c3})

    strength = ("strongest" if str_score >= 5 else
                "strong" if str_score >= 3 else
                "standard" if str_score >= 1 else "weak")
    conf = ("high" if conf_score >= 5 else
            "medium" if conf_score >= 2 else "low")

    return (strength, conf, tags,
            str_score, str_details, conf_score, conf_details)


def _grade_pb_ps(div: dict):
    """Grade consolidation divergence: strength + confidence.

    Strength = reversal magnitude potential (area ratio).
    Confidence = certainty pattern is real (dims, ratio margin).
    PB/PS are inherently weaker; strength caps at "strong".
    """
    tags: list[str] = []
    str_score = 0
    str_details: list[dict] = []
    conf_score = 0
    conf_details: list[dict] = []

    dims = div.get("div_dims", 2)
    ratio = div.get("ratio", 1.0)

    # === Strength: area ratio ===
    s1 = 0
    if ratio < 0.5:
        s1 = 2; s1_l = f"强盘背({ratio:.2f})"; tags.append("强盘背")
    elif ratio < 0.7:
        s1 = 1; s1_l = f"较强({ratio:.2f})"
    else:
        s1 = 0; s1_l = f"一般({ratio:.2f})"
    str_score += s1
    str_details.append({"dim": "背驰强度", "label": s1_l, "score": s1})

    # === Confidence: dims + ratio margin ===
    c1 = 0
    if dims == 3:
        c1 = 2; c1_l = "三维盘背"; tags.append("三维盘背")
    else:
        c1 = 0; c1_l = "二维盘背"; tags.append("二维盘背")
    conf_score += c1
    conf_details.append({"dim": "背驰维度", "label": c1_l, "score": c1})

    c2 = 0
    if ratio < 0.5:
        c2 = 2; c2_l = f"明确盘背({ratio:.2f})"
    elif ratio < 0.7:
        c2 = 1; c2_l = f"较明确({ratio:.2f})"
    elif ratio >= 0.85:
        c2 = -1; c2_l = f"边缘盘背({ratio:.2f})"
    else:
        c2 = 0; c2_l = f"尚可({ratio:.2f})"
    conf_score += c2
    conf_details.append({"dim": "盘背确定性", "label": c2_l, "score": c2})

    strength = ("strong" if str_score >= 2 else
                "standard" if str_score >= 1 else "weak")
    conf = ("medium" if conf_score >= 3 else
            "medium" if conf_score >= 1 else "low")

    return (strength, conf, tags,
            str_score, str_details, conf_score, conf_details)


def _grade_type2(t1: 'BuySellPoint', first_move: 'Stroke',
                 pullback: 'Stroke', ref_hub: 'Hub | None',
                 is_buy: bool):
    """Grade Type 2 buy/sell point: strength + confidence.

    Strength = operational value: merged position, pullback depth.
    Confidence = pattern certainty: DIF, volume, invalidation proximity.
    """
    tags: list[str] = []
    str_score = 0
    str_details: list[dict] = []
    conf_score = 0
    conf_details: list[dict] = []

    if is_buy:
        move_range = first_move.end.high - t1.price
        pb_depth = first_move.end.high - pullback.end.low
        merged = ref_hub and pullback.end.low > ref_hub.zg
        dif_val = pullback.dif_extreme
    else:
        move_range = t1.price - first_move.end.low
        pb_depth = pullback.end.high - first_move.end.low
        merged = ref_hub and pullback.end.high < ref_hub.zd
        dif_val = pullback.dif_extreme

    # === Strength dimensions ===
    # S1: merged with T3
    s1 = 0
    if merged:
        s1 = 3; s1_l = "二买三买合一" if is_buy else "二卖三卖合一"; tags.append(s1_l)
    else:
        s1_l = "未合一"
    str_score += s1
    str_details.append({"dim": "位置关系", "label": s1_l, "score": s1})

    # S2: pullback depth
    s2 = 0; s2_l = ""
    pb_ratio = 0.0
    if move_range > 0:
        pb_ratio = pb_depth / move_range
        if pb_ratio < 0.236:
            s2 = 4; s2_l = f"极浅回调({pb_ratio:.1%})"; tags.append("极浅回调")
        elif pb_ratio < 0.382:
            s2 = 3; s2_l = f"浅回调({pb_ratio:.1%})"; tags.append("浅回调")
        elif pb_ratio < 0.500:
            s2 = 2; s2_l = f"中等({pb_ratio:.1%})"
        elif pb_ratio < 0.618:
            s2 = 1; s2_l = f"偏深({pb_ratio:.1%})"
        elif pb_ratio < 0.786:
            s2_l = f"深回调({pb_ratio:.1%})"
        else:
            s2 = -1; s2_l = f"深回调({pb_ratio:.1%})"; tags.append("深回调")
    else:
        s2_l = "无回调"
    str_score += s2
    str_details.append({"dim": "回调深度", "label": s2_l, "score": s2})

    # === Confidence dimensions ===
    # C1: DIF position
    c1 = 0; c1_l = ""
    if is_buy:
        if dif_val > 0:
            c1 = 1; c1_l = f"DIF>0({dif_val:.4f})"; tags.append("DIF>0")
        elif dif_val < 0:
            c1 = -1; c1_l = f"DIF<0({dif_val:.4f})"
        else:
            c1_l = "DIF≈0"
    else:
        if dif_val < 0:
            c1 = 1; c1_l = f"DIF<0({dif_val:.4f})"; tags.append("DIF<0")
        elif dif_val > 0:
            c1 = -1; c1_l = f"DIF>0({dif_val:.4f})"
        else:
            c1_l = "DIF≈0"
    conf_score += c1
    conf_details.append({"dim": "DIF位置", "label": c1_l, "score": c1})

    # C2: volume confirmation
    c2 = 0; c2_l = ""
    if first_move.avg_volume > 0 and pullback.avg_volume > 0:
        pb_vol_ratio = pullback.avg_volume / first_move.avg_volume
        if is_buy:
            if pb_vol_ratio < 0.6:
                c2 = 2; c2_l = f"缩量回调✓({pb_vol_ratio:.0%})"; tags.append(c2_l)
            elif pb_vol_ratio < 0.8:
                c2 = 1; c2_l = f"温和缩量({pb_vol_ratio:.0%})"; tags.append(c2_l)
            elif pb_vol_ratio > 1.5:
                c2 = -1; c2_l = f"放量回调⚠({pb_vol_ratio:.0%})"; tags.append(c2_l)
            else:
                c2_l = f"量能正常({pb_vol_ratio:.0%})"
        else:
            if pb_vol_ratio < 0.6:
                c2 = 2; c2_l = f"缩量反弹✓({pb_vol_ratio:.0%})"; tags.append(c2_l)
            elif pb_vol_ratio < 0.8:
                c2 = 1; c2_l = f"温和缩量({pb_vol_ratio:.0%})"; tags.append(c2_l)
            elif pb_vol_ratio > 1.5:
                c2 = -1; c2_l = f"放量反弹⚠({pb_vol_ratio:.0%})"; tags.append(c2_l)
            else:
                c2_l = f"量能正常({pb_vol_ratio:.0%})"
    if c2_l:
        conf_score += c2
        conf_details.append({"dim": "量价配合", "label": c2_l, "score": c2})

    # C3: invalidation proximity
    c3 = 0; c3_l = ""
    if move_range > 0:
        if pb_ratio >= 0.786:
            c3 = -2; c3_l = f"接近失效位({pb_ratio:.1%})"
        elif pb_ratio >= 0.618:
            c3 = -1; c3_l = f"距失效位较近({pb_ratio:.1%})"
        elif pb_ratio < 0.382:
            c3 = 2; c3_l = f"远离失效位({pb_ratio:.1%})"
        else:
            c3 = 1; c3_l = f"距失效位安全({pb_ratio:.1%})"
        conf_score += c3
        conf_details.append({"dim": "失效距离", "label": c3_l, "score": c3})

    strength = ("strongest" if str_score >= 6 else
                "strong" if str_score >= 3 else
                "standard" if str_score >= 1 else "weak")
    conf = ("high" if conf_score >= 4 else
            "medium" if conf_score >= 1 else "low")

    return (strength, conf, tags,
            str_score, str_details, conf_score, conf_details)


def find_buy_sell_points(
    hubs: list[Hub],
    strokes: list[Stroke],
    bars: list[RawBar],
    trend_divs: list[dict],
    consol_divs: list[dict],
    level: str,
    segments: list[Segment] | None = None,
    raw_hubs: list[Hub] | None = None,
) -> list[BuySellPoint]:
    """Identify all three types of buy/sell points.

    ``hubs`` is used for Type 1/2 signals (typically merged hubs for trend
    consistency).  ``raw_hubs`` (pre-merge, individual hubs) is used for
    Type 3 signals because 3B/3S depend on breakout/pullback between
    *adjacent* hubs — merged hubs absorb all strokes and leave nothing to
    scan.
    """
    stroke_to_seg: dict[int, int] = {}
    if segments:
        for seg in segments:
            for s in seg.strokes:
                stroke_to_seg[s.idx] = seg.idx

    def _stroke_seg(stroke: Stroke | None) -> tuple[int, int]:
        """Return 1-indexed (stroke_num, seg_num) for display labels."""
        if stroke is None:
            return (0, 0)
        return (stroke.idx + 1, stroke_to_seg.get(stroke.idx, -2) + 1)

    points = []

    # ── Type 1: Trend Divergence (趋势背驰 → 一买/一卖) ──
    for div in trend_divs:
        stroke = _find_stroke_by_dt(strokes, div["dt"])
        if not stroke:
            continue
        s_idx, d_idx = _stroke_seg(stroke)
        loc = f"S{s_idx}" + (f"/D{d_idx}" if d_idx >= 0 else "")
        a_range = div.get("a_stroke_range", ("?", "?"))
        c_range = div.get("c_stroke_range", ("?", "?"))
        def _si(v: int | str) -> str:
            return str(v + 1) if isinstance(v, int) else str(v)
        a_tag = f"a(S{_si(a_range[0])}-S{_si(a_range[1])})" if a_range[0] != a_range[1] else f"a(S{_si(a_range[0])})"
        c_tag = f"c(S{_si(c_range[0])}-S{_si(c_range[1])})" if c_range[0] != c_range[1] else f"c(S{_si(c_range[0])})"
        ranges = [
            {"label": a_tag, "start_dt": div["a_start_dt"],
             "end_dt": div["a_end_dt"], "area": div["a_area"]},
            {"label": c_tag, "start_dt": div["c_start_dt"],
             "end_dt": div["c_end_dt"], "area": div["c_area"]},
        ]
        struct = div.get("structure", [])
        dims = div.get("div_dims", 1)
        dim_tag = f" ({dims}/3维)" if dims > 0 else ""
        (t1_strength, t1_conf, t1_tags,
         t1_str_score, t1_str_details,
         t1_conf_score, t1_conf_details) = _grade_type1(div)
        tag_str = "，".join(t1_tags)
        if div["direction"] == -1:
            points.append(BuySellPoint(
                type="1B", label="一买",
                dt=div["dt"], price=div["price"],
                description=(
                    f"[{loc}] 下跌趋势背驰{dim_tag}：c段面积({div['c_area']}) < a段({div['a_area']})，"
                    f"比值={div['ratio']:.2f}，"
                    f"{_STRENGTH_ZH_ALL[t1_strength]}（{tag_str}）"
                ),
                level=level,
                confidence=t1_conf,
                hub_idx=div["hub_idx"],
                stroke_idx=s_idx, seg_idx=d_idx,
                strength=t1_strength,
                strength_score=t1_str_score,
                strength_details=t1_str_details,
                conf_score=t1_conf_score,
                conf_details=t1_conf_details,
                area_ranges=ranges,
                structure=struct,
            ))
        else:
            points.append(BuySellPoint(
                type="1S", label="一卖",
                dt=div["dt"], price=div["price"],
                description=(
                    f"[{loc}] 上涨趋势背驰{dim_tag}：c段面积({div['c_area']}) < a段({div['a_area']})，"
                    f"比值={div['ratio']:.2f}，"
                    f"{_STRENGTH_ZH_ALL[t1_strength]}（{tag_str}）"
                ),
                level=level,
                confidence=t1_conf,
                hub_idx=div["hub_idx"],
                stroke_idx=s_idx, seg_idx=d_idx,
                strength=t1_strength,
                strength_score=t1_str_score,
                strength_details=t1_str_details,
                conf_score=t1_conf_score,
                conf_details=t1_conf_details,
                area_ranges=ranges,
                structure=struct,
            ))

    # ── Consolidation Divergence (盘整背驰) ──
    for div in consol_divs:
        stroke = _find_stroke_by_dt(strokes, div["dt"])
        if not stroke:
            continue
        s_idx, d_idx = _stroke_seg(stroke)
        loc = f"S{s_idx}" + (f"/D{d_idx}" if d_idx >= 0 else "")
        prev_si = div.get("prev_stroke_idx", "?")
        curr_si = div.get("curr_stroke_idx", "?")
        prev_label = f"S{prev_si + 1}" if isinstance(prev_si, int) else f"S{prev_si}"
        curr_label = f"S{curr_si + 1}" if isinstance(curr_si, int) else f"S{curr_si}"
        ranges = [
            {"label": prev_label, "start_dt": div["prev_start_dt"],
             "end_dt": div["prev_end_dt"], "area": div["prev_area"]},
            {"label": curr_label, "start_dt": div["curr_start_dt"],
             "end_dt": div["curr_end_dt"], "area": div["curr_area"]},
        ]
        hub = next((h for h in hubs if h.idx == div["hub_idx"]), None)
        struct: list[dict] = []
        if hub:
            struct.append({"tag": "A", "start_dt": hub.strokes[0].start.dt,
                           "end_dt": hub.strokes[-1].end.dt,
                           "zg": hub.zg, "zd": hub.zd})
        struct.append({"tag": "a", "start_dt": div["prev_start_dt"],
                       "end_dt": div["prev_end_dt"]})
        struct.append({"tag": "c", "start_dt": div["curr_start_dt"],
                       "end_dt": div["curr_end_dt"]})
        dims = div.get("div_dims", 1)
        dim_tag = f" ({dims}/3维)" if dims > 0 else ""
        (pb_strength, pb_conf, pb_tags,
         pb_str_score, pb_str_details,
         pb_conf_score, pb_conf_details) = _grade_pb_ps(div)
        pb_tag_str = "，".join(pb_tags) if pb_tags else ""
        if div["direction"] == -1:
            points.append(BuySellPoint(
                type="PB", label="盘整买点",
                dt=div["dt"], price=div["price"],
                description=(
                    f"[{loc}] 盘整背驰{dim_tag}：当次面积({div['curr_area']}) < 前次({div['prev_area']})，"
                    f"比值={div['ratio']:.2f}，"
                    f"{_STRENGTH_ZH_ALL[pb_strength]}（{pb_tag_str}）"
                ),
                level=level,
                confidence=pb_conf,
                hub_idx=div["hub_idx"],
                stroke_idx=s_idx, seg_idx=d_idx,
                strength=pb_strength,
                strength_score=pb_str_score,
                strength_details=pb_str_details,
                conf_score=pb_conf_score,
                conf_details=pb_conf_details,
                area_ranges=ranges,
                structure=struct,
            ))
        else:
            points.append(BuySellPoint(
                type="PS", label="盘整卖点",
                dt=div["dt"], price=div["price"],
                description=(
                    f"[{loc}] 盘整背驰{dim_tag}：当次面积({div['curr_area']}) < 前次({div['prev_area']})，"
                    f"比值={div['ratio']:.2f}，"
                    f"{_STRENGTH_ZH_ALL[pb_strength]}（{pb_tag_str}）"
                ),
                level=level,
                confidence=pb_conf,
                hub_idx=div["hub_idx"],
                stroke_idx=s_idx, seg_idx=d_idx,
                strength=pb_strength,
                strength_score=pb_str_score,
                strength_details=pb_str_details,
                conf_score=pb_conf_score,
                conf_details=pb_conf_details,
                area_ranges=ranges,
                structure=struct,
            ))

    # ── Type 2: First pullback after Type 1 (二买/二卖) ──
    hub_by_idx = {h.idx: h for h in hubs}

    for t1 in [p for p in points if p.type == "1B"]:
        t1_stroke = _find_stroke_by_dt(strokes, t1.dt)
        if not t1_stroke:
            continue
        first_up = _find_next_stroke(strokes, t1_stroke.idx, direction=1)
        if not first_up:
            continue
        first_pullback = _find_next_stroke(strokes, first_up.idx, direction=-1)
        if first_pullback and first_pullback.end.low > t1.price:
            s_idx, d_idx = _stroke_seg(first_pullback)
            loc = f"S{s_idx}" + (f"/D{d_idx}" if d_idx >= 0 else "")
            pb_low = first_pullback.end.low
            ref_hub = hub_by_idx.get(t1.hub_idx)
            (strength, conf, t2_tags,
             t2_str_score, t2_str_details,
             t2_conf_score, t2_conf_details) = _grade_type2(
                t1, first_up, first_pullback, ref_hub, is_buy=True)
            t2_tag_str = "，".join(t2_tags) if t2_tags else ""

            points.append(BuySellPoint(
                type="2B", label="二买",
                dt=first_pullback.end.dt, price=pb_low,
                description=(
                    f"[{loc}] 一买(S{t1.stroke_idx})后回调低点({pb_low:.3f})"
                    f"不破一买价({t1.price:.3f})，"
                    f"{_STRENGTH_ZH_ALL[strength]}（{t2_tag_str}）"
                ),
                level=level,
                confidence=conf,
                hub_idx=t1.hub_idx,
                stroke_idx=s_idx, seg_idx=d_idx,
                strength=strength,
                strength_score=t2_str_score,
                strength_details=t2_str_details,
                conf_score=t2_conf_score,
                conf_details=t2_conf_details,
                invalidation_price=t1.price,
            ))

    for t1 in [p for p in points if p.type == "1S"]:
        t1_stroke = _find_stroke_by_dt(strokes, t1.dt)
        if not t1_stroke:
            continue
        first_down = _find_next_stroke(strokes, t1_stroke.idx, direction=-1)
        if not first_down:
            continue
        first_rally = _find_next_stroke(strokes, first_down.idx, direction=1)
        if first_rally and first_rally.end.high < t1.price:
            s_idx, d_idx = _stroke_seg(first_rally)
            loc = f"S{s_idx}" + (f"/D{d_idx}" if d_idx >= 0 else "")
            rl_high = first_rally.end.high
            ref_hub = hub_by_idx.get(t1.hub_idx)
            (strength, conf, t2_tags,
             t2_str_score, t2_str_details,
             t2_conf_score, t2_conf_details) = _grade_type2(
                t1, first_down, first_rally, ref_hub, is_buy=False)
            t2_tag_str = "，".join(t2_tags) if t2_tags else ""

            points.append(BuySellPoint(
                type="2S", label="二卖",
                dt=first_rally.end.dt, price=rl_high,
                description=(
                    f"[{loc}] 一卖(S{t1.stroke_idx})后反弹高点({rl_high:.3f})"
                    f"不破一卖价({t1.price:.3f})，"
                    f"{_STRENGTH_ZH_ALL[strength]}（{t2_tag_str}）"
                ),
                level=level,
                confidence=conf,
                hub_idx=t1.hub_idx,
                stroke_idx=s_idx, seg_idx=d_idx,
                strength=strength,
                strength_score=t2_str_score,
                strength_details=t2_str_details,
                conf_score=t2_conf_score,
                conf_details=t2_conf_details,
                invalidation_price=t1.price,
            ))

    # ── Type 3: Hub breakout + pullback (三买/三卖) ──
    # Use raw (pre-merge) hubs so each individual hub can be checked for
    # departure/pullback. Merged hubs absorb all strokes, leaving nothing
    # to scan. Fall back to the (merged) hubs list when raw_hubs not given.
    t3_hubs = raw_hubs if raw_hubs else hubs

    # Determine hub-to-hub direction. evolution_type only distinguishes
    # "新生（上/下）" from "扩展"; expansion hubs don't carry direction.
    # Use midpoint comparison for a direction-agnostic classification.
    def _hub_direction(prev_h: Hub, curr_h: Hub) -> str:
        """Return 'up' / 'down' based on ZG/ZD position shift."""
        if curr_h.zd > prev_h.zg:
            return "up"
        if curr_h.zg < prev_h.zd:
            return "down"
        prev_mid = (prev_h.zg + prev_h.zd) / 2
        curr_mid = (curr_h.zg + curr_h.zd) / 2
        return "up" if curr_mid > prev_mid else "down"

    hub_dir: dict[int, str] = {}
    for i, h in enumerate(t3_hubs):
        evo = h.evolution_type
        if evo == "新生（上）":
            hub_dir[h.idx] = "up"
        elif evo == "新生（下）":
            hub_dir[h.idx] = "down"
        elif i > 0:
            hub_dir[h.idx] = _hub_direction(t3_hubs[i - 1], h)
        else:
            hub_dir[h.idx] = "up"

    hub_buy_rank: dict[int, int] = {}
    hub_sell_rank: dict[int, int] = {}
    hub_churn: dict[int, int] = {}
    up_run = 0
    down_run = 0
    _CHURN_WINDOW = 6
    for i, h in enumerate(t3_hubs):
        d = hub_dir[h.idx]
        if d == "up":
            up_run += 1
            down_run = 0
        else:
            down_run += 1
            up_run = 0

        if up_run > 0:
            hub_buy_rank[h.idx] = up_run
        elif down_run >= 2:
            hub_buy_rank[h.idx] = 0
        else:
            hub_buy_rank[h.idx] = 1
        if down_run > 0:
            hub_sell_rank[h.idx] = down_run
        elif up_run >= 2:
            hub_sell_rank[h.idx] = 0
        else:
            hub_sell_rank[h.idx] = 1

        window_dirs = [hub_dir[t3_hubs[j].idx]
                       for j in range(max(0, i - _CHURN_WINDOW + 1), i + 1)]
        flips = sum(1 for j in range(1, len(window_dirs))
                    if window_dirs[j] != window_dirs[j - 1])
        hub_churn[h.idx] = flips

    for i, hub in enumerate(t3_hubs):
        hub_end_idx = hub.strokes[-1].idx
        buy_rank = hub_buy_rank.get(hub.idx, 1)
        sell_rank = hub_sell_rank.get(hub.idx, 1)
        churn = hub_churn.get(hub.idx, 0)
        next_hub = t3_hubs[i + 1] if i + 1 < len(t3_hubs) else None

        _check_type3_buy(hub, strokes, hub_end_idx, points, level,
                         stroke_to_seg, buy_rank, churn, next_hub,
                         all_hubs=t3_hubs, hub_list_idx=i)
        _check_type3_sell(hub, strokes, hub_end_idx, points, level,
                          stroke_to_seg, sell_rank, churn, next_hub,
                          all_hubs=t3_hubs, hub_list_idx=i)

    points.sort(key=lambda p: p.dt)

    # ── Deduplication (去重) ──
    points = _dedup_signals(points)

    # ── Position Sizing Advice (仓位建议) ──
    _apply_position_advice(points)

    # ── Wolf Prevention Filter (防狼术) ──
    _apply_wolf_filter(points, bars)

    # ── Stale 3B/3S filter: suppress signals from ANY earlier hub whose price
    # falls below (3B) or above (3S) the last hub's boundary. These signals
    # are visually engulfed by a subsequent hub and no longer actionable. ──
    if len(t3_hubs) >= 2:
        last_h = t3_hubs[-1]
        last_h_idx = last_h.idx
        points = [p for p in points
                  if not (p.type == '3B' and p.hub_idx is not None
                          and p.hub_idx < last_h_idx
                          and p.price <= last_h.zd)
                  and not (p.type == '3S' and p.hub_idx is not None
                           and p.hub_idx < last_h_idx
                           and p.price >= last_h.zg)]

    # ── Assign signal_level based on the hub that generated the signal ──
    _assign_signal_levels(points, hubs, t3_hubs, level)

    return points


# ════════════════════════════════════════════════════════════════════
# 10b. Segment-Level Buy/Sell Points (线段级别买卖点)
# ════════════════════════════════════════════════════════════════════

def find_seg_buy_sell_points(
    seg_hubs: list[SegHub],
    segments: list[Segment],
    bars: list[RawBar],
    level: str,
) -> list[BuySellPoint]:
    """Identify Type 1/2/3 buy/sell points for segment-level hubs.

    Operates on SegHub + Segment objects analogous to how
    find_buy_sell_points operates on Hub + Stroke.
    """
    if not seg_hubs or len(segments) < 3:
        return []

    points: list[BuySellPoint] = []
    dt_idx = {b.dt: i for i, b in enumerate(bars)} if bars else {}

    def _seg_macd_area(seg: Segment, direction: int) -> float:
        """MACD histogram area for entire segment in given direction."""
        s_dt = seg.strokes[0].start.dt
        e_dt = seg.strokes[-1].end.dt
        return _macd_hist_area(bars, s_dt, e_dt, direction, dt_idx)

    def _seg_end_price(seg: Segment) -> float:
        if seg.direction == 1:
            return seg.high
        return seg.low

    # Build segment index for quick lookup
    seg_by_idx = {s.idx: s for s in segments}

    # ── Type 3: Segment exits seg_hub, pullback doesn't re-enter ──
    #
    # Key insight: the "exit segment" (first seg after core not overlapping hub)
    # may already BE the pullback if its direction opposes the breakout.
    # Example: hub ZG=120, core ends with S35(UP 65→658), then S36(DN 658→505)
    #   → S36 is entirely above ZG, direction DOWN → S36 IS the pullback, not exit.
    # If exit_seg direction matches breakout (UP for 3B), the NEXT opposite-dir seg
    # is the pullback.
    for i, hub in enumerate(seg_hubs):
        if not hub.segments:
            continue
        last_core_seg = hub.segments[-1]
        last_core_idx = last_core_seg.idx

        exit_seg = None
        for seg in segments:
            if seg.idx <= last_core_idx:
                continue
            overlaps = seg.high >= hub.zd and seg.low <= hub.zg
            if not overlaps:
                exit_seg = seg
                break

        if exit_seg is None:
            continue

        # --- 3B: entirely above ZG ---
        if exit_seg.low > hub.zg:
            if exit_seg.direction == -1:
                # exit_seg itself is the pullback (DOWN move above hub)
                pullback_seg = exit_seg
            else:
                # exit_seg is the true UP breakout; find next DOWN seg as pullback
                pullback_seg = None
                for seg in segments:
                    if seg.idx > exit_seg.idx and seg.direction == -1:
                        pullback_seg = seg
                        break
            if pullback_seg is not None and pullback_seg.low > hub.zg:
                price = pullback_seg.low
                points.append(BuySellPoint(
                    type="3B", label="三买(线段)",
                    dt=pullback_seg.end_dt, price=price,
                    description=(
                        f"[D{pullback_seg.idx}] 线段中枢{hub.idx}上方三买："
                        f"突破ZG={hub.zg:.2f}，"
                        f"回落段D{pullback_seg.idx}低点{price:.2f}>ZG"
                    ),
                    level=level,
                    confidence="medium",
                    hub_idx=hub.idx,
                    seg_idx=pullback_seg.idx,
                    strength="standard",
                    signal_level=f"{hub.hub_level.replace('中枢', '') if hub.hub_level else 'DF'}三买",
                ))

        # --- 3S: entirely below ZD ---
        elif exit_seg.high < hub.zd:
            if exit_seg.direction == 1:
                # exit_seg itself is the pullback (UP move below hub)
                pullback_seg = exit_seg
            else:
                # exit_seg is the true DOWN breakout; find next UP seg as pullback
                pullback_seg = None
                for seg in segments:
                    if seg.idx > exit_seg.idx and seg.direction == 1:
                        pullback_seg = seg
                        break
            if pullback_seg is not None and pullback_seg.high < hub.zd:
                price = pullback_seg.high
                points.append(BuySellPoint(
                    type="3S", label="三卖(线段)",
                    dt=pullback_seg.end_dt, price=price,
                    description=(
                        f"[D{pullback_seg.idx}] 线段中枢{hub.idx}下方三卖："
                        f"跌破ZD={hub.zd:.2f}，"
                        f"反弹段D{pullback_seg.idx}高点{price:.2f}<ZD"
                    ),
                    level=level,
                    confidence="medium",
                    hub_idx=hub.idx,
                    seg_idx=pullback_seg.idx,
                    strength="standard",
                    signal_level=f"{hub.hub_level.replace('中枢', '') if hub.hub_level else 'DF'}三卖",
                ))

    # ── Type 1: Segment-level trend divergence ──
    if len(seg_hubs) >= 2 and bars:
        for i in range(len(seg_hubs) - 1):
            h0, h1 = seg_hubs[i], seg_hubs[i + 1]
            is_down = h1.zd < h0.zd and h1.zg < h0.zg
            is_up = h1.zd > h0.zd and h1.zg > h0.zg
            if not is_down and not is_up:
                continue

            trend_dir = -1 if is_down else 1

            # Extend chain of same-direction seg_hubs
            j = i + 1
            while j + 1 < len(seg_hubs):
                nxt, cur = seg_hubs[j + 1], seg_hubs[j]
                same = ((nxt.zd < cur.zd and nxt.zg < cur.zg) if trend_dir == -1
                        else (nxt.zd > cur.zd and nxt.zg > cur.zg))
                if same:
                    j += 1
                else:
                    break

            first_hub = seg_hubs[i]
            last_hub = seg_hubs[j]

            # a-segment: before first_hub
            a_seg = first_hub.entry_segment
            if a_seg is None:
                continue

            # c-segment: after last_hub (last exit + next segment)
            last_core_idx = last_hub.segments[-1].idx if last_hub.segments else -1
            c_segs = [s for s in segments
                      if s.idx > last_core_idx and s.direction == trend_dir]
            if not c_segs:
                continue
            c_seg = c_segs[-1]  # furthest segment in trend direction

            a_area = _seg_macd_area(a_seg, trend_dir)
            c_area = _seg_macd_area(c_seg, trend_dir)

            if a_area <= 0:
                continue

            ratio = c_area / a_area
            diverged = ratio < 0.9

            if not diverged:
                continue

            # Confirm c extends beyond hub
            if trend_dir == -1:
                c_extreme = c_seg.low
                if c_extreme >= last_hub.zd:
                    continue
            else:
                c_extreme = c_seg.high
                if c_extreme <= last_hub.zg:
                    continue

            price = c_extreme
            sig_dt = c_seg.end_dt

            if trend_dir == -1:
                points.append(BuySellPoint(
                    type="1B", label="一买(线段)",
                    dt=sig_dt, price=price,
                    description=(
                        f"[D{c_seg.idx}] 线段级趋势背驰："
                        f"c段面积({c_area:.1f}) < a段({a_area:.1f})，"
                        f"比值={ratio:.2f}"
                    ),
                    level=level,
                    confidence="medium" if ratio < 0.7 else "low",
                    hub_idx=last_hub.idx,
                    seg_idx=c_seg.idx,
                    strength="strong" if ratio < 0.5 else "standard",
                    signal_level=f"{last_hub.hub_level.replace('中枢', '') if last_hub.hub_level else 'DF'}一买",
                ))
            else:
                points.append(BuySellPoint(
                    type="1S", label="一卖(线段)",
                    dt=sig_dt, price=price,
                    description=(
                        f"[D{c_seg.idx}] 线段级趋势背驰："
                        f"c段面积({c_area:.1f}) < a段({a_area:.1f})，"
                        f"比值={ratio:.2f}"
                    ),
                    level=level,
                    confidence="medium" if ratio < 0.7 else "low",
                    hub_idx=last_hub.idx,
                    seg_idx=c_seg.idx,
                    strength="strong" if ratio < 0.5 else "standard",
                    signal_level=f"{last_hub.hub_level.replace('中枢', '') if last_hub.hub_level else 'DF'}一卖",
                ))

    # ── Type 2: First pullback after Type 1 ──
    for t1 in [p for p in points if p.type == "1B" and p.seg_idx >= 0]:
        t1_seg = seg_by_idx.get(t1.seg_idx)
        if not t1_seg:
            continue
        # First up segment after t1
        first_up = None
        for seg in segments:
            if seg.idx > t1_seg.idx and seg.direction == 1:
                first_up = seg
                break
        if not first_up:
            continue
        # First pullback segment after first_up
        first_pb = None
        for seg in segments:
            if seg.idx > first_up.idx and seg.direction == -1:
                first_pb = seg
                break
        if first_pb and first_pb.low > t1.price:
            points.append(BuySellPoint(
                type="2B", label="二买(线段)",
                dt=first_pb.end_dt, price=first_pb.low,
                description=(
                    f"[D{first_pb.idx}] 线段一买(D{t1.seg_idx})后回调"
                    f"低点({first_pb.low:.2f})不破一买价({t1.price:.2f})"
                ),
                level=level,
                confidence="medium",
                hub_idx=t1.hub_idx,
                seg_idx=first_pb.idx,
                strength="standard",
                signal_level=f"{t1.signal_level.replace('一买', '')}二买",
            ))

    for t1 in [p for p in points if p.type == "1S" and p.seg_idx >= 0]:
        t1_seg = seg_by_idx.get(t1.seg_idx)
        if not t1_seg:
            continue
        first_down = None
        for seg in segments:
            if seg.idx > t1_seg.idx and seg.direction == -1:
                first_down = seg
                break
        if not first_down:
            continue
        first_rally = None
        for seg in segments:
            if seg.idx > first_down.idx and seg.direction == 1:
                first_rally = seg
                break
        if first_rally and first_rally.high < t1.price:
            points.append(BuySellPoint(
                type="2S", label="二卖(线段)",
                dt=first_rally.end_dt, price=first_rally.high,
                description=(
                    f"[D{first_rally.idx}] 线段一卖(D{t1.seg_idx})后反弹"
                    f"高点({first_rally.high:.2f})不破一卖价({t1.price:.2f})"
                ),
                level=level,
                confidence="medium",
                hub_idx=t1.hub_idx,
                seg_idx=first_rally.idx,
                strength="standard",
                signal_level=f"{t1.signal_level.replace('一卖', '')}二卖",
            ))

    points.sort(key=lambda p: p.dt)
    for i, p in enumerate(points):
        p.idx = i

    return points


_TYPE_ZH = {
    "1B": "一买", "1S": "一卖",
    "2B": "二买", "2S": "二卖",
    "3B": "三买", "3S": "三卖",
    "PB": "盘买", "PS": "盘卖",
}


def _assign_signal_levels(
    points: list[BuySellPoint],
    merged_hubs: list[Hub],
    raw_hubs: list[Hub],
    analysis_level: str,
):
    """Assign ``signal_level`` to each BuySellPoint.

    Signal level = hub level of the hub that generated the signal.
    Theory (缠论新课程 §二): "买卖点必须搭配级别描述（如'30分钟三买'）"

    For Type 1/2: uses merged_hubs (trend divergence detected on merged).
    For Type 3/PB/PS: uses raw_hubs (individual hub breakout/pullback).
    """
    merged_by_idx = {h.idx: h for h in merged_hubs}
    raw_by_idx = {h.idx: h for h in raw_hubs}

    base_stroke_level = get_hub_level_name(analysis_level, "stroke")

    for p in points:
        hub = None
        if p.type in ("3B", "3S", "PB", "PS"):
            hub = raw_by_idx.get(p.hub_idx)
        if hub is None:
            hub = merged_by_idx.get(p.hub_idx)
        if hub is None and p.hub_idx >= 0:
            hub = raw_by_idx.get(p.hub_idx)

        if hub and hub.hub_level:
            level_name = hub.hub_level.replace("中枢", "")
        else:
            level_name = base_stroke_level

        type_zh = _TYPE_ZH.get(p.type, p.type)
        p.signal_level = f"{level_name}{type_zh}"


_CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def _dedup_signals(points: list[BuySellPoint]) -> list[BuySellPoint]:
    """Remove duplicate and theoretically redundant signals.

    Two dedup passes:
    1. Same-type dedup: when multiple hubs produce the same type at the same
       stroke, keep only the highest-confidence one.
    2. Cross-type suppression: PB/PS are suppressed when a higher-priority
       signal (1B/1S, 2B/2S, 3B/3S) exists at the same stroke with the same
       direction.  Theory basis (108课/图解缠论2): for a given hub, the exit
       is either "hub continuation" (盘整背驰) or "hub destruction" (三买/三卖);
       they are mutually exclusive.  When trend divergence (1B/1S) fires at
       the same stroke, it subsumes the consolidation signal.
    """
    # Pass 1: same-type dedup (keep best confidence per stroke+type)
    best: dict[tuple[int, str], BuySellPoint] = {}
    for p in points:
        key = (p.stroke_idx, p.type)
        prev = best.get(key)
        if prev is None:
            best[key] = p
            continue
        p_rank = _CONF_RANK.get(p.confidence, 9)
        prev_rank = _CONF_RANK.get(prev.confidence, 9)
        if p_rank < prev_rank:
            best[key] = p
        elif p_rank == prev_rank and "(3/3维)" in p.description:
            best[key] = p

    # Pass 2: suppress PB/PS when a structural signal exists at the same stroke
    _STRUCTURAL_BUY = {"1B", "2B", "3B"}
    _STRUCTURAL_SELL = {"1S", "2S", "3S"}
    strokes_with_structural_buy: set[int] = set()
    strokes_with_structural_sell: set[int] = set()
    for (si, tp) in best:
        if tp in _STRUCTURAL_BUY:
            strokes_with_structural_buy.add(si)
        elif tp in _STRUCTURAL_SELL:
            strokes_with_structural_sell.add(si)

    suppressed = set()
    for (si, tp) in best:
        if tp == "PB" and si in strokes_with_structural_buy:
            suppressed.add((si, tp))
        elif tp == "PS" and si in strokes_with_structural_sell:
            suppressed.add((si, tp))

    result = [p for k, p in best.items() if k not in suppressed]
    return sorted(result, key=lambda p: p.dt)


# Buy types use "close < invalidation_price" to invalidate;
# sell types use "close > invalidation_price" to invalidate.
_BUY_TYPES = {"1B", "2B", "3B", "PB"}
_SELL_TYPES = {"1S", "2S", "3S", "PS"}

_INVALIDATION_REASONS = {
    "3B": "价格跌破中枢上沿ZG，三买失败",
    "3S": "价格突破中枢下沿ZD，三卖失败",
    "1B": "价格创新低，跌破一买价",
    "1S": "价格创新高，突破一卖价",
    "2B": "价格跌破一买低点",
    "2S": "价格突破一卖高点",
    "PB": "价格跌破盘整买点",
    "PS": "价格突破盘整卖点",
}


def _validate_signals(points: list[BuySellPoint], bars: list,
                      strokes: list[Stroke] | None = None) -> None:
    """Classify each signal into one of three states:
      - "pending"     (待确认): no confirming or invalidating price action yet
      - "confirmed"   (已确认): follow-through in the predicted direction
      - "invalidated" (已失效): price breached the invalidation level

    Confirmation logic (stroke-based):
      Buy signals  → confirmed when a subsequent upward stroke has high > signal price
      Sell signals → confirmed when a subsequent downward stroke has low < signal price

    Invalidation logic (bar-based, theory §7.2 / §2.2):
      3B: close < ZG | 3S: close > ZD | others: close breaches signal/1B price
    """
    if not bars or not points:
        return

    bar_dts = [b.dt for b in bars]
    strokes = strokes or []

    for p in points:
        if p.invalidation_price == 0.0:
            p.invalidation_price = p.price

        sig_dt = p.dt

        # --- 1. Confirmation check (stroke-based, takes priority) ---
        # Use s.end.dt > sig_dt so that strokes originating at the signal
        # point are included (sell signals sit at the start of a down-stroke,
        # buy signals at the start of an up-stroke).
        confirmed = False
        if strokes:
            if p.type in _BUY_TYPES:
                for s in strokes:
                    if s.end.dt <= sig_dt:
                        continue
                    if s.direction == 1 and s.end.high > p.price:
                        confirmed = True
                        break
            elif p.type in _SELL_TYPES:
                for s in strokes:
                    if s.end.dt <= sig_dt:
                        continue
                    if s.direction == -1 and s.end.low < p.price:
                        confirmed = True
                        break

        if confirmed:
            p.status = "confirmed"
            continue

        # --- 2. Invalidation check (bar-based, only if not confirmed) ---
        bar_start = None
        for i, dt in enumerate(bar_dts):
            if dt > sig_dt:
                bar_start = i
                break

        invalidated = False
        if bar_start is not None:
            inv_price = p.invalidation_price
            if p.type in _BUY_TYPES:
                for b in bars[bar_start:]:
                    if b.close < inv_price:
                        p.status = "invalidated"
                        p.invalidation_reason = (
                            f"{_INVALIDATION_REASONS.get(p.type, '信号失效')}"
                            f"（{b.dt} 收盘{b.close:.3f} < {inv_price:.3f}）"
                        )
                        invalidated = True
                        break
            elif p.type in _SELL_TYPES:
                for b in bars[bar_start:]:
                    if b.close > inv_price:
                        p.status = "invalidated"
                        p.invalidation_reason = (
                            f"{_INVALIDATION_REASONS.get(p.type, '信号失效')}"
                            f"（{b.dt} 收盘{b.close:.3f} > {inv_price:.3f}）"
                        )
                        invalidated = True
                        break

        if not invalidated:
            p.status = "pending"


_POSITION_ADVICE = {
    "1B": ("轻仓试探 1/3", "趋势背驰买入，风险较高，轻仓探底"),
    "2B": ("加至标准仓位 2/3", "一买确认后回调不破，可加仓"),
    "3B": ("满仓", "中枢突破回踩确认，趋势延续"),
    "PB": ("轻仓试探 1/3", "盘整背驰买入，可能假信号"),
    "1S": ("减至 1/3 或清仓", "趋势背驰卖出，锁定大部分利润"),
    "2S": ("清仓", "一卖确认后反弹不破，应清仓"),
    "3S": ("必须清仓", "中枢破位确认，下跌趋势延续"),
    "PS": ("减仓 1/3", "盘整背驰卖出，减仓观望"),
}


def _apply_position_advice(points: list[BuySellPoint]):
    """Assign position sizing advice to each buy/sell point.

    Per 108课 §6.4:
      1B → 1/3, 2B → 2/3, 3B → full, 1S → reduce to 1/3, 3S → must clear
    Adjusted per strength:
      strongest → upgrade position; weak → downgrade or skip
    """
    for p in points:
        base = _POSITION_ADVICE.get(p.type)
        if not base:
            continue

        advice, reason = base

        # 1B/1S position by strength
        if p.type == "1B":
            if p.strength == "strongest":
                advice = "加仓至 1/2"
                reason = "最强一买：多中枢+强背驰+DIF收敛，底部信号明确"
            elif p.strength == "strong":
                advice = "轻仓试探 1/3"
                reason = "强势一买，趋势背驰条件好"
            elif p.strength == "standard":
                advice = "轻仓试探 1/4"
                reason = "标准一买，背驰信号一般"
            elif p.strength == "weak":
                advice = "极轻仓或观望"
                reason = "弱一买：背驰不充分或单中枢，风险偏高"
        elif p.type == "1S":
            if p.strength == "strongest":
                advice = "清仓"
                reason = "最强一卖：多中枢+强背驰+DIF收敛，顶部信号明确"
            elif p.strength == "strong":
                advice = "减至 1/3 或清仓"
                reason = "强势一卖，趋势背驰条件好"
            elif p.strength == "standard":
                advice = "减至 1/2"
                reason = "标准一卖，背驰信号一般"
            elif p.strength == "weak":
                advice = "减仓 1/3"
                reason = "弱一卖：背驰不充分，可能继续上行"

        # PB/PS position by strength
        elif p.type == "PB":
            if p.strength in ("strong", "strongest"):
                advice = "轻仓试探 1/3"
                reason = "强盘整背驰买点，但后续路径不确定"
            elif p.strength == "standard":
                advice = "极轻仓 1/5"
                reason = "标准盘整买点，多路径可能"
            elif p.strength == "weak":
                advice = "观望"
                reason = "弱盘整买点，信号不充分"
        elif p.type == "PS":
            if p.strength in ("strong", "strongest"):
                advice = "减仓 1/3"
                reason = "强盘整背驰卖点"
            elif p.strength == "standard":
                advice = "小幅减仓"
                reason = "标准盘整卖点，多路径可能"
            elif p.strength == "weak":
                advice = "暂不操作"
                reason = "弱盘整卖点，信号不充分"

        # 2B/2S position by strength
        elif p.type == "2B":
            if p.strength == "strongest":
                advice = "满仓（二买三买合一）"
                reason = "最强二买，回抽不进中枢+极浅回调，常对应大行情"
            elif p.strength == "strong":
                advice = "加至标准仓位 2/3"
                reason = "强势二买，回调幅度小，信心较高"
            elif p.strength == "standard":
                advice = "加至 1/2"
                reason = "标准二买，确认一买有效"
            elif p.strength == "weak":
                advice = "维持轻仓，观察"
                reason = "弱二买：深度回调接近一买价，确认力度不足"
        elif p.type == "2S":
            if p.strength == "strongest":
                advice = "必须清仓（二卖三卖合一）"
                reason = "最强二卖，反弹不进中枢，下跌加速"
            elif p.strength == "strong":
                advice = "清仓"
                reason = "强势二卖，反弹力度弱"
            elif p.strength == "standard":
                advice = "减至 1/3"
                reason = "标准二卖，确认一卖有效"
            elif p.strength == "weak":
                advice = "减仓 1/2"
                reason = "弱二卖：深度反弹接近一卖价，确认力度不足"

        # 3B/3S position by strength (context-aware per 108课/图解缠论2)
        elif p.type == "3B":
            if p.strength == "strongest":
                advice = "满仓"
                reason = "最强三买：趋势确认+强突破+浅回抽，中枢上移段"
            elif p.strength == "strong":
                advice = "加至标准仓位 2/3"
                reason = "强势三买（趋势中或强盘整突破），中枢突破回踩确认"
            elif p.strength == "standard":
                advice = "轻仓试探 1/3"
                reason = "标准三买，条件一般，轻仓参与"
            elif p.strength == "weak":
                advice = "观望不参与"
                reason = "弱三买：盘整扩展风险高/双横盘/突破不力/趋势末端"
        elif p.type == "3S":
            if p.strength == "strongest":
                advice = "必须清仓"
                reason = "最强三卖：趋势确认+强破位+浅反弹，下跌加速"
            elif p.strength == "strong":
                advice = "减至 1/3 或清仓"
                reason = "强势三卖（趋势中或强盘整破位），中枢破位确认"
            elif p.strength == "standard":
                advice = "减仓 1/3"
                reason = "标准三卖，谨慎减仓"
            elif p.strength == "weak":
                advice = "暂不操作，观察"
                reason = "弱三卖：条件不充分，可能假破位"

        if p.confidence == "low":
            if "满仓" in advice:
                advice = advice.replace("满仓", "标准仓位 2/3") + "（置信度低，降仓）"
            elif "2/3" in advice:
                advice = advice.replace("2/3", "1/3") + "（置信度低，降仓）"

        p.position_advice = f"{advice} — {reason}"


def _apply_wolf_filter(points: list[BuySellPoint], bars: list[RawBar]):
    """Apply MACD zero-axis filter to buy/sell points.

    Per 108课 §五.5.7 / 缠论解析 场景F / 土匪注解 §三:
      - DIF below 0 → bearish dominated, buy signals get warning + confidence downgrade
      - DIF above 0 → bullish dominated, sell signals get warning + confidence downgrade
      - |DIF| < threshold → near zero axis, transitional zone
    """
    if not bars:
        return

    dt_to_bar = {b.dt: b for b in bars}

    for p in points:
        bar = dt_to_bar.get(p.dt)
        if not bar:
            continue

        is_buy = p.type in ("1B", "2B", "3B", "PB")
        dif = bar.dif

        near_zero_threshold = max(abs(bars[-1].close) * 0.001, 0.001)
        if abs(dif) < near_zero_threshold:
            p.macd_zone = "near_zero"
        elif dif > 0:
            p.macd_zone = "above_zero"
        else:
            p.macd_zone = "below_zero"

        if is_buy and dif < 0:
            p.wolf_warning = f"防狼术警告：DIF={dif:.4f}在0轴下，空头主导，买入需谨慎"
            if p.confidence == "high":
                p.confidence = "medium"
            elif p.confidence == "medium":
                p.confidence = "low"
        elif not is_buy and dif > 0:
            p.wolf_warning = f"防狼术警告：DIF={dif:.4f}在0轴上，多头主导，卖出需谨慎"
            if p.confidence == "high":
                p.confidence = "medium"
            elif p.confidence == "medium":
                p.confidence = "low"


def _check_type3_buy(hub: Hub, strokes: list[Stroke], hub_end_idx: int,
                     points: list[BuySellPoint], level: str,
                     stroke_to_seg: dict[int, int] | None = None,
                     trend_hub_rank: int = 1, churn: int = 0,
                     next_hub: Hub | None = None,
                     all_hubs: list["Hub"] | None = None,
                     hub_list_idx: int = -1):
    """Check for Type 3 buy point after hub with quality grading.

    Per 108课 lesson 20: departure and pullback must be the FIRST sub-level
    movements after leaving the hub. Scan stops at the next hub boundary.

    Quality dimensions (per 108课详解, 图解缠论2/3, 土匪注解):
      1. trend_hub_rank: 0=downtrend-end, 1=1st up-hub (best per L79), 2+=weaker
      2. breakout_strength: how far the breakout stroke exceeds ZG
      3. pullback_depth: how far the pullback stays above ZG (shallower = stronger)
      4. hub_width: number of strokes in hub (narrow 3-stroke hubs → less reliable)
      5. MACD: breakout stroke dif_extreme (above zero = stronger)
      6. churn: direction-flip count in recent hubs (high = large-level consolidation)
      7. expansion_risk (P1): probability that 3B leads to hub expansion
    """
    stm = stroke_to_seg or {}
    last_stroke = hub.strokes[-1]
    hub_range = hub.zg - hub.zd if hub.zg > hub.zd else 1e-9

    def _classify_departure_pullback_buy(breakout_stroke, pullback):
        """Classify departure+pullback combination per 108课 lesson 18.

        Lesson 18: hub destruction has only 3 combinations:
          趋势+盘整 (strongest), 趋势+反趋势, 盘整+反趋势
        Plus 盘整+盘整 (weak, per lesson 53 2008-1-8 解盘)

        At stroke level, "trend departure" is approximated by breakout
        range strength since individual strokes lack internal hub structure.
        """
        dep_above_zg = breakout_stroke.end.high - hub.zg
        dep_total = abs(breakout_stroke.end.high - breakout_stroke.start.low)
        pb_total = abs(pullback.start.high - pullback.end.low)

        dep_strong = (dep_above_zg > hub_range * 0.5
                      or dep_total > hub_range * 0.8)
        dep_weak = dep_above_zg < hub_range * 0.3 and dep_total < hub_range * 0.5

        if dep_above_zg > 0:
            retrace = pb_total / dep_above_zg
        else:
            retrace = 1.0
        pb_is_shallow = retrace < 0.382
        pb_is_deep = retrace > 0.618

        if dep_strong and pb_is_shallow:
            return "趋势+盘整", 3
        elif dep_strong and not pb_is_deep:
            return "趋势+回抽", 2
        elif dep_strong and pb_is_deep:
            return "趋势+反趋势", 1
        elif dep_weak and pb_is_shallow:
            return "盘整+盘整", -2
        elif dep_weak and pb_is_deep:
            return "盘整+反趋势", 0
        elif dep_weak:
            return "盘整离开", -1
        else:
            return "标准离开", 0

    def _assess_expansion_risk(breakout_stroke) -> tuple[int, str]:
        """P1: Assess probability that this 3B leads to hub expansion.

        Expansion = new hub's oscillation range overlaps with previous hub(s).
        Risk factors:
          1. Hub count already high (3+ → accumulated selling pressure)
          2. Accumulated range large (prior DD to GG) relative to breakout
          3. Hub is a merged/expansion hub (already upgraded level)
          4. Breakout "escape velocity" insufficient vs accumulated height
        """
        _hubs = all_hubs or []
        _hidx = hub_list_idx

        risk_score = 0
        risk_label = ""

        # Factor 1: number of same-direction hubs before this one
        if trend_hub_rank >= 4:
            risk_score += 3
            risk_label = f"极高(第{trend_hub_rank}中枢)"
        elif trend_hub_rank == 3:
            risk_score += 2
            risk_label = f"高(第{trend_hub_rank}中枢)"
        elif trend_hub_rank == 2:
            risk_score += 1
            risk_label = "中等(第2中枢)"

        # Factor 2: accumulated range vs breakout strength
        if _hubs and _hidx >= 1:
            first_dd = min(h.dd for h in _hubs[:_hidx + 1])
            last_gg = max(h.gg for h in _hubs[:_hidx + 1])
            accum_range = last_gg - first_dd
            breakout_height = breakout_stroke.end.high - hub.zg
            if accum_range > 0 and breakout_height > 0:
                escape_ratio = breakout_height / accum_range
                if escape_ratio < 0.1:
                    risk_score += 2
                    risk_label = (risk_label + "+逃逸不足") if risk_label else "逃逸不足"
                elif escape_ratio < 0.2:
                    risk_score += 1
                    risk_label = (risk_label + "+逃逸偏弱") if risk_label else "逃逸偏弱"

        # Factor 3: merged hub (already experienced expansion)
        if hub.is_merged:
            risk_score += 2
            risk_label = (risk_label + "+合并中枢") if risk_label else "合并中枢"

        # Factor 4: hub duration too long (stale breakout)
        if hub.duration_bars > 60:
            risk_score += 1
            risk_label = (risk_label + "+长期盘整") if risk_label else "长期盘整"

        if not risk_label:
            risk_label = "低" if risk_score == 0 else f"偏低({risk_score})"

        return risk_score, risk_label

    def _grade_3b(breakout_stroke, pullback, dep_count=1):
        breakout_pct = (breakout_stroke.end.high - hub.zg) / hub_range
        margin_pct = (pullback.end.low - hub.zg) / hub_range
        breakout_abs = (breakout_stroke.end.high - hub.zg) / hub.zg if hub.zg else 0
        hub_width = len(hub.strokes)
        dif_val = breakout_stroke.dif_extreme

        tags = []
        str_score = 0
        str_details = []
        conf_score = 0
        conf_details = []

        # === STRENGTH (operational value) ===
        # S1: trend context — scoring per 108课详解 L79:
        #   "尽量只介入第一个中枢的第三类买点。因为第二个中枢以后,
        #    形成大中枢的概率将急促加大"
        # rank=0: last downtrend hub breakout (not necessarily 二三买合一)
        # rank=1: first up-hub 3B (recommended by original text)
        # rank=2: second up-hub 3B (generally avoid per 扫地僧)
        s1 = 0; s1_l = ""
        if trend_hub_rank == 0:
            s1 = 5; s1_l = "下跌末端三买"; tags.append("下跌末端三买")
        elif trend_hub_rank == 1:
            s1 = 5; s1_l = "首个中枢三买"; tags.append("首个中枢三买")
        elif trend_hub_rank == 2:
            s1 = 2; s1_l = "第二中枢三买"; tags.append("第二中枢三买")
        elif trend_hub_rank == 3:
            s1 = 0; s1_l = f"第三中枢三买"; tags.append(f"{s1_l}⚠")
        elif trend_hub_rank <= 5:
            s1 = -2; s1_l = f"趋势末端(第{trend_hub_rank}中枢)"; tags.append(f"{s1_l}⚠")
        else:
            s1 = -4; s1_l = f"极晚期(第{trend_hub_rank}中枢)"; tags.append(f"{s1_l}⚠")
        str_score += s1
        str_details.append({"dim": "趋势位置", "label": s1_l, "score": s1})

        # S2: departure+pullback combination
        combo_label, combo_score = _classify_departure_pullback_buy(
            breakout_stroke, pullback)
        str_score += combo_score
        if combo_score != 0:
            tags.append(combo_label)
        str_details.append({"dim": "离开回抽", "label": combo_label,
                             "score": combo_score})

        # S3: breakout strength
        s3 = 0
        if breakout_abs > 0.03:
            s3 = 2; s3_l = f"强突破({breakout_abs:.1%})"; tags.append("强突破")
        elif breakout_abs > 0.01:
            s3 = 1; s3_l = f"一般({breakout_abs:.1%})"
        elif breakout_abs < 0.005:
            s3 = -1; s3_l = f"弱突破({breakout_abs:.1%})"; tags.append("弱突破⚠")
        else:
            s3_l = f"偏弱({breakout_abs:.1%})"
        str_score += s3
        str_details.append({"dim": "突破力度", "label": s3_l, "score": s3})

        # S4: MACD position
        s4 = 0
        if dif_val > 0:
            s4 = 1; s4_l = "DIF>0 多头区"; tags.append("DIF>0")
        elif dif_val < 0:
            s4 = -1; s4_l = "DIF<0 空头区"
        else:
            s4_l = "DIF≈0"
        str_score += s4
        str_details.append({"dim": "MACD", "label": s4_l, "score": s4})

        # S5: Volume during pullback
        s5 = 0; s5_l = "-"
        if breakout_stroke.avg_volume > 0 and pullback.avg_volume > 0:
            vol_ratio = pullback.avg_volume / breakout_stroke.avg_volume
            if vol_ratio < 0.5:
                s5 = 2; s5_l = f"缩量回抽({vol_ratio:.0%})"; tags.append(f"缩量回抽✓({vol_ratio:.0%})")
            elif vol_ratio < 0.7:
                s5 = 1; s5_l = f"温和缩量({vol_ratio:.0%})"; tags.append(f"温和缩量({vol_ratio:.0%})")
            elif vol_ratio > 1.5:
                s5 = -2; s5_l = f"放量回抽({vol_ratio:.0%})"; tags.append(f"放量回抽⚠({vol_ratio:.0%})")
            elif vol_ratio > 1.2:
                s5 = -1; s5_l = f"量能偏大({vol_ratio:.0%})"; tags.append(f"量能偏大({vol_ratio:.0%})")
            else:
                s5_l = f"正常({vol_ratio:.0%})"
        str_score += s5
        str_details.append({"dim": "量能", "label": s5_l, "score": s5})

        if hub.volume_trend == "shrink":
            str_score += 1
            str_details.append({"dim": "枢内蓄势", "label": "缩量蓄势", "score": 1})
            tags.append("枢内缩量蓄势✓")

        # === CONFIDENCE (pattern certainty) ===
        # C1: invalidation margin
        c1 = 0
        if margin_pct > 1.0:
            c1 = 3; c1_l = f"远离失效位(余{margin_pct:.0%})"
        elif margin_pct > 0.50:
            c1 = 2; c1_l = f"安全(余{margin_pct:.0%})"; tags.append("浅回抽")
        elif margin_pct > 0.10:
            c1 = 1; c1_l = f"适中(余{margin_pct:.0%})"
        elif margin_pct > 0.02:
            c1 = 0; c1_l = f"偏近(余{margin_pct:.0%})"
        else:
            c1 = -2; c1_l = f"极近失效位(余{margin_pct:.0%})"; tags.append("深回抽⚠")
        conf_score += c1
        conf_details.append({"dim": "失效距离", "label": c1_l, "score": c1})

        # C2: hub width
        c2 = 0
        if hub_width >= 7:
            c2 = 1; c2_l = f"充分构建({hub_width}笔)"; tags.append("充分换手")
        elif hub_width <= 3:
            c2 = -1; c2_l = f"窄中枢({hub_width}笔)"; tags.append("窄中枢⚠")
        else:
            c2_l = f"一般({hub_width}笔)"
        conf_score += c2
        conf_details.append({"dim": "中枢宽度", "label": c2_l, "score": c2})

        # C3: churn
        c3 = 0
        if churn >= 3:
            c3 = -3; c3_l = "频繁翻转"; tags.append("频繁翻转⚠震荡市")
        elif churn >= 2:
            c3 = -1; c3_l = "方向不稳"; tags.append("方向不稳")
        else:
            c3_l = "方向清晰"; c3 = 1
        conf_score += c3
        conf_details.append({"dim": "方向稳定性", "label": c3_l, "score": c3})

        # C4: flatness
        dep_range = abs(breakout_stroke.end.high - breakout_stroke.start.low)
        pb_range_val = abs(pullback.start.high - pullback.end.low)
        c4 = 0; c4_l = "正常"
        if hub_range > 0:
            dep_flat = dep_range / hub_range < 0.3
            pb_flat = pb_range_val / hub_range < 0.3
            if dep_flat and pb_flat:
                c4 = -4; c4_l = "双横盘"; tags.append("双横盘⚠非三买")
            elif dep_flat:
                c4 = -2; c4_l = "离开段偏弱"; tags.append("离开段偏弱⚠")
            else:
                c4 = 1; c4_l = "形态清晰"
        conf_score += c4
        if c4 != 1:
            conf_details.append({"dim": "形态清晰度", "label": c4_l, "score": c4})

        # C5: departure stroke count
        c5 = 0
        if dep_count >= 3:
            c5 = 1; c5_l = f"多笔离开({dep_count}笔)"
        else:
            c5_l = "单笔离开"
        conf_score += c5
        if c5 != 0:
            conf_details.append({"dim": "离开笔数", "label": c5_l, "score": c5})

        # C6 (P1): Expansion risk — penalize 3B signals likely to become expansion
        exp_risk, exp_label = _assess_expansion_risk(breakout_stroke)
        c6 = 0
        if exp_risk >= 5:
            c6 = -4; c6_l = f"扩展风险极高({exp_label})"
            tags.append("扩展风险极高⚠")
        elif exp_risk >= 3:
            c6 = -2; c6_l = f"扩展风险高({exp_label})"
            tags.append("扩展风险高⚠")
        elif exp_risk >= 2:
            c6 = -1; c6_l = f"扩展风险中({exp_label})"
            tags.append("扩展风险中")
        elif exp_risk == 0:
            c6 = 1; c6_l = "扩展风险低"
        else:
            c6_l = f"扩展风险偏低({exp_label})"
        conf_score += c6
        conf_details.append({"dim": "扩展风险", "label": c6_l, "score": c6})

        # P3: Raised requirements for merged/expansion hubs
        if hub.is_merged:
            str_score -= 2
            str_details.append({"dim": "合并中枢惩罚", "label": "扩展后合并中枢", "score": -2})
            tags.append("合并中枢⚠")

        # VETO: alternating hub pattern (上下上) → direct weak classification
        # Per 108课详解 L79 & 图解缠论2: alternating hubs without forming
        # a trend indicate large-level consolidation; 3B is unreliable.
        _veto = False
        _hubs = all_hubs or []
        _hidx = hub_list_idx
        if _hidx >= 2:
            evos = [_hubs[_hidx - 2].evolution_type,
                    _hubs[_hidx - 1].evolution_type,
                    _hubs[_hidx].evolution_type]
            if ("上" in evos[0] and "下" in evos[1] and "上" in evos[2]):
                _veto = True
                tags.append("中枢交替(上下上)⛔一票否决")
                str_details.append({"dim": "一票否决", "label": "上下上交替→大级别震荡", "score": 0})
                conf_details.append({"dim": "一票否决", "label": "无趋势结构", "score": 0})

        if _veto:
            strength = "weak"
            conf = "low"
        else:
            strength = ("strongest" if str_score >= 8 else
                        "strong" if str_score >= 5 else
                        "standard" if str_score >= 1 else "weak")
            conf = ("high" if conf_score >= 4 else
                    "medium" if conf_score >= 1 else "low")

        return (strength, conf, tags,
                str_score, str_details, conf_score, conf_details)

    def _make_3b(breakout_stroke, pullback, *, dep_count: int = 1,
                 pb_count: int = 1):
        (strength, conf, tags,
         str_score, str_dets, c_score, c_dets) = _grade_3b(
            breakout_stroke, pullback, dep_count)
        s_idx = pullback.idx + 1
        d_idx = stm.get(pullback.idx, -2) + 1
        loc = f"S{s_idx}" + (f"/D{d_idx}" if d_idx >= 0 else "")
        _STRENGTH_ZH = {
            "strongest": "最强", "strong": "强势",
            "standard": "标准", "weak": "弱",
        }
        breakout_pct = (breakout_stroke.end.high - hub.zg) / hub_range * 100
        margin_pct = (pullback.end.low - hub.zg) / hub_range * 100
        if dep_count > 1 or pb_count > 1:
            tags.append(f"次级别({dep_count}笔离开+{pb_count}笔回抽)")
        else:
            tags.append("单笔离开")
        tag_str = "，".join(tags)
        return BuySellPoint(
            type="3B", label="三买",
            dt=pullback.end.dt, price=pullback.end.low,
            description=(
                f"[{loc}] 离开中枢{hub.idx + 1}后回试，"
                f"低点({pullback.end.low:.3f})不破ZG({hub.zg:.3f})，"
                f"突破{breakout_pct:.0f}%/余量{margin_pct:.0f}%，"
                f"{_STRENGTH_ZH[strength]}（{tag_str}）"
            ),
            level=level, confidence=conf, hub_idx=hub.idx,
            stroke_idx=s_idx, seg_idx=d_idx,
            strength=strength,
            strength_score=str_score,
            strength_details=str_dets,
            conf_score=c_score,
            conf_details=c_dets,
            invalidation_price=hub.zg,
            trend_hub_rank=trend_hub_rank,
        )

    scan_limit = next_hub.strokes[0].idx if next_hub else len(strokes) + 1

    # Collect all strokes after the hub and before the next hub.
    post_hub = [s for s in strokes
                if s.idx > hub_end_idx and s.idx < scan_limit]

    # --- Single-stroke check ---
    # Path A: hub's last stroke already exceeds ZG (common when hubs are
    # consecutive and the departure is absorbed into the hub). Pullback is
    # bounded to the immediately next stroke only.
    single_found = False
    if last_stroke.direction == 1 and last_stroke.end.high > hub.zg:
        pullback = _find_next_stroke(
            strokes, last_stroke.idx, direction=-1,
            before_idx=last_stroke.idx + 3)
        if pullback and pullback.end.low > hub.zg:
            points.append(_make_3b(last_stroke, pullback))
            single_found = True

    # Path B: departure is a post-hub stroke (gap between consecutive hubs).
    # Pullback bounded by scan_limit so it stays before the next hub.
    if not single_found:
        for s in post_hub:
            if s.direction == 1 and s.end.high > hub.zg:
                pullback = _find_next_stroke(
                    strokes, s.idx, direction=-1, before_idx=scan_limit)
                if pullback and pullback.end.low > hub.zg:
                    points.append(_make_3b(s, pullback))
                    single_found = True
                break

    # --- Multi-stroke scan: complete sub-level departure + pullback ---
    # Only fires when single-stroke scan didn't produce a signal (each hub
    # should produce at most ONE type-3 buy signal).
    if not single_found:
        post_hub_ext = [s for s in strokes
                        if s.idx > hub_end_idx and s.idx < scan_limit]
        above_zg: list[Stroke] = []
        for s in post_hub_ext:
            if s.direction == 1:
                if s.end.high <= hub.zg:
                    continue
                above_zg.append(s)
            elif s.direction == -1:
                if s.end.low < hub.zg:
                    break
                above_zg.append(s)

        if len(above_zg) >= 3:
            up_above = [s for s in above_zg if s.direction == 1]
            if up_above:
                peak_s = max(up_above, key=lambda s: s.end.high)
                dep_up = len([s for s in up_above if s.idx <= peak_s.idx])
                pb_downs = [s for s in above_zg
                            if s.idx > peak_s.idx and s.direction == -1]
                if pb_downs:
                    trough_s = min(pb_downs, key=lambda s: s.end.low)
                    if trough_s.end.low > hub.zg and (dep_up > 1 or len(pb_downs) > 1):
                        points.append(_make_3b(
                            peak_s, trough_s,
                            dep_count=dep_up,
                            pb_count=len(pb_downs),
                        ))


def _check_type3_sell(hub: Hub, strokes: list[Stroke], hub_end_idx: int,
                      points: list[BuySellPoint], level: str,
                      stroke_to_seg: dict[int, int] | None = None,
                      trend_hub_rank: int = 1, churn: int = 0,
                      next_hub: Hub | None = None,
                      all_hubs: list["Hub"] | None = None,
                      hub_list_idx: int = -1):
    """Check for Type 3 sell point after hub with quality grading.

    Mirror of _check_type3_buy for the sell side.
    """
    stm = stroke_to_seg or {}
    last_stroke = hub.strokes[-1]
    hub_range = hub.zg - hub.zd if hub.zg > hub.zd else 1e-9

    def _classify_departure_pullback_sell(breakdown_stroke, rally):
        """Mirror of _classify_departure_pullback_buy for sell side."""
        dep_below_zd = hub.zd - breakdown_stroke.end.low
        dep_total = abs(breakdown_stroke.start.high - breakdown_stroke.end.low)
        rl_total = abs(rally.end.high - rally.start.low)

        dep_strong = (dep_below_zd > hub_range * 0.5
                      or dep_total > hub_range * 0.8)
        dep_weak = dep_below_zd < hub_range * 0.3 and dep_total < hub_range * 0.5

        if dep_below_zd > 0:
            retrace = rl_total / dep_below_zd
        else:
            retrace = 1.0
        rl_is_shallow = retrace < 0.382
        rl_is_deep = retrace > 0.618

        if dep_strong and rl_is_shallow:
            return "趋势+盘整", 3
        elif dep_strong and not rl_is_deep:
            return "趋势+回抽", 2
        elif dep_strong and rl_is_deep:
            return "趋势+反趋势", 1
        elif dep_weak and rl_is_shallow:
            return "盘整+盘整", -2
        elif dep_weak and rl_is_deep:
            return "盘整+反趋势", 0
        elif dep_weak:
            return "盘整离开", -1
        else:
            return "标准离开", 0

    def _assess_expansion_risk_sell(breakdown_stroke) -> tuple[int, str]:
        """P1: Mirror of buy-side expansion risk for sell signals."""
        _hubs = all_hubs or []
        _hidx = hub_list_idx

        risk_score = 0
        risk_label = ""

        if trend_hub_rank >= 4:
            risk_score += 3
            risk_label = f"极高(第{trend_hub_rank}中枢)"
        elif trend_hub_rank == 3:
            risk_score += 2
            risk_label = f"高(第{trend_hub_rank}中枢)"
        elif trend_hub_rank == 2:
            risk_score += 1
            risk_label = "中等(第2中枢)"

        if _hubs and _hidx >= 1:
            first_gg = max(h.gg for h in _hubs[:_hidx + 1])
            last_dd = min(h.dd for h in _hubs[:_hidx + 1])
            accum_range = first_gg - last_dd
            breakdown_depth = hub.zd - breakdown_stroke.end.low
            if accum_range > 0 and breakdown_depth > 0:
                escape_ratio = breakdown_depth / accum_range
                if escape_ratio < 0.1:
                    risk_score += 2
                    risk_label = (risk_label + "+逃逸不足") if risk_label else "逃逸不足"
                elif escape_ratio < 0.2:
                    risk_score += 1
                    risk_label = (risk_label + "+逃逸偏弱") if risk_label else "逃逸偏弱"

        if hub.is_merged:
            risk_score += 2
            risk_label = (risk_label + "+合并中枢") if risk_label else "合并中枢"

        if hub.duration_bars > 60:
            risk_score += 1
            risk_label = (risk_label + "+长期盘整") if risk_label else "长期盘整"

        if not risk_label:
            risk_label = "低" if risk_score == 0 else f"偏低({risk_score})"

        return risk_score, risk_label

    def _grade_3s(breakdown_stroke, rally, dep_count=1):
        margin_pct = (hub.zd - rally.end.high) / hub_range
        breakdown_abs = (hub.zd - breakdown_stroke.end.low) / hub.zd if hub.zd else 0
        hub_width = len(hub.strokes)
        dif_val = breakdown_stroke.dif_extreme

        tags = []
        str_score = 0
        str_details = []
        conf_score = 0
        conf_details = []

        # === STRENGTH ===
        # S1: trend context — symmetric with buy side
        s1 = 0; s1_l = ""
        if trend_hub_rank == 0:
            s1 = 5; s1_l = "上涨末端三卖"; tags.append("上涨末端三卖")
        elif trend_hub_rank == 1:
            s1 = 5; s1_l = "首个中枢三卖"; tags.append("首个中枢三卖")
        elif trend_hub_rank == 2:
            s1 = 2; s1_l = "第二中枢三卖"; tags.append("第二中枢三卖")
        elif trend_hub_rank == 3:
            s1 = 0; s1_l = f"第三中枢三卖"; tags.append(f"{s1_l}⚠")
        elif trend_hub_rank <= 5:
            s1 = -2; s1_l = f"趋势末端(第{trend_hub_rank}中枢)"; tags.append(f"{s1_l}⚠")
        else:
            s1 = -4; s1_l = f"极晚期(第{trend_hub_rank}中枢)"; tags.append(f"{s1_l}⚠")
        str_score += s1
        str_details.append({"dim": "趋势位置", "label": s1_l, "score": s1})

        # S2: departure+pullback combination
        combo_label, combo_score = _classify_departure_pullback_sell(
            breakdown_stroke, rally)
        str_score += combo_score
        if combo_score != 0:
            tags.append(combo_label)
        str_details.append({"dim": "离开回抽", "label": combo_label,
                             "score": combo_score})

        # S3: breakdown strength
        s3 = 0
        if breakdown_abs > 0.03:
            s3 = 2; s3_l = f"强破位({breakdown_abs:.1%})"; tags.append("强破位")
        elif breakdown_abs > 0.01:
            s3 = 1; s3_l = f"一般({breakdown_abs:.1%})"
        elif breakdown_abs < 0.005:
            s3 = -1; s3_l = f"弱破位({breakdown_abs:.1%})"; tags.append("弱破位⚠")
        else:
            s3_l = f"偏弱({breakdown_abs:.1%})"
        str_score += s3
        str_details.append({"dim": "破位力度", "label": s3_l, "score": s3})

        # S4: MACD
        s4 = 0
        if dif_val < 0:
            s4 = 1; s4_l = "DIF<0 空头区"; tags.append("DIF<0")
        elif dif_val > 0:
            s4 = -1; s4_l = "DIF>0 多头区"
        else:
            s4_l = "DIF≈0"
        str_score += s4
        str_details.append({"dim": "MACD", "label": s4_l, "score": s4})

        # S5: Volume
        s5 = 0; s5_l = "-"
        if breakdown_stroke.avg_volume > 0 and rally.avg_volume > 0:
            vol_ratio = rally.avg_volume / breakdown_stroke.avg_volume
            if vol_ratio < 0.5:
                s5 = 2; s5_l = f"缩量反弹({vol_ratio:.0%})"; tags.append(f"缩量反弹✓({vol_ratio:.0%})")
            elif vol_ratio < 0.7:
                s5 = 1; s5_l = f"温和缩量({vol_ratio:.0%})"; tags.append(f"温和缩量({vol_ratio:.0%})")
            elif vol_ratio > 1.5:
                s5 = -2; s5_l = f"放量反弹({vol_ratio:.0%})"; tags.append(f"放量反弹⚠({vol_ratio:.0%})")
            elif vol_ratio > 1.2:
                s5 = -1; s5_l = f"量能偏大({vol_ratio:.0%})"; tags.append(f"量能偏大({vol_ratio:.0%})")
            else:
                s5_l = f"正常({vol_ratio:.0%})"
        str_score += s5
        str_details.append({"dim": "量能", "label": s5_l, "score": s5})

        if hub.volume_trend == "shrink":
            str_score += 1
            str_details.append({"dim": "枢内蓄势", "label": "缩量蓄势", "score": 1})
            tags.append("枢内缩量蓄势✓")

        # === CONFIDENCE ===
        # C1: invalidation margin
        c1 = 0
        if margin_pct > 1.0:
            c1 = 3; c1_l = f"远离失效位(余{margin_pct:.0%})"
        elif margin_pct > 0.50:
            c1 = 2; c1_l = f"安全(余{margin_pct:.0%})"; tags.append("浅反弹")
        elif margin_pct > 0.10:
            c1 = 1; c1_l = f"适中(余{margin_pct:.0%})"
        elif margin_pct > 0.02:
            c1 = 0; c1_l = f"偏近(余{margin_pct:.0%})"
        else:
            c1 = -2; c1_l = f"极近失效位(余{margin_pct:.0%})"; tags.append("深反弹⚠")
        conf_score += c1
        conf_details.append({"dim": "失效距离", "label": c1_l, "score": c1})

        # C2: hub width
        c2 = 0
        if hub_width >= 7:
            c2 = 1; c2_l = f"充分构建({hub_width}笔)"; tags.append("充分换手")
        elif hub_width <= 3:
            c2 = -1; c2_l = f"窄中枢({hub_width}笔)"; tags.append("窄中枢⚠")
        else:
            c2_l = f"一般({hub_width}笔)"
        conf_score += c2
        conf_details.append({"dim": "中枢宽度", "label": c2_l, "score": c2})

        # C3: churn
        c3 = 0
        if churn >= 3:
            c3 = -3; c3_l = "频繁翻转"; tags.append("频繁翻转⚠震荡市")
        elif churn >= 2:
            c3 = -1; c3_l = "方向不稳"; tags.append("方向不稳")
        else:
            c3_l = "方向清晰"; c3 = 1
        conf_score += c3
        conf_details.append({"dim": "方向稳定性", "label": c3_l, "score": c3})

        # C4: flatness
        dep_range = abs(breakdown_stroke.start.high - breakdown_stroke.end.low)
        rl_range = abs(rally.end.high - rally.start.low)
        c4 = 0; c4_l = "正常"
        if hub_range > 0:
            dep_flat = dep_range / hub_range < 0.3
            rl_flat = rl_range / hub_range < 0.3
            if dep_flat and rl_flat:
                c4 = -4; c4_l = "双横盘"; tags.append("双横盘⚠非三卖")
            elif dep_flat:
                c4 = -2; c4_l = "离开段偏弱"; tags.append("离开段偏弱⚠")
            else:
                c4 = 1; c4_l = "形态清晰"
        conf_score += c4
        if c4 != 1:
            conf_details.append({"dim": "形态清晰度", "label": c4_l, "score": c4})

        # C5: departure stroke count
        c5 = 0
        if dep_count >= 3:
            c5 = 1; c5_l = f"多笔离开({dep_count}笔)"
        else:
            c5_l = "单笔离开"
        conf_score += c5
        if c5 != 0:
            conf_details.append({"dim": "离开笔数", "label": c5_l, "score": c5})

        # C6 (P1): Expansion risk
        exp_risk, exp_label = _assess_expansion_risk_sell(breakdown_stroke)
        c6 = 0
        if exp_risk >= 5:
            c6 = -4; c6_l = f"扩展风险极高({exp_label})"
            tags.append("扩展风险极高⚠")
        elif exp_risk >= 3:
            c6 = -2; c6_l = f"扩展风险高({exp_label})"
            tags.append("扩展风险高⚠")
        elif exp_risk >= 2:
            c6 = -1; c6_l = f"扩展风险中({exp_label})"
            tags.append("扩展风险中")
        elif exp_risk == 0:
            c6 = 1; c6_l = "扩展风险低"
        else:
            c6_l = f"扩展风险偏低({exp_label})"
        conf_score += c6
        conf_details.append({"dim": "扩展风险", "label": c6_l, "score": c6})

        # P3: Raised requirements for merged/expansion hubs
        if hub.is_merged:
            str_score -= 2
            str_details.append({"dim": "合并中枢惩罚", "label": "扩展后合并中枢", "score": -2})
            tags.append("合并中枢⚠")

        # VETO: alternating hub pattern (下上下) → direct weak classification
        _veto = False
        _hubs = all_hubs or []
        _hidx = hub_list_idx
        if _hidx >= 2:
            evos = [_hubs[_hidx - 2].evolution_type,
                    _hubs[_hidx - 1].evolution_type,
                    _hubs[_hidx].evolution_type]
            if ("下" in evos[0] and "上" in evos[1] and "下" in evos[2]):
                _veto = True
                tags.append("中枢交替(下上下)⛔一票否决")
                str_details.append({"dim": "一票否决", "label": "下上下交替→大级别震荡", "score": 0})
                conf_details.append({"dim": "一票否决", "label": "无趋势结构", "score": 0})

        if _veto:
            strength = "weak"
            conf = "low"
        else:
            strength = ("strongest" if str_score >= 8 else
                        "strong" if str_score >= 5 else
                        "standard" if str_score >= 1 else "weak")
            conf = ("high" if conf_score >= 4 else
                    "medium" if conf_score >= 1 else "low")

        return (strength, conf, tags,
                str_score, str_details, conf_score, conf_details)

    def _make_3s(breakdown_stroke, rally, *, dep_count: int = 1,
                 pb_count: int = 1):
        (strength, conf, tags,
         str_score, str_dets, c_score, c_dets) = _grade_3s(
            breakdown_stroke, rally, dep_count)
        s_idx = rally.idx + 1
        d_idx = stm.get(rally.idx, -2) + 1
        loc = f"S{s_idx}" + (f"/D{d_idx}" if d_idx >= 0 else "")
        _STRENGTH_ZH = {
            "strongest": "最强", "strong": "强势",
            "standard": "标准", "weak": "弱",
        }
        breakdown_pct = (hub.zd - breakdown_stroke.end.low) / hub_range * 100
        margin_pct = (hub.zd - rally.end.high) / hub_range * 100
        if dep_count > 1 or pb_count > 1:
            tags.append(f"次级别({dep_count}笔离开+{pb_count}笔回抽)")
        else:
            tags.append("单笔离开")
        tag_str = "，".join(tags)
        return BuySellPoint(
            type="3S", label="三卖",
            dt=rally.end.dt, price=rally.end.high,
            description=(
                f"[{loc}] 离开中枢{hub.idx + 1}后回抽，"
                f"高点({rally.end.high:.3f})不破ZD({hub.zd:.3f})，"
                f"破位{breakdown_pct:.0f}%/余量{margin_pct:.0f}%，"
                f"{_STRENGTH_ZH[strength]}（{tag_str}）"
            ),
            level=level, confidence=conf, hub_idx=hub.idx,
            stroke_idx=s_idx, seg_idx=d_idx,
            strength=strength,
            strength_score=str_score,
            strength_details=str_dets,
            conf_score=c_score,
            conf_details=c_dets,
            invalidation_price=hub.zd,
            trend_hub_rank=trend_hub_rank,
        )

    scan_limit = next_hub.strokes[0].idx if next_hub else len(strokes) + 1
    post_hub = [s for s in strokes
                if s.idx > hub_end_idx and s.idx < scan_limit]

    # --- Single-stroke check ---
    # Path A: hub's last stroke already breaks below ZD (common when hubs are
    # consecutive). Rally is bounded to the immediately next stroke only.
    single_found = False
    if last_stroke.direction == -1 and last_stroke.end.low < hub.zd:
        rally = _find_next_stroke(
            strokes, last_stroke.idx, direction=1,
            before_idx=last_stroke.idx + 3)
        if rally and rally.end.high < hub.zd:
            points.append(_make_3s(last_stroke, rally))
            single_found = True

    # Path B: departure is a post-hub stroke. Rally bounded by scan_limit.
    if not single_found:
        for s in post_hub:
            if s.direction == -1 and s.end.low < hub.zd:
                rally = _find_next_stroke(
                    strokes, s.idx, direction=1, before_idx=scan_limit)
                if rally and rally.end.high < hub.zd:
                    points.append(_make_3s(s, rally))
                    single_found = True
                break

    # --- Multi-stroke scan: complete sub-level departure + pullback ---
    # Only fires when single-stroke scan didn't produce a signal (each hub
    # should produce at most ONE type-3 sell signal).
    if not single_found:
        post_hub_ext = [s for s in strokes
                        if s.idx > hub_end_idx and s.idx < scan_limit]
        below_zd: list[Stroke] = []
        for s in post_hub_ext:
            if s.direction == -1:
                if s.end.low >= hub.zd:
                    continue
                below_zd.append(s)
            elif s.direction == 1:
                if s.end.high > hub.zd:
                    break
                below_zd.append(s)

        if len(below_zd) >= 3:
            dn_below = [s for s in below_zd if s.direction == -1]
            if dn_below:
                trough_s = min(dn_below, key=lambda s: s.end.low)
                dep_dn = len([s for s in dn_below if s.idx <= trough_s.idx])
                pb_ups = [s for s in below_zd
                          if s.idx > trough_s.idx and s.direction == 1]
                if pb_ups:
                    peak_s = max(pb_ups, key=lambda s: s.end.high)
                    if peak_s.end.high < hub.zd and (dep_dn > 1 or len(pb_ups) > 1):
                        points.append(_make_3s(
                            trough_s, peak_s,
                            dep_count=dep_dn,
                            pb_count=len(pb_ups),
                        ))


# ── Helpers ──

def _find_stroke_by_dt(strokes: list[Stroke], dt: str) -> Optional[Stroke]:
    for s in strokes:
        if s.end.dt == dt:
            return s
    return None


def _find_next_stroke(strokes: list[Stroke], after_idx: int,
                      direction: int,
                      before_idx: int | None = None) -> Optional[Stroke]:
    for s in strokes:
        if before_idx is not None and s.idx >= before_idx:
            break
        if s.idx > after_idx and s.direction == direction:
            return s
    return None


# ════════════════════════════════════════════════════════════════════
# 12b. Hub Position Annotation (中枢位置标注)
# ════════════════════════════════════════════════════════════════════

def _compute_position_vs_hub(
    price: float,
    hubs: list[Hub],
    seg_hubs: list[SegHub],
) -> tuple[str, dict]:
    """Determine current price position relative to the latest stroke-level hub.

    ``seg_hubs`` is retained for call-site compatibility and ignored.

    Returns (label, detail_dict) where label is one of:
      "中枢上方运行" / "中枢区间震荡" / "中枢下方运行" / ""
    and detail_dict contains numeric info for display.
    """
    _ = seg_hubs
    detail: dict = {}
    label = ""

    if hubs:
        h = hubs[-1]
        detail["stroke_hub"] = {
            "idx": h.idx, "zg": h.zg, "zd": h.zd,
            "gg": h.gg, "dd": h.dd,
            "start_dt": h.start_dt, "end_dt": h.end_dt,
        }
        if price > h.zg:
            label = "中枢上方运行"
            detail["stroke_position"] = "above"
            detail["distance_pct"] = round((price - h.zg) / h.zg * 100, 2)
        elif price < h.zd:
            label = "中枢下方运行"
            detail["stroke_position"] = "below"
            detail["distance_pct"] = round((h.zd - price) / h.zd * 100, 2)
        else:
            label = "中枢区间震荡"
            detail["stroke_position"] = "inside"
            span = h.zg - h.zd if h.zg > h.zd else 1
            detail["distance_pct"] = round(
                (price - h.zd) / span * 100, 2
            )

    return label, detail


# ════════════════════════════════════════════════════════════════════
# 12c. Multi-level Synthesis (多级别联立)
# ════════════════════════════════════════════════════════════════════

def _extract_date(dt_str: str) -> str:
    """Extract date portion from a datetime string (YYYY-MM-DD or YYYY-MM-DD HH:MM)."""
    return dt_str[:10] if len(dt_str) >= 10 else dt_str


def _level_summary(result: AnalysisResult) -> dict:
    """Extract key summary from a single-level result."""
    latest_price = result.raw_bars[-1].close if result.raw_bars else 0
    latest_dif = result.raw_bars[-1].dif if result.raw_bars else 0
    latest_signal = None
    if result.buy_sell_points:
        latest_signal = {
            "type": result.buy_sell_points[-1].type,
            "label": result.buy_sell_points[-1].label,
            "dt": result.buy_sell_points[-1].dt,
            "confidence": result.buy_sell_points[-1].confidence,
            "price": result.buy_sell_points[-1].price,
        }
    tc = result.trend_completion or {}
    return {
        "level": result.level,
        "trend": result.trend,
        "hub_position": result.position_vs_hub,
        "trend_completion": tc.get("status", ""),
        "completion_reason": tc.get("reason", ""),
        "latest_price": latest_price,
        "latest_dif": latest_dif,
        "dif_zone": "above_zero" if latest_dif > 0 else "below_zero",
        "num_hubs": len(result.hubs),
        "num_signals": len(result.buy_sell_points),
        "latest_signal": latest_signal,
    }


def _classify_trend_direction(trend: str) -> int:
    """Map trend string to direction: 1=up, -1=down, 0=neutral."""
    if "破坏" in trend:
        return 0
    if "上涨" in trend:
        return 1
    if "下跌" in trend:
        return -1
    return 0


def _check_direction_alignment(summaries: list[dict]) -> str:
    """Assess direction consistency across levels."""
    dirs = [_classify_trend_direction(s["trend"]) for s in summaries]
    if all(d == dirs[0] and d != 0 for d in dirs):
        return "三级共振" if dirs[0] == 1 else "三级共振（空）"
    nonzero = [d for d in dirs if d != 0]
    if len(nonzero) >= 2 and len(set(nonzero)) == 1:
        return "部分一致"
    if len(set(nonzero)) > 1:
        return "大小分歧"
    return "方向不明"


def _enrich_small_level_signals(
    daily: AnalysisResult,
    small: AnalysisResult,
) -> list[dict]:
    """Add big-level context to small-level buy/sell signals.

    Theory: 108课 §3.4 — daily stage determines how impactful smaller-level
    divergences are. 图解缠论3 §1.1 — 30min divergence + daily stage + 5min
    precision together form a valid buy point.
    """
    daily_dir = _classify_trend_direction(daily.trend)
    daily_hub_pos = daily.position_vs_hub
    daily_stage = "初中段" if len(daily.hubs) <= 1 else "末段"

    enriched = []
    for p in small.buy_sell_points:
        is_buy = p.type in ("1B", "2B", "3B", "PB")
        ctx: dict = {
            "source_level": small.level,
            "type": p.type,
            "label": p.label,
            "dt": p.dt,
            "price": p.price,
            "original_confidence": p.confidence,
            "daily_trend": daily.trend,
            "daily_hub_position": daily_hub_pos,
            "daily_stage": daily_stage,
        }

        adjusted_conf = p.confidence
        note_parts = []

        if is_buy:
            if daily_dir == 1 and "上方" in daily_hub_pos:
                note_parts.append("DF多头+中枢上方，买入环境良好")
                if adjusted_conf == "low":
                    adjusted_conf = "medium"
                elif adjusted_conf == "medium":
                    adjusted_conf = "high"
            elif daily_dir == 1 and "震荡" in daily_hub_pos:
                note_parts.append("DF多头+中枢区间，买入有支撑")
            elif daily_dir == -1 or "下方" in daily_hub_pos:
                note_parts.append("DF偏空/中枢下方，逆势买入需谨慎")
                if adjusted_conf == "high":
                    adjusted_conf = "medium"
                elif adjusted_conf == "medium":
                    adjusted_conf = "low"
        else:
            if daily_dir == -1 and "下方" in daily_hub_pos:
                note_parts.append("DF空头+中枢下方，卖出/做空环境良好")
                if adjusted_conf == "low":
                    adjusted_conf = "medium"
                elif adjusted_conf == "medium":
                    adjusted_conf = "high"
            elif daily_dir == 1 and "上方" in daily_hub_pos:
                note_parts.append("DF多头+中枢上方，逆势卖出，可能仅短差")
                if adjusted_conf == "high":
                    adjusted_conf = "medium"
                elif adjusted_conf == "medium":
                    adjusted_conf = "low"

        if daily_stage == "末段":
            if is_buy and daily_dir == -1:
                note_parts.append("DF下跌末段，小级别背驰可能引发大反转")
            elif not is_buy and daily_dir == 1:
                note_parts.append("DF上涨末段，小级别背驰可能引发大跳水")
        elif daily_stage == "初中段":
            if is_buy and daily_dir == 1:
                note_parts.append("DF上涨初中段，回调有限，买入安全边际高")
            elif not is_buy and daily_dir == -1:
                note_parts.append("DF下跌初中段，反弹有限")

        ctx["adjusted_confidence"] = adjusted_conf
        ctx["context_note"] = "；".join(note_parts) if note_parts else "无特殊DF环境"
        ctx["confidence_changed"] = adjusted_conf != p.confidence
        enriched.append(ctx)

    return enriched


def _find_resonance(results: list[AnalysisResult]) -> list[dict]:
    """Find buy/sell signal resonance across levels.

    Resonance: signals of the same direction (both buy or both sell) appearing
    on the same calendar date across 2+ different levels.

    Groups by (date, direction) first, then picks the highest-priority signal
    per level to avoid Cartesian-product duplication.
    """
    _PRIORITY = {"1B": 0, "1S": 0, "3B": 1, "3S": 1, "2B": 2, "2S": 2,
                 "PB": 3, "PS": 3}

    groups: dict[tuple[str, str], dict[str, dict]] = {}

    for r in results:
        for p in r.buy_sell_points:
            is_buy = p.type in ("1B", "2B", "3B", "PB")
            side = "buy" if is_buy else "sell"
            date = _extract_date(p.dt)
            key = (date, side)

            info = {
                "level": r.level, "type": p.type, "label": p.label,
                "dt": p.dt, "price": p.price, "confidence": p.confidence,
            }

            existing = groups.setdefault(key, {})
            prev = existing.get(r.level)
            if prev is None or _PRIORITY.get(p.type, 9) < _PRIORITY.get(prev["type"], 9):
                existing[r.level] = info

    resonances = []
    for (date, side), level_map in sorted(groups.items()):
        if len(level_map) < 2:
            continue
        signals = list(level_map.values())
        level_names = [s["level"] for s in signals]
        label_names = [s["label"] for s in signals]
        resonances.append({
            "date": date,
            "direction": side,
            "signals": signals,
            "note": "与".join(level_names) + "同日" + "+".join(label_names) + "共振",
        })

    return resonances


def _determine_overall_bias(summaries: list[dict], alignment: str) -> tuple[str, str]:
    """Determine overall market bias and action advice."""
    daily = summaries[0] if summaries else {}
    daily_dir = _classify_trend_direction(daily.get("trend", ""))
    daily_hub = daily.get("hub_position", "")
    daily_dif_zone = daily.get("dif_zone", "")

    if "三级共振" in alignment:
        if daily_dir == 1:
            return "偏多", "多头共振，持仓待涨或逢低加仓"
        else:
            return "偏空", "空头共振，空仓观望或逢高减仓"

    if daily_dir == 1:
        if "上方" in daily_hub:
            return "偏多", "DF多头+中枢上方，回调是买入机会"
        elif "震荡" in daily_hub:
            return "中性偏多", "DF多头+中枢区间震荡，可高抛低吸"
        else:
            return "中性", "DF上涨趋势但在中枢下方，等待企稳信号"
    elif daily_dir == -1:
        if "下方" in daily_hub:
            return "偏空", "DF空头+中枢下方，反弹是减仓机会"
        elif "震荡" in daily_hub:
            return "中性偏空", "DF空头+中枢区间震荡，轻仓博弈"
        else:
            return "中性", "DF下跌趋势但在中枢上方，关注是否三卖"
    return "中性震荡", "方向不明，观望为主"


def _div_time_range(div: dict) -> tuple[str, str]:
    """Extract the time range of the divergence segment."""
    if div["type"] == "trend":
        return (div.get("c_start_dt", ""), div.get("c_end_dt", ""))
    return (div.get("curr_start_dt", ""), div.get("curr_end_dt", ""))


def _is_within_range(dt: str, range_start: str, range_end: str) -> bool:
    """Check if a datetime string falls within a time range (inclusive)."""
    if not dt or not range_start or not range_end:
        return False
    dt_date = _extract_date(dt)
    start_date = _extract_date(range_start)
    end_date = _extract_date(range_end)
    return start_date <= dt_date <= end_date


def _find_divs_in_range(
    result: AnalysisResult, range_start: str, range_end: str, direction: int,
) -> list[dict]:
    """Find divergences within a time range matching the given direction."""
    matches = []
    for d in result.divergences:
        if d["direction"] != direction:
            continue
        if _is_within_range(d["dt"], range_start, range_end):
            matches.append(d)
    return matches


def _find_bsp_in_range(
    result: AnalysisResult, range_start: str, range_end: str, direction: int,
) -> list[BuySellPoint]:
    """Find buy/sell points within a time range matching direction."""
    is_buy_dir = direction == -1  # down divergence → buy signal
    matches = []
    for p in result.buy_sell_points:
        is_buy = p.type in ("1B", "2B", "3B", "PB")
        if is_buy != is_buy_dir:
            continue
        if _is_within_range(p.dt, range_start, range_end):
            matches.append(p)
    return matches


# ════════════════════════════════════════════════════════════════════
# P4: Cross-Level Signal Filter (多周期信号联立过滤)
#
# Theory basis (图解缠论3 §1.1, 缠论辅导 §二):
#   Lower-level signals are more reliable when aligned with higher-level trend.
#   A 30min 3B inside a daily downtrend is "逆势短差" and should be penalized.
#   A 30min 3B aligned with a daily 3B is boosted.
# ════════════════════════════════════════════════════════════════════

def cross_level_filter(
    lower_result: AnalysisResult,
    higher_result: AnalysisResult,
) -> None:
    """Filter lower-level 3B/3S signals based on higher-level environment.

    Modifies lower_result.buy_sell_points in-place:
      - Aligned with higher trend → boost strength_score +2
      - Against higher trend → penalize strength_score -3
      - Higher level in expansion/consolidation → penalize -1
    """
    if not higher_result.buy_sell_points and not higher_result.trend:
        return

    higher_trend = higher_result.trend
    higher_is_up = "上涨" in higher_trend
    higher_is_down = "下跌" in higher_trend
    higher_is_consolidation = "盘整" in higher_trend or "扩展" in higher_trend

    # Check if higher level has recent expansion-merged hubs
    higher_has_expansion = any(h.is_merged for h in higher_result.merged_hubs[-2:]
                               ) if higher_result.merged_hubs else False

    for p in lower_result.buy_sell_points:
        if p.type not in ("3B", "3S"):
            continue

        adjustment = 0
        label_parts = []

        if p.type == "3B":
            if higher_is_up:
                adjustment = 2
                label_parts.append("上级别上涨顺势")
            elif higher_is_down:
                adjustment = -3
                label_parts.append("上级别下跌逆势⚠")
            elif higher_is_consolidation:
                adjustment = -1
                label_parts.append("上级别盘整")

        elif p.type == "3S":
            if higher_is_down:
                adjustment = 2
                label_parts.append("上级别下跌顺势")
            elif higher_is_up:
                adjustment = -3
                label_parts.append("上级别上涨逆势⚠")
            elif higher_is_consolidation:
                adjustment = -1
                label_parts.append("上级别盘整")

        if higher_has_expansion:
            adjustment -= 1
            label_parts.append("上级别有扩展")

        if adjustment != 0:
            p.strength_score += adjustment
            p.strength_details.append({
                "dim": "上级别环境",
                "label": "，".join(label_parts),
                "score": adjustment,
            })
            # Recalculate strength grade
            p.strength = ("strongest" if p.strength_score >= 8 else
                          "strong" if p.strength_score >= 5 else
                          "standard" if p.strength_score >= 1 else "weak")


def find_interval_nests(
    daily: AnalysisResult,
    min30: Optional[AnalysisResult] = None,
    min5: Optional[AnalysisResult] = None,
) -> list[IntervalNest]:
    """Build interval nesting chains from large to small timeframes.

    For each daily divergence:
      1. Find 30min divergences/signals within its time range
      2. For each 30min match, find 5min divergences/signals within that range
      3. Build the deepest possible chain as the precision entry
    """
    nests = []

    for div in daily.divergences:
        big_range = _div_time_range(div)
        if not big_range[0] or not big_range[1]:
            continue

        nest = IntervalNest(
            big_level=daily.level,
            big_signal_type=div["type"],
            big_signal_dt=div["dt"],
            big_time_range=big_range,
            big_direction=div["direction"],
            precision_price=div.get("price", 0),
            precision_dt=div["dt"],
            depth=1,
        )

        mid_matched = False
        if min30:
            mid_divs = _find_divs_in_range(
                min30, big_range[0], big_range[1], div["direction"],
            )
            mid_bsps = _find_bsp_in_range(
                min30, big_range[0], big_range[1], div["direction"],
            )

            best_mid_div = mid_divs[-1] if mid_divs else None
            best_mid_bsp = mid_bsps[-1] if mid_bsps else None

            if best_mid_div:
                mid_range = _div_time_range(best_mid_div)
                nest.mid_level = min30.level
                nest.mid_signal_dt = best_mid_div["dt"]
                nest.mid_time_range = mid_range
                nest.precision_price = best_mid_div.get("price", nest.precision_price)
                nest.precision_dt = best_mid_div["dt"]
                nest.depth = 2
                mid_matched = True

                if min5 and mid_range[0] and mid_range[1]:
                    small_divs = _find_divs_in_range(
                        min5, mid_range[0], mid_range[1], div["direction"],
                    )
                    small_bsps = _find_bsp_in_range(
                        min5, mid_range[0], mid_range[1], div["direction"],
                    )
                    best_small = small_bsps[-1] if small_bsps else None
                    best_small_div = small_divs[-1] if small_divs else None

                    if best_small:
                        nest.small_level = min5.level
                        nest.small_signal_dt = best_small.dt
                        nest.precision_price = best_small.price
                        nest.precision_dt = best_small.dt
                        nest.depth = 3
                    elif best_small_div:
                        nest.small_level = min5.level
                        nest.small_signal_dt = best_small_div["dt"]
                        nest.precision_price = best_small_div.get("price", nest.precision_price)
                        nest.precision_dt = best_small_div["dt"]
                        nest.depth = 3

            elif best_mid_bsp:
                nest.mid_level = min30.level
                nest.mid_signal_dt = best_mid_bsp.dt
                nest.mid_time_range = ("", "")
                nest.precision_price = best_mid_bsp.price
                nest.precision_dt = best_mid_bsp.dt
                nest.depth = 2
                mid_matched = True

        if not mid_matched and min5:
            small_bsps = _find_bsp_in_range(
                min5, big_range[0], big_range[1], div["direction"],
            )
            if small_bsps:
                best = small_bsps[-1]
                nest.small_level = min5.level
                nest.small_signal_dt = best.dt
                nest.precision_price = best.price
                nest.precision_dt = best.dt
                nest.depth = 2

        direction_label = "买入" if div["direction"] == -1 else "卖出"
        type_label = "趋势背驰" if div["type"] == "trend" else "盘整背驰"
        nest.note = (
            f"{nest.big_level}{type_label}{direction_label}"
            f" → {'→'.join(filter(None, [nest.mid_level, nest.small_level]))}"
            f" 精确定位"
            if nest.depth > 1 else
            f"{nest.big_level}{type_label}{direction_label}（未找到下级别确认）"
        )
        nests.append(nest)

    return nests


def find_pending_3b(result: AnalysisResult) -> list[Pending3B]:
    """Find hubs that have been broken upward but pullback is not yet confirmed.

    These are "three-buy candidates" — the breakout has occurred but:
      - No pullback stroke has started yet (price still rising), OR
      - A pullback stroke is in progress but hasn't completed yet

    Excludes hubs that already produced a confirmed 3B signal.
    """
    pending = []
    hubs = result.hubs
    strokes = result.strokes
    bars = result.raw_bars

    if not hubs or not strokes or not bars:
        return pending

    confirmed_hub_idxs = set()
    for p in result.buy_sell_points:
        if p.type == "3B":
            confirmed_hub_idxs.add(p.hub_idx)

    last_bar = bars[-1]
    last_price = last_bar.close
    last_dt = last_bar.dt

    for hub in hubs:
        if hub.idx in confirmed_hub_idxs:
            continue

        hub_range = hub.zg - hub.zd if hub.zg > hub.zd else 1e-9

        hub_end_idx = hub.strokes[-1].idx
        post_hub = [s for s in strokes if s.idx > hub_end_idx]
        if not post_hub:
            continue

        breakout_stroke = None
        for s in post_hub:
            if s.direction == 1 and s.end.high > hub.zg:
                breakout_stroke = s
                break

        if not breakout_stroke:
            continue

        breakout_pct = (breakout_stroke.end.high - hub.zg) / hub_range * 100

        # Check what happened after the breakout
        post_breakout = [s for s in strokes if s.idx > breakout_stroke.idx]

        # Is there a completed down-stroke after the breakout?
        pullback_stroke = None
        for s in post_breakout:
            if s.direction == -1:
                pullback_stroke = s
                break

        if pullback_stroke and pullback_stroke.end.low <= hub.zg:
            # Pullback already broke ZG → 3B failed, not pending
            continue

        if pullback_stroke:
            # Down-stroke exists but didn't break ZG → check if it's the LAST stroke
            later_strokes = [s for s in strokes if s.idx > pullback_stroke.idx]
            if later_strokes:
                # More strokes after pullback → should already be a confirmed 3B
                continue
            # The pullback is the last completed stroke → "回抽进行中"
            current_low = pullback_stroke.end.low
            margin = current_low - hub.zg
            margin_pct = margin / hub_range * 100
            if margin < 0:
                # Already broke ZG → failed, not pending
                continue
            if margin_pct < 5:
                status = "回抽已至ZG附近"
            else:
                status = "回抽进行中"
        else:
            # No confirmed pullback stroke yet
            current_low = last_price
            margin = last_price - hub.zg
            margin_pct = margin / hub_range * 100
            if margin < 0:
                # Price fell below ZG without a proper structure → failed
                continue
            # Check if price has dropped from breakout high (pullback forming but not confirmed)
            drop_from_peak = breakout_stroke.end.high - last_price
            drop_ratio = drop_from_peak / hub_range if hub_range > 0 else 0
            if drop_ratio > 0.3:
                status = "回抽进行中"
            else:
                status = "突破等待回抽"

        # Filter: breakout too far from current price (hub is stale/irrelevant)
        if breakout_pct > 300:
            continue

        # Only report recent pending 3B (breakout is within last 15% of bars)
        breakout_bar_idx = next(
            (i for i, b in enumerate(bars) if b.dt >= breakout_stroke.end.dt),
            len(bars) - 1
        )
        recency_threshold = int(len(bars) * 0.85)
        if breakout_bar_idx < recency_threshold:
            continue

        # Hub rank (position in trend sequence)
        hub_rank = hub.trend_seq + 1 if hub.trend_seq >= 0 else 1

        p3b = Pending3B(
            hub_idx=hub.idx,
            hub_zg=hub.zg,
            hub_zd=hub.zd,
            hub_strokes=len(hub.strokes),
            breakout_dt=breakout_stroke.end.dt,
            breakout_high=breakout_stroke.end.high,
            breakout_pct=breakout_pct,
            current_low=current_low,
            margin_to_zg=margin,
            margin_pct=margin_pct,
            status=status,
            level=result.level,
            stop_loss=hub.zg,
            hub_rank=hub_rank,
            note=(
                f"中枢{hub.idx+1}(ZG={hub.zg:.3f})已突破"
                f"{breakout_pct:.0f}%@{breakout_stroke.end.dt}，"
                f"{status}，余量{margin_pct:.0f}%"
            ),
        )
        pending.append(p3b)

    return pending


def _confirm_3b_sub_level(
    signal_level_result: AnalysisResult,
    sub_level_result: AnalysisResult,
    daily: AnalysisResult,
) -> list[ThreeBuyConfirmation]:
    """Check sub-level divergence/buy for each 3B signal in signal_level_result.

    Theory (三买筛选与评价体系 §八):
      The pullback stroke of a 3B is a sub-level decline. Use interval nesting
      to find where that decline ENDS (sub-level 1B / consolidation divergence)
      = the precise entry point for the parent-level 3B.

    Confirmation hierarchy (strict interval nesting priority):
      Tier 1 - 区间套确认 (signals near pullback END, within last 30% of window):
        1B (一买): sub-level trend divergence ending the decline
        PB (盘整一买): sub-level consolidation divergence
        Divergence (趋势背驰/盘整背驰): raw divergence signal
      Tier 2 - 二买确认 (2B after a prior 1B/PB in window):
        Confirms the reversal is real
      Tier 3 - 共振确认 (structural buy signals not at pullback end):
        3B (三买): sub-level also shows breakout structure (resonance)
        Other signals early in the window

    For each 3B signal in signal_level_result:
      1. Identify the pullback stroke's time window (start_dt → end_dt)
      2. Search sub_level_result for divergences and buy signals within that window
      3. Classify by proximity to pullback end and signal type
      4. Assign confirmation tier and type
    """
    confirmations = []
    daily_dir = _classify_trend_direction(daily.trend)
    daily_env_map = {1: "日线多头", -1: "日线空头", 0: "日线中性"}
    daily_env = daily_env_map.get(daily_dir, "日线中性")

    strokes = signal_level_result.strokes

    for p in signal_level_result.buy_sell_points:
        if p.type != "3B":
            continue

        conf = ThreeBuyConfirmation(
            source_level=signal_level_result.level,
            source_3b_dt=p.dt,
            source_3b_price=p.price,
            source_3b_hub_idx=p.hub_idx,
            source_3b_strength=p.strength,
            daily_env=daily_env,
        )

        pullback_idx = p.stroke_idx - 1
        pullback_stroke = None
        for s in strokes:
            if s.idx == pullback_idx and s.direction == -1:
                pullback_stroke = s
                break

        if not pullback_stroke:
            for s in strokes:
                if (s.direction == -1 and s.end.dt == p.dt
                        and abs(s.end.low - p.price) < 1e-6):
                    pullback_stroke = s
                    break

        if not pullback_stroke:
            conf.overall_status = "pending"
            conf.note = "未能定位回抽笔，无法执行次级别确认"
            confirmations.append(conf)
            continue

        conf.pullback_start_dt = pullback_stroke.start.dt
        conf.pullback_end_dt = pullback_stroke.end.dt
        conf.sub_level = sub_level_result.level

        range_start = pullback_stroke.start.dt
        range_end = pullback_stroke.end.dt

        sub_divs = _find_divs_in_range(
            sub_level_result, range_start, range_end, direction=-1
        )
        sub_bsps = _find_bsp_in_range(
            sub_level_result, range_start, range_end, direction=-1
        )

        conf.sub_divergences = [
            {"type": d["type"], "dt": d["dt"],
             "price": d.get("price", 0), "direction": d["direction"]}
            for d in sub_divs
        ]
        conf.sub_buy_signals = [
            {"type": bp.type, "label": bp.label, "dt": bp.dt,
             "price": bp.price, "confidence": bp.confidence}
            for bp in sub_bsps
        ]

        # --- Interval nesting logic: classify signals by tier ---
        # Compute the "last 30%" time threshold for proximity to pullback end
        # Signals near the end are true interval-nesting confirmations;
        # signals early in the window are structural resonance at best.
        _last_30pct_dt = _compute_time_threshold(
            range_start, range_end, ratio=0.5
        )

        # Tier 1: 区间套确认 — 1B/PB/divergence near pullback end
        tier1_bsps = [
            bp for bp in sub_bsps
            if bp.type in ("1B", "PB") and bp.dt >= _last_30pct_dt
        ]
        tier1_divs = [
            d for d in sub_divs if d["dt"] >= _last_30pct_dt
        ]

        # Tier 2: 二买确认 — 2B anywhere in window (confirms prior reversal)
        tier2_bsps = [bp for bp in sub_bsps if bp.type == "2B"]

        # Tier 3: 共振确认 — 3B or early 1B/PB (structural, not interval nesting)
        tier3_bsps = [
            bp for bp in sub_bsps
            if bp.type == "3B" or (bp.type in ("1B", "PB") and bp.dt < _last_30pct_dt)
        ]

        # Also check: divergence early in window → weaker confirmation
        tier3_divs = [d for d in sub_divs if d["dt"] < _last_30pct_dt]

        # --- Assign confirmation result by priority ---
        if tier1_bsps:
            best = tier1_bsps[-1]
            conf.confirmed = True
            if best.type == "1B":
                conf.confirmation_type = "区间套一买"
            else:
                conf.confirmation_type = "区间套盘背"
            conf.confirmation_dt = best.dt
            conf.confirmation_price = best.price
            conf.overall_status = "confirmed"
            conf.note = (
                f"{conf.sub_level}{conf.confirmation_type}"
                f"（{conf.confirmation_dt}，价{conf.confirmation_price:.3f}）"
            )
        elif tier1_divs:
            best = tier1_divs[-1]
            conf.confirmed = True
            conf.confirmation_type = (
                "区间套趋势背驰" if best["type"] == "trend" else "区间套盘整背驰"
            )
            conf.confirmation_dt = best["dt"]
            conf.confirmation_price = best.get("price", 0)
            conf.overall_status = "confirmed"
            conf.note = (
                f"{conf.sub_level}{conf.confirmation_type}"
                f"（{conf.confirmation_dt}）"
            )
        elif tier2_bsps:
            best = tier2_bsps[-1]
            conf.confirmed = True
            conf.confirmation_type = "二买确认"
            conf.confirmation_dt = best.dt
            conf.confirmation_price = best.price
            conf.overall_status = "confirmed"
            conf.note = (
                f"{conf.sub_level}{conf.confirmation_type}"
                f"（{conf.confirmation_dt}，价{conf.confirmation_price:.3f}）"
            )
        elif tier3_bsps:
            best = tier3_bsps[-1]
            conf.confirmed = True
            if best.type == "3B":
                conf.confirmation_type = "共振三买"
            elif best.type == "1B":
                conf.confirmation_type = "共振一买"
            else:
                conf.confirmation_type = "共振盘背"
            conf.confirmation_dt = best.dt
            conf.confirmation_price = best.price
            conf.overall_status = "confirmed"
            conf.note = (
                f"{conf.sub_level}{conf.confirmation_type}"
                f"（{conf.confirmation_dt}，价{conf.confirmation_price:.3f}）"
                f"（非区间套：信号位于回抽段前半段）"
            )
        elif tier3_divs:
            best = tier3_divs[-1]
            conf.confirmed = True
            div_label = "趋势背驰" if best["type"] == "trend" else "盘整背驰"
            conf.confirmation_type = f"共振{div_label}"
            conf.confirmation_dt = best["dt"]
            conf.confirmation_price = best.get("price", 0)
            conf.overall_status = "confirmed"
            conf.note = (
                f"{conf.sub_level}{conf.confirmation_type}"
                f"（{conf.confirmation_dt}）"
                f"（非区间套：背驰位于回抽段前半段）"
            )
        else:
            conf.overall_status = "pending"
            conf.note = (
                f"回抽段（{range_start}~{range_end}）内"
                f"未发现{conf.sub_level}背驰/买点信号"
            )

        confirmations.append(conf)

    return confirmations


def _compute_time_threshold(start_dt: str, end_dt: str, ratio: float = 0.5) -> str:
    """Compute a time point at `ratio` of the way from start to end.

    Used to split the pullback time window into "early" and "late" portions.
    Signals in the late portion (near pullback end) are interval-nesting confirmations;
    signals in the early portion are structural resonance.
    """
    from datetime import datetime

    fmt_full = "%Y-%m-%d %H:%M:%S"
    fmt_date = "%Y-%m-%d"

    try:
        t_start = datetime.strptime(start_dt, fmt_full)
    except ValueError:
        try:
            t_start = datetime.strptime(start_dt, fmt_date)
        except ValueError:
            return start_dt

    try:
        t_end = datetime.strptime(end_dt, fmt_full)
    except ValueError:
        try:
            t_end = datetime.strptime(end_dt, fmt_date)
        except ValueError:
            return start_dt

    delta = t_end - t_start
    threshold = t_start + delta * ratio

    if " " in start_dt or " " in end_dt:
        return threshold.strftime(fmt_full)
    return threshold.strftime(fmt_date)


def synthesize_multi_level(
    daily: AnalysisResult,
    min30: Optional[AnalysisResult] = None,
    min5: Optional[AnalysisResult] = None,
    weekly: Optional[AnalysisResult] = None,
) -> MultiLevelSynthesis:
    """Synthesize signals across multiple timeframes.

    Theory basis:
      - 108课 §3.4: daily stage determines small-level divergence impact
      - 图解缠论3 §1.1: 30min divergence + daily position + 5min precision
      - 土匪注解 §1.3: multi-level classification, small → big escalation
    """
    syn = MultiLevelSynthesis()

    results = []
    if weekly:
        results.append(weekly)
    results.append(daily)
    if min30:
        results.append(min30)
    if min5:
        results.append(min5)

    syn.level_summary = [_level_summary(r) for r in results]
    syn.direction_alignment = _check_direction_alignment(syn.level_summary)

    for r in results:
        if r.level != daily.level:
            syn.enriched_signals.extend(
                _enrich_small_level_signals(daily, r)
            )

    syn.resonance_signals = _find_resonance(results)

    syn.interval_nests = find_interval_nests(daily, min30, min5)

    # Three-Buy sub-level confirmation (三买次级别背驰确认)
    # Check 30min 3B signals against 5min divergences/buy signals
    if min30 and min5:
        syn.three_buy_confirmations = _confirm_3b_sub_level(min30, min5, daily)
    # Also check daily 3B signals against 30min divergences/buy signals
    if min30:
        daily_3b_confs = _confirm_3b_sub_level(daily, min30, daily)
        syn.three_buy_confirmations = (
            syn.three_buy_confirmations + daily_3b_confs
        )
    # Check weekly 3B signals against daily divergences/buy signals
    if weekly and daily:
        weekly_3b_confs = _confirm_3b_sub_level(weekly, daily, weekly)
        syn.three_buy_confirmations = (
            syn.three_buy_confirmations + weekly_3b_confs
        )

    syn.overall_bias, syn.action_advice = _determine_overall_bias(
        syn.level_summary, syn.direction_alignment,
    )

    parts = []
    parts.append(f"方向判断：{syn.direction_alignment}")
    parts.append(f"整体倾向：{syn.overall_bias}")
    parts.append(f"操作建议：{syn.action_advice}")
    if syn.resonance_signals:
        parts.append(f"发现 {len(syn.resonance_signals)} 个跨级别共振信号")
    if syn.interval_nests:
        deep = [n for n in syn.interval_nests if n.depth >= 2]
        parts.append(f"区间套：{len(syn.interval_nests)} 个背驰段，"
                     f"{len(deep)} 个完成嵌套定位")
    if syn.three_buy_confirmations:
        confirmed = [c for c in syn.three_buy_confirmations if c.confirmed]
        pending = [c for c in syn.three_buy_confirmations if not c.confirmed]
        if confirmed:
            parts.append(f"三买确认：{len(confirmed)} 个已确认")
        if pending:
            parts.append(f"三买待确认：{len(pending)} 个")
    syn.summary = "；".join(parts)

    return syn


# ════════════════════════════════════════════════════════════════════
# 13. Full Analysis Pipeline
# ════════════════════════════════════════════════════════════════════

def analyze(bars: list[RawBar], level: str = "daily") -> AnalysisResult:
    """Run the complete Chanlun analysis pipeline on a list of bars.

    Pipeline:
      [1] MACD → [2] Inclusion → [3] Fractals → [4] Strokes →
      [5] Stroke MACD → [6] Segments → [7] Hubs → [8] Trend →
      [9] Divergence → [10] Buy/Sell Points
    """
    result = AnalysisResult(level=level, raw_bars=bars)

    if len(bars) < 10:
        return result

    # [1] MACD
    compute_macd(bars)

    # [2] Inclusion processing
    merged = inclusion_processing(bars)
    result.merged_bars = merged

    # [3] Fractal identification
    fractals = find_fractals(merged)
    result.fractals = fractals

    # [4] Stroke construction
    strokes = find_strokes(fractals, merged)
    result.strokes = strokes

    if len(strokes) < 3:
        result.trend = determine_trend([], strokes)
        return result

    # [5] Stroke MACD areas
    compute_stroke_macd(strokes, bars, merged)
    mark_stroke_divergence(strokes)

    # [6] Segment construction
    segments = find_segments(strokes)
    result.segments = segments

    # [7] Hub construction (stroke-level / 笔中枢)
    hubs = find_hubs(strokes)
    result.hubs = hubs

    # [7b] Hub evolution classification (includes level assignment)
    classify_hub_evolution(hubs, analysis_level=level)

    # [7b1] Hub duration annotation (P2)
    for h in hubs:
        if h.strokes:
            start_dt = h.strokes[0].start.dt
            end_dt = h.strokes[-1].end.dt
            start_idx = next((i for i, b in enumerate(bars) if b.dt >= start_dt), 0)
            end_idx = next((i for i, b in enumerate(bars) if b.dt >= end_dt), len(bars) - 1)
            h.duration_bars = max(1, end_idx - start_idx + 1)

    # [7b2] Hub volume metrics
    for h in hubs:
        if h.strokes:
            vols = [s.avg_volume for s in h.strokes if s.avg_volume > 0]
            if vols:
                h.avg_volume = sum(vols) / len(vols)
                if len(vols) >= 4:
                    mid = len(vols) // 2
                    first_avg = sum(vols[:mid]) / mid
                    second_avg = sum(vols[mid:]) / (len(vols) - mid)
                    if first_avg > 0:
                        ratio = second_avg / first_avg
                        if ratio < 0.7:
                            h.volume_trend = "shrink"
                        elif ratio > 1.3:
                            h.volume_trend = "expand"
                        else:
                            h.volume_trend = "flat"
                    else:
                        h.volume_trend = "flat"
                else:
                    h.volume_trend = "flat"

    # [7c] Merge expanded/expanded hubs for trend determination
    result.merged_hubs = merge_expanded_hubs(hubs, analysis_level=level)

    # [7d] Hub direction and sequence assignment
    assign_hub_direction_and_sequence(hubs)
    assign_hub_direction_and_sequence(result.merged_hubs)

    # [7e] Segment-level hubs (线段中枢)
    seg_hubs = find_seg_hubs(segments)
    classify_seg_hub_evolution(seg_hubs, analysis_level=level)
    assign_seg_hub_direction_and_sequence(seg_hubs)
    result.seg_hubs = seg_hubs

    # [7f] Merge expanded/expanded segment hubs
    result.merged_seg_hubs = merge_expanded_seg_hubs(seg_hubs, analysis_level=level)

    # [8] Trend determination (uses merged hubs to avoid
    #     "扩展" being misclassified as "趋势")
    result.trend = determine_trend(result.merged_hubs, strokes)

    # [8b] Hub position annotation
    if bars:
        result.position_vs_hub, result.hub_position_detail = \
            _compute_position_vs_hub(bars[-1].close, hubs, [])

    # [9] Divergence detection — use merged hubs for trend divergence
    #     so that expanded hubs are already combined and the result is
    #     consistent with determine_trend().
    trend_divs = check_trend_divergence(strokes, result.merged_hubs, bars=bars)
    consol_divs = check_consolidation_divergence(strokes, hubs)
    result.divergences = trend_divs + consol_divs

    # [10] Buy/sell points — merged hubs for T1/T2 consistency, raw hubs
    #     for T3 (3B/3S) so individual hub boundaries are available.
    result.buy_sell_points = find_buy_sell_points(
        result.merged_hubs, strokes, bars, trend_divs, consol_divs, level,
        segments=segments,
        raw_hubs=hubs,
    )

    # [10b] Validate signals against subsequent price action
    _validate_signals(result.buy_sell_points, bars, strokes)

    for i, p in enumerate(result.buy_sell_points):
        p.idx = i

    # [10b2] Pending 3B detection (三买预备信号)
    result.pending_3b = find_pending_3b(result)

    # [10c] Segment-level buy/sell points (线段级别买卖点)
    result.seg_buy_sell_points = find_seg_buy_sell_points(
        seg_hubs, segments, bars, level,
    )

    # [11] Trend completion assessment (uses merged hubs for consistency)
    result.trend_completion = assess_trend_completion(
        result.trend, result.merged_hubs, strokes,
        result.divergences, result.buy_sell_points,
    )

    # [12] Advanced MACD diagnostics
    diag: dict = {}
    area_est = estimate_area_2x(strokes, result.merged_hubs)
    if area_est:
        diag["area_2x_estimates"] = area_est
    compute_ma(bars)
    ma_divs = compute_ma_area_divergence(strokes, result.merged_hubs, bars)
    if ma_divs:
        diag["ma_area_divergences"] = ma_divs
    alt_divs = compute_doubled_macd_area(strokes, result.merged_hubs, bars)
    if alt_divs:
        diag["doubled_macd_divergences"] = alt_divs
    dp_warnings = detect_double_pullback_zero(bars)
    if dp_warnings:
        diag["double_pullback_warnings"] = dp_warnings
    result.macd_diagnostics = diag

    # [13] Volume profile — market activity assessment
    if len(bars) >= 30:
        volumes = [b.volume for b in bars if b.volume > 0]
        if len(volumes) >= 30:
            recent_5 = volumes[-5:]
            ma20 = volumes[-20:]
            recent_avg = sum(recent_5) / len(recent_5)
            ma20_avg = sum(ma20) / len(ma20)
            ratio = recent_avg / ma20_avg if ma20_avg > 0 else 1.0
            if ratio > 1.5:
                activity = "active"
            elif ratio < 0.6:
                activity = "inactive"
            else:
                activity = "normal"
            # Volume trend: compare last 10 bars vs previous 10 bars
            last10 = sum(volumes[-10:]) / 10
            prev10 = sum(volumes[-20:-10]) / 10
            if prev10 > 0:
                trend_ratio = last10 / prev10
                if trend_ratio > 1.3:
                    vol_trend = "expanding"
                elif trend_ratio < 0.7:
                    vol_trend = "shrinking"
                else:
                    vol_trend = "flat"
            else:
                vol_trend = "flat"
            result.volume_profile = {
                "activity": activity,
                "recent_avg": round(recent_avg, 0),
                "ma20_avg": round(ma20_avg, 0),
                "ratio": round(ratio, 2),
                "trend": vol_trend,
            }

    return result


def analyze_from_csv(filepath: str, level: str = "daily") -> AnalysisResult:
    """Convenience: load CSV → analyze."""
    bars = load_bars_from_csv(filepath)
    return analyze(bars, level)


# ════════════════════════════════════════════════════════════════════
# 14. Text Report
# ════════════════════════════════════════════════════════════════════

def _format_div_dims(d: dict) -> str:
    """Format divergence dimension flags for report display."""
    parts = []
    if d.get("area_diverged"):
        parts.append("面积✓")
    if d.get("dif_diverged"):
        parts.append("DIF✓")
    else:
        parts.append("DIF✗")
    if d.get("hist_peak_diverged"):
        parts.append("柱峰✓")
    else:
        parts.append("柱峰✗")
    if d.get("vol_diverged"):
        parts.append("量能✓")
    return " [" + " ".join(parts) + "]" if parts else ""


def format_report(result: AnalysisResult) -> str:
    """Generate a human-readable text report from analysis result."""
    lines = []
    lines.append(f"# 缠论分析报告 — {result.level}")
    lines.append("")

    if not result.raw_bars:
        lines.append("无数据")
        return "\n".join(lines)

    first, last = result.raw_bars[0], result.raw_bars[-1]
    lines.append(f"数据区间：{first.dt} ~ {last.dt}（{len(result.raw_bars)} 根K线）")
    lines.append(f"最新价格：{last.close}")
    lines.append("")

    lines.append(f"## 处理统计")
    lines.append(f"- 合并K线：{len(result.merged_bars)}（包含处理后）")
    lines.append(f"- 分型：{len(result.fractals)}"
                 f"（顶 {sum(1 for f in result.fractals if f.type == 'top')}，"
                 f"底 {sum(1 for f in result.fractals if f.type == 'bottom')}）")
    lines.append(f"- 笔：{len(result.strokes)}"
                 f"（上 {sum(1 for s in result.strokes if s.direction == 1)}，"
                 f"下 {sum(1 for s in result.strokes if s.direction == -1)}）")
    lines.append(f"- 线段：{len(result.segments)}")
    lines.append(f"- 中枢：{len(result.hubs)}")
    lines.append("")

    # Line segment list (最近10条线段)
    if result.segments:
        lines.append(f"## 线段列表（最近10条）")
        recent_segs = result.segments[-10:]
        for seg in recent_segs:
            direction = "上涨" if seg.direction == 1 else "下跌"
            icon = "↗" if seg.direction == 1 else "↘"
            display_num = seg.idx + 1  # 1-indexed for display
            if seg.direction == 1:
                start_price = seg.strokes[0].start.low
                end_price = seg.strokes[-1].end.high
                change_pct = (end_price / start_price - 1) * 100
            else:
                start_price = seg.strokes[0].start.high
                end_price = seg.strokes[-1].end.low
                change_pct = (end_price / start_price - 1) * 100
            lines.append(
                f"- D{display_num}：{icon} {direction}  "
                f"{start_price:.2f} → {end_price:.2f} "
                f"({change_pct:+.1f}%)  "
                f"({seg.start_dt} ~ {seg.end_dt})"
            )
        lines.append("")

    lines.append(f"## 走势类型")
    lines.append(f"**{result.trend}**")
    tc = result.trend_completion
    if tc:
        status_icon = {"进行中": "🔄", "疑似完成": "⚠️", "已确认完成": "✅"}.get(tc["status"], "")
        lines.append(f"- 走势状态：{status_icon} **{tc['status']}** [{tc['confidence']}]")
        if tc.get("stage"):
            lines.append(f"- 阶段：{tc['stage']}")
        if tc.get("reason"):
            lines.append(f"- 判断依据：{tc['reason']}")
    lines.append("")

    if result.position_vs_hub:
        lines.append(f"## 中枢位置")
        d = result.hub_position_detail
        lines.append(f"**{result.position_vs_hub}**")
        sh = d.get("stroke_hub")
        if sh:
            lines.append(f"- 参考中枢{sh['idx']+1}：ZG={sh['zg']:.3f} "
                         f"ZD={sh['zd']:.3f}（{sh['start_dt']} ~ {sh['end_dt']}）")
            pos = d.get("stroke_position", "")
            pct = d.get("distance_pct", 0)
            if pos == "above":
                lines.append(f"- 当前价高于中枢上沿 {pct}%，持股等待卖点")
            elif pos == "below":
                lines.append(f"- 当前价低于中枢下沿 {pct}%，空仓等待买点")
            else:
                lines.append(f"- 中枢区间内位置 {pct}%，可高抛低吸做差价")
        lines.append("")

    if result.volume_profile:
        vp = result.volume_profile
        act_icon = {"active": "🔥", "normal": "➖", "inactive": "❄️"}.get(vp["activity"], "")
        act_label = {"active": "活跃", "normal": "正常", "inactive": "低迷"}.get(vp["activity"], "")
        trend_label = {"expanding": "放量", "shrinking": "缩量", "flat": "平稳"}.get(vp["trend"], "")
        lines.append(f"## 量能活跃度")
        lines.append(f"{act_icon} **{act_label}**（近5日/MA20 = {vp['ratio']:.2f}，趋势：{trend_label}）")
        lines.append("")

    if result.hubs:
        lines.append(f"## 中枢列表")
        evo_advice = {
            "延伸": "→ 高抛低吸做差价",
            "新生（上）": "→ 趋势上行，持仓",
            "新生（下）": "→ 趋势下行，回避",
            "扩展": "→ 升级为上级别中枢，按盘整处理",
        }
        for h in result.hubs:
            evo = f" 【{h.evolution_type}】" if h.evolution_type else ""
            advice = f" {evo_advice.get(h.evolution_type, '')}" if h.evolution_type else ""
            lines.append(f"- 中枢{h.idx + 1}：ZG={h.zg:.3f} ZD={h.zd:.3f} "
                         f"（{h.start_dt} ~ {h.end_dt}，{len(h.strokes)}笔）"
                         f"{evo}{advice}")

        has_expansion = any(h.evolution_type == "扩展" for h in result.hubs)
        if has_expansion and result.merged_hubs and len(result.merged_hubs) < len(result.hubs):
            lines.append("")
            lines.append(f"### 合并后中枢（扩展合并视角）")
            lines.append(f"> 扩展中枢已合并为上级别中枢，走势判定基于此视角")
            for mh in result.merged_hubs:
                lines.append(
                    f"- 合并中枢{mh.idx + 1}：ZG={mh.zg:.3f} ZD={mh.zd:.3f} "
                    f"GG={mh.gg:.3f} DD={mh.dd:.3f}"
                    f"（{mh.start_dt} ~ {mh.end_dt}，{len(mh.strokes)}笔）"
                )
        lines.append("")

    if result.divergences:
        lines.append(f"## 背驰信号")
        for d in result.divergences:
            dtype = "趋势背驰" if d["type"] == "trend" else "盘整背驰"
            ddir = "下跌" if d["direction"] == -1 else "上涨"
            conf = d.get("div_confidence", "?")
            dims = d.get("div_dims", 1)
            dim_str = _format_div_dims(d)
            lines.append(f"- [{dtype}] {ddir} @ {d['dt']}，"
                         f"面积比={d.get('ratio', 0):.2f}，"
                         f"把握度={conf}（{dims}/3维）{dim_str}")
        lines.append("")

    if result.buy_sell_points:
        lines.append(f"## 买卖点信号")
        for p in result.buy_sell_points:
            wolf = " ⚠防狼" if p.wolf_warning else ""
            zone = f" [{p.macd_zone}]" if p.macd_zone else ""
            strength_tag = ""
            if p.strength:
                _STR_MAP = {"strongest": "🔥最强", "strong": "💪强势", "standard": "📌标准", "weak": "⚠弱"}
                strength_tag = f" {_STR_MAP.get(p.strength, p.strength)}"
            lines.append(f"- **{p.label}** [{p.confidence}]{strength_tag}{wolf}{zone} @ {p.dt}，"
                         f"价格={p.price:.3f}")
            lines.append(f"  {p.description}")
            if p.position_advice:
                lines.append(f"  💰 仓位建议：{p.position_advice}")
            if p.wolf_warning:
                lines.append(f"  ⚠ {p.wolf_warning}")
        lines.append("")

    # MACD status
    if result.raw_bars:
        last_bar = result.raw_bars[-1]
        lines.append(f"## MACD状态")
        lines.append(f"- DIF：{last_bar.dif:.4f}")
        lines.append(f"- DEA：{last_bar.dea:.4f}")
        lines.append(f"- MACD柱：{last_bar.macd_hist:.4f}")
        dif_pos = "0轴上（多头）" if last_bar.dif > 0 else "0轴下（空头）"
        lines.append(f"- 位置：{dif_pos}")
        if last_bar.dif < 0:
            lines.append(f"- **防狼术**：DIF在0轴下方，空头主导，买入信号需额外验证")
        lines.append("")

    return "\n".join(lines)


def format_synthesis_report(syn: MultiLevelSynthesis) -> str:
    """Generate a human-readable multi-level synthesis report."""
    lines = []
    lines.append("# 多级别联立分析")
    lines.append("")

    lines.append("## 各级别概况")
    lines.append("| 级别 | 走势 | 中枢位置 | DIF | 中枢数 | 信号数 | 最新信号 |")
    lines.append("|------|---------|---------|---------|--------|--------|---------|")
    for s in syn.level_summary:
        sig_str = s["latest_signal"]["label"] if s["latest_signal"] else "-"
        lines.append(
            f"| {s['level']} | {s['trend']} | {s['hub_position'] or '-'} "
            f"| {s['dif_zone']} | {s['num_hubs']} | {s['num_signals']} | {sig_str} |"
        )
    lines.append("")

    lines.append(f"## 方向一致性：**{syn.direction_alignment}**")
    lines.append(f"## 整体倾向：**{syn.overall_bias}**")
    lines.append(f"## 操作建议：{syn.action_advice}")
    lines.append("")

    if syn.resonance_signals:
        lines.append(f"## 跨级别共振（{len(syn.resonance_signals)} 个）")
        for r in syn.resonance_signals:
            sigs = " + ".join(
                f"{s['level']}-{s['label']}({s['confidence']})"
                for s in r["signals"]
            )
            lines.append(f"- **{r['date']}** [{r['direction']}] {sigs}")
            lines.append(f"  {r['note']}")
        lines.append("")

    if syn.enriched_signals:
        lines.append(f"## 上级别增强信号（{len(syn.enriched_signals)} 个）")
        changed = [s for s in syn.enriched_signals if s["confidence_changed"]]
        if changed:
            lines.append("")
            lines.append("置信度调整：")
            for s in changed:
                arrow = "↑" if _conf_rank(s["adjusted_confidence"]) > _conf_rank(s["original_confidence"]) else "↓"
                lines.append(
                    f"- {s['source_level']}-{s['label']} @ {s['dt']}："
                    f" {s['original_confidence']}{arrow}{s['adjusted_confidence']}"
                    f"（{s['context_note']}）"
                )
        lines.append("")

    if syn.interval_nests:
        deep_nests = [n for n in syn.interval_nests if n.depth >= 2]
        lines.append(f"## 区间套精确定位（{len(syn.interval_nests)} 个背驰段，"
                     f"{len(deep_nests)} 个完成嵌套）")
        lines.append("")
        for i, n in enumerate(syn.interval_nests, 1):
            dir_label = "▼下跌背驰→买入" if n.big_direction == -1 else "▲上涨背驰→卖出"
            type_label = "趋势" if n.big_signal_type == "trend" else "盘整"
            depth_stars = "★" * n.depth
            lines.append(f"### {i}. {depth_stars} {type_label}{dir_label}")
            lines.append("")
            lines.append(f"| 层级 | 级别 | 信号时间 | 时间范围 |")
            lines.append(f"|------|------|---------|---------|")
            lines.append(
                f"| **大** | {n.big_level} | {n.big_signal_dt} "
                f"| {n.big_time_range[0]} ~ {n.big_time_range[1]} |"
            )
            if n.mid_level:
                mid_range = (f"{n.mid_time_range[0]} ~ {n.mid_time_range[1]}"
                             if n.mid_time_range[0] else "-")
                lines.append(
                    f"| **中** | {n.mid_level} | {n.mid_signal_dt} | {mid_range} |"
                )
            if n.small_level:
                lines.append(
                    f"| **小** | {n.small_level} | {n.small_signal_dt} | - |"
                )
            lines.append("")
            lines.append(
                f"- **精确定位**：{n.precision_dt}，"
                f"价格 {n.precision_price:.2f}"
                if n.precision_price else
                f"- **精确定位**：{n.precision_dt}"
            )
            lines.append(f"- {n.note}")
            lines.append("")
        lines.append("")

    if syn.three_buy_confirmations:
        confirmed = [c for c in syn.three_buy_confirmations if c.confirmed]
        pending = [c for c in syn.three_buy_confirmations if not c.confirmed]

        lines.append(f"## 三买次级别确认（{len(confirmed)} 已确认 / "
                     f"{len(pending)} 待确认）")
        lines.append("")
        lines.append("> 理论：30分钟三买的回抽段是5分钟下跌走势，"
                     "5分钟背驰/一买 = 30分钟三买精确入场点")
        lines.append("")

        if confirmed:
            lines.append("### ✅ 已确认（次级别背驰/买点已出现）")
            lines.append("")
            lines.append("| 级别 | 三买时间 | 价格 | 强度 | 确认类型 | "
                         "确认时间 | 确认价格 | 日线环境 |")
            lines.append("|------|---------|------|------|---------|"
                         "---------|---------|---------|")
            for c in confirmed:
                lines.append(
                    f"| {c.source_level} | {c.source_3b_dt} "
                    f"| {c.source_3b_price:.3f} | {c.source_3b_strength} "
                    f"| {c.sub_level}{c.confirmation_type} "
                    f"| {c.confirmation_dt} | {c.confirmation_price:.3f} "
                    f"| {c.daily_env} |"
                )
            lines.append("")
            for c in confirmed:
                lines.append(f"- **{c.source_level}三买** @ {c.source_3b_dt}：{c.note}")
            lines.append("")

        if pending:
            lines.append("### ⏳ 待确认（回抽段内未见次级别背驰）")
            lines.append("")
            for c in pending:
                lines.append(
                    f"- {c.source_level}三买 @ {c.source_3b_dt}"
                    f"（{c.source_3b_strength}）— {c.note}"
                )
            lines.append("")

    return "\n".join(lines)


def _conf_rank(conf: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(conf, -1)


# ════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ════════════════════════════════════════════════════════════════════

def main():
    """Run analysis on a single CSV file."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Chanlun Analysis Engine v2")
    parser.add_argument("csv_file", help="Path to K-line CSV file")
    parser.add_argument("--level", default="daily",
                        choices=["weekly", "daily", "30min", "5min"],
                        help="Analysis level (default: daily)")
    args = parser.parse_args()

    if not os.path.exists(args.csv_file):
        print(f"File not found: {args.csv_file}", file=sys.stderr)
        sys.exit(1)

    result = analyze_from_csv(args.csv_file, args.level)
    print(format_report(result))


if __name__ == "__main__":
    main()
