"""
Chanlun (缠论) Technical Analysis for SMIC (688981.SH) Daily K-line.
Implements: inclusion processing, fractal identification, stroke, segment, hub.
"""

import math
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


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


@dataclass
class MergedKline:
    """K-line after inclusion processing."""
    idx: int
    start_idx: int
    end_idx: int
    dates: List[str]
    high: float
    low: float
    direction: int  # 1=up, -1=down during merge


@dataclass
class Fractal:
    """Top or Bottom fractal."""
    type: str  # "top" or "bottom"
    mk_idx: int  # index in merged k-lines
    high: float
    low: float
    date: str  # representative date
    kline_dates: List[str]


@dataclass
class Stroke:
    """笔 - connects alternating fractals."""
    idx: int
    start: Fractal
    end: Fractal
    direction: int  # 1=up, -1=down
    mk_count: int  # merged k-line count between fractals


@dataclass
class Segment:
    """线段 - at least 3 strokes."""
    idx: int
    start_stroke_idx: int
    end_stroke_idx: int
    direction: int  # 1=up, -1=down
    high: float
    low: float
    strokes: List[Stroke]


@dataclass
class Hub:
    """中枢 - overlapping zone of segments."""
    idx: int
    zg: float  # 中枢上沿 (min of highs)
    zd: float  # 中枢下沿 (max of lows)
    gg: float  # 最高高点
    dd: float  # 最低低点
    strokes: List[Stroke]
    start_date: str
    end_date: str


def parse_klines(filepath: str) -> List[Kline]:
    """Parse K-line data from the markdown file."""
    klines = []
    with open(filepath, 'r', encoding='utf-8') as f:
        in_table = False
        idx = 0
        for line in f:
            line = line.strip()
            if line.startswith('| 日期'):
                in_table = True
                continue
            if line.startswith('|---'):
                continue
            if in_table and line.startswith('|') and not line.startswith('| 日期'):
                parts = [p.strip() for p in line.split('|')[1:-1]]
                if len(parts) >= 11:
                    try:
                        klines.append(Kline(
                            idx=idx,
                            date=parts[0],
                            open=float(parts[1]),
                            close=float(parts[2]),
                            high=float(parts[3]),
                            low=float(parts[4]),
                            volume=int(parts[5]),
                            amount=float(parts[6]),
                            change_pct=float(parts[8]),
                        ))
                        idx += 1
                    except (ValueError, IndexError):
                        pass
            elif in_table and not line.startswith('|'):
                break
    return klines


def inclusion_processing(klines: List[Kline]) -> List[MergedKline]:
    """
    K-line inclusion processing (包含处理).
    Two K-lines have inclusion if: K1.high >= K2.high and K1.low <= K2.low (or vice versa).
    Merge direction: if trend is up, take higher high and higher low; if down, take lower.
    """
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
                dates=[k.date],
                high=k.high, low=k.low,
                direction=1 if k.high > last.high else -1,
            ))

    for i, m in enumerate(merged):
        m.idx = i

    return merged


def find_fractals(merged: List[MergedKline]) -> List[Fractal]:
    """
    Identify top and bottom fractals (顶分型/底分型).
    Top: K[i-1].high < K[i].high > K[i+1].high
    Bottom: K[i-1].low > K[i].low < K[i+1].low
    """
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


def find_strokes(fractals: List[Fractal], merged: List[MergedKline]) -> List[Stroke]:
    """
    Identify strokes (笔).
    Rules:
    - Alternating top and bottom fractals
    - At least 4 merged K-lines between fractal centers (i.e., ≥ 5 total including endpoints)
    - Top fractal high > bottom fractal high, bottom fractal low < top fractal low
    """
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
                    if f.high > last.high and last.low < f.low:
                        valid_fractals.append(f)
                    elif f.high <= last.high:
                        continue
                    else:
                        valid_fractals.append(f)
                elif last.type == "top" and f.type == "bottom":
                    if f.low < last.low and last.high > f.high:
                        valid_fractals.append(f)
                    elif f.low >= last.low:
                        continue
                    else:
                        valid_fractals.append(f)
                else:
                    valid_fractals.append(f)
            else:
                if f.type == "top" and f.high > last.high:
                    pass
                elif f.type == "bottom" and f.low < last.low:
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
            idx=len(strokes),
            start=start_f, end=end_f,
            direction=direction,
            mk_count=mk_count,
        ))

    return strokes


