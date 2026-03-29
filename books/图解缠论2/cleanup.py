#!/usr/bin/env python3
"""Clean up epub-extracted text. Mainly removes duplicate CIP/copyright blocks."""

import re
from pathlib import Path

BOOK_DIR = Path(__file__).parent
INPUT_PATH = BOOK_DIR / "图解缠论2.md"
OUTPUT_PATH = BOOK_DIR / "图解缠论2.md"

CIP_PATTERNS = [
    re.compile(r"^图书在版编目"),
    re.compile(r"^ISBN"),
    re.compile(r"^Ⅰ\.\s*①"),
    re.compile(r"^中国版本图书馆"),
    re.compile(r"^责任编辑"),
    re.compile(r"^责任审读"),
    re.compile(r"^责任印制"),
    re.compile(r"^封面设计"),
    re.compile(r"^出版发行"),
    re.compile(r"^印\s*刷\s*者"),
    re.compile(r"^经\s*销\s*者"),
    re.compile(r"^开\s+本"),
    re.compile(r"^印\s+张"),
    re.compile(r"^字\s+数"),
    re.compile(r"^版\s+次"),
    re.compile(r"^印\s+次"),
    re.compile(r"^定\s+价"),
    re.compile(r"^广告经营许可证"),
    re.compile(r"^中国经济出版社"),
    re.compile(r"^本版图书如存在"),
    re.compile(r"^版权所有"),
    re.compile(r"^国家版权局"),
    re.compile(r"^服务热线"),
]


def is_cip_line(line):
    stripped = line.strip()
    for pattern in CIP_PATTERNS:
        if pattern.match(stripped):
            return True
    return False


def clean_document(text):
    lines = text.split("\n")
    cleaned = []
    prev_empty = False
    cip_seen = False

    for line in lines:
        stripped = line.strip()

        if is_cip_line(stripped):
            if cip_seen:
                continue
            cip_seen = True

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
    original = len(text.split("\n"))
    cleaned = clean_document(text)
    final = len(cleaned.split("\n"))
    OUTPUT_PATH.write_text(cleaned, encoding="utf-8")
    print(f"Cleanup: {original} → {final} lines (removed {original - final})")


if __name__ == "__main__":
    main()
