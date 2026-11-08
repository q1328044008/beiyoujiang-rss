#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""杯友酱论坛 (beiyoujiang.com) RSS 生成器

站点是 Next.js 前端 + Express 后端，没有自带 RSS，但数据走几个公开接口：

    POST /api/auth/addTourist          {}                                    注册游客，拿 userId + token
    POST /api/post/getAllPost           {"plate","most","userId","page","pageSize"}   列表
    POST /api/post/getPost              {"postId","userId"}                    帖子详情（正文全文）
    POST /api/comment/getPostComment    {"postId","userId","reply","order","orderType"}  评论

用法示例：
    python generate_feed.py                        # 生成 feed.xml（全部板块，含正文和评论）
    python generate_feed.py --plates 2 --limit 20  # 只看「杯酱论坛」前 20 条
    python generate_feed.py --no-comments          # 不要评论，请求量减半
    python generate_feed.py --out docs/feed.xml

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
API_LIST = f"{BASE_URL}/api/post/getAllPost"
API_POST = f"{BASE_URL}/api/post/getPost"
API_COMMENTS = f"{BASE_URL}/api/comment/getPostComment"
API_GUEST = f"{BASE_URL}/api/auth/addTourist"
POST_URL = f"{BASE_URL}/messageDetail/{{post_id}}"
IMG_URL = f"{BASE_URL}/PostImg/{{filename}}"
SITE_TITLE = "杯友酱 · 玩具论坛"
SITE_DESC = (
    "杯友酱论坛（beiyoujiang.com）最新帖子，含正文与评论。"
    "站点为 18+ 成人玩具交流社区，内容仅面向年满 18 周岁的成年人。"
)

# plate: 1 大型倒模 / 2 杯酱论坛 / 3 杂鱼茶话
PLATES = {1: "大型倒模", 2: "杯酱论坛", 3: "杂鱼茶话"}

# most: 1 按发布时间 / 2 按最后回复
SORT_BY_PUBLISH = 1

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CST = timezone(timedelta(hours=8))

MAX_COMMENTS = 12
MIN_INTERVAL = 0.9   # 两次请求之间的最小间隔，避免触发站点限流
BACKOFF = 25         # 命中 429 后等待多久再重试


