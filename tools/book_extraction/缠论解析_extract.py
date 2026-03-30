#!/usr/bin/env python3
"""Extract text from scanned PDF pages using EasyOCR, output as Markdown.

This PDF is image-based (scanned book), so we use OCR instead of pdfplumber.
Each page has been pre-extracted as p{NNN}.jpg in images/ directory.
"""

import os
import re
import sys
from pathlib import Path

import easyocr

BOOK_DIR = Path(__file__).parent
IMG_DIR = BOOK_DIR / "images"
MD_PATH = BOOK_DIR / "缠论解析.md"

WATERMARK_PATTERNS = [
    r"www\.?\s*macd\.?\s*cn",
    r"广[州燮]\s*马?后?炮?\s*制作",
    r"广燮.*?制",
    r"www.*?macd.*?cn",
    r"WTT.*?macd",
]

CHAPTER_PATTERNS = [
    r"第[一二三四五六七八九十]+章",
    r"^前\s*言$",
    r"^结\s*语$",
    r"^目\s*录$",
]

SECTION_PATTERNS = [
    r"^[一二三四五六七八九十]+[、．.]",
]

START_PAGE = 4   # Skip cover(1), title(2), CIP(3)
END_PAGE = 160


def clean_watermark(text):
    for pattern in WATERMARK_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text.strip()


def is_chapter_start(line):
    for pattern in CHAPTER_PATTERNS:
        if re.search(pattern, line.strip()):
            return True
    return False


def is_section_start(line):
    for pattern in SECTION_PATTERNS:
        if re.search(pattern, line.strip()):
            return True
    return False


def process_page(reader, img_path, page_num):
    """OCR a single page and return cleaned text lines."""
    results = reader.readtext(str(img_path), detail=0, paragraph=True)

    cleaned_lines = []
    for block in results:
        block = clean_watermark(block)
        if not block:
            continue
        # Skip standalone page numbers
        stripped = block.strip()
        if stripped.isdigit() and 1 <= int(stripped) <= 200:
            continue
        if re.match(r"^[·.。]\s*\d+\s*[·.。]$", stripped):
            continue
        cleaned_lines.append(block)

    return cleaned_lines


def main():
    print("Initializing EasyOCR reader (this may take a moment)...")
    reader = easyocr.Reader(["ch_sim", "en"], gpu=False)

    all_output = []
    all_output.append("# 缠论解析——缠中说禅技术理论图解\n\n")
    all_output.append("> **作者**：江南小隐\n")
    all_output.append("> **出版社**：中国宇航出版社，2012年7月\n\n")
    all_output.append("> 本文由扫描版 PDF 通过 EasyOCR 自动提取，图片保存在 `images/` 文件夹中。\n")
    all_output.append("> 每页原始扫描件以 `p{页码}.jpg` 命名，可对照查看。\n\n")
    all_output.append("---\n\n")

    total = END_PAGE - START_PAGE + 1
    for page_num in range(START_PAGE, END_PAGE + 1):
        img_path = IMG_DIR / f"p{page_num:03d}.jpg"
        if not img_path.exists():
            print(f"  [SKIP] p{page_num:03d}.jpg not found")
            continue

        progress = page_num - START_PAGE + 1
        print(f"  [{progress}/{total}] Processing p{page_num:03d}.jpg ...", end="", flush=True)

        lines = process_page(reader, img_path, page_num)
        if not lines:
            print(" (empty)")
            continue

        page_text = "\n".join(lines)

        # Detect chapter/section boundaries
        first_line = lines[0] if lines else ""
        if is_chapter_start(first_line):
            all_output.append("\n---\n\n")
            all_output.append(f"## {first_line.strip()}\n\n")
            all_output.append(f"📖 *原始扫描: [p{page_num:03d}.jpg](images/p{page_num:03d}.jpg)*\n\n")
            remaining = "\n".join(lines[1:])
            if remaining.strip():
                all_output.append(remaining + "\n\n")
        else:
            has_section = False
            for line in lines:
                if is_section_start(line):
                    has_section = True
                    break

            if has_section:
                all_output.append(f"📖 *原始扫描: [p{page_num:03d}.jpg](images/p{page_num:03d}.jpg)*\n\n")

            all_output.append(page_text + "\n\n")

        print(f" OK ({len(lines)} blocks)")

    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.writelines(all_output)

    print(f"\nDone! Output: {MD_PATH}")
    print(f"Total pages processed: {total}")


if __name__ == "__main__":
    main()