def find_segments(strokes: List[Stroke]) -> List[Segment]:
    """
    Identify segments (线段).
    A segment is at least 3 consecutive strokes in the same overall direction.
    A segment ends when the opposite direction breaks the segment's starting point.
    """
    if len(strokes) < 3:
        return []

    segments = []
    seg_start = 0
    i = 0

    while i < len(strokes) - 2:
        seg_dir = strokes[i].direction
        j = i

        while j + 2 < len(strokes):
            next_same_dir = strokes[j + 2]

            if seg_dir == 1:
                if next_same_dir.direction == 1:
                    if next_same_dir.end.high < strokes[j].end.high:
                        break
                j += 2
            else:
                if next_same_dir.direction == -1:
                    if next_same_dir.end.low > strokes[j].end.low:
                        break
                j += 2

        end_idx = min(j + 1, len(strokes) - 1)
        seg_strokes = strokes[i:end_idx + 1]

        if len(seg_strokes) >= 3:
            all_highs = []
            all_lows = []
            for s in seg_strokes:
                all_highs.extend([s.start.high, s.end.high])
                all_lows.extend([s.start.low, s.end.low])

            segments.append(Segment(
                idx=len(segments),
                start_stroke_idx=i,
                end_stroke_idx=end_idx,
                direction=seg_dir,
                high=max(all_highs),
                low=min(all_lows),
                strokes=seg_strokes,
            ))

        i = end_idx + 1

    if not segments and len(strokes) >= 3:
        all_highs = []
        all_lows = []
        for s in strokes[:3]:
            all_highs.extend([s.start.high, s.end.high])
            all_lows.extend([s.start.low, s.end.low])

        segments.append(Segment(
            idx=0,
            start_stroke_idx=0,
            end_stroke_idx=2,
            direction=strokes[0].direction,
            high=max(all_highs),
            low=min(all_lows),
            strokes=strokes[:3],
        ))

    return segments


def find_hubs(strokes: List[Stroke]) -> List[Hub]:
    """
    Identify hubs/centers (中枢).
    A hub is formed by at least 3 consecutive strokes where there is
    a price overlap zone: ZD (max of lows) < ZG (min of highs).
    """
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

            start_date = hub_strokes[0].start.date
            end_date = hub_strokes[-1].end.date

            hubs.append(Hub(
                idx=len(hubs),
                zg=zg, zd=zd, gg=gg, dd=dd,
                strokes=hub_strokes,
                start_date=start_date,
                end_date=end_date,
            ))
            i = j
        else:
            i += 1

    return hubs


def determine_trend(hubs: List[Hub], strokes: List[Stroke]) -> str:
    """Determine the current trend type based on hub positions."""
    if len(hubs) == 0:
        if strokes:
            return "上涨趋势" if strokes[-1].direction == 1 else "下跌趋势"
        return "无法判断"

    if len(hubs) == 1:
        hub = hubs[0]
        last_stroke = strokes[-1] if strokes else None
        if last_stroke:
            last_price = max(last_stroke.end.high, last_stroke.start.high) if last_stroke.direction == 1 else min(last_stroke.end.low, last_stroke.start.low)
            if last_stroke.direction == 1 and last_stroke.end.high > hub.gg:
                return "中枢上方，可能进入上涨趋势"
            elif last_stroke.direction == -1 and last_stroke.end.low < hub.dd:
                return "中枢下方，可能进入下跌趋势"
            else:
                return "盘整（单中枢）"
        return "盘整（单中枢）"

    last_hub = hubs[-1]
    prev_hub = hubs[-2]

    if last_hub.zd > prev_hub.zg:
        return "上涨趋势（中枢上移）"
    elif last_hub.zg < prev_hub.zd:
        return "下跌趋势（中枢下移）"
    else:
        return "盘整（中枢重叠）"


