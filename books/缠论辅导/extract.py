#!/usr/bin/env python3
"""Extract text and images from 缠论辅导(一)(二) PDFs, output as single Markdown.

Both volumes are companion study guides with annotations for the 108 lessons.
Text is extractable with pdfplumber but has doubled characters from bold rendering.
"""

import re
import subprocess
from pathlib import Path

import pdfplumber

BOOK_DIR = Path(__file__).parent
IMG_DIR = BOOK_DIR / "images"
MD_PATH = BOOK_DIR / "缠论辅导.md"

PDFS = [
    (BOOK_DIR / "缠论辅导(一).pdf", "vol1-img"),
    (BOOK_DIR / "缠论辅导(二).pdf", "vol2-img"),
]

PAGE_HEADER_PATTERNS = [
    re.compile(r"^\s*-\s*\d+\s*-\s*$"),
    re.compile(r"page\s*\d+\s*of\s*\d+", re.IGNORECASE),
]


def build_page_images():
    """Build page→image file mapping from already-extracted images."""
    page_files = {}
    for f in sorted(IMG_DIR.glob("vol*-img-*")):
        m = re.match(r"(vol\d)-img-(\d+)", f.stem)
        if not m:
            continue
        vol = m.group(1)
        seq = int(m.group(2))
        page_files.setdefault((vol, seq), f.name)
    return page_files


def rename_images():
    """Rename vol*-img-NNN.ext → vol*-pNNN.ext for simpler referencing."""
    renamed = {}
    for f in sorted(IMG_DIR.glob("vol*-img-*")):
        m = re.match(r"(vol\d)-img-(\d+)", f.stem)
        if not m:
            continue
        vol = m.group(1)
        seq = int(m.group(2))
        ext = f.suffix.lower()
        new_name = f"{vol}-p{seq:03d}{ext}"
        new_path = IMG_DIR / new_name
        if not new_path.exists():
            f.rename(new_path)
        renamed[(vol, seq)] = new_name
    already = sorted(IMG_DIR.glob("vol*-p*"))
    for f in already:
        m = re.match(r"(vol\d)-p(\d+)", f.stem)
        if m:
            vol = m.group(1)
            seq = int(m.group(2))
            renamed[(vol, seq)] = f.name
    return renamed


def is_page_header(line):
    stripped = line.strip()
    for pattern in PAGE_HEADER_PATTERNS:
        if pattern.search(stripped):
            return True
    if stripped.isdigit() and stripped.isascii() and 1 <= int(stripped) <= 200:
        return True
    return False


def clean_text(text):
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        if is_page_header(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def extract_volume(pdf_path, vol_label, renamed, lines_out, page_offset=0):
    """Extract one volume and append to lines_out."""
    # Build page image mapping for this volume using pdfimages -list
    result = subprocess.run(
        ["pdfimages", "-list", str(pdf_path)],
        capture_output=True, text=True
    )
    seq_to_page = {}
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            page_num = int(parts[0])
            seq_num = int(parts[1])
            seq_to_page[seq_num] = page_num

    page_images = {}
    for (vol, seq), fname in renamed.items():
        if vol == vol_label:
            page_num = seq_to_page.get(seq, 0)
            page_images.setdefault(page_num, []).append(fname)
    for p in page_images:
        page_images[p].sort()

    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        for page_idx, page in enumerate(pdf.pages):
            page_num = page_idx + 1
            if page_num % 50 == 0:
                print(f"  [{vol_label}] [{page_num}/{total}] ...")

            text = page.extract_text() or ""
            if not text.strip():
                continue

            text = clean_text(text)
            if not text.strip():
                continue

            img_files = page_images.get(page_num, [])

            # Detect section headers (8x repeated chars)
            first_line = text.split("\n")[0].strip() if text.strip() else ""
            is_section = bool(re.match(r"^(.)\1{5,}", first_line))

            if is_section:
                decoded = ""
                i = 0
                while i < len(first_line):
                    c = first_line[i]
                    count = 1
                    while i + count < len(first_line) and first_line[i + count] == c:
                        count += 1
                    decoded += c
                    i += count
                lines_out.append(f"\n---\n\n## {decoded}\n\n")
                rest_lines = text.split("\n")[1:]
                rest = "\n".join(rest_lines).strip()
                if rest:
                    lines_out.append(rest + "\n\n")
            else:
                lines_out.append(text + "\n\n")

            for fname in img_files:
                lines_out.append(f"![{vol_label}第{page_num}页插图](images/{fname})\n\n")

    return total


def main():
    print("Step 1: Renaming images...")
    renamed = rename_images()
    print(f"  {len(renamed)} images processed")

    print("Step 2: Extracting text...")
    lines_out = []
    lines_out.append("# 缠论辅导（一）（二）合集\n\n")
    lines_out.append("> 学习笔记与辅导注解，覆盖「教你炒股票」108课的学习要点。\n")
    lines_out.append("> 本文由 PDF 自动提取，图片保存在 `images/` 文件夹中。\n\n")
    lines_out.append("---\n\n")

    lines_out.append("# 第一卷：缠论辅导（一）\n\n")
    p1 = extract_volume(PDFS[0][0], "vol1", renamed, lines_out)

    lines_out.append("\n---\n\n# 第二卷：缠论辅导（二）\n\n")
    p2 = extract_volume(PDFS[1][0], "vol2", renamed, lines_out, page_offset=p1)

    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines_out)

    print(f"\nDone! Output: {MD_PATH}")
    print(f"Total pages: {p1} + {p2} = {p1 + p2}")
    print(f"Images referenced: {len(renamed)}")


if __name__ == "__main__":
    main()
