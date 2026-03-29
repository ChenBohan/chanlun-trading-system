#!/usr/bin/env python3
"""Extract text from scanned PDF pages using EasyOCR, output as Markdown.

This is a 593-page scanned book (扫地僧读缠论108课札记), each page pre-extracted
as p{NNN}.png in images/ directory.
"""

import os
import re
from pathlib import Path

import easyocr

BOOK_DIR = Path(__file__).parent
IMG_DIR = BOOK_DIR / "images"
MD_PATH = BOOK_DIR / "缠论108课详解.md"

HEADER_PATTERNS = [
    re.compile(r"缠论\s*[I1l|]\s*[O0]\s*[8B]\s*[课谍诈]\s*详[觯解]"),
    re.compile(r"^\d{3}$"),
]

LESSON_PATTERN = re.compile(
    r"教你炒股票\s*(\d+)\s*[：:]\s*(.+)"
)

START_PAGE = 13
END_PAGE = 593


def is_header_or_footer(text):
    stripped = text.strip()
    for pattern in HEADER_PATTERNS:
        if pattern.search(stripped):
            return True
    if stripped.isdigit() and 1 <= int(stripped) <= 600:
        return True
    return False


def process_page(reader, img_path, page_num):
    results = reader.readtext(str(img_path), detail=0, paragraph=True)
    cleaned = []
    for block in results:
        block = block.strip()
        if not block:
            continue
        if is_header_or_footer(block):
            continue
        cleaned.append(block)
    return cleaned


def main():
    print("Initializing EasyOCR reader...")
    reader = easyocr.Reader(["ch_sim", "en"], gpu=False)

    lines_out = []
    lines_out.append("# 缠论108课详解——扫地僧读缠论108课札记\n\n")
    lines_out.append("> **作者**：扫地僧（雷永平）\n")
    lines_out.append("> **内容**：缠中说禅「教你炒股票」108课逐课详解札记\n\n")
    lines_out.append("> 本文由扫描版 PDF 通过 EasyOCR 自动提取。\n")
    lines_out.append("> 每页原始扫描件以 `p{页码}.png` 命名，可对照查看。\n\n")
    lines_out.append("---\n\n")

    total = END_PAGE - START_PAGE + 1
    seen_lessons = set()

    for page_num in range(START_PAGE, END_PAGE + 1):
        img_path = IMG_DIR / f"p{page_num:03d}.png"
        if not img_path.exists():
            continue

        progress = page_num - START_PAGE + 1
        if progress % 20 == 0 or progress == 1:
            print(f"  [{progress}/{total}] Processing p{page_num:03d}.png ...")

        blocks = process_page(reader, img_path, page_num)
        if not blocks:
            continue

        first_block = blocks[0]
        lesson_match = LESSON_PATTERN.search(first_block)
        if lesson_match:
            num = int(lesson_match.group(1))
            title = lesson_match.group(2).strip()
            if num not in seen_lessons:
                seen_lessons.add(num)
                lines_out.append("\n---\n\n")
                lines_out.append(f"## 教你炒股票 {num}：{title}\n\n")
                lines_out.append(
                    f"📖 *原始扫描: [p{page_num:03d}.png](images/p{page_num:03d}.png)*\n\n"
                )

        page_text = "\n".join(blocks)
        lines_out.append(page_text + "\n\n")

    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines_out)

    print(f"\nDone! Output: {MD_PATH}")
    print(f"Total pages processed: {total}")
    print(f"Unique lessons found: {len(seen_lessons)}")


if __name__ == "__main__":
    main()