def find_buy_sell_points(hubs: List[Hub], strokes: List[Stroke]) -> List[dict]:
    """Identify buy/sell points based on Chanlun theory."""
    points = []

    for hub in hubs:
        hub_start_stroke = hub.strokes[0]
        hub_end_stroke = hub.strokes[-1]

        for stroke in strokes:
            if stroke.direction == -1 and stroke.end.low < hub.dd:
                if stroke.idx > hub_end_stroke.idx:
                    points.append({
                        'type': '一买（第一类买点）',
                        'date': stroke.end.date,
                        'price': stroke.end.low,
                        'description': f'下跌跌破中枢{hub.idx+1}下沿({hub.dd:.2f})后的底分型',
                        'hub_idx': hub.idx,
                    })
                    break

        for stroke in strokes:
            if stroke.direction == 1 and stroke.end.high > hub.gg:
                if stroke.idx > hub_end_stroke.idx:
                    points.append({
                        'type': '一卖（第一类卖点）',
                        'date': stroke.end.date,
                        'price': stroke.end.high,
                        'description': f'上涨突破中枢{hub.idx+1}上沿({hub.gg:.2f})后的顶分型',
                        'hub_idx': hub.idx,
                    })
                    break

    return points


def generate_analysis_md(
    klines: List[Kline],
    merged: List[MergedKline],
    fractals: List[Fractal],
    strokes: List[Stroke],
    segments: List[Segment],
    hubs: List[Hub],
    buy_sell_points: List[dict],
    trend: str,
) -> str:
    """Generate comprehensive Chanlun analysis markdown."""

    from datetime import datetime
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    lines = [
        "# 中芯国际（688981.SH）日线缠论分析",
        "",
        f"> 分析时间：{now}",
        f"> 数据区间：{klines[0].date} ~ {klines[-1].date}（{len(klines)} 个交易日）",
        f"> 分析级别：日线",
        "",
        "---",
        "",
        "## 一、包含处理结果",
        "",
        f"- 原始 K 线数：**{len(klines)}**",
        f"- 包含处理后 K 线数：**{len(merged)}**",
        f"- 合并了 **{len(klines) - len(merged)}** 根包含 K 线",
        "",
    ]

    lines.extend([
        "## 二、分型识别",
        "",
    ])

    top_fractals = [f for f in fractals if f.type == "top"]
    bottom_fractals = [f for f in fractals if f.type == "bottom"]

    lines.extend([
        f"- 顶分型数量：**{len(top_fractals)}** 个",
        f"- 底分型数量：**{len(bottom_fractals)}** 个",
        "",
        "### 顶分型列表",
        "",
        "| 序号 | 日期 | 最高价 | 最低价 | 包含K线 |",
        "|------|------|--------|--------|---------|",
    ])

    for i, f in enumerate(top_fractals):
        date_str = ", ".join(f.kline_dates[:3]) + ("..." if len(f.kline_dates) > 3 else "")
        lines.append(f"| {i+1} | {f.date} | {f.high:.2f} | {f.low:.2f} | {date_str} |")

    lines.extend([
        "",
        "### 底分型列表",
        "",
        "| 序号 | 日期 | 最高价 | 最低价 | 包含K线 |",
        "|------|------|--------|--------|---------|",
    ])

    for i, f in enumerate(bottom_fractals):
        date_str = ", ".join(f.kline_dates[:3]) + ("..." if len(f.kline_dates) > 3 else "")
        lines.append(f"| {i+1} | {f.date} | {f.high:.2f} | {f.low:.2f} | {date_str} |")

    lines.extend([
        "",
        "## 三、笔的划分",
        "",
        f"共识别出 **{len(strokes)}** 笔",
        "",
        "| 笔序号 | 方向 | 起始分型 | 起始日期 | 终止分型 | 终止日期 | 价格变动 | K线跨度 |",
        "|--------|------|----------|----------|----------|----------|----------|---------|",
    ])

    for s in strokes:
        dir_str = "↑ 上升" if s.direction == 1 else "↓ 下降"
        start_type = "底" if s.start.type == "bottom" else "顶"
        end_type = "顶" if s.end.type == "top" else "底"

        if s.direction == 1:
            price_change = f"{s.start.low:.2f} → {s.end.high:.2f} (+{s.end.high - s.start.low:.2f})"
        else:
            price_change = f"{s.start.high:.2f} → {s.end.low:.2f} ({s.end.low - s.start.high:.2f})"

        lines.append(
            f"| {s.idx+1} | {dir_str} | {start_type} | {s.start.date} | {end_type} | {s.end.date} | {price_change} | {s.mk_count} |"
        )

    lines.extend([
        "",
        "## 四、线段的划分",
        "",
    ])

    if segments:
        lines.append(f"共识别出 **{len(segments)}** 条线段")
        lines.append("")
        lines.append("| 线段序号 | 方向 | 起始日期 | 终止日期 | 最高价 | 最低价 | 包含笔数 |")
        lines.append("|----------|------|----------|----------|--------|--------|----------|")

        for seg in segments:
            dir_str = "↑ 上升" if seg.direction == 1 else "↓ 下降"
            start_date = seg.strokes[0].start.date
            end_date = seg.strokes[-1].end.date
            lines.append(
                f"| {seg.idx+1} | {dir_str} | {start_date} | {end_date} | {seg.high:.2f} | {seg.low:.2f} | {len(seg.strokes)} |"
            )
    else:
        lines.append("未能形成完整线段（笔数不足或结构不清晰）")

    lines.extend([
        "",
        "## 五、中枢分析",
        "",
    ])

    if hubs:
        lines.append(f"共识别出 **{len(hubs)}** 个中枢")
        lines.append("")

        for hub in hubs:
            lines.extend([
                f"### 中枢 {hub.idx+1}",
                "",
                f"- **时间范围**：{hub.start_date} ~ {hub.end_date}",
                f"- **中枢区间**：ZD={hub.zd:.2f} ~ ZG={hub.zg:.2f}（中枢振幅 {hub.zg - hub.zd:.2f}）",
                f"- **极值范围**：DD={hub.dd:.2f} ~ GG={hub.gg:.2f}",
                f"- **包含笔数**：{len(hub.strokes)} 笔",
                "",
            ])
    else:
        lines.append("未能识别出标准中枢（需要至少3笔形成重叠区间）")

    lines.extend([
        "",
        "## 六、走势类型判断",
        "",
        f"**当前走势类型**：{trend}",
        "",
    ])

    if len(hubs) >= 2:
        for i in range(1, len(hubs)):
            prev_h = hubs[i - 1]
            curr_h = hubs[i]
            relation = ""
            if curr_h.zd > prev_h.zg:
                relation = "上移（多头趋势）"
            elif curr_h.zg < prev_h.zd:
                relation = "下移（空头趋势）"
            else:
                relation = "重叠（盘整）"
            lines.append(f"- 中枢{i} → 中枢{i+1}：{relation}")
        lines.append("")

    lines.extend([
        "## 七、买卖点分析",
        "",
    ])

    if buy_sell_points:
        for pt in buy_sell_points:
            emoji = "🟢" if "买" in pt['type'] else "🔴"
            lines.extend([
                f"### {emoji} {pt['type']}",
                "",
                f"- **日期**：{pt['date']}",
                f"- **价格**：{pt['price']:.2f}",
                f"- **说明**：{pt['description']}",
                "",
            ])
    else:
        lines.append("当前阶段未识别出明确的标准买卖点")
        lines.append("")

    lines.extend([
        "## 八、综合研判与操作建议",
        "",
    ])

    latest_price = klines[-1].close
    latest_date = klines[-1].date

    lines.extend([
        f"### 当前状态（{latest_date}）",
        "",
        f"- **收盘价**：{latest_price:.2f}",
        f"- **走势类型**：{trend}",
    ])

    if hubs:
        last_hub = hubs[-1]
        if latest_price > last_hub.zg:
            lines.append(f"- **相对中枢位置**：在最后一个中枢（{last_hub.zd:.2f}~{last_hub.zg:.2f}）**上方**")
        elif latest_price < last_hub.zd:
            lines.append(f"- **相对中枢位置**：在最后一个中枢（{last_hub.zd:.2f}~{last_hub.zg:.2f}）**下方**")
        else:
            lines.append(f"- **相对中枢位置**：在最后一个中枢（{last_hub.zd:.2f}~{last_hub.zg:.2f}）**内部**")

    if strokes:
        last_stroke = strokes[-1]
        dir_str = "上升笔" if last_stroke.direction == 1 else "下降笔"
        lines.append(f"- **当前笔**：第{last_stroke.idx+1}笔（{dir_str}），{last_stroke.start.date} → {last_stroke.end.date}")

    lines.extend([
        "",
        "### 关键价格位",
        "",
    ])

    if hubs:
        for hub in hubs:
            lines.append(f"- 中枢{hub.idx+1}上沿 ZG={hub.zg:.2f}，下沿 ZD={hub.zd:.2f}")

    if strokes:
        recent_tops = sorted([s.end.high for s in strokes if s.direction == 1], reverse=True)
        recent_bots = sorted([s.end.low for s in strokes if s.direction == -1])
        if recent_tops:
            lines.append(f"- 近期笔顶高点：{', '.join(f'{p:.2f}' for p in recent_tops[:5])}")
        if recent_bots:
            lines.append(f"- 近期笔底低点：{', '.join(f'{p:.2f}' for p in recent_bots[:5])}")

    lines.extend([
        "",
        "### 后续关注",
        "",
        "1. 观察当前笔是否完成（是否出现新的反向分型）",
        "2. 关注中枢震荡区间的突破方向",
        "3. 如出现第三类买/卖点，需结合成交量背驰判断",
        "4. 建议结合30分钟线做更精细的买卖点定位",
        "",
        "---",
        "",
        "## 附录：笔的走势图（文字版）",
        "",
    ])

    if strokes:
        lines.append("```")
        for s in strokes:
            if s.direction == 1:
                lines.append(f"  {s.start.date} [{s.start.low:.1f}] ──↗── {s.end.date} [{s.end.high:.1f}]")
            else:
                lines.append(f"  {s.start.date} [{s.start.high:.1f}] ──↘── {s.end.date} [{s.end.low:.1f}]")
        lines.append("```")

    lines.extend([
        "",
        "> **免责声明**：本分析仅基于缠论技术方法对历史数据进行结构化解读，不构成投资建议。",
        "> 缠论分析的有效性依赖于正确的分型、笔、线段划分，不同人的划分可能存在差异。",
    ])

    return "\n".join(lines)


