# beiyoujiang-rss

给 [杯友酱论坛](https://beiyoujiang.com/) 做的 RSS 订阅源。

**订阅地址**：https://q1328044008.github.io/beiyoujiang-rss/feed.xml

## 这是怎么做的

站点是 Next.js 前端 + Express 后端，没有官方 RSS，但帖子列表走一个公开接口：

```
POST https://beiyoujiang.com/api/post/getAllPost
Content-Type: application/json

{"plate": 2, "most": 1, "userId": null, "page": 1, "pageSize": 20}
```

- `plate`：板块。`1` 大型倒模 / `2` 杯酱论坛 / `3` 杂鱼茶话
- `most`：排序。`1` 按发布时间（RSS 用这个）/ `2` 按最后回复
- 返回里有 `id`、`title`、`content`、`createdAt`、`author`、`imageUrls` 等字段

`generate_feed.py` 就是这个接口的搬运工：抓列表 → 去重 → 按时间倒序 → 输出 RSS 2.0。
帖子链接按站内规则拼成 `https://beiyoujiang.com/messageDetail/{id}`，图片走 `/PostImg/{filename}`。

## 文件

| 文件 | 作用 |
| --- | --- |
| `generate_feed.py` | 抓取并生成 RSS，纯标准库，无需 pip install |
| `.github/workflows/rss.yml` | 每 30 分钟跑一次，更新 feed 并部署到 Pages |
| `docs/feed.xml` | 生成的订阅文件（Pages 发布的就这一个） |
| `docs/index.html` | 一页说明，打开主页就能看到订阅地址 |
| `feed.xml` | 仓库根目录的同一份副本，方便用 raw 地址订阅 |

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
- `--out`：输出路径，默认 `feed.xml`
- `--feed-url`：填了会写进 `<atom:link rel="self">`，方便阅读器识别

## 首次部署清单

1. 仓库 Settings → Pages → Source 选 **GitHub Actions**（这一步不做，workflow 的 deploy 会失败）
2. Actions 页面点一次 **更新 RSS 订阅源** → Run workflow，手动跑第一次
3. 等绿灯后访问 `https://q1328044008.github.io/beiyoujiang-rss/feed.xml`

## 可能的坑

- GitHub 的定时任务不保证准点，免费额度下延迟几分钟到十几分钟都正常。
- 抓取只读公开列表接口，不登录、不发帖、不点赞；每次请求间隔 1 秒，两个板块两页也就十来个请求。
- 如果哪天接口改了字段或加了风控，`generate_feed.py` 是唯一需要改的地方，改 `fetch_page()` 里的 payload 即可。
- 抓到的图片是站内相对路径转成的绝对地址，阅读器支持外链图片才会显示。
- 内容为 18+ 成人玩具社区帖子摘要，只适合成年读者自用。
