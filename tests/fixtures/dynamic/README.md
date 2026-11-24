# 动态解析 fixture 来源

全部是对 2026-11-24 探针真实响应的裁剪, 没有手编假结构。
探针脚本与原始响应在 `/tmp/dynprobe/`(一次性, 未入仓库)。

| 文件 | 来源 | 裁剪方式 | 用途 |
|---|---|---|---|
| feed_space_page.json | /tmp/dynprobe/page1.json | 信封原样, items 只留 1 条 AV + 1 条 DRAW(纯图) | 覆盖 MAJOR_TYPE_ARCHIVE 与 MAJOR_TYPE_DRAW 两条分支 |
| feed_space_opus.json | /tmp/dynprobe/opus_style.json | 信封原样, items 只留 1 条 AV + 1 条 OPUS(814 字正文) | 覆盖 MAJOR_TYPE_OPUS 分支与 features 参数的必要性 |
| feed_space_empty.json | /tmp/dynprobe/resp/polymer-feed-space__3_wbi_2.json | 重新序列化保存, JSON 内容一致(源 131B → 170B, 缩进与中文不转义所致, 非逐字节复制) | 限流软空: code=0 但 items=[] |
| feed_space_risk.json | /tmp/dynprobe/resp/polymer-feed-space__2_UA+Referer+buvid_1.json | 重新序列化保存, JSON 内容一致(源 39B → 48B, 同上, 非逐字节复制) | 风控: code=-352 |

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

## 空页判据不许依赖 has_more(附一次转述失真的追查)

对 `/tmp/dynprobe/` 下**全部 9 份**真实响应逐份数过(`code` / `items` 条数 / `has_more` / `total` / `offset`):

| 文件 | code | items | has_more | total | offset |
|---|---|---|---|---|---|
| page1.json | 0 | 13 | true | "0" | 1272778807620665396 |
| page2.json | 0 | 12 | true | "0" | 1271561759950372868 |
| page3.json | 0 | 12 | true | "0" | 1270037411616784385 |
| page4.json | 0 | 12 | true | "0" | 1268963725593804806 |
| opus_style.json | 0 | 13 | true | "0" | 1272778807620665396 |
| resp/…__3_wbi_1.json | 0 | 13 | true | "0" | 1272778807620665396 |
| resp/…__3_wbi_2.json | 0 | **0** | **false** | "0" | `""` |
| resp/…__2_UA+Referer+buvid_1.json | -352 | 无 data | — | — | — |
| resp/…__2_UA+Referer+buvid_2.json | -352 | 无 data | — | — | — |

- `items=[]` 的样本只有 1 份, 它的 `has_more=false`、`offset=""`。
- `items=[] 且 has_more=true` 的样本: **0 份, 一次都没出现过**。
- `has_more=true` 只出现在有 12/13 条的页上(有数据才有下一页), 也就是它与"有没有数据"同向, 不构成矛盾。

### 那句"阶段 1 探针的空页 has_more=true"是从哪来的

追查 `/tmp/dynprobe/` 下的自身记录(`RESULTS.md`、`summary.json`、`probe*.py` 输出):

- `RESULTS.md` 第 2 节原本记的就是 `2s/5s → items=0, has_more=False`、`15s/30s → 13, True` —— **探针记录从一开始就没有"空页 has_more=true"这一事实**;
- `summary.json` 里 `describe()` 只记 `http/code/message/items`, 根本没记 `has_more`, 也产生不了这句话;
- 这句话最早出现在**阶段 1 的验收报告文字**里(以及阶段 2 派活时的判据转述), 不是任何一份响应。

所以它是**转述失真**: 报告文字说空页 `has_more=true`, 原始响应里从来没有, 我又把报告文字抄进了本 README。原始响应面前, 报告文字不算数。

### 结论(不变, 但理由要写对)

判据只看 `code` 与 `items` 是否为空, **不看 `has_more`**:
空页只见过 `has_more=false`, 任何人想改用 `has_more` 做判据, 都得先证明它在空页上的取值, 现在证不了。`classify_payload()` 的实现就是这么写的, 别改。
