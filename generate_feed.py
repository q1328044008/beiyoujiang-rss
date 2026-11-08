#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""杯友酱论坛 (beiyoujiang.com) RSS 生成器

站点是 Next.js 前端 + Express 后端，没有自带 RSS，但列表数据走公开接口：
    POST https://beiyoujiang.com/api/post/getAllPost
    body: {"plate": 板块ID, "most": 排序, "userId": null, "page": n, "pageSize": m}

用法示例：
    python generate_feed.py                       # 生成 feed.xml（默认全部板块）
    python generate_feed.py --plates 2 --pages 2  # 只看「杯酱论坛」前 2 页
    python generate_feed.py --out docs/feed.xml --limit 40

只依赖 Python 标准库，方便在 GitHub Actions 里零依赖运行。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

BASE_URL = "https://beiyoujiang.com"
API_URL = f"{BASE_URL}/api/post/getAllPost"
POST_URL = f"{BASE_URL}/messageDetail/{{post_id}}"
IMG_URL = f"{BASE_URL}/PostImg/{{filename}}"
SITE_TITLE = "杯友酱 · 玩具论坛"
SITE_DESC = (
    "杯友酱论坛（beiyoujiang.com）最新帖子。"
    "站点为 18+ 成人玩具交流社区，内容仅面向年满 18 周岁的成年人。"
)

# plate: 1 大型倒模 / 2 杯酱论坛 / 3 杂鱼茶话
PLATES = {
    1: "大型倒模",
    2: "杯酱论坛",
    3: "杂鱼茶话",
}

# most: 1 按发布时间 / 2 按最后回复
SORT_BY_PUBLISH = 1

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CST = timezone(timedelta(hours=8))


def fetch_page(plate: int, page: int, page_size: int, retries: int = 3) -> list[dict]:
    """取某个板块的第 page 页帖子列表。"""
    payload = json.dumps(
        {
            "plate": plate,
            "most": SORT_BY_PUBLISH,
            "userId": None,
            "page": page,
            "pageSize": page_size,
        }
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": UA,
    }

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                API_URL, data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
            data = json.loads(body)
            posts = data.get("data") or []
            if not isinstance(posts, list):
                raise ValueError(f"接口返回结构异常: {body[:200]}")
            return posts
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    print(f"[warn] plate={plate} page={page} 抓取失败: {last_err}", file=sys.stderr)
    return []


