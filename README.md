<p align="center">
  <img src="docs/screenshots/banner.png" width="100%" alt="lidaxiao-monitor">
</p>

<h1 align="center">📺 lidaxiao-monitor · 李大霄视频自动监控分析系统</h1>

<p align="center">
  <b>AI 全天候盯着李大霄的 B站主页 —— 检测 · 下载 · OCR字幕 · 话术解码 · 每日日报,全自动</b>
</p>

<p align="center">
  <a href="https://github.com/menghuanshiguang/lidaxiao-monitor/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/menghuanshiguang/lidaxiao-monitor/actions"><img src="https://img.shields.io/github/actions/workflow/status/menghuanshiguang/lidaxiao-monitor/monitor.yml?label=auto%20monitor" alt="Actions"></a>
  <img src="https://img.shields.io/badge/Python-3.12+-green.svg" alt="Python">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/GPU-DirectML%20(optional)-orange.svg" alt="GPU">
</p>

---

## 🎯 它解决什么问题?

> **李大霄每天发 3-4 条视频**,你追不过来;
> 他满嘴 **"不是推荐"**,你猜不透真实意图;
> 他的观点天天变,**前后矛盾你记不住**。

这个项目让 **AI 替你盯着他**:新视频一发布,自动下载、自动识别字幕、自动拆解话术,生成一份**《李大霄话术解码日报》**——核心观点、关键数据、暗示提取、**片尾暗示(视觉识别)**、市场定性、观点连续性,打开就一目了然。

> 📊 **最新日报**: [查看 latest.md](./docs/reports/latest.md) · 📅 [全部日报索引](./docs/reports/index.md) · 📚 [周度总结](./docs/reports/weekly.md)

---

## ✨ 功能亮点

| | | |
|:---:|:---:|:---:|
| 🔍 **自动检测** | ⬇️ **自动下载** | 📝 **硬字幕 OCR** |
| 基于 bilidown CLI(内置 wbi 签名 + 412 反爬),状态文件对比,无新视频秒退 | 480P mp4 自动下载,失败自动重试;登录 cookies 解锁高清 | ffmpeg 抽帧 + RapidOCR,字幕带自动探测,去重/降噪;RTX 显卡 DirectML 加速 6-8 倍 |
| 🤖 **AI 话术解码** | 📄 **每日日报** | ☁️ **云端全自动** |
| AI 拆解:核心观点 / 关键数据 / **暗示提取(不是推荐=真实关注点)** / 市场定性 / 操作含义 / **观点连续性(升级/反转一眼看出)** | `reports/YYYY-MM-DD.md` 按日期归档,同天多视频追加,顶部当日摘要 | GitHub Actions 定时运行(每天 14:40 / 20:00),报告自动提交,电脑不用开机 |
| 🎬 **片尾暗示识别** | 🖼️ **视觉模型读屏** | 🧊 **选帧 + 兜底** |
| 片尾卡片(结束语 / 荐书卡 / 合规声明 / 数据面板)+**图形动画**(地球≈地球顶、钻石≈钻石底、婴儿底等标志性比喻)——OCR 全丢,改用**视觉模型**读,报告单列【片尾暗示】 | `deepseek-v4-flash-vision-exp` 多图识别;16×16 签名去重 + **最远点采样**选帧,覆盖口播/卡片/地球动画/声明等不同场景 | 结果缓存 `state/ending_hints.json` 随仓库同步;OpenCode 额度用尽自动切 DeepSeek 官方;视觉不可用时只跳过该小节 |

**流程示意**

<p align="center">
  <img src="docs/screenshots/pipeline.png" width="100%" alt="pipeline">
</p>

---

## 📸 效果展示

**AI 生成的日报(真实内容, 2026-08-13《高度警惕美股泡沫爆破》)**

<p align="center">
  <img src="docs/screenshots/report.png" width="85%" alt="report">
</p>

---

## 🚀 快速开始(3 步)

### 1️⃣ 准备依赖

