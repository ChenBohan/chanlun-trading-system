#!/usr/bin/env python3
"""Extract text and images from 土匪注解版缠中说禅全集 PDF, output as Markdown.

This is a 1208-page annotated version of the "教你炒股票" 108 lessons.
Text is directly extractable with pdfplumber. Images are stock chart thumbnails.
"""

import re
import subprocess
from pathlib import Path

import pdfplumber

BOOK_DIR = Path(__file__).parent
PDF_PATH = BOOK_DIR / "土匪注解版缠中说禅全集.pdf"
IMG_DIR = BOOK_DIR / "images"
MD_PATH = BOOK_DIR / "土匪注解版缠中说禅全集.md"

LESSON_PATTERN = re.compile(
    r"教你炒股票\s*(\d+)\s*[：:]\s*(.+?)(?:\(|（)(\d{4})"
)

PAGE_HEADER_PATTERNS = [
    re.compile(r"教你炒股票\s*\d+\s*[：:].+?page\s*\d+\s*of\s*\d+", re.IGNORECASE),
    re.compile(r"第\d+页[，,]共\d+页"),
    re.compile(r"page\s*\d+\s*of\s*\d+", re.IGNORECASE),
]

FRONT_MATTER_END = 56


def parse_pdfimages_list():
    """Build seq_num -> page_num mapping from pdfimages -list."""
    result = subprocess.run(
        ["pdfimages", "-list", str(PDF_PATH)],
        capture_output=True, text=True
    )
    mapping = {}
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            page_num = int(parts[0])
            seq_num = int(parts[1])
            mapping[seq_num] = page_num
    return mapping


def rename_images(mapping):
    """Rename remaining img-NNN.{ext} files to p{page}_{idx}.{ext} format.
    If files are already renamed (p*.{ext}), build the mapping from existing files.
    """
    raw_files = sorted(IMG_DIR.glob("img-*"))

    if raw_files:
        page_counters = {}
        renamed = {}
        for f in raw_files:
            seq_str = f.stem.replace("img-", "")
            try:
                seq_num = int(seq_str)
            except ValueError:
                continue

            page_num = mapping.get(seq_num, 0)
            page_counters.setdefault(page_num, 0)
            page_counters[page_num] += 1
            idx = page_counters[page_num]

            ext = f.suffix.lower()
            new_name = f"p{page_num:04d}_{idx}{ext}"
            new_path = IMG_DIR / new_name
            f.rename(new_path)
            renamed[seq_num] = new_name
        return renamed

    # Already renamed - build page_images directly from existing p*.* files
    existing = sorted(IMG_DIR.glob("p*.*"))
    page_files = {}
    for f in existing:
        m = re.match(r"p(\d+)_(\d+)", f.stem)
        if m:
            page_num = int(m.group(1))
            page_files.setdefault(page_num, []).append(f.name)
    for page_num in page_files:
        page_files[page_num].sort()
    return page_files


def is_page_header(line):
    """Check if a line is a pagination header/footer."""
    stripped = line.strip()
    for pattern in PAGE_HEADER_PATTERNS:
        if pattern.search(stripped):
            return True
    if stripped.isdigit() and 1 <= int(stripped) <= 1300:
        return True
    return False


def clean_text(text):
    """Basic text cleanup for a single page."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        if is_page_header(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def detect_lesson_start(text):
    """Detect if this page starts a new lesson. Returns (number, title, year) or None."""
    if not text:
        return None
    first_300 = text[:300]
    m = LESSON_PATTERN.search(first_300)
    if m:
        return int(m.group(1)), m.group(2).strip(), m.group(3)
    return None


def main():
    print("Step 1: Parsing pdfimages list...")
    mapping = parse_pdfimages_list()
    print(f"  Found {len(mapping)} image entries")

    print("Step 2: Checking/renaming images...")
    result = rename_images(mapping)

    if isinstance(result, dict) and result and isinstance(next(iter(result.values())), list):
        page_images = result
        total_imgs = sum(len(v) for v in page_images.values())
        print(f"  Found {total_imgs} already-renamed images")
    else:
        renamed = result
        print(f"  Renamed {len(renamed)} images")
        page_images = {}
        for seq_num, page_num in mapping.items():
            if seq_num in renamed:
                page_images.setdefault(page_num, []).append(renamed[seq_num])
        for page_num in page_images:
            page_images[page_num].sort()

    print("Step 3: Extracting text from PDF...")
    lines_out = []
    lines_out.append("# 土匪注解版缠中说禅全集\n\n")
    lines_out.append("> **编者**：土匪\n")
    lines_out.append("> **内容**：缠中说禅「教你炒股票」108课完整注解版\n\n")
    lines_out.append("> 本文由 PDF 自动提取，图片保存在 `images/` 文件夹中。\n")
    lines_out.append("> 包含原文、评论精选和土匪注解（标记为「匪注」）。\n\n")
    lines_out.append("---\n\n")

    seen_lessons = set()

    with pdfplumber.open(str(PDF_PATH)) as pdf:
        total = len(pdf.pages)
        for page_idx, page in enumerate(pdf.pages):
            page_num = page_idx + 1
            if page_num % 100 == 0:
                print(f"  [{page_num}/{total}] ...")

            text = page.extract_text() or ""
            if not text.strip():
                continue

            text = clean_text(text)
            if not text.strip():
                continue

            img_files = page_images.get(page_num, [])

            if page_num <= FRONT_MATTER_END:
                if page_num == 1:
                    continue
                lines_out.append(text + "\n\n")
                for fname in img_files:
                    lines_out.append(f"![第{page_num}页插图](images/{fname})\n\n")
                continue

            lesson_info = detect_lesson_start(text)
            if lesson_info:
                num, title, year = lesson_info
                if num not in seen_lessons:
                    seen_lessons.add(num)
                    lines_out.append("\n---\n\n")
                    lines_out.append(f"## 教你炒股票 {num}：{title}（{year}）\n\n")

            lines_out.append(text + "\n\n")

            for fname in img_files:
                lines_out.append(f"![第{page_num}页插图](images/{fname})\n\n")

    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines_out)

    print(f"\nDone! Output: {MD_PATH}")
    print(f"Total pages processed: {total}")
    print(f"Unique lessons found: {len(seen_lessons)}")
    print(f"Images referenced: {sum(len(v) for v in page_images.values())}")


if __name__ == "__main__":
    main()
