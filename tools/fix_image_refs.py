#!/usr/bin/env python3
"""Fix image references across all books in the repository.

Handles:
1. Books with zero refs (缠论解析): insert page-scan refs at estimated page breaks
2. Books with wrong format (108课详解): fix [link]() to ![image]() format
3. All books: add comprehensive image gallery appendix for orphan images
"""

import os
import re
import glob
from pathlib import Path

BOOKS_DIR = Path(__file__).resolve().parent.parent / "books"


def get_disk_images(book_path):
    """Return sorted set of image filenames on disk."""
    img_dir = book_path / "images"
    if not img_dir.exists():
        return set()
    return {f for f in os.listdir(img_dir)
            if f.lower().endswith(('.png', '.jpg', '.jpeg'))}


def get_md_refs(book_path):
    """Return set of image filenames referenced in all .md files."""
    refs = set()
    for md_file in glob.glob(str(book_path / "**/*.md"), recursive=True):
        with open(md_file, 'r', encoding='utf-8') as fh:
            content = fh.read()
            for m in re.finditer(r'!?\[.*?\]\(images/([^)"]+?)(?:\s+"[^"]*")?\)', content):
                refs.add(m.group(1))
    return refs


def natural_sort_key(s):
    """Sort strings with embedded numbers naturally."""
    return [int(c) if c.isdigit() else c.lower()
            for c in re.split(r'(\d+)', s)]


