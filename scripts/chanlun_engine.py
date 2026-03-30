"""
Enhanced Chanlun (缠论) Multi-Level Analysis for SMIC (688981.SH).
Implements: inclusion processing, fractal, stroke, segment, hub,
MACD divergence, and all three types of buy/sell points.
"""

import math
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from datetime import datetime


@dataclass
class Kline:
    idx: int
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: int
    amount: float
    change_pct: float
    ema12: float = 0.0
    ema26: float = 0.0
    dif: float = 0.0
    dea: float = 0.0
    macd: float = 0.0


@dataclass
class MergedKline:
    idx: int
    start_idx: int
    end_idx: int
    dates: List[str]
    high: float
    low: float
    direction: int


@dataclass
class Fractal:
    type: str
    mk_idx: int
    high: float
    low: float
    date: str
    kline_dates: List[str]


@dataclass
class Stroke:
    idx: int
    start: Fractal
    end: Fractal
    direction: int
    mk_count: int
    macd_area: float = 0.0


@dataclass
class Hub:
    idx: int
    zg: float
    zd: float
    gg: float
    dd: float
    strokes: List[Stroke]
    start_date: str
    end_date: str


@dataclass
class BuySellPoint:
    type: str          # "1B", "2B", "3B", "1S", "2S", "3S"
    label: str
    date: str
    price: float
    description: str
    level: str         # "daily" or "30min"
    confidence: str    # "high", "medium", "low"
    hub_idx: int = -1


# ─── Data Parsing ───

def parse_klines(filepath: str) -> List[Kline]:
    klines = []
    with open(filepath, 'r', encoding='utf-8') as f:
        in_table = False
        idx = 0
        for line in f:
            line = line.strip()
            if line.startswith('| 日期') or line.startswith('| 时间'):
                in_table = True
                continue
            if line.startswith('|---'):
                continue
            if in_table and line.startswith('|') and not line.startswith('| 日期') and not line.startswith('| 时间'):
                parts = [p.strip() for p in line.split('|')[1:-1]]
                if len(parts) >= 11:
                    try:
                        klines.append(Kline(
                            idx=idx, date=parts[0],
                            open=float(parts[1]), close=float(parts[2]),
                            high=float(parts[3]), low=float(parts[4]),
                            volume=int(parts[5]), amount=float(parts[6]),
                            change_pct=float(parts[8]),
                        ))
                        idx += 1
                    except (ValueError, IndexError):
                        pass
            elif in_table and not line.startswith('|'):
                break
    return klines


# ─── MACD Calculation ───

def compute_macd(klines: List[Kline], short=12, long=26, signal=9):
    if not klines:
        return
    klines[0].ema12 = klines[0].close
    klines[0].ema26 = klines[0].close
    klines[0].dif = 0.0
    klines[0].dea = 0.0
    klines[0].macd = 0.0

    a_short = 2.0 / (short + 1)
    a_long = 2.0 / (long + 1)
    a_signal = 2.0 / (signal + 1)

    for i in range(1, len(klines)):
        k = klines[i]
        prev = klines[i - 1]
        k.ema12 = prev.ema12 * (1 - a_short) + k.close * a_short
        k.ema26 = prev.ema26 * (1 - a_long) + k.close * a_long
        k.dif = k.ema12 - k.ema26
        k.dea = prev.dea * (1 - a_signal) + k.dif * a_signal
        k.macd = 2 * (k.dif - k.dea)


# ─── Inclusion Processing ───

def inclusion_processing(klines: List[Kline]) -> List[MergedKline]:
    if not klines:
        return []
    merged = [MergedKline(
        idx=0, start_idx=0, end_idx=0,
        dates=[klines[0].date],
        high=klines[0].high, low=klines[0].low,
        direction=0,
    )]
    for i in range(1, len(klines)):
        k = klines[i]
        last = merged[-1]
        has_inclusion = (
            (last.high >= k.high and last.low <= k.low) or
            (k.high >= last.high and k.low <= last.low)
        )
        if has_inclusion:
            if len(merged) >= 2:
                direction = 1 if last.high > merged[-2].high else -1
            else:
                direction = 1 if k.close >= k.open else -1
            if direction == 1:
                last.high = max(last.high, k.high)
                last.low = max(last.low, k.low)
            else:
                last.high = min(last.high, k.high)
                last.low = min(last.low, k.low)
            last.end_idx = i
            last.dates.append(k.date)
            last.direction = direction
        else:
            new_idx = last.idx + 1
            merged.append(MergedKline(
                idx=new_idx, start_idx=i, end_idx=i,
                dates=[k.date], high=k.high, low=k.low,
                direction=1 if k.high > last.high else -1,
            ))
    for i, m in enumerate(merged):
        m.idx = i
    return merged


# ─── Fractals ───

def find_fractals(merged: List[MergedKline]) -> List[Fractal]:
    fractals = []
    for i in range(1, len(merged) - 1):
        prev_m, curr_m, next_m = merged[i - 1], merged[i], merged[i + 1]
        if curr_m.high > prev_m.high and curr_m.high > next_m.high:
            fractals.append(Fractal(
                type="top", mk_idx=i,
                high=curr_m.high, low=curr_m.low,
                date=curr_m.dates[0],
                kline_dates=prev_m.dates + curr_m.dates + next_m.dates,
            ))
        elif curr_m.low < prev_m.low and curr_m.low < next_m.low:
            fractals.append(Fractal(
                type="bottom", mk_idx=i,
                high=curr_m.high, low=curr_m.low,
                date=curr_m.dates[0],
                kline_dates=prev_m.dates + curr_m.dates + next_m.dates,
            ))
    return fractals


# ─── Strokes ───

def find_strokes(fractals: List[Fractal], merged: List[MergedKline]) -> List[Stroke]:
    if len(fractals) < 2:
        return []
    valid_fractals = [fractals[0]]
    for i in range(1, len(fractals)):
        f = fractals[i]
        last = valid_fractals[-1]
        if f.type == last.type:
            if f.type == "top" and f.high > last.high:
                valid_fractals[-1] = f
            elif f.type == "bottom" and f.low < last.low:
                valid_fractals[-1] = f
        else:
            mk_gap = abs(f.mk_idx - last.mk_idx)
            if mk_gap >= 4:
                if last.type == "bottom" and f.type == "top":
                    if f.high > last.high:
                        valid_fractals.append(f)
                elif last.type == "top" and f.type == "bottom":
                    if f.low < last.low:
                        valid_fractals.append(f)
                else:
                    valid_fractals.append(f)
            else:
                pass

    strokes = []
    for i in range(1, len(valid_fractals)):
        start_f = valid_fractals[i - 1]
        end_f = valid_fractals[i]
        if start_f.type == end_f.type:
            continue
        direction = 1 if end_f.type == "top" else -1
        mk_count = abs(end_f.mk_idx - start_f.mk_idx)
        strokes.append(Stroke(
            idx=len(strokes), start=start_f, end=end_f,
            direction=direction, mk_count=mk_count,
        ))
    return strokes


# ─── MACD Area for Strokes ───