_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def plain_text(html: str) -> str:
    """把帖子内容里的 HTML 标签去掉，保留可读文本。"""
    if not html:
        return ""
    text = _HTML_TAG.sub(" ", html)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = _WS.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_time(value: str | None) -> datetime:
    """接口时间形如 2026-11-07T19:58:23.368Z（UTC）。"""
    if not value:
        return datetime.now(timezone.utc)
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def excerpt(text: str, limit: int = 240) -> str:
    text = plain_text(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def plate_name(plate) -> str:
    try:
        return PLATES.get(int(plate), f"板块{plate}")
    except (TypeError, ValueError):
        return "论坛"


def build_description(post: dict) -> str:
    """item 的 description：HTML 片段（CDATA 包裹）。"""
    parts: list[str] = []
    body = excerpt(post.get("content") or "")
    if body:
        parts.append(f"<p>{escape(body)}</p>")

    images = post.get("imageUrls") or []
    for name in images[:2]:
        if not name:
            continue
        url = IMG_URL.format(filename=urllib.parse.quote(str(name)))
        parts.append(f'<p><img src="{escape(url)}" alt="" /></p>')

    author = (post.get("author") or {}).get("username") or "匿名"
    meta = (
        f"板块：{plate_name(post.get('plate'))} ｜ 作者：{author} ｜ "
        f"浏览 {post.get('readingQuantity', 0)} ｜ 回复 {post.get('commentCount', 0)} ｜ "
        f"点赞 {post.get('likeCount', 0)}"
    )
    parts.append(f"<p><small>{escape(meta)}</small></p>")
    return "\n".join(parts)


def collect_posts(plates: list[int], pages: int, page_size: int, limit: int) -> list[dict]:
    seen: set[int] = set()
    items: list[dict] = []
    for plate in plates:
        for page in range(1, pages + 1):
            posts = fetch_page(plate, page, page_size)
            if not posts:
                break
            for post in posts:
                pid = post.get("id")
                if pid is None or pid in seen:
                    continue
                if post.get("isHidden") or post.get("published") is False:
                    continue
                seen.add(pid)
                items.append(post)
            if len(posts) < page_size:
                break
            time.sleep(1)  # 轻一点，别给站点压力
    items.sort(key=lambda p: parse_time(p.get("createdAt")), reverse=True)
    return items[:limit]


def build_rss(items: list[dict], feed_url: str) -> str:
    now = datetime.now(timezone.utc).astimezone(CST)
    lines: list[str] = []
    add = lines.append

    add('<?xml version="1.0" encoding="UTF-8"?>')
    add('<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">')
    add("  <channel>")
    add(f"    <title>{escape(SITE_TITLE)}</title>")
    add(f"    <link>{escape(BASE_URL)}/</link>")
    add(f"    <description>{escape(SITE_DESC)}</description>")
    add("    <language>zh-cn</language>")
    add(f"    <lastBuildDate>{format_datetime(now)}</lastBuildDate>")
    add(f"    <generator>beiyoujiang-rss</generator>")
    add("    <category>18+</category>")
    if feed_url:
        add(f'    <atom:link href="{escape(feed_url)}" rel="self" type="application/rss+xml"/>')

    for post in items:
        pid = post.get("id")
        link = POST_URL.format(post_id=pid)
        title = (post.get("title") or "").strip() or f"帖子 #{pid}"
        pub = parse_time(post.get("createdAt")).astimezone(CST)
        desc = build_description(post)
        author = (post.get("author") or {}).get("username") or "匿名"

        add("    <item>")
        add(f"      <title>{escape(title)}</title>")
        add(f"      <link>{escape(link)}</link>")
        add(f'      <guid isPermaLink="true">{escape(link)}</guid>')
        add(f"      <pubDate>{format_datetime(pub)}</pubDate>")
        add(f"      <author>{escape(author)}</author>")
        add(f"      <category>{escape(plate_name(post.get('plate')))}</category>")
        add(f"      <description><![CDATA[{desc}]]></description>")
        add("    </item>")

    add("  </channel>")
    add("</rss>")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成杯友酱论坛 RSS")
    parser.add_argument(
        "--plates",
        default="1,2,3",
        help="板块 ID，逗号分隔。1=大型倒模 2=杯酱论坛 3=杂鱼茶话（默认全部）",
    )
    parser.add_argument("--pages", type=int, default=2, help="每个板块抓几页（默认 2）")
    parser.add_argument("--page-size", type=int, default=20, help="每页条数（默认 20）")
    parser.add_argument("--limit", type=int, default=40, help="最终保留多少条（默认 40）")
    parser.add_argument("--out", default="feed.xml", help="输出文件路径（默认 feed.xml）")
    parser.add_argument("--feed-url", default="", help="填写后写入 atom:self 自引用地址")
    args = parser.parse_args()

    plates = [int(x) for x in str(args.plates).replace("，", ",").split(",") if x.strip()]
    plates = [p for p in plates if p in PLATES] or list(PLATES)

    print(f"[info] 抓取板块: {', '.join(plate_name(p) for p in plates)}")
    items = collect_posts(plates, args.pages, args.page_size, args.limit)
    if not items:
        print("[error] 没抓到任何帖子，接口可能改过了。", file=sys.stderr)
        return 1

    xml = build_rss(items, args.feed_url)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(xml)

    newest = parse_time(items[0].get("createdAt")).astimezone(CST)
    print(f"[ok] 写入 {args.out}：{len(items)} 条，最新一条 {newest:%Y-%m-%d %H:%M}（CST）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
