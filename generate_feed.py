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
    python generate_feed.py --out docs/feed.xml --text-out docs/feed-text.xml \\
        --app-out docs/feed-app.xml --mirror-dir docs/p \\
        --feed-url https://example.github.io/repo/feed.xml

关于 iOS「閲讀」（Yuedu-reader）：
    该 App 默认的 Reader 模式会去抓 item 的 link 页面做「全文提取」。
    而 beiyoujiang.com 的帖子页是纯前端渲染，抓到的只有「加载中」占位文字，
    于是正文被这段占位覆盖，看起来「只有标题」。解决方式有两层：
      1. 把正文/评论改成结构良好的 <p> 块，让 App 判定「feed 自带全文足够」
         从而不再抓原帖（见 is_substantial 的阈值）；
      2. 额外为每篇帖子生成一个静态镜像页（docs/p/<id>.html），并把
         feed-app.xml 的 link 指向镜像页，这样即使 App 坚持抓全文，
         抓到的也是真实正文与评论。

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
from html import escape, unescape

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
MAX_IMAGES = 6
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


# ---------------------------------------------------------------------------
# HTML -> 结构化块
# ---------------------------------------------------------------------------

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_BR = re.compile(r"<br\s*/?>", re.I)
_BLOCK_OPEN = re.compile(r"<(?:p|div|li|h[1-6]|blockquote|section|article|figure|tr)\b[^>]*>", re.I)
_BLOCK_END = re.compile(r"</(?:p|div|li|h[1-6]|blockquote|section|article|figure|tr)>", re.I)
_LI_OPEN = re.compile(r"<li[^>]*>", re.I)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_KEEP_TAG = re.compile(r"</?(?:a|img|b|strong|em|i)\b[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_BLANK = re.compile(r"\n{3,}")
# 站内相对链接（/images/xxx.jpg、/messageDetail/1）补成绝对地址
_REL_ATTR = re.compile(r'(?P<attr>\b(?:src|href)\s*=\s*")(?P<url>/(?!/)[^"]*)(?P<end>")', re.I)


def _absolutize_attrs(fragment: str) -> str:
    return _REL_ATTR.sub(lambda m: m.group("attr") + BASE_URL + m.group("url") + m.group("end"),
                         fragment)

# 这些标签算「结构块」，iOS「閲讀」用它判断 feed 是否自带完整正文
_BLOCK_SELECTOR = ("p", "figure", "blockquote", "ul", "ol", "h2", "h3", "footer")


def _strip_tags(text: str) -> str:
    return _ANY_TAG.sub("", text)


def html_fragments(html: str) -> list[str]:
    """把论坛里的富文本拆成若干个结构良好的 <p> 块。

    论坛正文是 <div>/<br>/<img> 混排；App 的阅读器只有在正文有足够多的
    <p> 级块时才会直接使用 feed 内容（否则会去抓原帖页做全文提取，
    原帖页是 JS 单页应用，抓到的只有加载占位）。所以这里统一转成
    一段一 <p>、一图一 <p> 的结构。
    """
    if not html:
        return []

    text = _SCRIPT_STYLE.sub("", html)
    text = text.replace("]]>", "]]&gt;")
    text = _BR.sub("\n", text)
    text = _BLOCK_OPEN.sub("\n", text)
    text = _BLOCK_END.sub("\n", text)
    text = _LI_OPEN.sub("• ", text)

    # 只保留行内标签，其余（div/span/font/自定义组件…）一律去掉。
    # 用占位符先保护需要保留的标签，避免被 _ANY_TAG 一起吃掉。
    kept: list[str] = []

    def protect(match: re.Match) -> str:
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    text = _KEEP_TAG.sub(protect, text)
    text = _ANY_TAG.sub("", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)
    text = _absolutize_attrs(text)
    text = _BLANK.sub("\n\n", text)

    blocks: list[str] = []
    for chunk in text.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        images = _IMG.findall(chunk)
        rest = _IMG.sub("", chunk).strip()
        rest = rest.replace("&nbsp;", " ").strip()
        if rest:
            blocks.append(f"<p>{rest}</p>")
        for img in images:
            blocks.append(f"<p>{img}</p>")
    return blocks


def html_to_text(html: str) -> str:
    """HTML -> 纯文本（保留段落换行），给纯文本版 feed 用。"""
    if not html:
        return ""
    text = _BR.sub("\n", _SCRIPT_STYLE.sub("", html))
    text = _BLOCK_OPEN.sub("\n", text)
    text = _BLOCK_END.sub("\n", text)
    text = _LI_OPEN.sub("- ", text)
    text = _ANY_TAG.sub("", text)
    text = unescape(text).replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = _BLANK.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def plain_text(html: str) -> str:
    return re.sub(r"\s+", " ", html_to_text(html)).strip()


def abs_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE_URL + url
    return url


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


def image_src(name: str) -> str:
    if name.startswith("http://") or name.startswith("https://") or name.startswith("/"):
        return escape(abs_url(name))
    return escape(IMG_URL.format(filename=urllib.parse.quote(name)))


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

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


def collect_posts(client: Client, plates: list[int], pages: int, page_size: int, limit: int,
                  per_plate: int = 0) -> list[dict]:
    """按板块抓列表。per_plate > 0 时每个板块各留这么多条，保证冷门板块不被热门板块挤掉。"""
    seen: set[int] = set()
    items: list[dict] = []
    for plate in plates:
        taken = 0
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
                taken += 1
                if per_plate and taken >= per_plate:
                    break
            if (per_plate and taken >= per_plate) or len(posts) < page_size:
                break
            time.sleep(1)
    items.sort(key=lambda p: parse_time(p.get("createdAt")), reverse=True)
    return items if per_plate else items[:limit]


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


# ---------------------------------------------------------------------------
# 渲染正文
# ---------------------------------------------------------------------------

def comment_blocks(comment: dict, replies_limit: int = 4) -> list[str]:
    author = escape((comment.get("author") or {}).get("username") or "匿名")
    stamp = escape(local_time(comment.get("createdAt")))
    floor = comment.get("floor")
    head = f"<b>#{floor} {author}</b>" if floor else f"<b>{author}</b>"
    blocks = [f"<p>{head} · <small>{stamp}</small></p>"]

    body_blocks = html_fragments(comment.get("content") or "")
    blocks.extend(body_blocks or ["<p><i>（该楼层无文字内容）</i></p>"])

    for reply in (comment.get("replies") or [])[:replies_limit]:
        reply_author = escape((reply.get("author") or {}).get("username") or "匿名")
        reply_body = plain_text(reply.get("content") or "")
        if reply_body:
            blocks.append(f"<p>　└ 回复 {reply_author}：{reply_body}</p>")
    return blocks


def build_blocks(post: dict, detail: dict, comments: list[dict], with_comments: bool,
                 original_url: str, max_comments: int = MAX_COMMENTS) -> list[str]:
    """返回正文的 <p> 块列表（HTML 版 & 镜像页共用）。"""
    blocks: list[str] = []

    content = detail.get("content") or post.get("content") or ""
    body_blocks = html_fragments(content)
    if body_blocks:
        blocks.extend(body_blocks)
    else:
        blocks.append("<p><i>（本帖没有文字正文，可能只有配图）</i></p>")

    images = normalize_images(detail.get("imageUrls")) or normalize_images(post.get("imageUrls"))
    for name in images[:MAX_IMAGES]:
        blocks.append(f'<p><img src="{image_src(name)}" alt="配图" /></p>')

    author = escape((post.get("author") or {}).get("username") or "匿名")
    meta = (
        f"板块：{escape(plate_name(post.get('plate')))} ｜ 作者：{author} ｜ "
        f"发布 {escape(local_time(post.get('createdAt')))} ｜ "
        f"浏览 {post.get('readingQuantity', 0)} ｜ 回复 {post.get('commentCount', 0)} ｜ "
        f"点赞 {post.get('likeCount', 0)}"
    )
    blocks.append(f"<p>{meta}</p>")

    if with_comments:
        total = len(comments)
        if total:
            shown = min(total, max_comments)
            note = f"（共 {total} 条，显示最新 {shown} 条）" if total > shown else ""
            blocks.append(f"<p><b>—— 评论（{total}）{note} ——</b></p>")
            for comment in comments[:max_comments]:
                blocks.extend(comment_blocks(comment))
        else:
            blocks.append("<p><b>—— 评论（0） ——</b></p>")
            blocks.append("<p><i>（暂时还没有评论，去原帖抢个沙发？）</i></p>")

    blocks.append(
        '<footer><p><small>本条由杯友酱 RSS 自动抓取，完整讨论请到 '
        f'<a href="{escape(original_url)}">原帖</a>。</small></p></footer>'
    )
    return blocks


def build_html_body(post: dict, detail: dict, comments: list[dict], with_comments: bool,
                    original_url: str, max_comments: int = MAX_COMMENTS) -> str:
    return "\n".join(build_blocks(post, detail, comments, with_comments, original_url, max_comments))


# ---------------------------------------------------------------------------
# 静态镜像页
# ---------------------------------------------------------------------------

MIRROR_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 16px 16px 48px; background: #fff; color: #1c1c1e;
       font: 17px/1.7 -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; }
