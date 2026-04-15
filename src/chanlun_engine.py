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
    """A hub/pivot (中枢)."""
    idx: int
    zg: float             # upper bound (min of highs)
    zd: float             # lower bound (max of lows)
    gg: float             # highest price in hub range
    dd: float             # lowest price in hub range
    strokes: list[Stroke] = field(default_factory=list)
    evolution_type: str = ""   # "延伸"/"新生（上）"/"新生（下）"/"扩展"/""

    @property
    def start_dt(self) -> str:
        return self.strokes[0].start.dt if self.strokes else ""

    @property
    def end_dt(self) -> str:
        return self.strokes[-1].end.dt if self.strokes else ""


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
    strength: str = ""      # "strongest"(二买三买合一) / "strong"(二买高于一买) / "standard"(标准二买)
    position_advice: str = ""  # position sizing advice (e.g. "轻仓试探1/3", "满仓")
    idx: int = -1  # sequential index, assigned in analyze()
    invalidation_price: float = 0.0  # price that invalidates this signal
    status: str = "active"  # "active" / "invalidated"
    invalidation_reason: str = ""  # reason for invalidation


@dataclass
class SegHub:
    """A hub built from segments (线段中枢), one level above stroke hubs."""
    idx: int
    zg: float
    zd: float
    gg: float
    dd: float
    segments: list[Segment] = field(default_factory=list)
    evolution_type: str = ""

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
    trend: str = ""
    merged_hubs: list[Hub] = field(default_factory=list)
    divergences: list[dict] = field(default_factory=list)
    buy_sell_points: list[BuySellPoint] = field(default_factory=list)
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
                last.high = max(last.high, b.high)
                last.low = max(last.low, b.low)
            else:
                last.high = min(last.high, b.high)
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
                dt=curr.dates[0],
            ))
        elif (curr.low < prev.low and curr.low < nxt.low and
              curr.high < prev.high and curr.high < nxt.high):
            fractals.append(Fractal(
                type="bottom", mk_idx=i,
                high=curr.high, low=curr.low,
                dt=curr.dates[0],
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
    return strokes


# ════════════════════════════════════════════════════════════════════
# 7. MACD Area for Strokes
# ════════════════════════════════════════════════════════════════════

def compute_stroke_macd(strokes: list[Stroke], bars: list[RawBar],
                        merged: list[MergedBar]):
    """Compute MACD metrics for each stroke.

    For each stroke, computes:
      - macd_area:   histogram area (same-direction; fallback to absolute)
      - dif_extreme: max DIF for up-strokes, min DIF for down-strokes
      - hist_peak:   maximum |histogram| bar value within the stroke
    """
    dt_to_idx = {b.dt: b.idx for b in bars}

    for s in strokes:
        si = dt_to_idx.get(s.start.dt, 0)
        ei = dt_to_idx.get(s.end.dt, len(bars) - 1)
        dir_area = 0.0
        abs_area = 0.0
        peak_hist = 0.0
        dif_vals = []
        for ki in range(si, min(ei + 1, len(bars))):
            v = bars[ki].macd_hist
            abs_area += abs(v)
            if abs(v) > peak_hist:
                peak_hist = abs(v)
            dif_vals.append(bars[ki].dif)
            if (s.direction == 1 and v > 0) or (s.direction == -1 and v < 0):
                dir_area += abs(v)
        s.macd_area = dir_area if dir_area > 0 else abs_area
        s.hist_peak = peak_hist
        if dif_vals:
            s.dif_extreme = max(dif_vals) if s.direction == 1 else min(dif_vals)
        else:
            s.dif_extreme = 0.0


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
# Rules (108课 §1.4, 图解缠论 §一):
#   - Hub = price overlap zone of at least 3 consecutive strokes
#   - ZG = min(highs of 3 strokes), ZD = max(lows of 3 strokes)
#   - Valid when ZD < ZG
#   - Extension: subsequent strokes crossing ZD~ZG extend the hub
#   - GG = highest price, DD = lowest price in hub range
# ════════════════════════════════════════════════════════════════════

def _stroke_range(s: Stroke) -> tuple[float, float]:
    """Get (high, low) price range of a stroke."""
    return max(s.start.high, s.end.high), min(s.start.low, s.end.low)


def find_hubs(strokes: list[Stroke]) -> list[Hub]:
    """Build hubs from stroke sequence."""
    if len(strokes) < 3:
        return []

    hubs = []
    i = 0

    while i < len(strokes) - 2:
        ranges = [_stroke_range(strokes[k]) for k in range(i, i + 3)]
        zg = min(r[0] for r in ranges)
        zd = max(r[1] for r in ranges)

        if zd >= zg:
            i += 1
            continue

        hub_strokes = list(strokes[i:i + 3])
        j = i + 3

        while j < len(strokes):
            sj_h, sj_l = _stroke_range(strokes[j])
            if sj_l < zg and sj_h > zd:
                hub_strokes.append(strokes[j])
                j += 1
            else:
                break

        gg = max(_stroke_range(s)[0] for s in hub_strokes)
        dd = min(_stroke_range(s)[1] for s in hub_strokes)

        hubs.append(Hub(
            idx=len(hubs), zg=zg, zd=zd, gg=gg, dd=dd,
            strokes=hub_strokes,
        ))
        i = j

    return hubs


def _segment_range(seg: Segment) -> tuple[float, float]:
    """Get (high, low) price range of a segment."""
    h = max(max(s.start.high, s.end.high) for s in seg.strokes)
    l = min(min(s.start.low, s.end.low) for s in seg.strokes)
    return h, l


def find_seg_hubs(segments: list[Segment]) -> list[SegHub]:
    """Build hubs from segment sequence (线段中枢).

    Same overlap logic as stroke hubs, but one level up:
    3 consecutive segments with price overlap form a segment-level hub.
    """
    if len(segments) < 3:
        return []

    hubs = []
    i = 0

    while i < len(segments) - 2:
        ranges = [_segment_range(segments[k]) for k in range(i, i + 3)]
        zg = min(r[0] for r in ranges)
        zd = max(r[1] for r in ranges)

        if zd >= zg:
            i += 1
            continue

        hub_segs = list(segments[i:i + 3])
        j = i + 3

        while j < len(segments):
            sj_h, sj_l = _segment_range(segments[j])
            if sj_l < zg and sj_h > zd:
                hub_segs.append(segments[j])
                j += 1
            else:
                break

        gg = max(_segment_range(s)[0] for s in hub_segs)
        dd = min(_segment_range(s)[1] for s in hub_segs)

        hubs.append(SegHub(
            idx=len(hubs), zg=zg, zd=zd, gg=gg, dd=dd,
            segments=hub_segs,
        ))
        i = j

    return hubs


# ════════════════════════════════════════════════════════════════════
# 9b. Hub Evolution Classification (中枢演化分类)
#
# Theory (108课 §1.4 / 缠论解析 场景B / 图解缠论2 §2.2-2.4):
#   - 延伸: hub oscillation beyond initial 3 strokes (swing-trade zone)
#   - 新生: adjacent hubs with non-overlapping [ZD,ZG] → trend continuation
#   - 扩展: adjacent hubs with overlapping [DD,GG] → upgrade to larger hub
# ════════════════════════════════════════════════════════════════════

_EXTENSION_THRESHOLD = 5  # strokes needed to classify as "延伸"


def classify_hub_evolution(hubs: list[Hub]):
    """Classify each hub's evolution type in-place.

    For a single hub: extended oscillation if strokes > threshold.
    For adjacent pairs: new birth vs expansion based on range overlap.
    """
    if not hubs:
        return

    for h in hubs:
        if len(h.strokes) >= _EXTENSION_THRESHOLD:
            h.evolution_type = "延伸"

    for i in range(1, len(hubs)):
        prev, curr = hubs[i - 1], hubs[i]

        # Use CORE range [ZD, ZG] for overlap — NOT oscillation [DD, GG].
        # Chan Theory defines a trend as two hubs whose cores don't overlap.
        # DD/GG includes extreme stroke excursions beyond the hub core, so
        # using DD/GG would misclassify many valid trends as expansion.
        core_overlap = curr.zd <= prev.zg and curr.zg >= prev.zd

        if core_overlap:
            curr.evolution_type = "扩展"
        else:
            if curr.zd > prev.zg:
                curr.evolution_type = "新生（上）"
            else:
                curr.evolution_type = "新生（下）"


def classify_seg_hub_evolution(seg_hubs: list[SegHub]):
    """Same logic for segment-level hubs."""
    if not seg_hubs:
        return

    for h in seg_hubs:
        if len(h.segments) >= _EXTENSION_THRESHOLD:
            h.evolution_type = "延伸"

    for i in range(1, len(seg_hubs)):
        prev, curr = seg_hubs[i - 1], seg_hubs[i]
        osc_overlap = curr.dd <= prev.gg and curr.gg >= prev.dd
        core_overlap = curr.zd <= prev.zg and curr.zg >= prev.zd

        if osc_overlap and not core_overlap:
            curr.evolution_type = "扩展"
        elif not osc_overlap:
            if curr.zg > prev.zg:
                curr.evolution_type = "新生（上）"
            else:
                curr.evolution_type = "新生（下）"
        elif core_overlap:
            curr.evolution_type = "扩展"


# ════════════════════════════════════════════════════════════════════
# 9c. Hub Merge After Expansion (扩展中枢合并)
#
# When adjacent hubs are classified as "扩展", they should be merged
# into a single larger-level hub for trend determination purposes.
# (108课 §1.4 / 图解缠论2 §2.4 / 缠论解析 场景B)
# ════════════════════════════════════════════════════════════════════

def merge_expanded_hubs(hubs: list[Hub]) -> list[Hub]:
    """Merge adjacent hubs where the later one is classified as expansion.

    Returns a new list where expanded hubs are combined into larger hubs.
    Original hub list is not modified.
    """
    if len(hubs) <= 1:
        return list(hubs)

    merged: list[Hub] = []
    for h in hubs:
        if merged and h.evolution_type == "扩展":
            prev = merged[-1]
            all_strokes = prev.strokes + h.strokes
            if len(all_strokes) >= 3:
                highs = [max(s.start.high, s.end.high) for s in all_strokes]
                lows = [min(s.start.low, s.end.low) for s in all_strokes]
                new_zg = min(highs[:3])
                new_zd = max(lows[:3])
            else:
                new_zg = min(prev.zg, h.zg)
                new_zd = max(prev.zd, h.zd)
            merged[-1] = Hub(
                idx=prev.idx,
                zg=new_zg,
                zd=new_zd,
                gg=max(prev.gg, h.gg),
                dd=min(prev.dd, h.dd),
                strokes=all_strokes,
                evolution_type="延伸",
            )
        else:
            merged.append(Hub(
                idx=len(merged),
                zg=h.zg, zd=h.zd, gg=h.gg, dd=h.dd,
                strokes=list(h.strokes),
                evolution_type=h.evolution_type,
            ))

    for i, h in enumerate(merged):
        h.idx = i
    return merged


def merge_expanded_seg_hubs(seg_hubs: list[SegHub]) -> list[SegHub]:
    """Same merge logic for segment-level hubs."""
    if len(seg_hubs) <= 1:
        return list(seg_hubs)

    merged: list[SegHub] = []
    for h in seg_hubs:
        if merged and h.evolution_type == "扩展":
            prev = merged[-1]
            all_segs = prev.segments + h.segments
            merged[-1] = SegHub(
                idx=prev.idx,
                zg=min(prev.zg, h.zg),
                zd=max(prev.zd, h.zd),
                gg=max(prev.gg, h.gg),
                dd=min(prev.dd, h.dd),
                segments=all_segs,
                evolution_type="延伸",
            )
        else:
            merged.append(SegHub(
                idx=len(merged),
                zg=h.zg, zd=h.zd, gg=h.gg, dd=h.dd,
                segments=list(h.segments),
                evolution_type=h.evolution_type,
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
    """Determine current trend type."""
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

    h_prev, h_last = hubs[-2], hubs[-1]
    if h_last.zd > h_prev.zg:
        return "上涨趋势"
    elif h_last.zg < h_prev.zd:
        return "下跌趋势"
    return "盘整"


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

    elif trend == "盘整":
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

def check_trend_divergence(strokes: list[Stroke], hubs: list[Hub]) -> list[dict]:
    """Detect trend divergence (趋势背驰).

    Groups consecutive same-direction hub shifts, then compares the
    segment before the first hub (a) with segment after the last hub (c).
    """
    divergences = []
    if len(hubs) < 2:
        return divergences

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

        # Limit a-segment to strokes between the previous hub and the
        # first hub of this trend group (not ALL historical strokes).
        a_start_idx = hubs[i - 1].strokes[-1].idx if i > 0 else 0
        seg_a = [s for s in strokes
                 if a_start_idx <= s.idx < first_hub.strokes[0].idx
                 and s.direction == trend_dir]

        # Limit c-segment to strokes between the last hub and the next
        # hub (or end of data).
        c_end_idx = hubs[j + 1].strokes[0].idx if j + 1 < len(hubs) else strokes[-1].idx + 1
        seg_c = [s for s in strokes
                 if s.idx > last_hub.strokes[-1].idx
                 and s.idx < c_end_idx
                 and s.direction == trend_dir]

        if seg_a and seg_c:
            # c-segment must extend beyond last hub to confirm trend continuation.
            # Downtrend: c's low must break below last_hub.zd;
            # Uptrend: c's high must break above last_hub.zg.
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

            a_area = sum(s.macd_area for s in seg_a)
            c_area = sum(s.macd_area for s in seg_c)
            if a_area > 0 and c_area < a_area:
                area_diverged = True
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

                dims = sum([area_diverged, dif_diverged, hist_peak_diverged])
                div_confidence = "high" if dims == 3 else ("medium" if dims >= 2 else "low")

                trigger = seg_c[-1]
                structure = [
                    {"tag": "a", "start_dt": seg_a[0].start.dt,
                     "end_dt": seg_a[-1].end.dt},
                    {"tag": "A", "start_dt": first_hub.strokes[0].start.dt,
                     "end_dt": first_hub.strokes[-1].end.dt,
                     "zg": first_hub.zg, "zd": first_hub.zd},
                ]
                if last_hub.idx != first_hub.idx:
                    b_start = first_hub.strokes[-1].end.dt
                    b_end = last_hub.strokes[0].start.dt
                    structure.append(
                        {"tag": "b", "start_dt": b_start, "end_dt": b_end})
                    structure.append(
                        {"tag": "B", "start_dt": last_hub.strokes[0].start.dt,
                         "end_dt": last_hub.strokes[-1].end.dt,
                         "zg": last_hub.zg, "zd": last_hub.zd})
                structure.append(
                    {"tag": "c", "start_dt": seg_c[0].start.dt,
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
                    "hub_idx": last_hub.idx,
                    "price": trigger.end.low if trend_dir == -1 else trigger.end.high,
                    "a_start_dt": seg_a[0].start.dt,
                    "a_end_dt": seg_a[-1].end.dt,
                    "a_stroke_range": (seg_a[0].idx, seg_a[-1].idx),
                    "c_start_dt": seg_c[0].start.dt,
                    "c_end_dt": seg_c[-1].end.dt,
                    "c_stroke_range": (seg_c[0].idx, seg_c[-1].idx),
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
             if s.direction == 1 and max(s.start.high, s.end.high) > hub.zg],
            key=lambda s: s.idx,
        )
        exit_down = sorted(
            [s for s in relevant
             if s.direction == -1 and min(s.start.low, s.end.low) < hub.zd],
            key=lambda s: s.idx,
        )

        for exits in [exit_up, exit_down]:
            for k in range(1, len(exits)):
                prev_e, curr_e = exits[k - 1], exits[k]
                if prev_e.macd_area > 0 and curr_e.macd_area < prev_e.macd_area:
                    area_diverged = True
                    # DIF extreme comparison
                    if curr_e.direction == -1:
                        dif_diverged = curr_e.dif_extreme > prev_e.dif_extreme
                    else:
                        dif_diverged = curr_e.dif_extreme < prev_e.dif_extreme
                    # Histogram peak comparison
                    hist_peak_diverged = curr_e.hist_peak < prev_e.hist_peak

                    dims = sum([area_diverged, dif_diverged, hist_peak_diverged])
                    div_confidence = "high" if dims == 3 else ("medium" if dims >= 2 else "low")

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
        seg_a = [s for s in strokes
                 if s.idx < first_hub.strokes[0].idx and s.direction == trend_dir]
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

def find_buy_sell_points(
    hubs: list[Hub],
    strokes: list[Stroke],
    bars: list[RawBar],
    trend_divs: list[dict],
    consol_divs: list[dict],
    level: str,
    segments: list[Segment] | None = None,
) -> list[BuySellPoint]:
    """Identify all three types of buy/sell points."""
    stroke_to_seg: dict[int, int] = {}
    if segments:
        for seg in segments:
            for s in seg.strokes:
                stroke_to_seg[s.idx] = seg.idx

    def _stroke_seg(stroke: Stroke | None) -> tuple[int, int]:
        """Return (stroke_idx, seg_idx) for a given stroke."""
        if stroke is None:
            return (-1, -1)
        return (stroke.idx, stroke_to_seg.get(stroke.idx, -1))

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
        a_tag = f"a(S{a_range[0]}-S{a_range[1]})" if a_range[0] != a_range[1] else f"a(S{a_range[0]})"
        c_tag = f"c(S{c_range[0]}-S{c_range[1]})" if c_range[0] != c_range[1] else f"c(S{c_range[0]})"
        ranges = [
            {"label": a_tag, "start_dt": div["a_start_dt"],
             "end_dt": div["a_end_dt"], "area": div["a_area"]},
            {"label": c_tag, "start_dt": div["c_start_dt"],
             "end_dt": div["c_end_dt"], "area": div["c_area"]},
        ]
        struct = div.get("structure", [])
        dims = div.get("div_dims", 1)
        dim_tag = f" ({dims}/3维)" if dims > 0 else ""
        conf = div.get("div_confidence", "medium")
        if div["direction"] == -1:
            points.append(BuySellPoint(
                type="1B", label="一买",
                dt=div["dt"], price=div["price"],
                description=(
                    f"[{loc}] 下跌趋势背驰{dim_tag}：c段面积({div['c_area']}) < a段({div['a_area']})，"
                    f"比值={div['ratio']:.2f}"
                ),
                level=level,
                confidence=conf,
                hub_idx=div["hub_idx"],
                stroke_idx=s_idx, seg_idx=d_idx,
                area_ranges=ranges,
                structure=struct,
            ))
        else:
            points.append(BuySellPoint(
                type="1S", label="一卖",
                dt=div["dt"], price=div["price"],
                description=(
                    f"[{loc}] 上涨趋势背驰{dim_tag}：c段面积({div['c_area']}) < a段({div['a_area']})，"
                    f"比值={div['ratio']:.2f}"
                ),
                level=level,
                confidence=conf,
                hub_idx=div["hub_idx"],
                stroke_idx=s_idx, seg_idx=d_idx,
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
        ranges = [
            {"label": f"S{prev_si}", "start_dt": div["prev_start_dt"],
             "end_dt": div["prev_end_dt"], "area": div["prev_area"]},
            {"label": f"S{curr_si}", "start_dt": div["curr_start_dt"],
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
        conf = div.get("div_confidence", "low")
        if div["direction"] == -1:
            points.append(BuySellPoint(
                type="PB", label="盘整买点",
                dt=div["dt"], price=div["price"],
                description=(
                    f"[{loc}] 盘整背驰{dim_tag}：当次面积({div['curr_area']}) < 前次({div['prev_area']})，"
                    f"比值={div['ratio']:.2f}"
                ),
                level=level,
                confidence=conf,
                hub_idx=div["hub_idx"],
                stroke_idx=s_idx, seg_idx=d_idx,
                area_ranges=ranges,
                structure=struct,
            ))
        else:
            points.append(BuySellPoint(
                type="PS", label="盘整卖点",
                dt=div["dt"], price=div["price"],
                description=(
                    f"[{loc}] 盘整背驰{dim_tag}：当次面积({div['curr_area']}) < 前次({div['prev_area']})，"
                    f"比值={div['ratio']:.2f}"
                ),
                level=level,
                confidence=conf,
                hub_idx=div["hub_idx"],
                stroke_idx=s_idx, seg_idx=d_idx,
                area_ranges=ranges,
                structure=struct,
            ))

    # ── Type 2: First pullback after Type 1 (二买/二卖) ──
    # Per 108课 §2.4, three strength levels:
    #   strongest: 2B+3B merged — pullback stays above the hub ZG (二买三买合一)
    #   strong: pullback low > 1B price (回调幅度小)
    #   standard: pullback low <= 1B price but doesn't break (标准二买)
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
            if ref_hub and pb_low > ref_hub.zg:
                strength = "strongest"
                strength_label = "最强（二买三买合一）"
                conf = "high"
            elif pb_low > t1.price:
                strength = "strong"
                strength_label = "强势（高于一买）"
                conf = "high" if pb_low > t1.price * 1.02 else "medium"
            else:
                strength = "standard"
                strength_label = "标准"
                conf = "medium"

            points.append(BuySellPoint(
                type="2B", label="二买",
                dt=first_pullback.end.dt, price=pb_low,
                description=(
                    f"[{loc}] 一买(S{t1.stroke_idx})后回调低点({pb_low:.3f})"
                    f"不破一买价({t1.price:.3f})，{strength_label}"
                ),
                level=level,
                confidence=conf,
                hub_idx=t1.hub_idx,
                stroke_idx=s_idx, seg_idx=d_idx,
                strength=strength,
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
            if ref_hub and rl_high < ref_hub.zd:
                strength = "strongest"
                strength_label = "最强（二卖三卖合一）"
                conf = "high"
            elif rl_high < t1.price:
                strength = "strong"
                strength_label = "强势（低于一卖）"
                conf = "high" if rl_high < t1.price * 0.98 else "medium"
            else:
                strength = "standard"
                strength_label = "标准"
                conf = "medium"

            points.append(BuySellPoint(
                type="2S", label="二卖",
                dt=first_rally.end.dt, price=rl_high,
                description=(
                    f"[{loc}] 一卖(S{t1.stroke_idx})后反弹高点({rl_high:.3f})"
                    f"不破一卖价({t1.price:.3f})，{strength_label}"
                ),
                level=level,
                confidence=conf,
                hub_idx=t1.hub_idx,
                stroke_idx=s_idx, seg_idx=d_idx,
                strength=strength,
                invalidation_price=t1.price,
            ))

    # ── Type 3: Hub breakout + pullback (三买/三卖) ──
    for hub in hubs:
        hub_end_idx = hub.strokes[-1].idx

        # 3B: up breakout above ZG, pullback stays above ZG
        _check_type3_buy(hub, strokes, hub_end_idx, points, level, stroke_to_seg)
        # 3S: down breakout below ZD, rally stays below ZD
        _check_type3_sell(hub, strokes, hub_end_idx, points, level, stroke_to_seg)

    points.sort(key=lambda p: p.dt)

    # ── Deduplication (去重) ──
    points = _dedup_signals(points)

    # ── Position Sizing Advice (仓位建议) ──
    _apply_position_advice(points)

    # ── Wolf Prevention Filter (防狼术) ──
    _apply_wolf_filter(points, bars)

    return points


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

        # Find the first bar after the signal
        bar_start = None
        for i, dt in enumerate(bar_dts):
            if dt > sig_dt:
                bar_start = i
                break

        # --- Invalidation check (bar-based) ---
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

        if invalidated:
            continue

        # --- Confirmation check (stroke-based) ---
        if not strokes:
            p.status = "pending"
            continue

        confirmed = False
        if p.type in _BUY_TYPES:
            for s in strokes:
                if s.start.dt <= sig_dt:
                    continue
                if s.direction == 1 and s.end.high > p.price:
                    confirmed = True
                    break
        elif p.type in _SELL_TYPES:
            for s in strokes:
                if s.start.dt <= sig_dt:
                    continue
                if s.direction == -1 and s.end.low < p.price:
                    confirmed = True
                    break

        p.status = "confirmed" if confirmed else "pending"


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
    Also adjusts for 2B strength:
      strongest (二买三买合一) → directly to full position
    """
    for p in points:
        base = _POSITION_ADVICE.get(p.type)
        if not base:
            continue

        advice, reason = base

        if p.type == "2B" and p.strength == "strongest":
            advice = "满仓（二买三买合一）"
            reason = "最强二买，回抽不进中枢，常对应大行情"
        elif p.type == "2B" and p.strength == "strong":
            advice = "加至标准仓位 2/3（强势二买）"
            reason = "二买高于一买，回调幅度小，信心较高"
        elif p.type == "2S" and p.strength == "strongest":
            advice = "必须清仓（二卖三卖合一）"
            reason = "最强二卖，反弹不进中枢，下跌可能加速"

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
                     stroke_to_seg: dict[int, int] | None = None):
    """Check for Type 3 buy point after hub."""
    stm = stroke_to_seg or {}
    last_stroke = hub.strokes[-1]

    def _make_3b(pullback):
        s_idx = pullback.idx
        d_idx = stm.get(s_idx, -1)
        loc = f"S{s_idx}" + (f"/D{d_idx}" if d_idx >= 0 else "")
        return BuySellPoint(
            type="3B", label="三买",
            dt=pullback.end.dt, price=pullback.end.low,
            description=(
                f"[{loc}] 离开中枢{hub.idx + 1}后回试，"
                f"低点({pullback.end.low:.3f})不破ZG({hub.zg:.3f})"
            ),
            level=level, confidence="high", hub_idx=hub.idx,
            stroke_idx=s_idx, seg_idx=d_idx,
            invalidation_price=hub.zg,
        )

    if last_stroke.direction == 1 and last_stroke.end.high > hub.zg:
        pullback = _find_next_stroke(strokes, last_stroke.idx, direction=-1)
        if pullback and pullback.end.low > hub.zg:
            points.append(_make_3b(pullback))
            return

    for s in strokes:
        if s.idx <= hub_end_idx:
            continue
        if s.direction == 1 and s.end.high > hub.zg:
            pullback = _find_next_stroke(strokes, s.idx, direction=-1)
            if pullback and pullback.end.low > hub.zg:
                points.append(_make_3b(pullback))
            break


def _check_type3_sell(hub: Hub, strokes: list[Stroke], hub_end_idx: int,
                      points: list[BuySellPoint], level: str,
                      stroke_to_seg: dict[int, int] | None = None):
    """Check for Type 3 sell point after hub."""
    stm = stroke_to_seg or {}
    last_stroke = hub.strokes[-1]

    def _make_3s(rally):
        s_idx = rally.idx
        d_idx = stm.get(s_idx, -1)
        loc = f"S{s_idx}" + (f"/D{d_idx}" if d_idx >= 0 else "")
        return BuySellPoint(
            type="3S", label="三卖",
            dt=rally.end.dt, price=rally.end.high,
            description=(
                f"[{loc}] 离开中枢{hub.idx + 1}后回抽，"
                f"高点({rally.end.high:.3f})不破ZD({hub.zd:.3f})"
            ),
            level=level, confidence="high", hub_idx=hub.idx,
            stroke_idx=s_idx, seg_idx=d_idx,
            invalidation_price=hub.zd,
        )

    if last_stroke.direction == -1 and last_stroke.end.low < hub.zd:
        rally = _find_next_stroke(strokes, last_stroke.idx, direction=1)
        if rally and rally.end.high < hub.zd:
            points.append(_make_3s(rally))
            return

    for s in strokes:
        if s.idx <= hub_end_idx:
            continue
        if s.direction == -1 and s.end.low < hub.zd:
            rally = _find_next_stroke(strokes, s.idx, direction=1)
            if rally and rally.end.high < hub.zd:
                points.append(_make_3s(rally))
            break


# ── Helpers ──

def _find_stroke_by_dt(strokes: list[Stroke], dt: str) -> Optional[Stroke]:
    for s in strokes:
        if s.end.dt == dt:
            return s
    return None


def _find_next_stroke(strokes: list[Stroke], after_idx: int,
                      direction: int) -> Optional[Stroke]:
    for s in strokes:
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
                note_parts.append("日线多头+中枢上方，买入环境良好")
                if adjusted_conf == "low":
                    adjusted_conf = "medium"
                elif adjusted_conf == "medium":
                    adjusted_conf = "high"
            elif daily_dir == 1 and "震荡" in daily_hub_pos:
                note_parts.append("日线多头+中枢区间，买入有支撑")
            elif daily_dir == -1 or "下方" in daily_hub_pos:
                note_parts.append("日线偏空/中枢下方，逆大级别买入需谨慎")
                if adjusted_conf == "high":
                    adjusted_conf = "medium"
                elif adjusted_conf == "medium":
                    adjusted_conf = "low"
        else:
            if daily_dir == -1 and "下方" in daily_hub_pos:
                note_parts.append("日线空头+中枢下方，卖出/做空环境良好")
                if adjusted_conf == "low":
                    adjusted_conf = "medium"
                elif adjusted_conf == "medium":
                    adjusted_conf = "high"
            elif daily_dir == 1 and "上方" in daily_hub_pos:
                note_parts.append("日线多头+中枢上方，逆大级别卖出，可能仅短差")
                if adjusted_conf == "high":
                    adjusted_conf = "medium"
                elif adjusted_conf == "medium":
                    adjusted_conf = "low"

        if daily_stage == "末段":
            if is_buy and daily_dir == -1:
                note_parts.append("日线下跌末段，小级别背驰可能引发大反转")
            elif not is_buy and daily_dir == 1:
                note_parts.append("日线上涨末段，小级别背驰可能引发大跳水")
        elif daily_stage == "初中段":
            if is_buy and daily_dir == 1:
                note_parts.append("日线上涨初中段，回调有限，买入安全边际高")
            elif not is_buy and daily_dir == -1:
                note_parts.append("日线下跌初中段，反弹有限")

        ctx["adjusted_confidence"] = adjusted_conf
        ctx["context_note"] = "；".join(note_parts) if note_parts else "无特殊大级别环境"
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
            return "偏多", "日线多头+中枢上方，回调是买入机会"
        elif "震荡" in daily_hub:
            return "中性偏多", "日线多头+中枢区间震荡，可高抛低吸"
        else:
            return "中性", "日线上涨趋势但在中枢下方，等待企稳信号"
    elif daily_dir == -1:
        if "下方" in daily_hub:
            return "偏空", "日线空头+中枢下方，反弹是减仓机会"
        elif "震荡" in daily_hub:
            return "中性偏空", "日线空头+中枢区间震荡，轻仓博弈"
        else:
            return "中性", "日线下跌趋势但在中枢上方，关注是否三卖"
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
            f"{nest.big_level}{type_label}{direction_label}（未找到更小级别确认）"
        )
        nests.append(nest)

    return nests


def synthesize_multi_level(
    daily: AnalysisResult,
    min30: Optional[AnalysisResult] = None,
    min5: Optional[AnalysisResult] = None,
) -> MultiLevelSynthesis:
    """Synthesize signals across multiple timeframes.

    Theory basis:
      - 108课 §3.4: daily stage determines small-level divergence impact
      - 图解缠论3 §1.1: 30min divergence + daily position + 5min precision
      - 土匪注解 §1.3: multi-level classification, small → big escalation
    """
    syn = MultiLevelSynthesis()

    results = [daily]
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

    # [6] Segment construction
    segments = find_segments(strokes)
    result.segments = segments

    # [7] Hub construction (stroke-level / 笔中枢)
    hubs = find_hubs(strokes)
    result.hubs = hubs

    # [7b] Hub evolution classification
    classify_hub_evolution(hubs)

    # [7c] Merge expanded hubs for trend determination
    result.merged_hubs = merge_expanded_hubs(hubs)

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
    trend_divs = check_trend_divergence(strokes, result.merged_hubs)
    consol_divs = check_consolidation_divergence(strokes, hubs)
    result.divergences = trend_divs + consol_divs

    # [10] Buy/sell points — use merged hubs so that hub references
    #     (hub_idx, ZG/ZD for 2B/2S, 3B/3S) are consistent with trend detection.
    result.buy_sell_points = find_buy_sell_points(
        result.merged_hubs, strokes, bars, trend_divs, consol_divs, level,
        segments=segments,
    )

    # [10b] Validate signals against subsequent price action
    _validate_signals(result.buy_sell_points, bars, strokes)

    for i, p in enumerate(result.buy_sell_points):
        p.idx = i

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

    if result.hubs:
        lines.append(f"## 中枢列表")
        evo_advice = {
            "延伸": "→ 高抛低吸做差价",
            "新生（上）": "→ 趋势上行，持仓",
            "新生（下）": "→ 趋势下行，回避",
            "扩展": "→ 升级为更大级别中枢，按盘整处理",
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
            lines.append(f"> 扩展中枢已合并为更大级别中枢，走势判定基于此视角")
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
                _STR_MAP = {"strongest": "🔥最强", "strong": "💪强势", "standard": "📌标准"}
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
    lines.append("| 级别 | 走势类型 | 中枢位置 | DIF区域 | 中枢数 | 信号数 | 最新信号 |")
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
        lines.append(f"## 跨级别共振信号（{len(syn.resonance_signals)} 个）")
        for r in syn.resonance_signals:
            sigs = " + ".join(
                f"{s['level']}-{s['label']}({s['confidence']})"
                for s in r["signals"]
            )
            lines.append(f"- **{r['date']}** [{r['direction']}] {sigs}")
            lines.append(f"  {r['note']}")
        lines.append("")

    if syn.enriched_signals:
        lines.append(f"## 大级别增强信号（{len(syn.enriched_signals)} 个）")
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
                        choices=["daily", "30min", "5min"],
                        help="Analysis level (default: daily)")
    args = parser.parse_args()

    if not os.path.exists(args.csv_file):
        print(f"File not found: {args.csv_file}", file=sys.stderr)
        sys.exit(1)

    result = analyze_from_csv(args.csv_file, args.level)
    print(format_report(result))


if __name__ == "__main__":
    main()