```bash
# Python 3.12+ / Git / ffmpeg (放入 tools/ffmpeg/bin/ 或加入 PATH)
pip install -r requirements.txt
git clone https://github.com/menghuanshiguang/bilibili-downloader-cli.git _repo
```

### 2️⃣ 配置密钥与登录

```bash
# AI 分析密钥 (三级兜底: OpenCode Go -> deepseek-chat-cli -> DeepSeek 官方 API)
echo "OPENCODE_GO_API_KEY=sk-xxxx" >> .env
echo "DSV_TOKEN=xxxx" >> .env
echo "DEEPSEEK_API_KEY=sk-xxxx" >> .env

# B站扫码登录 (一次性, 解锁高清并降低风控)
python _setup/login_wait.py     # 浏览器打开二维码, B站App扫码
```

> 本地启用 `deepseek-chat-cli` 兜底(可选): 首次会自动克隆公开仓库, 还需要安装它的依赖
> ```bash
> pip install -r _repo/deepseek-chat-cli/requirements.txt
> ```

### 3️⃣ 运行

```bash
python monitor.py              # 无新视频秒退; 有新视频自动处理
python monitor.py --force      # 强制重新处理最新视频
python monitor.py --check-vision   # 自检片尾视觉通道(不下载不分析)
```

> 💡 更多用法:批量补全历史字幕 `python batch_subtitles.py` · 发布规律分析 `python _setup/analyze_pubtimes.py` · 本地定时任务 `powershell -File _setup/install_schedule.ps1`

---

## ⏰ 自动运行(GitHub Actions)

本仓库内置 CI,配置以下 Secret 后即可云端全自动:

| Secret | 说明 |
|--------|------|
| `OPENCODE_GO_API_KEY` | OpenCode Zen Go API 密钥(AI 分析主通道,模型 `deepseek-v4-flash`,推理 `max`) |
| `DSV_TOKEN` | DeepSeek 网页版登录 token(第二兜底 `deepseek-chat-cli` 使用) |
| `DEEPSEEK_API_KEY` | DeepSeek 官方 API 密钥(最后兜底) |
| `BILIBILI_COOKIES_B64` | B站登录 cookies(base64,由 `data/cookies.txt` 转换) |

> `deepseek-chat-cli` 已提供公开仓库(不含 token),Actions 直接公开克隆,无需 PAT。

```bash
gh secret set OPENCODE_GO_API_KEY --repo <your>/lidaxiao-monitor
gh secret set DSV_TOKEN --repo <your>/lidaxiao-monitor
gh secret set DEEPSEEK_API_KEY --repo <your>/lidaxiao-monitor
gh secret set BILIBILI_COOKIES_B64 --repo <your>/lidaxiao-monitor
```

- 每天 **北京时间 14:40 / 20:00** 自动运行(也可手动 `Run workflow`)
- 报告自动提交到 `docs/reports/`(日报全部保留)+ 上传 artifact
- 状态与字幕通过 Actions 缓存持久化,无新视频秒退,不重复分析
- 片尾视觉通道有**非阻断预检**(`python monitor.py --check-vision`):额度/密钥问题会提前打在日志里,失败也只跳过【片尾暗示】小节,不影响主流程

---

## 📂 目录结构

```
├── monitor.py              # 主脚本 (检测/下载/OCR/片尾视觉/分析/报告)
├── batch_subtitles.py      # 批量字幕提取 (全量历史视频, 断点续传)
├── config.json             # 配置 (UID/OCR/LLM/片尾视觉)
├── requirements.txt        # Python 依赖
├── _setup/
│   ├── login_wait.py       # B站扫码登录辅助
│   ├── install_schedule.ps1# 本地计划任务安装 (科学化7次/天)
│   ├── fetch_pubdates.py   # 拉取UP主历史发布时间
│   ├── analyze_pubtimes.py # 发布规律分析
│   └── weekly_summary.py   # 周度总结生成
├── docs/reports/           # 自动生成的日报 (CI 提交)
├── state/                  # 状态: last_bvid / processed / 片尾识别缓存 (入库同步)
├── data/                   # 状态/字幕/帧 (运行时生成, 不入库)
└── reports/                # 本地日报 (不入库)
```