def main():
    data_path = "/home/chenbohan/Documents/gp/688981_中芯国际/日线数据.md"
    output_path = "/home/chenbohan/Documents/gp/688981_中芯国际/日线缠论分析.md"

    print("=== 中芯国际（688981.SH）日线缠论分析 ===\n")

    print("[1/7] Parsing K-line data...")
    klines = parse_klines(data_path)
    print(f"  -> Parsed {len(klines)} K-lines")

    print("[2/7] Inclusion processing...")
    merged = inclusion_processing(klines)
    print(f"  -> After inclusion: {len(merged)} merged K-lines (removed {len(klines) - len(merged)})")

    print("[3/7] Finding fractals...")
    fractals = find_fractals(merged)
    tops = [f for f in fractals if f.type == "top"]
    bots = [f for f in fractals if f.type == "bottom"]
    print(f"  -> Found {len(tops)} top fractals, {len(bots)} bottom fractals")

    print("[4/7] Finding strokes...")
    strokes = find_strokes(fractals, merged)
    print(f"  -> Found {len(strokes)} strokes")
    for s in strokes:
        dir_c = "↑" if s.direction == 1 else "↓"
        print(f"      Stroke {s.idx+1}: {dir_c} {s.start.date}({s.start.type}) -> {s.end.date}({s.end.type}), span={s.mk_count}")

    print("[5/7] Finding segments...")
    segments = find_segments(strokes)
    print(f"  -> Found {len(segments)} segments")

    print("[6/7] Finding hubs...")
    hubs = find_hubs(strokes)
    print(f"  -> Found {len(hubs)} hubs")
    for h in hubs:
        print(f"      Hub {h.idx+1}: {h.start_date}~{h.end_date}, ZD={h.zd:.2f}~ZG={h.zg:.2f}, {len(h.strokes)} strokes")

    trend = determine_trend(hubs, strokes)
    print(f"\n  Trend: {trend}")

    print("[7/7] Finding buy/sell points...")
    buy_sell_points = find_buy_sell_points(hubs, strokes)
    print(f"  -> Found {len(buy_sell_points)} buy/sell points")

    print("\nGenerating analysis report...")
    md = generate_analysis_md(klines, merged, fractals, strokes, segments, hubs, buy_sell_points, trend)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"  -> Written to {output_path}")

    print("\nDone!")


if __name__ == "__main__":
    main()
