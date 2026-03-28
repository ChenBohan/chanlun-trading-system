#!/usr/bin/env python3
"""Clean up extracted markdown: remove page numbers, improve image descriptions."""

import re
from pathlib import Path

MD_PATH = Path("/home/chenbohan/Documents/gp/缠论108课/缠中说禅108课.md")

def cleanup():
    text = MD_PATH.read_text(encoding="utf-8")
    lines = text.split("\n")
    cleaned = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Remove standalone page numbers (PDF footers)
        stripped = line.strip()
        if stripped.isdigit() and 1 <= int(stripped) <= 300:
            prev_empty = (i > 0 and not lines[i-1].strip())
            next_empty = (i + 1 < len(lines) and not lines[i+1].strip())
            if prev_empty or next_empty:
                i += 1
                continue

        # Improve image descriptions by looking at surrounding context
        img_match = re.match(r'!\[第(\d+)页插图-(.*?)\]\((.*?)\)', line)
        if img_match:
            page_num = img_match.group(1)
            fname = img_match.group(2)
            path = img_match.group(3)

            desc = _find_image_context(lines, i, page_num, fname)
            cleaned.append(f"![{desc}]({path})")
            i += 1
            continue

        cleaned.append(line)
        i += 1

    # Remove excessive blank lines (max 2 consecutive)
    result = []
    blank_count = 0
    for line in cleaned:
        if not line.strip():
            blank_count += 1
            if blank_count <= 2:
                result.append(line)
        else:
            blank_count = 0
            result.append(line)

    MD_PATH.write_text("\n".join(result), encoding="utf-8")
    print(f"Cleaned: {len(lines)} -> {len(result)} lines")


def _find_image_context(lines, img_idx, page_num, fname):
    """Find nearby text context to generate a meaningful image description."""
    context_lines = []

    # Look backward for context (up to 5 lines)
    for j in range(max(0, img_idx - 5), img_idx):
        stripped = lines[j].strip()
        if stripped and not stripped.startswith("![")\
                and not stripped.startswith("---")\
                and not stripped.isdigit():
            context_lines.append(stripped)

    # Look forward for context (up to 3 lines)
    for j in range(img_idx + 1, min(len(lines), img_idx + 4)):
        stripped = lines[j].strip()
        if stripped and not stripped.startswith("![")\
                and not stripped.startswith("---")\
                and not stripped.isdigit():
            context_lines.append(stripped)

    # Try to find lesson title nearby
    lesson_title = None
    for j in range(max(0, img_idx - 30), img_idx):
        m = re.match(r'(教你炒股票\s*\d+[：:].*?)[\(（]', lines[j].strip())
        if m:
            lesson_title = m.group(1).strip()

    # Build description
    if lesson_title:
        base = lesson_title
    else:
        base = f"p{page_num}"

    # Try to detect chart-related captions
    for ctx in context_lines:
        caption_match = re.match(r'^[\d、]*[、.]?\s*(.{4,30})$', ctx)
        if caption_match and any(kw in ctx for kw in [
            '分型', '中枢', '背驰', '走势', '买点', '卖点', '线段',
            '图', '均线', 'MACD', '日线', '周线', '月线', '30分',
            '5分', '1分', '季线', '底分型', '顶分型', '缺口',
            '确认', '上涨', '下跌', '盘整', '趋势'
        ]):
            return f"{base} - {caption_match.group(1).strip()} ({fname})"

    return f"{base} - 配图 ({fname})"


if __name__ == "__main__":
    cleanup()