class Client:
    """极简接口客户端：处理请求头、重试、游客身份。"""

    def __init__(self, user_agent: str = UA, retries: int = 4):
        self.ua = user_agent
        self.retries = retries
        self.user_id: int | None = None
        self.token: str | None = None
        self._last_at = 0.0

    def _pace(self) -> None:
        gap = time.monotonic() - self._last_at
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        self._last_at = time.monotonic()

    def post(self, url: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "User-Agent": self.ua,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._pace()
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8", "replace")
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise ValueError(f"接口返回非对象: {body[:160]}")
                return parsed
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code == 429:
                    wait = int(exc.headers.get("Retry-After") or 0) or BACKOFF * attempt
                    print(f"[warn] 被限流，等待 {wait}s 后重试 ({attempt}/{self.retries})",
                          file=sys.stderr)
                    time.sleep(wait)
                    continue
                if attempt < self.retries:
                    time.sleep(2 * attempt)
            except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
                last_err = exc
                if attempt < self.retries:
                    time.sleep(2 * attempt)
        print(f"[warn] 请求失败 {url}: {last_err}", file=sys.stderr)
        return {}

    def ensure_guest(self) -> None:
        """详情/评论接口要求 userId，先注册一个游客身份。"""
        res = self.post(API_GUEST, {})
        email = res.get("emailData") or {}
        uid = email.get("id")
        token = email.get("token")
        if uid:
            self.user_id = int(uid)
            self.token = token
            print(f"[info] 游客身份 userId={self.user_id}")
        else:
            print("[warn] 游客注册失败，详情与评论将跳过", file=sys.stderr)


_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_SRC_REL = re.compile(r'(src|href)="/(?![/])')
_STYLE_ATTR = re.compile(r'\sstyle="[^"]*"')


def plain_text(html: str) -> str:
    """把 HTML 片段转成可读纯文本。"""
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
    return _WS.sub(" ", text).strip()


def abs_html(html: str) -> str:
    """正文里的站内相对图片/链接转成绝对地址，顺手去掉内联 style。"""
    if not html:
        return ""
    out = _SRC_REL.sub(r'\1="' + BASE_URL + "/", html)
    out = _STYLE_ATTR.sub("", out)
    return out.replace("]]>", "]]&gt;")


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_time(value: str | None) -> str:
    return f"{parse_time(value).astimezone(CST):%Y-%m-%d %H:%M}"


def plate_name(plate) -> str:
    try:
        return PLATES.get(int(plate), f"板块{plate}")
    except (TypeError, ValueError):
        return "论坛"


def excerpt(text: str, limit: int = 240) -> str:
    text = plain_text(text)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def fetch_page(client: Client, plate: int, page: int, page_size: int) -> list[dict]:
    res = client.post(
        API_LIST,
        {
            "plate": plate,
            "most": SORT_BY_PUBLISH,
            "userId": client.user_id,
            "page": page,
            "pageSize": page_size,
        },
    )
    posts = res.get("data") or []
    return posts if isinstance(posts, list) else []


def fetch_detail(client: Client, post_id: int) -> dict:
    res = client.post(API_POST, {"postId": int(post_id), "userId": client.user_id})
    data = res.get("data") or {}
    return (data.get("post") or {}) if isinstance(data, dict) else {}


def fetch_comments(client: Client, post_id: int) -> list[dict]:
    res = client.post(
        API_COMMENTS,
        {
            "postId": int(post_id),
            "userId": client.user_id,
            "reply": None,
            "order": 1,
            "orderType": "time",
        },
    )
    data = res.get("data") or []
    return data if isinstance(data, list) else []


def render_comment(comment: dict, depth: int = 0) -> list[str]:
    author = (comment.get("author") or {}).get("username") or "匿名"
    body = abs_html(comment.get("content") or "") or plain_text(comment.get("content") or "")
    stamp = local_time(comment.get("createdAt"))
    floor = comment.get("floor")
    head = f"<b>{escape(author)}</b>"
    if floor:
        head += f" <small>#{floor} 楼</small>"
    head += f" <small>{escape(stamp)}</small>"
    lines = [f'<div style="margin:0 0 8px 0">', f"<div>{head}</div>", f"<div>{body}</div>"]
    for reply in (comment.get("replies") or [])[:4]:
        reply_author = (reply.get("author") or {}).get("username") or "匿名"
        reply_body = abs_html(reply.get("content") or "")
        lines.append(
            f'<div style="margin:6px 0 0 16px;opacity:.85">'
            f"<small>回复 {escape(reply_author)}：</small> {reply_body}</div>"
        )
    lines.append("</div>")
    return lines


def normalize_images(value) -> list[str]:
    """接口里 imageUrls 有时是数组，有时是 JSON 字符串，统一成文件名字符串列表。"""
    if not value:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("["):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = [raw]
        else:
            value = [raw]
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            url = item.get("url") or item.get("filename")
            if url:
                names.append(str(url))
    return names


def build_body(post: dict, detail: dict, comments: list[dict], with_comments: bool) -> str:
    parts: list[str] = []

    body_html = abs_html(detail.get("content") or post.get("content") or "")
    if body_html:
        parts.append(f"<p>{body_html}</p>")

    images = normalize_images(detail.get("imageUrls")) or normalize_images(post.get("imageUrls"))
    for name in images[:3]:
        url = IMG_URL.format(filename=urllib.parse.quote(name))
        parts.append(f'<p><img src="{escape(url)}" alt="配图" /></p>')

    author = (post.get("author") or {}).get("username") or "匿名"
    meta = (
        f"板块：{plate_name(post.get('plate'))} ｜ 作者：{author} ｜ "
        f"发布 {local_time(post.get('createdAt'))} ｜ "
        f"浏览 {post.get('readingQuantity', 0)} ｜ 回复 {post.get('commentCount', 0)} ｜ "
        f"点赞 {post.get('likeCount', 0)}"
    )
    parts.append(f"<p><small>{escape(meta)}</small></p>")

    if with_comments and comments:
        parts.append(f"<p><b>—— 评论（{len(comments)}） ——</b></p>")
        for comment in comments[:MAX_COMMENTS]:
            parts.extend(render_comment(comment))

    return "\n".join(parts)


def collect_posts(client: Client, plates: list[int], pages: int, page_size: int, limit: int) -> list[dict]:
    seen: set[int] = set()
    items: list[dict] = []
    for plate in plates:
        for page in range(1, pages + 1):
            posts = fetch_page(client, plate, page, page_size)
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
            time.sleep(1)
    items.sort(key=lambda p: parse_time(p.get("createdAt")), reverse=True)
    return items[:limit]


def enrich(client: Client, posts: list[dict], with_comments: bool) -> list[tuple[dict, dict, list[dict]]]:
    """给每条帖子补正文全文（和评论）。"""
    out: list[tuple[dict, dict, list[dict]]] = []
    if not client.user_id:
        return [(post, {}, []) for post in posts]

    for idx, post in enumerate(posts, 1):
        pid = post.get("id")
        detail = fetch_detail(client, pid)
        count = post.get("commentCount") or post.get("CommentCount") or 0
        comments = fetch_comments(client, pid) if (with_comments and count) else []
        if detail:
            post = {**post, "content": detail.get("content") or post.get("content"),
                    "imageUrls": detail.get("imageUrls") or post.get("imageUrls")}
        out.append((post, detail, comments))
        if idx % 10 == 0:
            print(f"[info] 已补全 {idx}/{len(posts)} 条")
    return out


def build_rss(items: list[tuple[dict, dict, list[dict]]], feed_url: str, with_comments: bool) -> str:
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
    add("    <generator>beiyoujiang-rss</generator>")
    add("    <category>18+</category>")
    if feed_url:
        add(f'    <atom:link href="{escape(feed_url)}" rel="self" type="application/rss+xml"/>')

    for post, detail, comments in items:
        pid = post.get("id")
        link = POST_URL.format(post_id=pid)
        title = (post.get("title") or "").strip() or f"帖子 #{pid}"
        pub = parse_time(post.get("createdAt")).astimezone(CST)
        author = (post.get("author") or {}).get("username") or "匿名"
        html = build_body(post, detail, comments, with_comments)

        add("    <item>")
        add(f"      <title>{escape(title)}</title>")
        add(f"      <link>{escape(link)}</link>")
        add(f'      <guid isPermaLink="true">{escape(link)}</guid>')
        add(f"      <pubDate>{format_datetime(pub)}</pubDate>")
        add(f"      <author>{escape(author)}</author>")
        add(f"      <category>{escape(plate_name(post.get('plate')))}</category>")
        add(f"      <description><![CDATA[{html}]]></description>")
        add(f"      <content:encoded><![CDATA[{html}]]></content:encoded>")
        add("    </item>")

    add("  </channel>")
    add("</rss>")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成杯友酱论坛 RSS（含正文与评论）")
    parser.add_argument("--plates", default="1,2,3",
                        help="板块 ID，逗号分隔。1=大型倒模 2=杯酱论坛 3=杂鱼茶话（默认全部）")
    parser.add_argument("--pages", type=int, default=2, help="每个板块抓几页（默认 2）")
    parser.add_argument("--page-size", type=int, default=20, help="每页条数（默认 20）")
    parser.add_argument("--limit", type=int, default=40, help="最终保留多少条（默认 40）")
    parser.add_argument("--out", default="feed.xml", help="输出文件路径（默认 feed.xml）")
    parser.add_argument("--feed-url", default="", help="填写后写入 atom:self 自引用地址")
    parser.add_argument("--no-comments", action="store_true", help="不抓评论，请求量减半")
    parser.add_argument("--no-detail", action="store_true", help="不抓正文全文，只用列表摘要")
    args = parser.parse_args()

    plates = [int(x) for x in str(args.plates).replace("，", ",").split(",") if x.strip()]
    plates = [p for p in plates if p in PLATES] or list(PLATES)

    client = Client()
    with_comments = not args.no_comments
    with_detail = not args.no_detail
    if with_detail or with_comments:
        client.ensure_guest()

    print(f"[info] 抓取板块: {', '.join(plate_name(p) for p in plates)}")
    posts = collect_posts(client, plates, args.pages, args.page_size, args.limit)
    if not posts:
        print("[error] 没抓到任何帖子，接口可能改过了。", file=sys.stderr)
        return 1
    print(f"[info] 列表 {len(posts)} 条，开始补正文" + ("与评论" if with_comments else ""))

    items = enrich(client, posts, with_comments) if with_detail else [(p, {}, []) for p in posts]

    xml = build_rss(items, args.feed_url, with_comments)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(xml)

    total_comments = sum(len(c) for _, _, c in items)
    newest = parse_time(items[0][0].get("createdAt")).astimezone(CST)
    print(f"[ok] 写入 {args.out}：{len(items)} 条、{total_comments} 条评论，"
          f"最新一条 {newest:%Y-%m-%d %H:%M}（CST）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
