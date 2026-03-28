#!/usr/bin/env python3
"""Extract text and images from 缠中说禅 108课 PDF, output as Markdown with image links."""

import os
import re
import subprocess
from pathlib import Path

import pdfplumber
from PIL import Image

PDF_PATH = "/home/chenbohan/Downloads/缠中说禅：108课文字插图版 (baymax是超人) (Z-Library)-1.pdf"
OUT_DIR = Path("/home/chenbohan/Documents/gp/缠论108课")
IMG_DIR = OUT_DIR / "images"
MD_PATH = OUT_DIR / "缠中说禅108课.md"

IMG_DIR.mkdir(parents=True, exist_ok=True)


def parse_pdfimages_list():
    """Run pdfimages -list and parse output to get page -> image mapping."""
    result = subprocess.run(
        ["pdfimages", "-list", PDF_PATH],
        capture_output=True, text=True
    )
    mapping = {}  # seq_num -> page_num (1-based)
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            page_num = int(parts[0])
            seq_num = int(parts[1])
            mapping[seq_num] = page_num
    return mapping


def convert_and_rename_images(mapping):
    """Convert PPM->PNG, rename all images to p{page}_{idx}.{ext} format."""
    raw_files = sorted(IMG_DIR.glob("img-*"))
    page_counters = {}
    renamed = {}  # seq_num -> new_filename

    for f in raw_files:
        seq_str = f.stem.replace("img-", "")
        seq_num = int(seq_str)
        page_num = mapping.get(seq_num, 0)

        page_counters.setdefault(page_num, 0)
        page_counters[page_num] += 1
        idx = page_counters[page_num]

        if f.suffix.lower() in (".ppm", ".pbm", ".pgm"):
            new_name = f"p{page_num:03d}_{idx}.png"
            new_path = IMG_DIR / new_name
            img = Image.open(f)
            img.save(new_path, "PNG")
            f.unlink()
        else:
            ext = f.suffix.lower()
            new_name = f"p{page_num:03d}_{idx}{ext}"
            new_path = IMG_DIR / new_name
            f.rename(new_path)

        renamed[seq_num] = new_name

    return renamed


def extract_text_and_images():
    """Extract text page by page, interleaving image references by position."""
    mapping = parse_pdfimages_list()
    renamed = convert_and_rename_images(mapping)

    # Build page -> list of image filenames (ordered by position on page)
    page_images = {}
    for seq_num, page_num in mapping.items():
        page_images.setdefault(page_num, []).append(renamed.get(seq_num, f"img-{seq_num:03d}"))
    for page_num in page_images:
        page_images[page_num].sort()

    lines_out = []
    lines_out.append("# 缠中说禅：教你炒股票 108 课（文字插图版）\n\n")
    lines_out.append("> 本文由 PDF 自动提取，图片保存在 `images/` 文件夹中。\n\n")
    lines_out.append("---\n\n")

    with pdfplumber.open(PDF_PATH) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            page_num = page_idx + 1
            text = page.extract_text() or ""
            imgs_on_page = page.images
            img_files = page_images.get(page_num, [])

            if not text.strip() and not img_files:
                continue

            # Get image vertical positions for interleaving
            img_positions = []
            for i, img_meta in enumerate(imgs_on_page):
                fname = img_files[i] if i < len(img_files) else None
                if fname:
                    img_positions.append((img_meta["top"], fname))

            if not imgs_on_page:
                # Pure text page
                lines_out.append(text + "\n\n")
            else:
                # Page with images - need to interleave
                text_lines = text.split("\n") if text.strip() else []

                if not text_lines:
                    # Image-only page (or minimal caption)
                    for _, fname in sorted(img_positions):
                        desc = _guess_image_desc(fname, page_num)
                        lines_out.append(f"![{desc}](images/{fname})\n\n")
                    if text.strip():
                        lines_out.append(text.strip() + "\n\n")
                else:
                    page_height = page.height
                    # Estimate text line positions based on even distribution
                    n_lines = len(text_lines)

                    # Get actual text character positions to determine where images go
                    chars = page.chars
                    if chars:
                        text_top = min(c["top"] for c in chars)
                        text_bottom = max(c["bottom"] for c in chars)
                    else:
                        text_top = 50
                        text_bottom = page_height - 50

                    # For each image, figure out which text line it comes after
                    img_insert_points = []
                    for img_top, fname in sorted(img_positions):
                        if n_lines <= 1:
                            line_idx = 0
                        else:
                            ratio = (img_top - text_top) / max(text_bottom - text_top, 1)
                            ratio = max(0, min(1, ratio))
                            line_idx = int(ratio * n_lines)
                        img_insert_points.append((line_idx, fname))

                    # Build output with images inserted
                    inserted = set()
                    for line_idx, text_line in enumerate(text_lines):
                        # Check if any images should be inserted before this line
                        for insert_idx, fname in img_insert_points:
                            if insert_idx <= line_idx and fname not in inserted:
                                desc = _guess_image_desc(fname, page_num)
                                lines_out.append(f"\n![{desc}](images/{fname})\n\n")
                                inserted.add(fname)
                        lines_out.append(text_line + "\n")

                    # Insert remaining images at the end
                    for _, fname in img_insert_points:
                        if fname not in inserted:
                            desc = _guess_image_desc(fname, page_num)
                            lines_out.append(f"\n![{desc}](images/{fname})\n\n")

                    lines_out.append("\n")

            # Detect lesson boundaries and add separators
            if _is_lesson_start(text):
                lines_out.insert(-1, "\n---\n\n")

    # Write output
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines_out)

    print(f"Done! Output: {MD_PATH}")
    print(f"Images: {IMG_DIR}/ ({len(list(IMG_DIR.glob('p*')))} files)")


def _guess_image_desc(fname, page_num):
    """Generate a brief image description based on filename and page."""
    return f"第{page_num}页插图-{fname}"


def _is_lesson_start(text):
    """Check if this page starts a new lesson."""
    if not text:
        return False
    first_line = text.split("\n")[0].strip()
    return bool(re.match(r"(教你炒股票\s*\d+|股市闲谈)", first_line))


if __name__ == "__main__":
    extract_text_and_images()
