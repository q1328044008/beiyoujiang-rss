# 杯友酱论坛 RSS 订阅源

把 [beiyoujiang.com](https://beiyoujiang.com/) 的公开帖子做成 RSS，**含正文全文、配图和楼中楼评论**，
每 30 分钟由 GitHub Actions 自动更新。

> 站点为 18+ 成人玩具交流社区，本仓库只聚合其公开接口数据，内容仅面向成年读者。

## 订阅地址

| 用途 | 地址 | 说明 |
| --- | --- | --- |
| 浏览器 / 网页阅读器 | `https://q1328044008.github.io/beiyoujiang-rss/feed.xml` | item 链接指向原帖 |
| **iOS「閲讀」/ Legado 等 App** | `https://q1328044008.github.io/beiyoujiang-rss/feed-app.xml` | item 链接指向**静态镜像页**，正文评论一定显示 |
| 极简 / 不渲染 HTML 的阅读器 | `https://q1328044008.github.io/beiyoujiang-rss/feed-text.xml` | 纯文字 |

Inoreader / Feedly / NetNewsWire / Reeder / FreshRSS 用 `feed.xml`；
iOS「閲讀」（Yuedu-reader）、安卓「阅读 / Legado」用 `feed-app.xml` 最稳。

## 为什么之前「只有标题」？

`beiyoujiang.com` 的帖子页是 Next.js 单页应用，服务端只返回一个空壳：

```html
<img src="/images/load.gif"> 主人别急~ 杯酱正在努力装填玩具弹药...
<title>可可狼姬和赤鸢 - 杯友酱</title>
```

而 iOS「閲讀」的阅读器默认是 **Reader（全文）模式**：它会先判断 feed 自带的正文是否「够结构化」，
不够就自己去抓 item 的 `link` 页面做正文提取（`RSSArticleContentLoader` → `RSSArticleExtractor`）。
抓到的既然是「加载中」占位，正文就被这段占位覆盖 —— 于是只剩标题。
（其判据 `RSSArticleHTMLSanitizer.hasSubstantialStructuredContent`：结构块 ≥ 4 且文字 ≥ 260，
或含图片且结构块 ≥ 2、文字 ≥ 180；块只算 `p/figure/blockquote/ul/ol/h2/h3/footer`。）

旧版 feed 把整篇帖子塞进一个 `<p>`、评论塞进一串 `<div>`，既不是合法 HTML 嵌套
（`<p><div>…</div></p>`），结构块数量也常常不达标，所以大多数条目都会触发「去抓原帖」→ 空。

## 现在怎么解决

1. **正文重排为真正的段落块**：一段一 `<p>`、一图一 `<p>`，评论楼层也各自成块，
   并补上「原帖」页脚。这样大多数条目都能让 App 直接使用 feed 内容，不再抓原帖。
2. **静态镜像页兜底**：为每篇帖子生成 `docs/p/<帖子ID>.html`（纯静态、手机排版、
   含正文 + 配图 + 最新评论 + 原帖链接），`feed-app.xml` 的 item 链接直接指向镜像页。
   即使 App 坚持抓「全文」，抓到的也是真实内容，不会再是加载占位。
3. **图片镜像**：`beiyoujiang.com` 开了防盗链（Referer 不是本站就 403），而阅读器加载正文时
   的 Referer 正是 Pages 地址，图片必然裂开。生成时把图片下载到 `docs/img/` 再引用同源地址。
4. **自检脚本**：`check_feed.py` 用与 App 相同的阈值检查每条 item，CI 里跑一遍，
   避免以后改坏了又变成「只有标题」。

## 本地运行

只依赖 Python 标准库（`check_feed.py` 也不需要第三方包）：

```bash
python generate_feed.py \
  --out docs/feed.xml \
  --text-out docs/feed-text.xml \
  --app-out docs/feed-app.xml \
  --mirror-dir docs/p \
  --mirror-base "https://q1328044008.github.io/beiyoujiang-rss/p/" \
  --img-dir docs/img \
  --img-base "https://q1328044008.github.io/beiyoujiang-rss/img/" \
  --per-plate 15 --pages 2 --page-size 20 \
  --feed-url "https://q1328044008.github.io/beiyoujiang-rss/feed.xml" \
  --app-feed-url "https://q1328044008.github.io/beiyoujiang-rss/feed-app.xml"

python check_feed.py docs/feed.xml docs/feed-app.xml docs/feed-text.xml
```

常用参数：

- `--plates 1,2,3`：1 = 大型倒模，2 = 杯酱论坛，3 = 杂鱼茶话
- `--per-plate 15`：每个板块各留 15 条（冷门板块不会被热门板块挤掉）
- `--max-comments 12`：每帖最多显示多少条评论（默认 12，接口里按时间倒序）
- `--no-comments` / `--no-detail`：少发请求，适合调试
- `--app-out` / `--mirror-dir` / `--mirror-base`：生成 App 专用 feed 与镜像页
- `--img-dir` / `--img-base`：把配图镜像到 Pages，绕开原站防盗链（不清空、不重复下载）

## 目录

```
generate_feed.py          抓取 + 生成 RSS / 镜像页
check_feed.py             结构自检（与「閲讀」的判据一致）
docs/feed.xml             主订阅源（原帖链接）
docs/feed-text.xml        纯文本版
docs/feed-app.xml         App 专用版（镜像页链接）
docs/p/<id>.html          每篇帖子的静态镜像页（每次运行重建，旧页面会清理）
docs/img/<hash>.<ext>     镜像的配图（按图片地址哈希命名，先存在就跳过）
docs/index.html           订阅说明页
.github/workflows/rss.yml 每 30 分钟自动更新
```

## 已知边界

- 只抓公开接口，游客身份（`addTourist`）失效时会退化为「只有列表摘要」，日志里会有 `[warn]`。
- 每帖默认只带最新 12 条评论，避免个别热帖（几百条评论）把 feed 撑到几 MB。
- 镜像页只保留当前 feed 里的帖子，旧的会被清理；想长期保存请用 App 的收藏功能。
- 配图会镜像到 `docs/img/`（首次约 7〜8 MB），只有新帖子的图才会新增，旧图随帖子滚出 feed 时清理；
  git 历史里仍会留档，长期仓库体积会缓慢增长。