def compute_stroke_macd_areas(strokes: List[Stroke], klines: List[Kline], merged: List[MergedKline]):
    date_to_kidx = {k.date: k.idx for k in klines}
    for s in strokes:
        start_date = s.start.date
        end_date = s.end.date
        si = date_to_kidx.get(start_date, 0)
        ei = date_to_kidx.get(end_date, len(klines) - 1)
        area = 0.0
        for ki in range(si, min(ei + 1, len(klines))):
            area += abs(klines[ki].macd)
        s.macd_area = area


# ─── Hubs ───

def find_hubs(strokes: List[Stroke]) -> List[Hub]:
    if len(strokes) < 3:
        return []
    hubs = []
    i = 0
    while i < len(strokes) - 2:
        s1, s2, s3 = strokes[i], strokes[i + 1], strokes[i + 2]
        stroke_ranges = []
        for s in [s1, s2, s3]:
            h = max(s.start.high, s.end.high)
            l = min(s.start.low, s.end.low)
            stroke_ranges.append((h, l))
        zg = min(r[0] for r in stroke_ranges)
        zd = max(r[1] for r in stroke_ranges)
        if zd < zg:
            hub_strokes = [s1, s2, s3]
            j = i + 3
            while j < len(strokes):
                sj = strokes[j]
                sj_h = max(sj.start.high, sj.end.high)
                sj_l = min(sj.start.low, sj.end.low)
                if sj_l < zg and sj_h > zd:
                    hub_strokes.append(sj)
                    j += 1
                else:
                    break
            gg = max(max(s.start.high, s.end.high) for s in hub_strokes)
            dd = min(min(s.start.low, s.end.low) for s in hub_strokes)
            hubs.append(Hub(
                idx=len(hubs), zg=zg, zd=zd, gg=gg, dd=dd,
                strokes=hub_strokes,
                start_date=hub_strokes[0].start.date,
                end_date=hub_strokes[-1].end.date,
            ))
            i = j
        else:
            i += 1
    return hubs


# ─── Trend Type ───

def determine_trend(hubs: List[Hub], strokes: List[Stroke]) -> str:
    if not hubs:
        if strokes:
            return "上涨" if strokes[-1].direction == 1 else "下跌"
        return "无法判断"
    if len(hubs) == 1:
        hub = hubs[0]
        last_stroke = strokes[-1] if strokes else None
        if last_stroke:
            if last_stroke.direction == 1 and last_stroke.end.high > hub.gg:
                return "中枢上方运行"
            elif last_stroke.direction == -1 and last_stroke.end.low < hub.dd:
                return "中枢下方运行"
            else:
                return "盘整（单中枢）"
        return "盘整（单中枢）"
    last_hub, prev_hub = hubs[-1], hubs[-2]
    if last_hub.zd > prev_hub.zg:
        return "上涨趋势（中枢上移）"
    elif last_hub.zg < prev_hub.zd:
        return "下跌趋势（中枢下移）"
    else:
        return "盘整（中枢重叠）"


# ─── Divergence Detection ───

def check_trend_divergence(strokes: List[Stroke], hubs: List[Hub]) -> List[dict]:
    """
    Detect trend divergence: in a+A+b+B+c structure,
    if MACD area of c < MACD area of a, divergence is present.
    """
    divergences = []
    if len(hubs) < 2:
        return divergences

    for hi in range(1, len(hubs)):
        prev_hub = hubs[hi - 1]
        curr_hub = hubs[hi]

        prev_hub_end_idx = prev_hub.strokes[-1].idx
        curr_hub_start_idx = curr_hub.strokes[0].idx
        curr_hub_end_idx = curr_hub.strokes[-1].idx

        seg_a_strokes = [s for s in strokes if s.idx <= prev_hub.strokes[0].idx]
        seg_c_strokes = [s for s in strokes if s.idx > curr_hub_end_idx]

        if not seg_a_strokes or not seg_c_strokes:
            continue

        a_area = sum(s.macd_area for s in seg_a_strokes)
        c_area = sum(s.macd_area for s in seg_c_strokes)

        if a_area > 0 and c_area < a_area:
            last_c = seg_c_strokes[-1]
            divergences.append({
                'type': 'trend',
                'direction': last_c.direction,
                'date': last_c.end.date,
                'a_area': a_area,
                'c_area': c_area,
                'ratio': c_area / a_area if a_area > 0 else 0,
                'hub_idx': hi,
            })

    return divergences


def check_consolidation_divergence(strokes: List[Stroke], hubs: List[Hub]) -> List[dict]:
    """
    Detect consolidation divergence: comparing consecutive exits from the same hub.
    """
    divergences = []
    for hub in hubs:
        exit_segments = []
        hub_stroke_idxs = set(s.idx for s in hub.strokes)
        hub_end_idx = hub.strokes[-1].idx

        for s in strokes:
            if s.idx in hub_stroke_idxs:
                continue
            sh = max(s.start.high, s.end.high)
            sl = min(s.start.low, s.end.low)
            if sh > hub.zg or sl < hub.zd:
                if s.idx > hub.strokes[0].idx:
                    exit_segments.append(s)

        for i in range(1, len(exit_segments)):
            prev_exit = exit_segments[i - 1]
            curr_exit = exit_segments[i]
            if prev_exit.direction == curr_exit.direction:
                if curr_exit.macd_area < prev_exit.macd_area and prev_exit.macd_area > 0:
                    divergences.append({
                        'type': 'consolidation',
                        'direction': curr_exit.direction,
                        'date': curr_exit.end.date,
                        'prev_area': prev_exit.macd_area,
                        'curr_area': curr_exit.macd_area,
                        'ratio': curr_exit.macd_area / prev_exit.macd_area,
                        'hub_idx': hub.idx,
                    })

    return divergences


# ─── Three Types of Buy/Sell Points ───

