#!/usr/bin/env python3
"""Clean up text extracted from 土匪注解版缠中说禅全集 PDF.

Main issues:
1. Doubled characters from PDF bold rendering (e.g. "任任何" → "任何")
2. Page headers/footers mixed into body text
3. Duplicate lesson titles (both as heading and in body)
4. Year extraction artifacts in headers
"""

import re
from pathlib import Path

BOOK_DIR = Path(__file__).parent
INPUT_PATH = BOOK_DIR / "土匪注解版缠中说禅全集.md"
OUTPUT_PATH = BOOK_DIR / "土匪注解版缠中说禅全集.md"

PAGE_HEADER_PATTERNS = [
    re.compile(r"教你炒股票\s*\d+\s*[：:].*?page\s*e?\s*\d+\s*of\s*\d+", re.IGNORECASE),
    re.compile(r"第\d+页[，,]共\d+页"),
    re.compile(r"^page\s*e?\s*\d+\s*of\s*\d+$", re.IGNORECASE),
]

# Characters very unlikely to be legitimately doubled in Chinese
ALWAYS_DEDUPE = set(
    "的了在是不到得地着过把被给让向与和对从比为以於"
    "个这那些就都也还又才能会要可"
    "上下中前后里外间"
    "有很太最更已再"
    "但如果所因虽然而则于"
    "只本其每各多少"
    "去来回出进入"
    "种样般"
    "行走势型户手"
)

# Characters that can be legitimately doubled (keep both)
LEGIT_DOUBLE = set("哈呵嘿看想说天日年月人好")


def count_doubled_ratio(line):
    """Count ratio of consecutive identical Chinese characters in a line."""
    chars = [c for c in line if '\u4e00' <= c <= '\u9fff']
    if len(chars) < 4:
        return 0
    doubled = 0
    for i in range(len(chars) - 1):
        if chars[i] == chars[i + 1]:
            doubled += 1
    return doubled / len(chars)


def deduplicate_line(line):
    """Remove doubled characters from a line with high doubling ratio."""
    result = []
    i = 0
    while i < len(line):
        c = line[i]
        if i + 1 < len(line) and c == line[i + 1] and '\u4e00' <= c <= '\u9fff':
            if c not in LEGIT_DOUBLE:
                result.append(c)
                i += 2
                continue
        result.append(c)
        i += 1
    return "".join(result)


def deduplicate_always(line):
    """Always deduplicate characters in ALWAYS_DEDUPE set, regardless of threshold."""
    result = []
    i = 0
    while i < len(line):
        c = line[i]
        if i + 1 < len(line) and c == line[i + 1] and c in ALWAYS_DEDUPE:
            result.append(c)
            i += 2
            continue
        result.append(c)
        i += 1
    return "".join(result)


def is_page_header(line):
    """Check if line is a page header/footer."""
    stripped = line.strip()
    for pattern in PAGE_HEADER_PATTERNS:
        if pattern.search(stripped):
            return True
    if re.match(r"^\d{1,4}$", stripped):
        return True
    return False


def fix_lesson_header(line):
    """Fix doubled chars and year in ## lesson headers."""
    m = re.match(r"^(## 教你炒股票 \d+：)(.+?)（(\d{4})）$", line.strip())
    if m:
        prefix = m.group(1)
        title = deduplicate_line(m.group(2))
        title = re.sub(r"，，", "，", title)
        title = re.sub(r"。。", "。", title)
        year = m.group(3)
        if year == "2000" or year == "2200":
            year = "2006"
        return f"{prefix}{title}（{year}）"
    return line


def is_duplicate_title(line, seen_lessons):
    """Check if this is a duplicate of the lesson title in the body."""
    m = re.match(r"^教你炒股票\s*(\d+)\s*[：:]", line.strip())
    if m:
        num = int(m.group(1))
        if num in seen_lessons:
            return True
    return False


def clean_document(text):
    """Clean the entire document."""
    lines = text.split("\n")
    cleaned = []
    prev_empty = False
    seen_lessons = set()

    for line in lines:
        stripped = line.strip()

        if is_page_header(line):
            continue

        if stripped.startswith("## 教你炒股票"):
            line = fix_lesson_header(line)
            m = re.search(r"教你炒股票 (\d+)", line)
            if m:
                seen_lessons.add(int(m.group(1)))
            prev_empty = False
            cleaned.append(line)
            continue

        if is_duplicate_title(stripped, seen_lessons):
            continue

        line = deduplicate_always(line)

        # Fix doubled punctuation
        line = re.sub(r"，，", "，", line)
        line = re.sub(r"。。", "。", line)
        line = re.sub(r"！！", "！", line)
        line = re.sub(r"？？", "？", line)

        ratio = count_doubled_ratio(line.strip())
        if ratio > 0.06:
            line = deduplicate_line(line)

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
    print(f"  Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
