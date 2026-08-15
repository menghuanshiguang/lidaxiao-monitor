# 📺 李大霄视频自动监控分析系统

监控 B站 UP主 **李大霄** (UID `2137589551`) 的视频更新,自动完成
**检测 → 下载 → OCR字幕 → AI分析 → 每日报告**,全程无人值守。

## ✨ 特性

- 🔍 **更新检测**: 基于 [bilidown CLI](https://github.com/menghuanshiguang/bilibili-downloader-cli)(内置 wbi 签名 + cookies 反爬),状态文件对比,无新视频秒退
- ⬇️ **视频下载**: 480P mp4,失败自动重试,登录 cookies 解锁高清
- 📝 **硬字幕提取**: ffmpeg 抽帧 + RapidOCR,字幕带自动探测(排除水印/logo),相邻帧去重,图表噪声过滤;支持 **RTX GPU DirectML 加速**
- 🤖 **AI 分析**: DeepSeek "李大霄话术解码"——核心观点/关键数据/暗示提取(🔴🟢⚪⚠️)/市场定性/操作含义/观点连续性
- 📄 **每日报告**: `reports/YYYY-MM-DD.md`,同天多视频追加,顶部当日摘要
- 🔁 **幂等可靠**: 已处理列表 + 并发锁,中断重跑不重复;断点续传
- ⏰ **科学定时**: 基于 400 条发布历史统计的 7 次/天检查(覆盖 10-15 时与 18-23 时两个高峰)

> ⚠️ **免责声明**: 本项目输出的所有分析均为对视频内容的自动化提炼,
> 不构成任何投资建议。投资有风险,入市需谨慎。

## 📊 最新报告 (GitHub Actions 自动更新)

| | |
|---|---|
| **📅 周度总结** | [📄 本周周报](./docs/reports/weekly.md) |
| **今日/最新日报** | [📄 latest.md](./docs/reports/latest.md) |
| **历史日报索引** | [📚 全部日报](./docs/reports/index.md) |

> 报告由 GitHub Actions 定时(每天 2 次:北京时间 **14:40** 和 **20:00**)
> 自动检测 → 下载 → OCR → AI 分析生成,并提交到 `docs/reports/`(日报保留 30 天,
> 周报每周五更新)。

## 目录结构

```
daxiao/
├── monitor.py              # 主脚本 (全部逻辑)
├── batch_subtitles.py      # 批量字幕提取 (全量历史视频)
├── config.json             # 配置 (UID/OCR/LLM 参数)
├── .env                    # DEEPSEEK_API_KEY (AI分析用, 不入库)
├── run_monitor.cmd         # 运行包装器 (设 PATH/HOME, 输出日志)
├── README.md               # 本文件
├── _repo/                  # bilidown CLI (见依赖说明)
├── _setup/
│   ├── login_wait.py       # B站扫码登录辅助 (自动刷新二维码, 最长30分钟)
│   ├── install_schedule.ps1# 安装科学化定时检查任务 (7次/天)
│   ├── fetch_pubdates.py   # 拉取UP主历史发布时间
│   └── analyze_pubtimes.py # 发布规律分析
├── data/                   # 状态/字幕/帧/日志 (运行时生成, 不入库)
│   ├── last_bvid.txt       # 上次处理的BV号
│   ├── processed.txt       # 已处理BV列表 (bvid|时间|状态|标题)
│   ├── monitor.lock        # 并发锁
│   ├── cookies.txt         # 登录 cookies 备份
│   ├── subtitles/<BV>.txt  # OCR 字幕
│   └── frames/<BV>/        # 抽帧缓存
├── downloads/<BV>/         # 视频文件 (480P mp4)
├── reports/YYYY-MM-DD.md   # 每日报告
└── tools/                  # ffmpeg / gh 等本地工具 (不入库)
```

## 依赖

| 组件 | 用途 | 安装方式 |
|------|------|----------|
| Python 3.12+ | 主脚本 | python.org 安装包 |
| Git Bash | bilidown 运行环境 | git-scm.com (Windows) |
| bilidown CLI | B站反爬+下载 | 克隆 [bilibili-downloader-cli](https://github.com/menghuanshiguang/bilibili-downloader-cli) 到 `_repo/` |
| ffmpeg/ffprobe | 抽帧/合并 | 放入 `tools/ffmpeg/bin/` (gyan.dev 完整版) |
| yt-dlp | bilidown 依赖 | `pip install yt-dlp` |
| rapidocr-onnxruntime | 硬字幕OCR | `pip install rapidocr-onnxruntime` |
| onnxruntime-directml | GPU加速OCR (RTX显卡) | `pip install onnxruntime-directml` |
| qrcode/pillow | 登录二维码 | `pip install qrcode pillow` |
| requests | LLM API 调用 | `pip install requests` |

> `python3` 垫片(Windows):bilidown 的 bash 脚本调用 `python3`,Windows 无此命令,
> 将 `python.exe` 复制为 `%APPDATA%\Python\Python312\Scripts\python3.exe` 即可
> (该目录需在 PATH 中)。bash 路径可用环境变量 `BILIDOWN_BASH` 覆盖。

## 使用

### 1. 登录 (一次性, 解锁720P+并降低风控)

```cmd
python _setup\login_wait.py
```
自动生成 `login_qr.html` 并打开浏览器,用 **B站 App 扫码确认**。
cookies 保存到 `%USERPROFILE%\.cache\bilibili-login-cookies.txt`
及 `data\cookies.txt`。二维码过期自动刷新,最长等待30分钟。

### 2. 手动运行

```cmd
python monitor.py          # 无新视频秒退; 有新视频自动处理
python monitor.py --force  # 强制重新处理最新视频
```

或使用包装器(日志写入 `data\run.log`):

```cmd
run_monitor.cmd [--force]
```

### 3. 定时运行 (Windows 任务计划)

```powershell
powershell -ExecutionPolicy Bypass -File _setup\install_schedule.ps1
```
创建单个计划任务 `LiDaxiaoMonitor`,每天 **7 次**定时检查
(09:30 / 11:30 / 14:00 / **14:50** / 17:00 / 20:00 / 22:30)。

> 检查时间基于 400 条历史发布的统计分析(见 `发布规律分析.md`):
> 李大霄日均发布 3.67 条,高峰集中在 10-15 时(40%)与 18-23 时(43%),
> 中位间隔 3 小时。7 次检查保证高峰时段新视频最长等待 ≤3 小时、
> 平均约 1.5 小时;低谷时段(0-9时)由 09:30 一次兜底。
> **14:50 为 A股收盘前 10 分钟检查**,确保午间高峰视频在收盘前入报告。

查看/删除:

```cmd
schtasks /query /tn "LiDaxiaoMonitor"
schtasks /delete /tn "LiDaxiaoMonitor" /f
```

## 工作流程

1. **检测**: `bilidown space 2137589551 N` 拉取主页视频列表(内置 wbi 签名+cookies),
   与 `data/last_bvid.txt` 对比; 相同→秒退。新视频按发布时间从旧到新逐个处理。
2. **下载**: `bilidown dl <BV> video downloads/<BV> mp4 480 1`(内置412反爬规避),
   失败自动重试1次, 仍失败→报告记录错误并跳过。
3. **字幕**: ffmpeg 按自适应间隔抽帧(时长/160,限1~4秒)→ 探测字幕带
   (排除顶部logo/右上角水印,优先底部)→ RapidOCR 识别(**有RTX显卡时自动
   走 DirectML GPU 加速,约6-8倍提速**)→ 过滤时间戳/行情图表噪声/低置信度
   → 相邻帧去重(含相似度去重)→ **字幕过少时底部带/整帧兜底重扫**。
   结果存 `data/subtitles/<BV>.txt`。
4. **分析**: DeepSeek API(`deepseek-chat`)按"李大霄话术解码"规则分析:
   核心观点(3-8条带原文数据)/ 关键数据清单(美债、巴菲特指标等)/
   暗示提取(🔴警示🟢看多⚪中性⚠️风险)/ 市场定性(反弹/反转/见顶/调整/防御,
   注意"没量=不是反转"框架)/ 操作含义(方向/仓位/风险)/ 观点连续性
   (与最近5天历史报告对比: 延续/升级/新增/反转)。
   输出末尾固定免责声明。失败重试3次。
5. **报告**: `reports/YYYY-MM-DD.md`(按视频发布日期归档)。
   当天已有报告→追加; 顶部含当日摘要(每视频一句话)与视频清单。
   无字幕视频→记录"无字幕,跳过分析"; 当天无视频→不建报告。
6. **状态**: 处理完更新 `last_bvid.txt`; 每个视频记入 `processed.txt`
   (状态: ok / no-subtitle / partial / error),中断重跑不重复分析;
   `data/monitor.lock` 并发锁防止任务重叠(8小时陈旧锁自动接管)。

## 验收对照

- [x] 无新视频 → 秒退 (last_bvid 相同即退出)
- [x] 重复运行同一视频 → 不重复分析 (processed.txt)
- [x] 同天多个视频 → 追加到同一份报告
- [x] 报告格式符合规范 (标题/视频清单/逐视频分析/摘要/免责声明)
- [x] 无字幕视频 → 优雅跳过并记录
- [x] 输出 `✅ 今日报告: 路径`

## 批量字幕提取 (全量历史视频)

```cmd
python batch_subtitles.py              # 全量, 2 workers (推荐)
python batch_subtitles.py --workers 3  # 内存充足时提速
python batch_subtitles.py --limit 10   # 测试前10条
```

- 逐条: 下载480p → 抽帧(3秒间隔) → OCR底部字幕带 → 存 `data/subtitles/<BV>.txt` → **立即删除视频和帧**(磁盘保护)
- 无字幕视频写空文件标记, 重跑自动跳过 (断点续传, 可随时中断)
- 风控: 下载随机间隔3-8秒, 失败重试2次, 连续3次失败暂停120秒
- 内存: 每 worker 独立进程约550MB; 全量2478条约需 **20-28小时** (2 workers)
- 下载失败清单: `data/batch_failed.txt`; 进度日志: `data/batch.log`

## 故障排查

| 现象 | 处理 |
|------|------|
| 412 风控 | 确认已登录 (cookies 含 SESSDATA), 重跑登录 |
| OCR 无字幕 | 视频可能确实无硬字幕; 检查 `data/subtitles/<BV>.txt` |
| AI 分析失败 | 检查 `.env` 中 DEEPSEEK_API_KEY 余额; 重跑 `python monitor.py --force` |
| 计划任务不跑 | 检查任务是否以当前用户运行、`run_monitor.cmd` 路径是否含空格(已加引号) |
