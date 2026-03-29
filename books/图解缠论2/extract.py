#!/usr/bin/env python3
"""Extract text and images from 图解缠论2 epub, output as Markdown."""

import os
import re
from pathlib import Path

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

BOOK_DIR = Path(__file__).parent
EPUB_PATH = BOOK_DIR / "《图解缠论2》 [陈秋明].epub"
IMG_DIR = BOOK_DIR / "images"
MD_PATH = BOOK_DIR / "图解缠论2.md"


def extract_images(book):
    """Extract all images and return href->filename mapping."""
    mapping = {}
    idx = 0
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        idx += 1
        content_type = item.media_type
        ext = "png"
        if "jpeg" in content_type or "jpg" in content_type:
            ext = "jpg"
        elif "gif" in content_type:
            ext = "gif"

        fname = f"img_{idx:03d}.{ext}"
        img_path = IMG_DIR / fname
        with open(img_path, "wb") as f:
            f.write(item.get_content())

        href = item.get_name()
        mapping[href] = fname
        base = os.path.basename(href)
        mapping[base] = fname

    return mapping


def html_to_markdown(html_content, img_map, doc_dir=""):
    """Convert HTML content to Markdown with image references."""
    soup = BeautifulSoup(html_content, "html.parser")
    lines = []

    for elem in soup.find_all(["h1", "h2", "h3", "h4", "p", "img", "div"]):
        if elem.name == "img":
            src = elem.get("src", "")
            base = os.path.basename(src)
            fname = img_map.get(base, img_map.get(src, ""))
            if fname:
                alt = elem.get("alt", "插图")
                lines.append(f"![{alt}](images/{fname})\n")
            continue

        if elem.name in ["h1", "h2", "h3", "h4"]:
            level = int(elem.name[1])
            text = elem.get_text().strip()
            if text:
                prefix = "#" * (level + 1)
                lines.append(f"\n{prefix} {text}\n")
            continue

        if elem.name in ["p", "div"]:
            imgs = elem.find_all("img")
            for img in imgs:
                src = img.get("src", "")
                base = os.path.basename(src)
                fname = img_map.get(base, img_map.get(src, ""))
                if fname:
                    alt = img.get("alt", "插图")
                    lines.append(f"![{alt}](images/{fname})\n")

            text = elem.get_text().strip()
            if text:
                lines.append(text + "\n")

    return "\n".join(lines)


def main():
    print("Reading epub...")
    book = epub.read_epub(str(EPUB_PATH))

    print("Extracting images...")
    img_map = extract_images(book)
    print(f"  Extracted {len(img_map) // 2} images")

    print("Extracting text...")
    lines_out = []
    lines_out.append("# 图解缠论2——买卖点逻辑与操作系统\n\n")
    lines_out.append("> **作者**：陈秋明\n")
    lines_out.append("> **系列**：缠中说禅中枢理论系列\n\n")
    lines_out.append("> 本文由 epub 自动提取，图片保存在 `images/` 文件夹中。\n\n")
    lines_out.append("---\n\n")

    docs = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    for doc in docs:
        content = doc.get_content()
        md = html_to_markdown(content, img_map)
        if md.strip():
            lines_out.append(md + "\n\n")

    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines_out)

    total_lines = sum(1 for _ in open(MD_PATH))
    print(f"\nDone! Output: {MD_PATH}")
    print(f"Total lines: {total_lines}")


if __name__ == "__main__":
    main()