---

## ❓ FAQ

**Q: "不是推荐"到底是什么?**
A: 李大霄的合规话术。本项目 AI 会专门提取这类暗示,标注语境(机会暗示/风险警示)和情绪(🔴警示/🟢看多/⚪中性/⚠️风险)。

**Q: 片尾暗示是怎么读的?为什么不用 OCR?**
A: 片尾有两类东西 OCR 都拿不到:① 整屏密集文字(合规声明/荐书卡/数据面板),会被 OCR 的"数字过多=噪声"规则丢掉;② **图形动画**(如地球/星球≈"地球顶"、钻石≈"钻石底"、婴儿底等李大霄标志性比喻)。所以单独用 **v4-flash 视觉模型**读片尾画面:ffmpeg 取最后 45 秒的帧 → 16×16 签名去重 + **最远点采样**选出最多 6 帧(保证覆盖口播结束语 / 荐书卡 / 地球动画 / 声明卡等不同场景,均匀取样会整段漏掉)→ 输出【片尾口语/卡片/画面/数据/暗示】五行,写进报告并参与 AI 分析。识别结果按 BV 缓存到 `state/ending_hints.json`,同一视频不重复调用。不想用可以 `config.json` 里设 `"vision": {"enabled": false}` 关闭(主流程不受影响)。

**Q: 视频下载不完整会怎样?**
A: 已加下载完整性校验:用 B站元数据时长对比 `ffprobe` 实际时长,明显偏短(默认 <97%)就自动重下一次;重下仍残缺则在报告里打 `⚠️ 视频下载不完整` 提示。这条校验是必需的——截断的 mp4 容器头仍报完整时长,ffmpeg 会提前结束,抽帧变少,片尾窗口会落在视频中间,导致读不到真正的片尾(以及结论不全)。

**Q: 为什么用 OCR 而不是官方字幕?**
A: 多数视频无官方字幕,且硬字幕(画面内文字)才是他真正展示的内容——点位、百分比、表格全在画面上。

**Q: 本地跑还是云端跑?**
A: 都可以。GitHub Actions 云端全自动(推荐);本地 Windows 计划任务同样支持(科学化 7 次/天,基于 400 条发布历史统计)。

**Q: cookies 失效了怎么办?**
A: 重新扫码登录后更新 Secret:`python _setup/login_wait.py` → 转换 base64 → `gh secret set BILIBILI_COOKIES_B64`。

**Q: 每天跑多少次最合理?**
A: 数据分析显示李大霄日均发布 3.67 条,高峰在 10-15 时(40%)与 18-23 时(43%),中位间隔 3 小时。云端 2 次 + 本地 7 次方案见 `发布规律分析.md`。

---

## 🧩 技术栈

[bilidown CLI](https://github.com/menghuanshiguang/bilibili-downloader-cli)(B站反爬/下载) · ffmpeg(抽帧/片尾取帧) · RapidOCR + onnxruntime(-directml)(OCR) · OpenCode Zen Go API(主, 文本 `deepseek-v4-flash` + 视觉 `deepseek-v4-flash-vision-exp`) + [deepseek-chat-cli](https://github.com/menghuanshiguang/deepseek-chat-cli)(第二兜底) + DeepSeek API(最后兜底, 视觉同模型) · GitHub Actions(定时/部署)

> **调用优先级**:文本分析 `opencode-go → deepseek-chat-cli → DeepSeek 官方 API`;片尾识图 `opencode-go → DeepSeek 官方 API`(网页版 CLI 不支持图片输入,识图不经过它)。每一级失败/无密钥自动降到下一级,日志里能看到实际用了谁。

## ⚖️ 免责声明

本项目所有分析均为对视频内容的**自动化提炼,不构成任何投资建议**。投资有风险,入市需谨慎。

## 📄 License

[MIT](./LICENSE) © 2026 [menghuanshiguang](https://github.com/menghuanshiguang)

---

<p align="center">
  <b>如果这个项目对你有帮助,点个 ⭐ Star 就是最大的支持!</b>
</p>
