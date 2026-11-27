# lidaxiao-monitor

[![auto monitor](https://img.shields.io/github/actions/workflow/status/menghuanshiguang/lidaxiao-monitor/monitor.yml?label=auto%20monitor)](https://github.com/menghuanshiguang/lidaxiao-monitor/actions)

**最新日报**：[latest.md](docs/reports/latest.md) · [全部日报索引](docs/reports/index.md) · [周度总结](docs/reports/weekly.md) · **操作建议**：[signal/latest.md](docs/signal/latest.md) · [全部信号](docs/signal/index.md)

盯住李大霄的 B 站主页：新视频自动下载、抽字幕、出分析，每个交易日写成一份日报。

## 它能做什么

- 发现新视频就处理，按 BV 号去重（记在 `state/processed.txt`），不漏也不重复
- 每条视频出一段结构化解码：一句话摘要、关键数据、核心观点、话术拆解、风险提示，末尾附免责声明
- 同日动态（纯文字动态，以及图文动态的文字部分）按 `id_str` 去重，写进同一份日报的「李大霄动态」节
- 分析走可插拔通道：API → CLI → 备用 API → 免费车道，逐级降级；抓不到动态或分析失败只降级，不出空节、不中断视频链路
- 他的微博视频号先发、B 站不发的那些短片也纳入日报：**只有 B 站没有的才走微博**，同一内容按归一化标题 + 发布时间邻近配对，两个源各出一条时只留 B 站那条

## 快速开始

Python 3.12（CI 用的就是 3.12）、git、ffmpeg（放 `tools/ffmpeg/bin/` 或加进 PATH；缺了抽帧会静默失败，报告照样出，但里面没有字幕分析）。下载器没有 pip 包，要手工预置到 `_repo/`，缺了直接拿不到视频列表：

```bash
git clone https://github.com/menghuanshiguang/bilibili-downloader-cli.git _repo
pip install -r requirements.txt

python3 monitor.py                 # 处理还没看过的视频
python3 monitor.py --force         # 强制重跑最新一条
python3 monitor.py --check-vision  # 只自检片尾视觉模型，不下载不分析
```

`python3 monitor.py --help` 列出全部参数（`--uid` / `--limit` / `--config`）。
测试：`bash tests/run_all.sh`。本机判「不新增失败」，ubuntu 全绿由 `tests.yml` 承担。

## 配置

`config.json` 里改一项就够：`"uid"` —— 要盯的 UP 主，默认 `2137589551`（李大霄）。

其余是各条分析通道的开关与顺序。密钥从环境变量读，也可以写在仓库根的 `.env` 里；缺了自动跳过那条通道，不中断流程。

## 自动运行

`.github/workflows/monitor.yml` 每 3 小时跑一次（cron `0 */3 * * *`），也可以手动 dispatch，输入 `force` 与 `llm`。

workflow 读这些 secret：

- `BILIBILI_COOKIES_B64`：B 站登录 cookie 的 base64，**必需**。缺了动态接口一律 412
- `OPENCODE_GO_API_KEY` / `DEEPSEEK_API_KEY` / `DSV_TOKEN`：分析通道的密钥，**可选**
- `WEIBO_SUB`：微博 `.weibo.cn` 的**访客** cookie（未登录态，只读时间线用），**可选**。缺了微博这一路取数降级为「本次没拿到」，B 站链路照常跑完

一个密钥都不配也能跑：workflow 会自己把免费车道（`dsh-opencode-free` 网关）起起来兜底。本机想走同一条路，得自己 clone 那个仓库、装 bun、`node launch.mjs` 把网关起在 `127.0.0.1:18787`。

跑完由 workflow 自己把报告提交回 `docs/reports/`。点进任一次 run 的 step 可看原始日志。

## 输出

`docs/reports/<日期>.md`，按视频发布日期命名，每支视频一节，节里挂片尾识别：

```
## 今日摘要
## 视频清单
## 视频1:《标题》 (BV号)
### 📽 片尾画面识别
## 视频2:…
## 李大霄动态        ← 当天有新动态才有这一节
```

来自微博的条目在章节头与清单行用 `wb<微博id>` 标记（B 站是 `BV号`），章节里还有一行
`> source=weibo ｜ 微博 id=… ｜ 原始链接`，一眼能分辨、也能按来源过滤。

**微博这一路的覆盖上限要说清**：免登录只给时间线第一页 12 条（其中约 10 条最新、2 条置顶），
按当前发帖频率约覆盖 17.6 小时。所以「无缺漏」的真实边界是**两轮成功 run 的间隔小于这个跨度**
（cron 每 3 小时一轮，够），而不是「历史全量补回」。置顶条目和历史回填（早于 7 天）只参与配对、
不下不报；每轮日志会打出 `bili/weibo/合并后/跳过/排除/队列` 六个数与窗口首末、跨度，用它们核对。

配对用的 B 站集合是**本次拉到的同一页**（`config.limit`，默认 10 条），日志里的 `B站池=` 就是它。
只要微博窗口那 17.6 小时内他在 B 站发布不超过这个数，窗口内每条微博都有机会配上；超了就会把
旧片当「微博独有」再下一遍 —— 那时该调大 `limit`（代价是每条多一次 `view` 接口调用）。

`docs/reports/latest.md` 是最近一份的副本（不是链接）。进度记在 `state/`（`processed.txt`、`last_bvid.txt`、`dynamics_seen.json`），随仓库同步，用来让本地与云端不重复分析同一条。

---

报告里的观点来自李大霄本人，本项目只做记录与提炼，不构成投资建议。License: MIT。
