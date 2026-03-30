#!/usr/bin/env python3
"""Clean up OCR output from scanned PDF extraction.

Fixes common EasyOCR errors for this book:
- Header/footer remnants (缠论108课详解 variants, page numbers)
- OCR misrecognitions (I08→108, DD→ID, etc.)
- Duplicate lesson titles
- Compress empty lines
"""

import re
from pathlib import Path

BOOK_DIR = Path(__file__).parent
INPUT_PATH = BOOK_DIR / "缠论108课详解.md"
OUTPUT_PATH = BOOK_DIR / "缠论108课详解.md"

HEADER_PATTERNS = [
    re.compile(r"^缠论\s*[I1l|]\s*[O0]\s*[8B]\s*[课谍诈]\s*详[觯解醒]?\s*$"),
    re.compile(r"^_论\s*[I1l|]\s*[O0]\s*[8B]\s*[课谍]\s*详[觯解醒]?\s*$"),
    re.compile(r"^教[你竹]炒股票\s*[|I1l]\s*\d*\s*[:：]?\s*.{0,30}$"),
]

FOOTER_PATTERNS = [
    re.compile(r"^0\s*原文来源[:：]?\s*h[tl]{2}p"),
    re.compile(r"^0\s*[|I1l]\s*文来源"),
    re.compile(r"^OD?\s*原文来源"),
]

OCR_FIXES = [
    (r"\bI08\b", "108"),
    (r"\bIO8\b", "108"),
    (r"\bIU8\b", "108"),
    (r"\bIOR\b", "108"),
    (r"\b1O8\b", "108"),
    (r"\bl08\b", "108"),
    (r"本\s*ID", "本ID"),
    (r"本\s*{D", "本ID"),
    (r"本\s*DD", "本ID"),
    (r"本\s*{D}", "本ID"),
    (r"笫", "第"),
    (r"儆", "做"),
    (r"丁夫", "工夫"),
    (r"酉游记", "西游记"),
    (r"酉天", "西天"),
    (r"K线", "K线"),
    (r"&线", "K线"),
    (r"飞线", "K线"),
    (r"下线(?=组合|包含|处理|分型|图)", "K线"),
]

DUPLICATE_TITLE_PATTERN = re.compile(
    r"^教你炒股票\s*\d+\s*[:：]\s*.+"
)


def is_header(line):
    stripped = line.strip()
    for pattern in HEADER_PATTERNS:
        if pattern.match(stripped):
            return True
    return False


def is_footer(line):
    stripped = line.strip()
    for pattern in FOOTER_PATTERNS:
        if pattern.match(stripped):
            return True
    if re.match(r"^\d{3}$", stripped):
        return True
    return False


def apply_ocr_fixes(line):
    for pattern, replacement in OCR_FIXES:
        line = re.sub(pattern, replacement, line)
    return line


def clean_document(text):
    lines = text.split("\n")
    cleaned = []
    prev_empty = False
    seen_lessons = set()

    for line in lines:
        stripped = line.strip()

        if line.startswith("#") or line.startswith(">") or line.startswith("📖"):
            if stripped.startswith("## 教你炒股票"):
                m = re.search(r"教你炒股票\s*(\d+)", stripped)
                if m:
                    seen_lessons.add(int(m.group(1)))
            line = apply_ocr_fixes(line)
            cleaned.append(line)
            prev_empty = False
            continue

        if line.startswith("---"):
            cleaned.append(line)
            prev_empty = False
            continue

        if is_header(stripped) or is_footer(stripped):
            continue

        if DUPLICATE_TITLE_PATTERN.match(stripped):
            m = re.search(r"教你炒股票\s*(\d+)", stripped)
            if m and int(m.group(1)) in seen_lessons:
                continue

        line = apply_ocr_fixes(line)

        if not stripped:
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