article { max-width: 720px; margin: 0 auto; }
h1 { font-size: 1.5em; line-height: 1.35; margin: .2em 0 .4em; }
p { margin: .6em 0; }
img { max-width: 100%; height: auto; border-radius: 10px; }
a { color: #0a66c2; }
small { color: #6b7280; }
footer { margin-top: 2em; padding-top: 1em; border-top: 1px solid #e5e7eb; }
@media (prefers-color-scheme: dark) {
  body { background: #000; color: #e5e7eb; }
  a { color: #6cb4ff; }
  small { color: #9ca3af; }
  footer { border-top-color: #2c2c2e; }
}
"""

MIRROR_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · 杯友酱</title>
<meta name="description" content="{desc}">
<style>{css}</style>
</head>
<body>
<article>
<h1>{title}</h1>
{body}
</article>
</body>
</html>
"""


def render_mirror(post: dict, body_html: str) -> str:
    title = escape((post.get("title") or "").strip() or f"帖子 #{post.get('id')}")
    desc = escape(plain_text(post.get("content") or "")[:120]) or "杯友酱论坛帖子"
    return MIRROR_TEMPLATE.format(title=title, desc=desc, css=MIRROR_CSS, body=body_html)


# ---------------------------------------------------------------------------
# 生成 RSS
# ---------------------------------------------------------------------------

def build_rss(items: list[tuple[dict, dict, list[dict]]], feed_url: str,
              with_comments: bool, fmt: str = "html",
              link_for=None, max_comments: int = MAX_COMMENTS) -> str:
    """fmt: html=富文本；text=纯文本；link_for(post) 返回 item 的 <link>。"""
    now = datetime.now(timezone.utc).astimezone(CST)
    plain = fmt == "text"
    desc = SITE_DESC + ("纯文本版，适合不渲染 HTML 的阅读器。" if plain else "")
    lines: list[str] = []
    add = lines.append

    add('<?xml version="1.0" encoding="UTF-8"?>')
    add('<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">')
    add("  <channel>")
    add(f"    <title>{escape(SITE_TITLE)}</title>")
    add(f"    <link>{escape(BASE_URL)}/</link>")
    add(f"    <description>{escape(desc)}</description>")
    add("    <language>zh-cn</language>")
    add(f"    <lastBuildDate>{format_datetime(now)}</lastBuildDate>")
    add("    <generator>beiyoujiang-rss</generator>")
    add("    <ttl>30</ttl>")
    add("    <category>18+</category>")
    if feed_url:
        add(f'    <atom:link href="{escape(feed_url)}" rel="self" type="application/rss+xml"/>')

    for post, detail, comments in items:
        pid = post.get("id")
        original = POST_URL.format(post_id=pid)
        link = link_for(post) if link_for else original
        title = (post.get("title") or "").strip() or f"帖子 #{pid}"
        pub = parse_time(post.get("createdAt")).astimezone(CST)
        author = (post.get("author") or {}).get("username") or "匿名"

        body_html = build_html_body(post, detail, comments, with_comments, original, max_comments)
        if plain:
            body = escape(html_to_text(body_html))
        else:
            body = f"<![CDATA[{body_html}]]>"

        add("    <item>")
        add(f"      <title>{escape(title)}</title>")
        add(f"      <link>{escape(link)}</link>")
        if link == original:
            add(f'      <guid isPermaLink="true">{escape(original)}</guid>')
        else:
            add(f'      <guid isPermaLink="false">{escape(original)}</guid>')
        add(f"      <pubDate>{format_datetime(pub)}</pubDate>")
        add(f"      <author>{escape(author)}</author>")
        add(f"      <dc:creator>{escape(author)}</dc:creator>")
        add(f"      <category>{escape(plate_name(post.get('plate')))}</category>")
        add(f"      <description>{body}</description>")
        add(f"      <content:encoded>{body}</content:encoded>")
        add("    </item>")

    add("  </channel>")
    add("</rss>")
    return "\n".join(lines) + "\n"


def write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def write_mirrors(items, mirror_dir: str, with_comments: bool,
                  max_comments: int = MAX_COMMENTS) -> set[str]:
    """为每条帖子写静态镜像页，并清理本次没有生成的旧页面。"""
    os.makedirs(mirror_dir, exist_ok=True)
    keep: set[str] = set()
    for post, detail, comments in items:
        pid = post.get("id")
        body = build_html_body(post, detail, comments, with_comments,
                               POST_URL.format(post_id=pid), max_comments)
        page = render_mirror(post, body)
        write_text(os.path.join(mirror_dir, f"{pid}.html"), page)
        keep.add(f"{pid}.html")
    for name in os.listdir(mirror_dir):
        if name.endswith(".html") and name not in keep:
            try:
                os.remove(os.path.join(mirror_dir, name))
            except OSError:
                pass
    return keep


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="生成杯友酱论坛 RSS（含正文与评论）")
    parser.add_argument("--plates", default="1,2,3",
                        help="板块 ID，逗号分隔。1=大型倒模 2=杯酱论坛 3=杂鱼茶话（默认全部）")
    parser.add_argument("--pages", type=int, default=2, help="每个板块抓几页（默认 2）")
    parser.add_argument("--page-size", type=int, default=20, help="每页条数（默认 20）")
    parser.add_argument("--limit", type=int, default=40, help="最终保留多少条（默认 40）")
    parser.add_argument("--per-plate", type=int, default=0,
                        help="每个板块各留多少条（填了就忽略 --limit，冷门板块不会被挤掉）")
    parser.add_argument("--max-comments", type=int, default=MAX_COMMENTS,
                        help=f"每条帖子最多显示多少条评论（默认 {MAX_COMMENTS}）")
    parser.add_argument("--out", default="feed.xml", help="输出文件路径（默认 feed.xml）")
    parser.add_argument("--format", default="html", choices=["html", "text"],
                        help="html=富文本（默认）；text=纯文本，适合不渲染 HTML 的阅读器")
    parser.add_argument("--feed-url", default="", help="填写后写入 atom:self 自引用地址")
    parser.add_argument("--text-out", default="",
                        help="同时再输出一份纯文本 feed（供不渲染 HTML 的阅读器使用）")
    parser.add_argument("--app-out", default="",
                        help="输出「App 专用」feed：item 链接指向静态镜像页，正文评论必显示")
    parser.add_argument("--app-feed-url", default="", help="App 专用 feed 的 atom:self 地址")
    parser.add_argument("--mirror-dir", default="",
                        help="静态镜像页输出目录（配合 --app-out 使用，如 docs/p）")
    parser.add_argument("--mirror-base", default="",
                        help="镜像页的公开访问前缀，如 https://user.github.io/repo/p/")
    parser.add_argument("--no-comments", action="store_true", help="不抓评论，请求量减半")
    parser.add_argument("--no-detail", action="store_true", help="不抓正文全文，只用列表摘要")
    args = parser.parse_args()

    plates = [int(x) for x in str(args.plates).replace("，", ",").split(",") if x.strip()]
    plates = [p for p in plates if p in PLATES] or list(PLATES)

    if args.app_out and not args.mirror_dir:
        parser.error("--app-out 需要同时指定 --mirror-dir")
    if args.app_out and not args.mirror_base:
        parser.error("--app-out 需要同时指定 --mirror-base")

    client = Client()
    with_comments = not args.no_comments
    with_detail = not args.no_detail
    if with_detail or with_comments:
        client.ensure_guest()

    print(f"[info] 抓取板块: {', '.join(plate_name(p) for p in plates)}"
          + (f"（每板块 {args.per_plate} 条）" if args.per_plate else ""))
    posts = collect_posts(client, plates, args.pages, args.page_size, args.limit, args.per_plate)
    if not posts:
        print("[error] 没抓到任何帖子，接口可能改过了。", file=sys.stderr)
        return 1
    print(f"[info] 列表 {len(posts)} 条，开始补正文" + ("与评论" if with_comments else ""))

    items = enrich(client, posts, with_comments) if with_detail else [(p, {}, []) for p in posts]

    xml = build_rss(items, args.feed_url, with_comments, args.format,
                    max_comments=args.max_comments)
    write_text(args.out, xml)

    if args.text_out:
        text_url = args.feed_url.replace("feed.xml", "feed-text.xml") if args.feed_url else ""
        text_xml = build_rss(items, text_url, with_comments, "text",
                             max_comments=args.max_comments)
        write_text(args.text_out, text_xml)
        print(f"[ok] 纯文本版写入 {args.text_out}")

    if args.app_out:
        keep = write_mirrors(items, args.mirror_dir, with_comments, args.max_comments)
        base = args.mirror_base if args.mirror_base.endswith("/") else args.mirror_base + "/"

        def mirror_link(post: dict) -> str:
            return f"{base}{post.get('id')}.html"

        app_xml = build_rss(items, args.app_feed_url, with_comments, "html",
                            link_for=mirror_link, max_comments=args.max_comments)
        write_text(args.app_out, app_xml)
        print(f"[ok] App 专用 feed 写入 {args.app_out}，镜像页 {len(keep)} 个 → {args.mirror_dir}")

    total_comments = sum(len(c) for _, _, c in items)
    newest = parse_time(items[0][0].get("createdAt")).astimezone(CST)
    print(f"[ok] 写入 {args.out}：{len(items)} 条、{total_comments} 条评论，"
          f"最新一条 {newest:%Y-%m-%d %H:%M}（CST）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