def find_all_buy_sell_points(
    hubs: List[Hub], strokes: List[Stroke], klines: List[Kline],
    trend_divs: List[dict], consol_divs: List[dict], level: str
) -> List[BuySellPoint]:
    points = []

    # --- Type 1 Buy/Sell (趋势背驰) ---
    for div in trend_divs:
        if div['direction'] == -1:
            stroke = next((s for s in strokes if s.end.date == div['date']), None)
            if stroke:
                points.append(BuySellPoint(
                    type="1B", label="一买（第一类买点）",
                    date=div['date'], price=stroke.end.low,
                    description=f"下跌趋势背驰：c段MACD面积({div['c_area']:.1f}) < a段({div['a_area']:.1f})，力度衰减比={div['ratio']:.2f}",
                    level=level, confidence="high" if div['ratio'] < 0.6 else "medium",
                    hub_idx=div['hub_idx'],
                ))
        elif div['direction'] == 1:
            stroke = next((s for s in strokes if s.end.date == div['date']), None)
            if stroke:
                points.append(BuySellPoint(
                    type="1S", label="一卖（第一类卖点）",
                    date=div['date'], price=stroke.end.high,
                    description=f"上涨趋势背驰：c段MACD面积({div['c_area']:.1f}) < a段({div['a_area']:.1f})，力度衰减比={div['ratio']:.2f}",
                    level=level, confidence="high" if div['ratio'] < 0.6 else "medium",
                    hub_idx=div['hub_idx'],
                ))

    # --- Type 1 from consolidation divergence ---
    for div in consol_divs:
        if div['direction'] == -1:
            stroke = next((s for s in strokes if s.end.date == div['date']), None)
            if stroke:
                points.append(BuySellPoint(
                    type="1B", label="一买（盘整背驰买点）",
                    date=div['date'], price=stroke.end.low,
                    description=f"盘整背驰：本次下离MACD面积({div['curr_area']:.1f}) < 前次({div['prev_area']:.1f})，比={div['ratio']:.2f}",
                    level=level, confidence="medium" if div['ratio'] < 0.7 else "low",
                    hub_idx=div['hub_idx'],
                ))
        elif div['direction'] == 1:
            stroke = next((s for s in strokes if s.end.date == div['date']), None)
            if stroke:
                points.append(BuySellPoint(
                    type="1S", label="一卖（盘整背驰卖点）",
                    date=div['date'], price=stroke.end.high,
                    description=f"盘整背驰：本次上离MACD面积({div['curr_area']:.1f}) < 前次({div['prev_area']:.1f})，比={div['ratio']:.2f}",
                    level=level, confidence="medium" if div['ratio'] < 0.7 else "low",
                    hub_idx=div['hub_idx'],
                ))

    # --- Type 2 Buy/Sell (一买/一卖后的第一次回调/反弹) ---
    type1_buys = [p for p in points if p.type == "1B"]
    type1_sells = [p for p in points if p.type == "1S"]

    for t1b in type1_buys:
        t1b_stroke = next((s for s in strokes if s.end.date == t1b.date), None)
        if not t1b_stroke:
            continue
        for s in strokes:
            if s.idx > t1b_stroke.idx and s.direction == 1:
                next_down = next(
                    (ns for ns in strokes if ns.idx > s.idx and ns.direction == -1),
                    None
                )
                if next_down and next_down.end.low > t1b.price:
                    points.append(BuySellPoint(
                        type="2B", label="二买（第二类买点）",
                        date=next_down.end.date, price=next_down.end.low,
                        description=f"一买({t1b.date})后第一次回调低点({next_down.end.low:.2f})不破一买价({t1b.price:.2f})",
                        level=level,
                        confidence="high" if next_down.end.low > t1b.price * 1.02 else "medium",
                        hub_idx=t1b.hub_idx,
                    ))
                break

    for t1s in type1_sells:
        t1s_stroke = next((s for s in strokes if s.end.date == t1s.date), None)
        if not t1s_stroke:
            continue
        for s in strokes:
            if s.idx > t1s_stroke.idx and s.direction == -1:
                next_up = next(
                    (ns for ns in strokes if ns.idx > s.idx and ns.direction == 1),
                    None
                )
                if next_up and next_up.end.high < t1s.price:
                    points.append(BuySellPoint(
                        type="2S", label="二卖（第二类卖点）",
                        date=next_up.end.date, price=next_up.end.high,
                        description=f"一卖({t1s.date})后第一次反弹高点({next_up.end.high:.2f})不破一卖价({t1s.price:.2f})",
                        level=level,
                        confidence="high" if next_up.end.high < t1s.price * 0.98 else "medium",
                        hub_idx=t1s.hub_idx,
                    ))
                break

    # --- Type 3 Buy/Sell (中枢回试) ---
    for hub in hubs:
        hub_end_stroke = hub.strokes[-1]

        for s in strokes:
            if s.idx <= hub_end_stroke.idx:
                continue
            if s.direction == 1 and s.end.high > hub.zg:
                next_down = next(
                    (ns for ns in strokes if ns.idx > s.idx and ns.direction == -1),
                    None
                )
                if next_down and next_down.end.low > hub.zg:
                    points.append(BuySellPoint(
                        type="3B", label="三买（第三类买点）",
                        date=next_down.end.date, price=next_down.end.low,
                        description=f"向上离开中枢{hub.idx+1}后回试，低点({next_down.end.low:.2f})不破ZG({hub.zg:.2f})",
                        level=level, confidence="high",
                        hub_idx=hub.idx,
                    ))
                elif next_down and next_down.end.low <= hub.zg:
                    pass
                break

        for s in strokes:
            if s.idx <= hub_end_stroke.idx:
                continue
            if s.direction == -1 and s.end.low < hub.zd:
                next_up = next(
                    (ns for ns in strokes if ns.idx > s.idx and ns.direction == 1),
                    None
                )
                if next_up and next_up.end.high < hub.zd:
                    points.append(BuySellPoint(
                        type="3S", label="三卖（第三类卖点）",
                        date=next_up.end.date, price=next_up.end.high,
                        description=f"向下离开中枢{hub.idx+1}后回抽，高点({next_up.end.high:.2f})不破ZD({hub.zd:.2f})",
                        level=level, confidence="high",
                        hub_idx=hub.idx,
                    ))
                break

    # --- MACD zero-axis check for Type 2 ---
    macd_enhanced = []
    for p in points:
        if p.type in ("2B", "2S"):
            date_kline = next((k for k in klines if k.date == p.date), None)
            if date_kline:
                if p.type == "2B" and date_kline.dif > 0:
                    p.description += "（MACD DIF已上穿0轴后回抽确认）"
                    p.confidence = "high"
                elif p.type == "2S" and date_kline.dif < 0:
                    p.description += "（MACD DIF已下穿0轴后回抽确认）"
                    p.confidence = "high"

    return points


# ─── Single Level Analysis ───

def analyze_level(filepath: str, level_name: str) -> dict:
    print(f"\n{'='*60}")
    print(f"  {level_name} analysis")
    print(f"{'='*60}")

    print(f"[1/8] Parsing K-line data from {filepath}...")
    klines = parse_klines(filepath)
    print(f"  -> {len(klines)} K-lines")
    if not klines:
        return {}

    print("[2/8] Computing MACD...")
    compute_macd(klines)
    last_k = klines[-1]
    print(f"  -> Latest MACD: DIF={last_k.dif:.3f}, DEA={last_k.dea:.3f}, MACD={last_k.macd:.3f}")

    print("[3/8] Inclusion processing...")
    merged = inclusion_processing(klines)
    print(f"  -> {len(merged)} merged K-lines (removed {len(klines)-len(merged)})")

    print("[4/8] Finding fractals...")
    fractals = find_fractals(merged)
    tops = [f for f in fractals if f.type == "top"]
    bots = [f for f in fractals if f.type == "bottom"]
    print(f"  -> {len(tops)} top, {len(bots)} bottom fractals")

    print("[5/8] Finding strokes...")
    strokes = find_strokes(fractals, merged)
    print(f"  -> {len(strokes)} strokes")
    for s in strokes:
        d = "↑" if s.direction == 1 else "↓"
        print(f"      {s.idx+1}: {d} {s.start.date} -> {s.end.date}")

    print("[6/8] Computing stroke MACD areas...")
    compute_stroke_macd_areas(strokes, klines, merged)

    print("[7/8] Finding hubs...")
    hubs = find_hubs(strokes)
    print(f"  -> {len(hubs)} hubs")
    for h in hubs:
        print(f"      Hub {h.idx+1}: {h.start_date}~{h.end_date}, ZD={h.zd:.2f}~ZG={h.zg:.2f}")

    trend = determine_trend(hubs, strokes)
    print(f"  -> Trend: {trend}")

    print("[8/8] Divergence & buy/sell points...")
    trend_divs = check_trend_divergence(strokes, hubs)
    consol_divs = check_consolidation_divergence(strokes, hubs)
    print(f"  -> Trend divergences: {len(trend_divs)}, consolidation divergences: {len(consol_divs)}")

    bsp = find_all_buy_sell_points(hubs, strokes, klines, trend_divs, consol_divs, level_name)
    print(f"  -> Buy/sell points: {len(bsp)}")
    for p in bsp:
        emoji = "🟢" if "B" in p.type else "🔴"
        print(f"      {emoji} {p.label} @ {p.date} price={p.price:.2f} [{p.confidence}]")

    return {
        'klines': klines,
        'merged': merged,
        'fractals': fractals,
        'strokes': strokes,
        'hubs': hubs,
        'trend': trend,
        'trend_divs': trend_divs,
        'consol_divs': consol_divs,
        'buy_sell_points': bsp,
    }


