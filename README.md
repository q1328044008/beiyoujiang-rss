# beiyoujiang-rss

给 [杯友酱论坛](https://beiyoujiang.com/) 做的 RSS 订阅源。

抓取脚本、订阅源与说明页由 **豆包（Doubao）** 协助搭建与维护；数据来自站点公开接口。

- **富文本版**（浏览器 / 网页阅读器）：https://q1328044008.github.io/beiyoujiang-rss/feed.xml
- **纯文本版**（「阅读」/ Legado 这类 App 用这条）：https://q1328044008.github.io/beiyoujiang-rss/feed-text.xml

两版内容一样：正文全文 + 配图 + 楼中楼评论，区别只在正文是 HTML 还是纯文本。

## 在「阅读」(Legado) 里用

新建一个 RSS 订阅，地址粘贴 `feed-text.xml` 那条即可。不需要 js 源，也不需要登录账号。
纯文本版保证正文和评论是纯文字，App 里不渲染 HTML 也能看全。

## 这是怎么做的

站点是 Next.js 前端 + Express 后端，没有官方 RSS，但数据走几个公开接口：

```
POST /api/auth/addTourist        {}                                          # 注册游客，拿 userId + token

POST /api/post/getAllPost        {"plate":2,"most":1,"userId":null,"page":1,"pageSize":20}
POST /api/post/getPost           {"postId":13609,"userId":<游客id>}          # 正文全文
POST /api/comment/getPostComment {"postId":13609,"userId":<游客id>,"reply":null,"order":1,"orderType":"time"}
```

- `plate`：板块。`1` 大型倒模 / `2` 杯酱论坛 / `3` 杂鱼茶话
- `most`：排序。`1` 按发布时间（RSS 用这个）/ `2` 按最后回复
- 列表接口返回 `id`、`title`、`content`（摘要）、`commentCount`、`createdAt` 等
- 详情和评论接口要求带 `userId`（游客 id 即可）＋ `Authorization: Bearer <token>`，所以脚本开头会先注册一个游客身份

`generate_feed.py` 的流程：注册游客 → 抓列表 → 去重排序 → 逐条补正文和评论 → 输出 RSS 2.0。
帖子里同时写入 `description` 和 `content:encoded`（全文 + 配图 + 楼中楼评论），兼容只认其中一个字段的阅读器。
帖子链接按站内规则拼成 `https://beiyoujiang.com/messageDetail/{id}`，图片走 `/PostImg/{filename}`。

## 文件

| 文件 | 作用 |
| --- | --- |
| `generate_feed.py` | 抓取并生成 RSS，纯标准库，无需 pip install |
| `.github/workflows/rss.yml` | 每 30 分钟跑一次，同时更新 `docs/feed.xml` 与 `docs/feed-text.xml` 并提交 |
| `docs/feed.xml` | 富文本订阅文件，Pages 发布的就是它 |
| `docs/feed-text.xml` | 纯文本订阅文件，给不渲染 HTML 的阅读器 |
| `docs/index.html` | 一页说明，打开站点首页能看到订阅地址 |

## 发布方式

GitHub Pages 走「Deploy from a branch」：分支 `main`、目录 `/docs`。
每次 `docs/feed.xml` 有更新就被工作流提交，Pages 自动重新发布，不依赖 Actions 部署权限。

## 本地跑

```bash
python generate_feed.py                                    # 全部板块，生成 feed.xml
python generate_feed.py --plates 2 --pages 3 --limit 60    # 只看杯酱论坛，抓 3 页 60 条
python generate_feed.py --out docs/feed.xml                # 输出到 docs 目录
```

参数说明：

- `--plates`：板块，逗号分隔，默认 `1,2,3`
- `--pages`：每个板块抓几页，默认 2
- `--page-size`：每页条数，默认 20
- `--limit`：最终保留条数，默认 40
- `--per-plate`：每个板块各留多少条。填了会忽略 `--limit`，冷门板块不会被热门板块挤掉。线上跑的是 `--plates 1,2,3 --per-plate 15`，共 45 条，三块各 15 条
- `--out`：输出路径，默认 `feed.xml`
- `--text-out`：顺便再输出一份纯文本 feed，比如 `--text-out docs/feed-text.xml`
- `--format`：`html`（默认）或 `text`，单独生成纯文本版时用
- `--feed-url`：填了会写进 `<atom:link rel="self">`，方便阅读器识别
- `--no-comments`：不抓评论，请求量减半（40 条约 40 次请求 → 约 70 次）
- `--no-detail`：不抓正文全文，只用列表摘要，速度最快

## 可能遇到的坑

- 站点对短时间高频请求会返回 **429**。脚本按每 0.9 秒一次的节奏发请求，命中 429 会自动退避 25 秒重试；没有评论的帖子直接跳过评论接口，请求数能省一半。
- GitHub 的定时任务不保证准点，免费额度下延迟几分钟到十几分钟都正常。
- 抓取只读公开接口，不登录、不发帖、不点赞；游客账号每次运行新建一个，只用于读详情。
- 如果哪天接口改了字段或加了风控，`generate_feed.py` 是唯一需要改的地方，改 `Client.post()` 的 payload 即可。
- 抓到的图片是站内相对路径转成的绝对地址，阅读器支持外链图片才会显示。
- 内容为 18+ 成人玩具社区帖子摘要，只适合成年读者自用。
