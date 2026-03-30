#!/usr/bin/env python3
"""Clean up OCR output from scanned PDF extraction.

Fixes common EasyOCR errors for Chinese financial/technical text:
- K线 misrecognized as &线, 飞线, 《线, k线, 下线, etc.
- Watermark remnants
- Page headers mixed into body text
- Standalone page numbers
- Punctuation normalization
"""

import re
from pathlib import Path

BOOK_DIR = Path(__file__).parent
INPUT_PATH = BOOK_DIR / "缠论解析.md"
OUTPUT_PATH = BOOK_DIR / "缠论解析.md"

# K线 OCR variants to fix
KLINE_FIXES = [
    (r"&线", "K线"),
    (r"飞线", "K线"),
    (r"《线", "K线"),
    (r"下线", "K线"),
    (r"k线", "K线"),
    (r"及线", "K线"),
    (r"长线", "K线"),  # context-dependent, be careful
]

# Page header patterns to remove
PAGE_HEADERS = [
    r"^缠论解析[\s\-—一]+缠中说禅技术理论图解\s*$",
    r"^缠论解析\s*$",
]

# Watermark remnants
WATERMARK_REMNANTS = [
    r"\.\s*Cn\s*",
    r"WTT\.\s*",
    r"macd\s*",
    r"广燮.*?制\s*",
]

# Common OCR fixes for this book
OCR_FIXES = [
    (r"通讨", "通过"),
    (r"买荬", "买卖"),
    (r"入$", "人"),
    (r"笫", "第"),
    (r"商点", "高点"),
    (r"处理开始c", "处理开始。"),
    (r"离低蒸全酃:其瘢-劲腧\(#缒围", "高低点全部在其相邻K线的高低点范围"),
]


def clean_line(line):
    """Clean a single line of OCR text."""
    # Remove page headers
    for pattern in PAGE_HEADERS:
        if re.match(pattern, line.strip()):
            return ""

    # Remove watermark remnants
    for pattern in WATERMARK_REMNANTS:
        line = re.sub(pattern, "", line, flags=re.IGNORECASE)

    # Fix K线 variants (only when not part of other words)
    for old, new in KLINE_FIXES:
        line = line.replace(old, new)

    # Common OCR fixes
    for old, new in OCR_FIXES:
        line = re.sub(old, new, line)

    # Remove standalone page numbers at line boundaries
    stripped = line.strip()
    if re.match(r"^[·.。]\s*\d+\s*[·.。]$", stripped):
        return ""
    if stripped.isdigit() and 1 <= int(stripped) <= 200:
        return ""
    if re.match(r"^(I{1,3}V?|IV|V|VI{0,3})$", stripped):
        return ""

    # Clean up excessive spaces
    line = re.sub(r"  +", " ", line)

    return line


def clean_document(text):
    """Clean the entire document."""
    lines = text.split("\n")
    cleaned = []
    prev_empty = False
    seen_chapters = set()

    for line in lines:
        result = clean_line(line)

        # De-duplicate chapter headers (keep only the first occurrence)
        chapter_match = re.match(r"^##\s*(第[一二三四五六七八九十]+章\s*.+|前\s*言|目\s*录|结\s*语)", result.strip())
        if chapter_match:
            chapter_key = re.sub(r"\s+", "", chapter_match.group(1))
            if chapter_key in seen_chapters:
                result = ""
            else:
                seen_chapters.add(chapter_key)

        # Remove orphaned scan reference lines when their chapter header was removed
        if not result.strip() and cleaned and cleaned[-1].strip().startswith("📖"):
            cleaned.pop()

        # Remove the --- separator before removed chapter headers
        if not result.strip() and cleaned and cleaned[-1].strip() == "---":
            # Check if this was a page boundary separator (not the first one)
            if len(seen_chapters) > 0:
                cleaned.pop()

        # Compress consecutive empty lines (max 2)
        if not result.strip():
            if prev_empty:
                continue
            prev_empty = True
        else:
            prev_empty = False

        cleaned.append(result)

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
    print(f"  Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