# ─── Multi-Level Synthesis ───

def synthesize_multilevel(daily: dict, min30: dict, title: str = "中芯国际（688981.SH）") -> str:
    lines = []
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    daily_klines = daily.get('klines', [])
    min30_klines = min30.get('klines', [])
    daily_strokes = daily.get('strokes', [])
    min30_strokes = min30.get('strokes', [])
    daily_hubs = daily.get('hubs', [])
    min30_hubs = min30.get('hubs', [])
    daily_bsp = daily.get('buy_sell_points', [])
    min30_bsp = min30.get('buy_sell_points', [])
    daily_trend = daily.get('trend', '无法判断')
    min30_trend = min30.get('trend', '无法判断')

    latest_price = daily_klines[-1].close if daily_klines else 0
    latest_date = daily_klines[-1].date if daily_klines else "N/A"

    lines.extend([
        f"# {title} 缠论多级别买卖点分析报告",
        "",
        f"> 分析时间：{now}",
        f"> 最新价格：**{latest_price:.2f}**（{latest_date}）",
        f"> 日线数据：{daily_klines[0].date} ~ {daily_klines[-1].date}（{len(daily_klines)} 根）" if daily_klines else "",
        f"> 30分钟数据：{min30_klines[0].date} ~ {min30_klines[-1].date}（{len(min30_klines)} 根）" if min30_klines else "",
        "",
        "---",
        "",
    ])

    # === Section 1: Daily Analysis ===
    lines.extend([
        "## 一、日线级别分析",
        "",
        f"### 1.1 走势结构",
        "",
        f"- **走势类型**：{daily_trend}",
        f"- **笔数**：{len(daily_strokes)}",
        f"- **中枢数**：{len(daily_hubs)}",
        "",
    ])

    if daily_hubs:
        lines.append("### 1.2 中枢一览")
        lines.append("")
        lines.append("| 中枢 | 时间范围 | ZD（下沿） | ZG（上沿） | 振幅 | DD（最低） | GG（最高） | 笔数 |")
        lines.append("|------|----------|-----------|-----------|------|-----------|-----------|------|")
        for h in daily_hubs:
            lines.append(f"| {h.idx+1} | {h.start_date} ~ {h.end_date} | {h.zd:.2f} | {h.zg:.2f} | {h.zg-h.zd:.2f} | {h.dd:.2f} | {h.gg:.2f} | {len(h.strokes)} |")
        lines.append("")

    if daily_strokes:
        lines.append("### 1.3 笔的划分")
        lines.append("")
        lines.append("| 笔 | 方向 | 起始 | 终止 | 价格变动 | MACD面积 |")
        lines.append("|-----|------|------|------|----------|----------|")
        for s in daily_strokes:
            d = "↑ 上升" if s.direction == 1 else "↓ 下降"
            if s.direction == 1:
                pc = f"{s.start.low:.2f} → {s.end.high:.2f}"
            else:
                pc = f"{s.start.high:.2f} → {s.end.low:.2f}"
            lines.append(f"| {s.idx+1} | {d} | {s.start.date} | {s.end.date} | {pc} | {s.macd_area:.1f} |")
        lines.append("")

    # MACD Status
    if daily_klines:
        lk = daily_klines[-1]
        lines.extend([
            "### 1.4 MACD 状态",
            "",
            f"- **DIF**：{lk.dif:.3f}",
            f"- **DEA**：{lk.dea:.3f}",
            f"- **MACD柱**：{lk.macd:.3f}（{'红柱' if lk.macd > 0 else '绿柱'}）",
            f"- **黄白线与0轴关系**：DIF {'在0轴上方' if lk.dif > 0 else '在0轴下方'}，DEA {'在0轴上方' if lk.dea > 0 else '在0轴下方'}",
        ])
        if lk.dif < 0 and lk.dea < 0:
            lines.append("- **⚠️ 防狼术提示**：黄白线均在0轴下方，空头主导，应回避或等待重新上0轴")
        lines.append("")

    # Divergence
    if daily.get('trend_divs') or daily.get('consol_divs'):
        lines.append("### 1.5 背驰信号")
        lines.append("")
        for d in daily.get('trend_divs', []):
            emoji = "📉" if d['direction'] == -1 else "📈"
            lines.append(f"- {emoji} **趋势背驰**（{d['date']}）：c/a面积比 = {d['ratio']:.2f}")
        for d in daily.get('consol_divs', []):
            emoji = "📉" if d['direction'] == -1 else "📈"
            lines.append(f"- {emoji} **盘整背驰**（{d['date']}）：面积比 = {d['ratio']:.2f}")
        lines.append("")

    # Daily BSP
    if daily_bsp:
        lines.append("### 1.6 日线买卖点")
        lines.append("")
        for p in daily_bsp:
            emoji = "🟢" if "B" in p.type else "🔴"
            conf = {"high": "⭐⭐⭐", "medium": "⭐⭐", "low": "⭐"}.get(p.confidence, "")
            lines.extend([
                f"#### {emoji} {p.label}  {conf}",
                "",
                f"- **日期**：{p.date}",
                f"- **价格**：{p.price:.2f}",
                f"- **说明**：{p.description}",
                "",
            ])
    else:
        lines.extend([
            "### 1.6 日线买卖点",
            "",
            "当前日线级别未识别出标准买卖点。",
            "",
        ])

    lines.extend(["---", ""])

    # === Section 2: 30-min Analysis ===
    lines.extend([
        "## 二、30分钟级别分析",
        "",
        f"### 2.1 走势结构",
        "",
        f"- **走势类型**：{min30_trend}",
        f"- **笔数**：{len(min30_strokes)}",
        f"- **中枢数**：{len(min30_hubs)}",
        "",
    ])

    if min30_hubs:
        lines.append("### 2.2 中枢一览")
        lines.append("")
        lines.append("| 中枢 | 时间范围 | ZD（下沿） | ZG（上沿） | 振幅 | 笔数 |")
        lines.append("|------|----------|-----------|-----------|------|------|")
        for h in min30_hubs:
            lines.append(f"| {h.idx+1} | {h.start_date} ~ {h.end_date} | {h.zd:.2f} | {h.zg:.2f} | {h.zg-h.zd:.2f} | {len(h.strokes)} |")
        lines.append("")

    if min30_strokes:
        lines.append("### 2.3 笔的划分")
        lines.append("")
        lines.append("| 笔 | 方向 | 起始 | 终止 | 价格变动 | MACD面积 |")
        lines.append("|-----|------|------|------|----------|----------|")
        for s in min30_strokes:
            d = "↑" if s.direction == 1 else "↓"
            if s.direction == 1:
                pc = f"{s.start.low:.2f} → {s.end.high:.2f}"
            else:
                pc = f"{s.start.high:.2f} → {s.end.low:.2f}"
            lines.append(f"| {s.idx+1} | {d} | {s.start.date} | {s.end.date} | {pc} | {s.macd_area:.1f} |")
        lines.append("")

    if min30_klines:
        lk = min30_klines[-1]
        lines.extend([
            "### 2.4 MACD 状态",
            "",
            f"- **DIF**：{lk.dif:.3f}",
            f"- **DEA**：{lk.dea:.3f}",
            f"- **MACD柱**：{lk.macd:.3f}（{'红柱' if lk.macd > 0 else '绿柱'}）",
            "",
        ])

    if min30.get('trend_divs') or min30.get('consol_divs'):
        lines.append("### 2.5 背驰信号")
        lines.append("")
        for d in min30.get('trend_divs', []):
            emoji = "📉" if d['direction'] == -1 else "📈"
            lines.append(f"- {emoji} **趋势背驰**（{d['date']}）：c/a面积比 = {d['ratio']:.2f}")
        for d in min30.get('consol_divs', []):
            emoji = "📉" if d['direction'] == -1 else "📈"
            lines.append(f"- {emoji} **盘整背驰**（{d['date']}）：面积比 = {d['ratio']:.2f}")
        lines.append("")

    if min30_bsp:
        lines.append("### 2.6 30分钟买卖点")
        lines.append("")
        for p in min30_bsp:
            emoji = "🟢" if "B" in p.type else "🔴"
            conf = {"high": "⭐⭐⭐", "medium": "⭐⭐", "low": "⭐"}.get(p.confidence, "")
            lines.extend([
                f"#### {emoji} {p.label}  {conf}",
                "",
                f"- **时间**：{p.date}",
                f"- **价格**：{p.price:.2f}",
                f"- **说明**：{p.description}",
                "",
            ])
    else:
        lines.extend([
            "### 2.6 30分钟买卖点",
            "",
            "当前30分钟级别未识别出标准买卖点。",
            "",
        ])

    lines.extend(["---", ""])

    # === Section 3: Multi-Level Synthesis ===
    lines.extend([
        "## 三、多级别联立研判",
        "",
        f"### 3.1 级别关系",
        "",
        f"- **日线走势**：{daily_trend}",
        f"- **30分钟走势**：{min30_trend}",
        "",
    ])

    # Position analysis
    lines.append("### 3.2 当前位置分析")
    lines.append("")
    lines.append(f"**最新价格 {latest_price:.2f}（{latest_date}）**")
    lines.append("")

    if daily_hubs:
        last_dh = daily_hubs[-1]
        if latest_price > last_dh.zg:
            lines.append(f"- 相对日线中枢（{last_dh.zd:.2f}~{last_dh.zg:.2f}）：**在上沿上方**，距上沿 +{latest_price - last_dh.zg:.2f}")
        elif latest_price < last_dh.zd:
            lines.append(f"- 相对日线中枢（{last_dh.zd:.2f}~{last_dh.zg:.2f}）：**在下沿下方**，距下沿 {latest_price - last_dh.zd:.2f}")
        else:
            lines.append(f"- 相对日线中枢（{last_dh.zd:.2f}~{last_dh.zg:.2f}）：**在中枢内部**")

    if min30_hubs:
        last_mh = min30_hubs[-1]
        if latest_price > last_mh.zg:
            lines.append(f"- 相对30分钟中枢（{last_mh.zd:.2f}~{last_mh.zg:.2f}）：**在上沿上方**")
        elif latest_price < last_mh.zd:
            lines.append(f"- 相对30分钟中枢（{last_mh.zd:.2f}~{last_mh.zg:.2f}）：**在下沿下方**")
        else:
            lines.append(f"- 相对30分钟中枢（{last_mh.zd:.2f}~{last_mh.zg:.2f}）：**在中枢内部**")

    if daily_strokes:
        ls = daily_strokes[-1]
        d = "上升笔" if ls.direction == 1 else "下降笔"
        lines.append(f"- 日线当前笔：第{ls.idx+1}笔（{d}），{ls.start.date} → {ls.end.date}")
    if min30_strokes:
        ls = min30_strokes[-1]
        d = "上升笔" if ls.direction == 1 else "下降笔"
        lines.append(f"- 30分钟当前笔：第{ls.idx+1}笔（{d}），{ls.start.date} → {ls.end.date}")
    lines.append("")

    # Key prices
    lines.append("### 3.3 关键价格位")
    lines.append("")
    lines.append("| 价格位 | 价格 | 含义 |")
    lines.append("|--------|------|------|")

    key_prices = []
    for h in daily_hubs:
        key_prices.append((h.zg, f"日线中枢{h.idx+1}上沿 ZG"))
        key_prices.append((h.zd, f"日线中枢{h.idx+1}下沿 ZD"))
    for h in min30_hubs:
        key_prices.append((h.zg, f"30分中枢{h.idx+1}上沿 ZG"))
        key_prices.append((h.zd, f"30分中枢{h.idx+1}下沿 ZD"))
    if daily_strokes:
        tops = sorted([s.end.high for s in daily_strokes if s.direction == 1], reverse=True)
        bots = sorted([s.end.low for s in daily_strokes if s.direction == -1])
        if tops:
            key_prices.append((tops[0], "日线笔最高点"))
        if bots:
            key_prices.append((bots[0], "日线笔最低点"))

    key_prices.sort(key=lambda x: x[0], reverse=True)
    for price, label in key_prices:
        marker = " ← 当前价" if abs(price - latest_price) / latest_price < 0.01 else ""
        lines.append(f"| {label} | {price:.2f} | {marker} |")
    lines.append("")

    # === Section 4: Comprehensive Recommendation ===
    all_bsp = daily_bsp + min30_bsp
    buys = [p for p in all_bsp if "B" in p.type]
    sells = [p for p in all_bsp if "S" in p.type]

    recent_buys = sorted([p for p in buys], key=lambda p: p.date, reverse=True)
    recent_sells = sorted([p for p in sells], key=lambda p: p.date, reverse=True)

    lines.extend([
        "### 3.4 综合买卖点汇总",
        "",
        "| 级别 | 类型 | 日期 | 价格 | 置信度 |",
        "|------|------|------|------|--------|",
    ])
    for p in sorted(all_bsp, key=lambda x: x.date, reverse=True):
        emoji = "🟢" if "B" in p.type else "🔴"
        conf = {"high": "高", "medium": "中", "low": "低"}.get(p.confidence, "")
        lines.append(f"| {p.level} | {emoji} {p.label} | {p.date} | {p.price:.2f} | {conf} |")
    lines.append("")

    # === Section 5: Operation Strategy ===
    lines.extend([
        "## 四、操作策略建议",
        "",
    ])

    # Determine current stance
    if daily_klines:
        lk = daily_klines[-1]
        is_bear_macd = lk.dif < 0 and lk.dea < 0

        lines.append("### 4.1 当前市场状态判断")
        lines.append("")

        if "下跌" in daily_trend:
            lines.append("**日线级别处于下跌走势中。**")
            lines.append("")
            lines.append("根据缠论操作系统：")
            lines.append("- 下跌趋势中以持币观望为主")
            lines.append("- 等待下跌趋势背驰（一买信号）")
            lines.append("- 如已有30分钟级别买点，可轻仓试探，但需严格设置止损")
        elif "盘整" in daily_trend:
            lines.append("**日线级别处于盘整走势中。**")
            lines.append("")
            lines.append("根据缠论操作系统：")
            lines.append("- 盘整中可做中枢震荡操作（先卖后买）")
            lines.append("- 关注中枢突破方向（三买/三卖信号）")
            lines.append("- 临近中枢上沿可减仓，临近下沿可加仓")
        elif "上涨" in daily_trend:
            lines.append("**日线级别处于上涨走势中。**")
            lines.append("")
            lines.append("根据缠论操作系统：")
            lines.append("- 上涨趋势中应持股为主")
            lines.append("- 关注上涨趋势背驰（一卖信号）")
            lines.append("- 中枢上移段应满仓，不做短差")
        else:
            lines.append(f"**当前走势：{daily_trend}**")
            lines.append("")

        lines.append("")
        if is_bear_macd:
            lines.append("> ⚠️ **防狼术**：MACD 黄白线均在0轴下方，空头主导。应保持谨慎，避免盲目抄底。")
            lines.append("")

        lines.append("### 4.2 具体操作建议")
        lines.append("")

        if recent_buys:
            latest_buy = recent_buys[0]
            lines.append(f"**最近买点**：{latest_buy.label} @ {latest_buy.date}，价格 {latest_buy.price:.2f}")
            lines.append("")
            if latest_buy.price < latest_price:
                lines.append(f"- 该买点价格（{latest_buy.price:.2f}）低于当前价（{latest_price:.2f}），已过。")
            elif latest_buy.price >= latest_price:
                lines.append(f"- 该买点价格（{latest_buy.price:.2f}）高于等于当前价（{latest_price:.2f}），关注回调确认。")
            lines.append("")

        if recent_sells:
            latest_sell = recent_sells[0]
            lines.append(f"**最近卖点**：{latest_sell.label} @ {latest_sell.date}，价格 {latest_sell.price:.2f}")
            lines.append("")

        lines.extend([
            "### 4.3 仓位参考（根据交易系统）",
            "",
            "| 信号 | 建议仓位 |",
            "|------|---------|",
            "| 一买确认 | 轻仓试探（1/3） |",
            "| 二买确认 | 加至标准仓位（2/3） |",
            "| 三买确认 | 满仓 |",
            "| 一卖信号 | 减至1/3或清仓 |",
            "| 三卖确认 | 必须清仓 |",
            "",
        ])

        lines.extend([
            "### 4.4 止损规则",
            "",
        ])
        if recent_buys:
            for b in recent_buys[:3]:
                if b.type == "1B":
                    lines.append(f"- 一买（{b.date}，{b.price:.2f}）：若再出现同级别下跌缠绕则退出")
                elif b.type == "2B":
                    lines.append(f"- 二买（{b.date}，{b.price:.2f}）：若跌破对应一买低点则反弹清仓")
                elif b.type == "3B":
                    hub_zg = None
                    if b.hub_idx >= 0:
                        for h in (daily_hubs + min30_hubs):
                            if h.idx == b.hub_idx:
                                hub_zg = h.zg
                                break
                    if hub_zg:
                        lines.append(f"- 三买（{b.date}，{b.price:.2f}）：若跌破中枢上沿 ZG={hub_zg:.2f} 则减仓或清仓")

        lines.append("")

    # === Section 6: Follow-up ===
    lines.extend([
        "### 4.5 后续关注事项",
        "",
        "1. **日线级别**：观察当前笔是否完成（需出现反向分型确认）",
        "2. **中枢突破方向**：关注价格是否有效突破中枢上沿/下沿",
        "3. **MACD 辅助**：",
        "   - 关注 DIF 与 DEA 的交叉（金叉/死叉）",
        "   - 关注黄白线是否回到0轴附近（变盘信号）",
        "   - 关注柱子面积变化（背驰判断）",
        "4. **30分钟级别**：用更小级别定位精确买卖点（区间套）",
        "5. **成交量配合**：买点需缩量确认底部，突破需放量确认有效性",
        "",
        "---",
        "",
        "> **重要声明**：本分析基于缠论技术方法对历史数据进行结构化解读，不构成投资建议。",
        "> 股市有风险，投资需谨慎。任何理论都不能保证盈利，关键在于严格执行操作纪律和风险控制。",
    ])

    return "\n".join(lines)


