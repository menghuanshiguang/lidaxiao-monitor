# lidaxiao-monitor

盯住李大霄的 B 站主页：新视频自动下载、抽字幕、出分析，每个交易日写成一份日报。

## 它能做什么

- 发现新视频就处理，按 BV 号去重（记在 `state/processed.txt`），不漏也不重复
- 每条视频出一段结构化解码：一句话摘要、关键数据、核心观点、话术拆解、风险提示，末尾附免责声明
- 同日动态（纯文字动态，以及图文动态的文字部分）按 `id_str` 去重，写进同一份日报的「李大霄动态」节
- 分析走可插拔通道：API → CLI → 备用 API → 免费车道，逐级降级；抓不到动态或分析失败只降级，不出空节、不中断视频链路

## 快速开始

Python 3.12（CI 用的就是 3.12）：

```bash
pip install -r requirements.txt
python3 monitor.py                 # 处理还没看过的视频
python3 monitor.py --force         # 强制重跑最新一条
python3 monitor.py --check-vision  # 只自检片尾视觉模型，不下载不分析
```

`python3 monitor.py --help` 列出全部参数（`--uid` / `--limit` / `--config`）。
测试：`bash tests/run_all.sh`。本机判「不新增失败」，ubuntu 全绿由 `tests.yml` 承担。

## 配置

`config.json` 里改一项就够：`"uid"` —— 要盯的 UP 主，默认 `2137589551`（李大霄）。

其余是各条分析通道的开关与顺序，不配也能跑。API 密钥从环境变量读，缺了自动跳过那条通道，不中断流程。

## 自动运行

`.github/workflows/monitor.yml` 每 3 小时跑一次（cron `0 */3 * * *`），也可以手动 dispatch，输入 `force` 与 `llm`。

仓库配两个 secret：

- `BILIBILI_COOKIES_B64`：B 站登录 cookie 的 base64。缺了视频列表和动态接口都会 412
- `DEEPSEEK_API_KEY`（或 `OPENCODE_GO_API_KEY`）：分析通道，至少给一个

跑完由 workflow 自己把报告提交回 `docs/reports/`。

## 输出

`docs/reports/<日期>.md`，按视频发布日期命名：

```
## 今日摘要
## 视频清单
## 视频1:《标题》 (BV号)
## 视频2:…
## 李大霄动态        ← 当天有新动态才有这一节
```

`docs/reports/latest.md` 指向最近一份。进度记在 `state/`（`processed.txt`、`last_bvid.txt`、`dynamics_seen.json`），随仓库同步，用来让本地与云端不重复分析同一条。

---

报告里的观点来自李大霄本人，本项目只做记录与提炼，不构成投资建议。License: MIT。
