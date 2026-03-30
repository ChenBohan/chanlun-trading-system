#!/usr/bin/env python3
"""Clean up text extracted from 缠论辅导(一)(二) PDFs.

Main issues:
1. Heavy character doubling (nearly every char doubled in many sections)
2. 8x repeated chars in decorative titles
3. TOC entries with dots and page numbers
4. Page footers (- NN -)
"""

import re
from pathlib import Path

BOOK_DIR = Path(__file__).parent
INPUT_PATH = BOOK_DIR / "缠论辅导.md"
OUTPUT_PATH = BOOK_DIR / "缠论辅导.md"

# Characters that can legitimately appear doubled
LEGIT_DOUBLE = set("哈呵嘿看想说好")


def count_doubled_ratio(line):
    """Count ratio of consecutive identical chars (Chinese + ASCII letters)."""
    chars = [c for c in line if ('\u4e00' <= c <= '\u9fff') or c.isalpha()]
    if len(chars) < 4:
        return 0
    doubled = sum(1 for i in range(len(chars) - 1) if chars[i] == chars[i + 1])
    return doubled / len(chars)


def deduplicate_chars(line):
    """Remove all doubled characters from a line (aggressive mode)."""
    result = []
    i = 0
    while i < len(line):
        c = line[i]
        is_cjk = '\u4e00' <= c <= '\u9fff'
        is_cjk_punct = c in '，。！？、：；""''（）《》【】…—'
        is_alpha = c.isalpha()

        if i + 1 < len(line) and c == line[i + 1]:
            if is_cjk and c not in LEGIT_DOUBLE:
                result.append(c)
                i += 2
                continue
            elif is_cjk_punct:
                result.append(c)
                i += 2
                continue
            elif is_alpha:
                result.append(c)
                i += 2
                continue
        result.append(c)
        i += 1
    return "".join(result)


def decode_8x_title(line):
    """Decode 8x repeated decorative title chars like 引引引引引引引引言言言言言言言言 → 引言."""
    stripped = line.strip()
    if not stripped:
        return line
    if not any('\u4e00' <= c <= '\u9fff' for c in stripped):
        return line

    first_char = stripped[0]
    repeat_count = 1
    while repeat_count < len(stripped) and stripped[repeat_count] == first_char:
        repeat_count += 1

    if repeat_count < 4:
        return line

    decoded = ""
    i = 0
    while i < len(stripped):
        c = stripped[i]
        count = 1
        while i + count < len(stripped) and stripped[i + count] == c:
            count += 1
        decoded += c
        i += count

    if len(decoded) <= len(stripped) // 3:
        return decoded
    return line


def is_toc_line(line):
    """Check if line is a TOC entry with dots and page numbers."""
    stripped = line.strip()
    if re.search(r'\.{5,}', stripped) or re.search(r'…{3,}', stripped):
        return True
    if re.match(r'^.*?-+\s*\d+\s*-+\s*$', stripped) and len(stripped) > 20:
        if '.' * 5 in stripped or '…' * 2 in stripped:
            return True
    return False


def is_page_footer(line):
    """Check if line is a page footer like '- 51 -'."""
    return bool(re.match(r'^\s*-+\s*\d+\s*-+\s*$', line.strip()))


def clean_document(text):
    lines = text.split("\n")
    cleaned = []
    prev_empty = False

    for line in lines:
        if line.startswith("#") or line.startswith(">") or line.startswith("!") or line.startswith("---"):
            cleaned.append(line)
            prev_empty = False
            continue

        stripped = line.strip()

        if is_page_footer(stripped):
            continue

        if is_toc_line(stripped):
            continue

        decoded = decode_8x_title(stripped)
        if decoded != stripped and len(decoded) < len(stripped) // 2:
            line = decoded

        ratio = count_doubled_ratio(line)
        if ratio > 0.15:
            line = deduplicate_chars(line)

        if not line.strip():
            if prev_empty:
                continue
            prev_empty = True
        else:
            prev_empty = False

        cleaned.append(line)

    return "\n".join(cleaned)


def main():
    text = INPUT_PATH.read_text(encoding="utf-8")
    original_lines = len(text.split("\n"))

    cleaned = clean_document(text)
    cleaned_lines = len(cleaned.split("\n"))

    OUTPUT_PATH.write_text(cleaned, encoding="utf-8")

    print(f"Cleanup complete!")
    print(f"  Before: {original_lines} lines")
    print(f"  After:  {cleaned_lines} lines")
    print(f"  Removed: {original_lines - cleaned_lines} lines")


if __name__ == "__main__":
    main()