# ─── HTML Chart Generation ───

def generate_html_chart(daily: dict, min30: dict, title: str = "688981.SH 中芯国际") -> str:
    daily_klines = daily.get('klines', [])
    daily_strokes = daily.get('strokes', [])
    daily_hubs = daily.get('hubs', [])
    daily_bsp = daily.get('buy_sell_points', [])
    min30_klines = min30.get('klines', [])
    min30_strokes = min30.get('strokes', [])
    min30_hubs = min30.get('hubs', [])
    min30_bsp = min30.get('buy_sell_points', [])

    def klines_to_js(klines):
        items = []
        for k in klines:
            items.append(f'["{k.date}",{k.open},{k.close},{k.high},{k.low},{k.volume},{k.macd:.4f},{k.dif:.4f},{k.dea:.4f}]')
        return "[" + ",".join(items) + "]"

    def strokes_to_js(strokes):
        items = []
        for s in strokes:
            if s.direction == 1:
                items.append(f'{{start:"{s.start.date}",end:"{s.end.date}",startP:{s.start.low},endP:{s.end.high},dir:1}}')
            else:
                items.append(f'{{start:"{s.start.date}",end:"{s.end.date}",startP:{s.start.high},endP:{s.end.low},dir:-1}}')
        return "[" + ",".join(items) + "]"

    def hubs_to_js(hubs):
        items = []
        for h in hubs:
            items.append(f'{{start:"{h.start_date}",end:"{h.end_date}",zg:{h.zg},zd:{h.zd},gg:{h.gg},dd:{h.dd}}}')
        return "[" + ",".join(items) + "]"

    def bsp_to_js(bsp):
        items = []
        for p in bsp:
            items.append(f'{{type:"{p.type}",label:"{p.label}",date:"{p.date}",price:{p.price},desc:"{p.description[:60]}",conf:"{p.confidence}"}}')
        return "[" + ",".join(items) + "]"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title} 缠论多级别分析图</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }}
