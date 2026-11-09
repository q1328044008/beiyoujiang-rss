#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成结果自检（仅标准库）：确保每条 item 的正文结构足以被「閲讀 / Legado」直接渲染。

iOS「閲讀」(Yuedu-reader) 的阅读器只有在 feed 自带正文足够「结构化」时才会直接
显示 feed 内容，否则会去抓原帖页做全文提取——而 beiyoujiang.com 的帖子页是
JS 单页应用，抓到的只有「加载中」占位，于是看起来「只有标题」。

它的判据（RSSArticleHTMLSanitizer.hasSubstantialStructuredContent）是：

    blockCount >= 4 且 textCount >= max(260, 摘要长度)
    或 imageCount > 0 且 blockCount >= 2 且 textCount >= 180

blockCount 只统计 p / figure / blockquote / ul / ol / h2 / h3 / footer。
本脚本用同一套阈值检查生成结果，并顺带检查相对链接、镜像页是否齐全。

用法：python check_feed.py docs/feed.xml docs/feed-app.xml
退出码：0 = 无硬错误（短帖只给警告）；1 = 有硬错误。
"""

from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

BLOCK_TAGS = {"p", "figure", "blockquote", "ul", "ol", "h2", "h3", "footer"}
TEXT_TRANSPARENT = {"p", "figure", "blockquote", "ul", "ol", "h2", "h3", "footer",
                    "div", "span", "small", "b", "strong", "em", "i", "a", "li"}


class BlockCounter(HTMLParser):
    """统计结构块数量、文本长度、图片数量。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.blocks: list[dict] = []
        self.images = 0
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "img":
            self.images += 1
            for frame in self.stack:
                frame["has_img"] = True
            return
        if tag in BLOCK_TAGS:
            frame = {"tag": tag, "text": 0, "has_img": False}
            self.stack.append(frame)
            self.blocks.append(frame)

    def handle_endtag(self, tag: str) -> None:
        if tag in BLOCK_TAGS:
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index]["tag"] == tag:
                    frame = self.stack[index]
                    del self.stack[index:]
                    if frame["text"] or frame["has_img"]:
                        frame["text"] = max(frame["text"], 1)
                    break

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        self.text_parts.append(text)
        for frame in self.stack:
            frame["text"] += len(text)


def analyse(fragment: str) -> dict:
    counter = BlockCounter()
    counter.feed(fragment)
    counter.close()
    blocks = len([b for b in counter.blocks if b["text"] or b["has_img"]])
    text_count = len(re.sub(r"\s+", " ", "".join(counter.text_parts)).strip())
    ok = (blocks >= 4 and text_count >= 260) or (
        counter.images > 0 and blocks >= 2 and text_count >= 180
    )
    return {"ok": ok, "blocks": blocks, "images": counter.images, "chars": text_count}


def main(argv: list[str]) -> int:
    paths = argv[1:] or ["docs/feed.xml"]
    hard_errors = 0
    warnings = 0
    for path in paths:
        if not os.path.exists(path):
            print(f"[skip] {path} 不存在")
            continue
        tree = ET.parse(path)
        channel = tree.getroot().find("channel")
        items = channel.findall("item")
        base = os.path.dirname(os.path.abspath(path))

        thin: list[tuple[str, dict]] = []
        total, min_blocks, min_chars = 0, 999, 10**9
        relative_links = 0
        missing_mirrors = 0
        empty_items = 0
        plain_text_feed = True

        for item in items:
            total += 1
            body = item.findtext("description") or ""
            if not body.strip():
                empty_items += 1
                continue
            if "<" in body:
                plain_text_feed = False
            if re.search(r'(?:src|href)="/(?!/)', body):
                relative_links += 1
            info = analyse(body)
            min_blocks = min(min_blocks, info["blocks"])
            min_chars = min(min_chars, info["chars"])
            if not info["ok"]:
                thin.append((item.findtext("title") or "", info))
            link = (item.findtext("link") or "").strip()
            if "/p/" in link and link.endswith(".html"):
                name = link.rsplit("/", 1)[-1]
                if not os.path.exists(os.path.join(base, "p", name)):
                    missing_mirrors += 1

        print(f"\n== {path} ==")
        if plain_text_feed:
            print(f"items={total}  纯文本版（无 HTML 标签，不参与结构检查）  "
                  f"相对链接={relative_links}  缺镜像页={missing_mirrors}  空正文={empty_items}")
            hard_errors += relative_links + missing_mirrors + empty_items
            continue
        print(f"items={total}  结构达标={total - len(thin)}/{total}  最少块数={min_blocks}  "
              f"最少文字={min_chars}  相对链接={relative_links}  缺镜像页={missing_mirrors}  "
              f"空正文={empty_items}")
        for title, info in thin[:15]:
            print(f"  [短帖] {title!r} blocks={info['blocks']} chars={info['chars']} "
                  f"imgs={info['images']}（App 会改抓原帖页，若用 feed-app.xml 则无影响）")

        warnings += len(thin)
        hard_errors += relative_links + missing_mirrors + empty_items

    print(f"\n结果：硬错误 {hard_errors} 处，短帖提醒 {warnings} 条")
    return 0 if hard_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