def fix_108ke_xiangje(book_path):
    """Fix 缠论108课详解: convert link format to image format."""
    md_file = book_path / "缠论108课详解.md"
    if not md_file.exists():
        return 0
    
    with open(md_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Fix: 📖 *原始扫描: [pXXX.png](images/pXXX.png)*
    # To:  📖 *原始扫描:* ![pXXX.png](images/pXXX.png)
    pattern = r'📖 \*原始扫描: \[([^\]]+)\]\((images/[^)]+)\)\*'
    replacement = r'📖 *原始扫描:* ![\1](\2)'
    new_content, count = re.subn(pattern, replacement, content)
    
    if count > 0:
        with open(md_file, 'w', encoding='utf-8') as f:
            f.write(new_content)
    
    return count


def add_page_refs_for_scan_book(book_path, md_filename, img_pattern, total_pages):
    """For OCR-based scan books, add page image refs at estimated page breaks.
    
    Strategy: insert a page-scan reference approximately every N lines,
    matching the page count to total lines.
    """
    md_file = book_path / md_filename
    if not md_file.exists():
        return 0
    
    with open(md_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    disk_imgs = get_disk_images(book_path)
    
    # Get already-referenced images
    content = ''.join(lines)
    existing_refs = set()
    for m in re.finditer(r'!\[.*?\]\(images/([^)"]+?)(?:\s+"[^"]*")?\)', content):
        existing_refs.add(m.group(1))
    
    # Build page-to-image mapping
    page_imgs = {}
    for img in disk_imgs:
        match = re.match(img_pattern, img)
        if match:
            page_num = int(match.group(1))
            if img not in existing_refs:
                page_imgs[page_num] = img
    
    if not page_imgs:
        return 0
    
    # Find header end (skip frontmatter)
    header_end = 0
    for i, line in enumerate(lines):
        if line.strip() == '---' and i > 5:
            header_end = i + 1
            break
    
    content_lines = len(lines) - header_end
    if content_lines <= 0 or total_pages <= 0:
        return 0
    
    lines_per_page = content_lines / total_pages
    
    inserted = 0
    new_lines = []
    for i, line in enumerate(lines):
        new_lines.append(line)
        if i >= header_end:
            est_page = int((i - header_end) / lines_per_page) + 1
            # At double-blank-line boundaries, insert page ref if available
            if (line.strip() == '' and i + 1 < len(lines) and lines[i + 1].strip() == ''
                    and est_page in page_imgs):
                img = page_imgs.pop(est_page)
                new_lines.append(f'\n![📖 第{est_page}页扫描](images/{img})\n')
                inserted += 1
    
    if inserted > 0:
        with open(md_file, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
    
    return inserted


def add_image_gallery(book_path, md_filename):
    """Add comprehensive image gallery appendix to the main md file."""
    md_file = book_path / md_filename
    if not md_file.exists():
        return 0
    
    disk_imgs = get_disk_images(book_path)
    
    # Get all refs from ALL md files in this book
    all_refs = get_md_refs(book_path)
    
    orphans = sorted(disk_imgs - all_refs, key=natural_sort_key)
    
    if not orphans:
        return 0
    
    # Check if gallery already exists
    with open(md_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if '## 附录：完整图片目录' in content:
        return 0
    
    # Build gallery
    gallery_lines = [
        '\n\n---\n\n',
        '## 附录：完整图片目录\n\n',
        '> 以下列出本书 `images/` 目录中所有图片，包含未在正文中内联引用的页面扫描。\n',
        '> AI 可通过 Read 工具查看任意图片：`books/{}/images/<文件名>`\n\n'.format(book_path.name),
    ]
    
    # Group by prefix pattern
    gallery_lines.append(f'共 **{len(disk_imgs)}** 张图片（正文已引用 {len(all_refs)} 张，')
    gallery_lines.append(f'以下补充 {len(orphans)} 张未内联引用的图片）：\n\n')
    
    for img in orphans:
        gallery_lines.append(f'![{img}](images/{img})\n\n')
    
    with open(md_file, 'a', encoding='utf-8') as f:
        f.writelines(gallery_lines)
    
    return len(orphans)


def fix_placeholder_refs():
    """Fix known placeholder references."""
    # 炒股引申的哲理: img_xxx.png placeholder
    md = BOOKS_DIR / "炒股引申的哲理" / "炒股引申的哲理.md"
    if md.exists():
        with open(md, 'r', encoding='utf-8') as f:
            content = f.read()
        content = content.replace('![插图](images/img_xxx.png)', '')
        with open(md, 'w', encoding='utf-8') as f:
            f.write(content)
    
    # 图解缠论2: '...' broken link
    md = BOOKS_DIR / "图解缠论2-买卖点逻辑与操作系统" / "图解缠论2.md"
    if md.exists():
        with open(md, 'r', encoding='utf-8') as f:
            content = f.read()
        content = content.replace('![...](images/...)', '')
        with open(md, 'w', encoding='utf-8') as f:
            f.write(content)


def main():
    print("=== Fixing image references across all books ===\n")
    
    # 1. Fix 缠论108课详解 link format
    book = BOOKS_DIR / "缠论108课详解"
    count = fix_108ke_xiangje(book)
    print(f"[缠论108课详解] Fixed {count} link→image format conversions")
    
    # 2. Fix placeholder refs
    fix_placeholder_refs()
    print("[小修复] Fixed placeholder refs (img_xxx.png, ...)")
    
    # 3. Add image galleries for all books with orphans
    books_config = [
        ("缠论解析", "缠论解析.md"),
        ("缠论108课详解", "缠论108课详解.md"),
        ("图解缠论-核心理论推导与实战演示", "图解缠论.md"),
        ("缠论辅导", "缠论辅导.md"),
        ("缠论-土匪整理版", "土匪注解版缠中说禅全集.md"),
        ("图解缠论2-买卖点逻辑与操作系统", "图解缠论2.md"),
        ("图解缠论3-技术面、基本面、比价轮动的立体操盘", "图解缠论3.md"),
        ("缠论108课", "缠中说禅108课.md"),
        ("炒股引申的哲理", "炒股引申的哲理.md"),
    ]
    
    for book_name, md_name in books_config:
        book_path = BOOKS_DIR / book_name
        if not book_path.exists():
            print(f"[{book_name}] SKIP - directory not found")
            continue
        
        count = add_image_gallery(book_path, md_name)
        if count > 0:
            print(f"[{book_name}] Added gallery with {count} orphan images")
        else:
            print(f"[{book_name}] No orphan images - OK")


if __name__ == "__main__":
    main()