.header {{ padding: 20px 30px; border-bottom: 1px solid #21262d; }}
.header h1 {{ font-size: 22px; color: #58a6ff; }}
.header .meta {{ color: #8b949e; font-size: 13px; margin-top: 6px; }}
.tabs {{ display: flex; gap: 0; border-bottom: 1px solid #21262d; padding: 0 30px; }}
.tab {{ padding: 10px 20px; cursor: pointer; color: #8b949e; border-bottom: 2px solid transparent; font-size: 14px; }}
.tab.active {{ color: #58a6ff; border-bottom-color: #58a6ff; }}
.tab:hover {{ color: #c9d1d9; }}
.chart-container {{ padding: 20px 30px; }}
canvas {{ display: block; width: 100%; background: #0d1117; border-radius: 6px; }}
.legend {{ display: flex; gap: 20px; padding: 10px 30px; flex-wrap: wrap; }}
.legend-item {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: #8b949e; }}
.legend-color {{ width: 14px; height: 14px; border-radius: 2px; }}
.info-panel {{ padding: 15px 30px; display: flex; gap: 30px; flex-wrap: wrap; }}
.info-card {{ background: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 12px 16px; min-width: 200px; }}
.info-card .label {{ font-size: 11px; color: #8b949e; text-transform: uppercase; }}
.info-card .value {{ font-size: 18px; font-weight: 600; margin-top: 4px; }}
.info-card .value.up {{ color: #f85149; }}
.info-card .value.down {{ color: #3fb950; }}
.bsp-list {{ padding: 10px 30px 30px; }}
.bsp-item {{ display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-radius: 6px; margin-bottom: 4px; }}
.bsp-item:hover {{ background: #161b22; }}
.bsp-badge {{ padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }}
.bsp-badge.buy {{ background: rgba(63,185,80,0.2); color: #3fb950; }}
.bsp-badge.sell {{ background: rgba(248,81,73,0.2); color: #f85149; }}
.bsp-date {{ color: #8b949e; font-size: 13px; width: 120px; }}
.bsp-price {{ font-weight: 600; width: 80px; }}
.bsp-desc {{ color: #8b949e; font-size: 12px; flex: 1; }}
.bsp-conf {{ font-size: 11px; }}
</style>
</head>
<body>
<div class="header">
  <h1>{title} — 缠论多级别买卖点分析</h1>
  <div class="meta">分析时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} | 日线 {len(daily_klines)} 根 | 30分钟 {len(min30_klines)} 根</div>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('daily')">日线级别</div>
  <div class="tab" onclick="switchTab('min30')">30分钟级别</div>
</div>

<div class="info-panel" id="infoPanel"></div>

<div class="chart-container">
  <canvas id="klineCanvas" height="400"></canvas>
</div>
<div class="chart-container">
  <canvas id="macdCanvas" height="150"></canvas>
</div>

<div class="legend">
  <div class="legend-item"><div class="legend-color" style="background:#f85149"></div>阳线/上升笔</div>
  <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div>阴线/下降笔</div>
  <div class="legend-item"><div class="legend-color" style="background:rgba(88,166,255,0.2);border:1px solid #58a6ff"></div>中枢区间</div>
  <div class="legend-item"><div class="legend-color" style="background:#f0883e"></div>笔连线</div>
  <div class="legend-item"><div class="legend-color" style="background:#ffd33d;border-radius:50%"></div>买点</div>
  <div class="legend-item"><div class="legend-color" style="background:#da3633;border-radius:50%"></div>卖点</div>
</div>

<h3 style="padding:10px 30px;color:#58a6ff;font-size:16px" id="bspTitle">买卖点列表</h3>
<div class="bsp-list" id="bspList"></div>

<script>
const dailyData = {{
  klines: {klines_to_js(daily_klines)},
  strokes: {strokes_to_js(daily_strokes)},
  hubs: {hubs_to_js(daily_hubs)},
  bsp: {bsp_to_js(daily_bsp)},
  trend: "{daily.get('trend', 'N/A')}"
}};
const min30Data = {{
  klines: {klines_to_js(min30_klines)},
  strokes: {strokes_to_js(min30_strokes)},
  hubs: {hubs_to_js(min30_hubs)},
  bsp: {bsp_to_js(min30_bsp)},
  trend: "{min30.get('trend', 'N/A')}"
}};

let currentTab = 'daily';

function switchTab(tab) {{
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelector(`.tab:nth-child(${{tab==='daily'?1:2}})`).classList.add('active');
  render();
}}

function getData() {{ return currentTab === 'daily' ? dailyData : min30Data; }}

function render() {{
  const data = getData();
  renderInfo(data);
  renderKline(data);
  renderMACD(data);
  renderBSP(data);
}}

function renderInfo(data) {{
  const k = data.klines;
  if (!k.length) return;
  const last = k[k.length-1];
  const prev = k.length > 1 ? k[k.length-2] : last;
  const change = last[2] - prev[2];
  const changePct = (change / prev[2] * 100);
  const cls = change >= 0 ? 'up' : 'down';
  const sign = change >= 0 ? '+' : '';
  document.getElementById('infoPanel').innerHTML = `
    <div class="info-card"><div class="label">最新价</div><div class="value ${{cls}}">${{last[2].toFixed(2)}}</div></div>
    <div class="info-card"><div class="label">涨跌</div><div class="value ${{cls}}">${{sign}}${{change.toFixed(2)}} (${{sign}}${{changePct.toFixed(2)}}%)</div></div>
    <div class="info-card"><div class="label">走势类型</div><div class="value">${{data.trend}}</div></div>
    <div class="info-card"><div class="label">中枢数</div><div class="value">${{data.hubs.length}}</div></div>
    <div class="info-card"><div class="label">笔数</div><div class="value">${{data.strokes.length}}</div></div>
    <div class="info-card"><div class="label">买卖点</div><div class="value">${{data.bsp.length}}</div></div>
  `;
}}

function renderKline(data) {{
  const canvas = document.getElementById('klineCanvas');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = 400 * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = 400;
  ctx.clearRect(0, 0, W, H);

  const k = data.klines;
  if (!k.length) return;
  const pad = {{t:20, b:30, l:60, r:20}};
  const cw = (W - pad.l - pad.r) / k.length;
  const allH = k.map(x => x[3]);
  const allL = k.map(x => x[4]);
  const maxP = Math.max(...allH) * 1.01;
  const minP = Math.min(...allL) * 0.99;
  const scaleY = p => pad.t + (maxP - p) / (maxP - minP) * (H - pad.t - pad.b);
  const scaleX = i => pad.l + i * cw + cw / 2;

  // grid
  ctx.strokeStyle = '#21262d';
  ctx.lineWidth = 0.5;
  for (let i = 0; i < 5; i++) {{
    const y = pad.t + i * (H - pad.t - pad.b) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W-pad.r, y); ctx.stroke();
    const p = maxP - i * (maxP - minP) / 4;
    ctx.fillStyle = '#8b949e'; ctx.font = '11px monospace'; ctx.textAlign = 'right';
    ctx.fillText(p.toFixed(2), pad.l - 6, y + 4);
  }}

  // hubs
  const dateIdx = {{}};
  k.forEach((kk, i) => dateIdx[kk[0]] = i);
  data.hubs.forEach(h => {{
    const si = dateIdx[h.start] ?? 0;
    const ei = dateIdx[h.end] ?? k.length - 1;
    const x1 = scaleX(si) - cw/2;
    const x2 = scaleX(ei) + cw/2;
    ctx.fillStyle = 'rgba(88,166,255,0.08)';
    ctx.fillRect(x1, scaleY(h.zg), x2-x1, scaleY(h.zd)-scaleY(h.zg));
    ctx.strokeStyle = 'rgba(88,166,255,0.4)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(x1, scaleY(h.zg)); ctx.lineTo(x2, scaleY(h.zg)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x1, scaleY(h.zd)); ctx.lineTo(x2, scaleY(h.zd)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#58a6ff'; ctx.font = '10px sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(`ZG=${{h.zg.toFixed(1)}}`, x2+3, scaleY(h.zg)+3);
    ctx.fillText(`ZD=${{h.zd.toFixed(1)}}`, x2+3, scaleY(h.zd)+12);
  }});

  // candlesticks
  k.forEach((kk, i) => {{
    const [dt, o, c, hi, lo] = kk;
    const x = scaleX(i);
    const bw = Math.max(cw * 0.6, 1);
    const isUp = c >= o;
    ctx.strokeStyle = ctx.fillStyle = isUp ? '#f85149' : '#3fb950';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, scaleY(hi)); ctx.lineTo(x, scaleY(lo)); ctx.stroke();
    const top = scaleY(Math.max(o, c));
    const bot = scaleY(Math.min(o, c));
    const bh = Math.max(bot - top, 1);
    if (isUp) {{ ctx.fillRect(x-bw/2, top, bw, bh); }}
    else {{ ctx.fillRect(x-bw/2, top, bw, bh); }}
  }});

  // strokes
  ctx.strokeStyle = '#f0883e';
  ctx.lineWidth = 1.5;
  data.strokes.forEach(s => {{
    const si = dateIdx[s.start] ?? 0;
    const ei = dateIdx[s.end] ?? 0;
    ctx.beginPath();
    ctx.moveTo(scaleX(si), scaleY(s.startP));
    ctx.lineTo(scaleX(ei), scaleY(s.endP));
    ctx.stroke();
  }});

  // buy/sell points
  data.bsp.forEach(p => {{
    const pi = dateIdx[p.date];
    if (pi === undefined) return;
    const x = scaleX(pi);
    const y = scaleY(p.price);
    const isBuy = p.type.includes('B');
    ctx.beginPath();
    if (isBuy) {{
      ctx.moveTo(x, y+12); ctx.lineTo(x-7, y+22); ctx.lineTo(x+7, y+22); ctx.closePath();
      ctx.fillStyle = '#ffd33d'; ctx.fill();
    }} else {{
      ctx.moveTo(x, y-12); ctx.lineTo(x-7, y-22); ctx.lineTo(x+7, y-22); ctx.closePath();
      ctx.fillStyle = '#da3633'; ctx.fill();
    }}
    ctx.fillStyle = isBuy ? '#ffd33d' : '#da3633';
    ctx.font = 'bold 10px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(p.label.substring(0,2), x, isBuy ? y+34 : y-24);
  }});

  // date labels
  ctx.fillStyle = '#8b949e'; ctx.font = '10px monospace'; ctx.textAlign = 'center';
  const step = Math.max(Math.floor(k.length / 8), 1);
  for (let i = 0; i < k.length; i += step) {{
    const label = k[i][0].length > 10 ? k[i][0].substring(5) : k[i][0].substring(5);
    ctx.fillText(label, scaleX(i), H - 8);
  }}
}}

function renderMACD(data) {{
  const canvas = document.getElementById('macdCanvas');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = 150 * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = 150;
  ctx.clearRect(0, 0, W, H);

  const k = data.klines;
  if (!k.length) return;
  const pad = {{t:10, b:20, l:60, r:20}};
  const cw = (W - pad.l - pad.r) / k.length;

  const macds = k.map(x => x[6]);
  const difs = k.map(x => x[7]);
  const deas = k.map(x => x[8]);
  const allVals = [...macds, ...difs, ...deas];
  const maxV = Math.max(...allVals.map(Math.abs)) * 1.1 || 1;

  const scaleY = v => pad.t + (maxV - v) / (2 * maxV) * (H - pad.t - pad.b);
  const scaleX = i => pad.l + i * cw + cw / 2;
  const zeroY = scaleY(0);

  // zero line
  ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(W-pad.r, zeroY); ctx.stroke();
  ctx.fillStyle = '#8b949e'; ctx.font = '10px monospace'; ctx.textAlign = 'right';
  ctx.fillText('0', pad.l - 6, zeroY + 3);

  // MACD bars
  k.forEach((kk, i) => {{
    const m = kk[6];
    const x = scaleX(i);
    const bw = Math.max(cw * 0.5, 1);
    ctx.fillStyle = m >= 0 ? '#f85149' : '#3fb950';
    const y1 = zeroY;
    const y2 = scaleY(m);
    ctx.fillRect(x - bw/2, Math.min(y1,y2), bw, Math.abs(y2-y1) || 1);
  }});

  // DIF line
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1.2;
  ctx.beginPath();
  k.forEach((kk, i) => {{
    const x = scaleX(i), y = scaleY(kk[7]);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }});
  ctx.stroke();

  // DEA line
  ctx.strokeStyle = '#f0883e'; ctx.lineWidth = 1.2;
  ctx.beginPath();
  k.forEach((kk, i) => {{
    const x = scaleX(i), y = scaleY(kk[8]);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }});
  ctx.stroke();

  // labels
  ctx.fillStyle = '#58a6ff'; ctx.font = '10px sans-serif'; ctx.textAlign = 'left';
  ctx.fillText('DIF', W-pad.r-80, pad.t+12);
  ctx.fillStyle = '#f0883e';
  ctx.fillText('DEA', W-pad.r-40, pad.t+12);
}}

function renderBSP(data) {{
  const list = document.getElementById('bspList');
  if (!data.bsp.length) {{
    list.innerHTML = '<div style="color:#8b949e;padding:10px">当前级别未识别出标准买卖点</div>';
    return;
  }}
  list.innerHTML = data.bsp.map(p => {{
    const isBuy = p.type.includes('B');
    const cls = isBuy ? 'buy' : 'sell';
    const stars = p.conf === 'high' ? '⭐⭐⭐' : p.conf === 'medium' ? '⭐⭐' : '⭐';
    return `<div class="bsp-item">
      <span class="bsp-badge ${{cls}}">${{p.label.substring(0,2)}}</span>
      <span class="bsp-date">${{p.date}}</span>
      <span class="bsp-price" style="color:${{isBuy?'#3fb950':'#f85149'}}">${{p.price.toFixed(2)}}</span>
      <span class="bsp-desc">${{p.desc}}</span>
      <span class="bsp-conf">${{stars}}</span>
    </div>`;
  }}).join('');
}}

window.addEventListener('load', render);
window.addEventListener('resize', render);
</script>
</body>
</html>"""
    return html


def main():
    base_dir = "/home/chenbohan/Documents/gp/688981_中芯国际"
    daily_path = os.path.join(base_dir, "日线数据.md")
    min30_path = os.path.join(base_dir, "30分钟线数据.md")

    print("=" * 60)
    print("  中芯国际（688981.SH）缠论多级别买卖点分析")
    print("=" * 60)

    daily = analyze_level(daily_path, "日线")
    min30 = analyze_level(min30_path, "30分钟")

    print(f"\n{'='*60}")
    print("  Generating reports...")
    print(f"{'='*60}")

    md = synthesize_multilevel(daily, min30)
    md_path = os.path.join(base_dir, "缠论买卖点分析.md")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"  -> Markdown report: {md_path}")

    html = generate_html_chart(daily, min30)
    html_path = os.path.join(base_dir, "缠论分析图.html")
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  -> HTML chart: {html_path}")

    print("\nDone!")


if __name__ == "__main__":
    main()
