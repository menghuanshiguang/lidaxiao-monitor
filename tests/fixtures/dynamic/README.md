# 动态解析 fixture 来源

全部是对 2026-11-24 探针真实响应的裁剪, 没有手编假结构。
探针脚本与原始响应在 `/tmp/dynprobe/`(一次性, 未入仓库)。

| 文件 | 来源 | 裁剪方式 | 用途 |
|---|---|---|---|
| feed_space_page.json | /tmp/dynprobe/page1.json | 信封原样, items 只留 1 条 AV + 1 条 DRAW(纯图) | 覆盖 MAJOR_TYPE_ARCHIVE 与 MAJOR_TYPE_DRAW 两条分支 |
| feed_space_opus.json | /tmp/dynprobe/opus_style.json | 信封原样, items 只留 1 条 AV + 1 条 OPUS(814 字正文) | 覆盖 MAJOR_TYPE_OPUS 分支与 features 参数的必要性 |
| feed_space_empty.json | /tmp/dynprobe/resp/polymer-feed-space__3_wbi_2.json | 原样复制 | 限流软空: code=0 但 items=[] |
| feed_space_risk.json | /tmp/dynprobe/resp/polymer-feed-space__2_UA+Referer+buvid_1.json | 原样复制 | 风控: code=-352 |

## sha256(全长)

- `feed_space_empty.json`: `bd550ce7844f6111f2fcee4fa0cf1fe9a90bb43e4c15c99c51c6955e2b85f21c`
- `feed_space_opus.json`: `fa6e036a6ab03f14f30299a6a9dd4c25f063fc7d72f3d92b60f7cd97fbecfee4`
- `feed_space_page.json`: `a27344e358fda368996ae5592349f33fc59a84b8fd51ad989f88e666db77626d`
- `feed_space_risk.json`: `2c515e81569b2e2682b66c052d15d75df7a5b1530d00a1e9183cc1c99fbaa3a7`

## 两份一页 fixture 为什么形状不同

同一 id `1273504300584140805` 在两种请求下形状不同:

- 不带 `features=itemOpusStyle`: `major.type=MAJOR_TYPE_DRAW`, `desc=null`, 正文 0 字, 图在 `major.draw.items[]`
- 带 `features=itemOpusStyle`: `major.type=MAJOR_TYPE_OPUS`, 正文 814 字在 `major.opus.summary.text`, 图在 `major.opus.pics[]`

所以两份 fixture 都要留, 且生产请求必须带这个参数, 否则正文丢失。
另: `polymer/web-dynamic/v1/detail` 对同一 id 返回 DRAW 形状、看不到那 814 字, 不能用来核对 fixture。
