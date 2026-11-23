<p align="center">
  <img src="docs/screenshots/banner.png" width="100%" alt="lidaxiao-monitor">
</p>

<h1 align="center">📺 lidaxiao-monitor · 李大霄视频自动监控分析系统</h1>

<p align="center">
  <b>AI 全天候盯着李大霄的B站主页 —— 检测 · 下载 · OCR字幕 · 话术解码 · 每日日报,全自动</b><br>
  <b>免密免费 AI 层开箱即用 · 云端 Actions + 本地守护双保险,永不漏更</b>
</p>

<p align="center">
  <a href="https://github.com/menghuanshiguang/lidaxiao-monitor/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/menghuanshiguang/lidaxiao-monitor/actions"><img src="https://img.shields.io/github/actions/workflow/status/menghuanshiguang/lidaxiao-monitor/monitor.yml?label=auto%20monitor" alt="Actions"></a>
  <img src="https://img.shields.io/badge/Python-3.12+-green.svg" alt="Python">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/AI-Free%20lane%20%2B%204%20fallbacks-brightgreen.svg" alt="AI">
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
| bilidown CLI(wbi 签名 + 412 反爬),状态对比,无新视频秒退;只处理**前天以来**的视频,`--force` 可强制 | 480P mp4,失败重试;**完整性校验**(ffprobe 实际时长 ≥97% 才算下全,截断自动重下) | ffmpeg 抽帧 + RapidOCR,字幕带自动探测,去重/降噪;RTX 显卡 DirectML 加速 6-8 倍 |
| 🤖 **AI 话术解码** | 📄 **每日日报** | ☁️ **云端 + 本地双保险** |
| 核心观点 / 关键数据 / **暗示提取**("不是推荐"反话逐条解码) / 市场定性 / 操作含义 / **观点连续性**(延续/升级/反转一眼看出) | `reports/YYYY-MM-DD.md` 按日期归档,同天多视频追加,顶部当日摘要;发布时按 BV 合并进 `docs/reports/` | Actions 定时(14:40/20:00)+ 本地守护(**14:39/19:59,早 1 分钟**),`gh variable` 认领标志互斥,**谁先跑另一方自动跳过** |
| 🎬 **片尾暗示识别** | 🖼️ **视觉模型读屏** | 🆓 **免密免费 AI 层** |
| 片尾卡片(结束语/荐书卡/合规声明/数据面板)+**图形动画**(地球≈地球顶、钻石≈钻石底、婴儿底)——OCR 全丢,改用**视觉模型**读,报告单列【片尾暗示】 | 16×16 签名去重 + **最远点采样**选帧(≤6 帧覆盖口播/卡片/动画/声明);画面比喻单独提取为 **🎯 画面图形暗示**(形态 → 含义 → 🔴/🟢)置顶 | `opencode-zen-free` 公共免密网关(**零密钥**),文本 + 片尾识图双链路,默认置顶;挂了自动落到下面 4 级兜底 |

**流程示意**

<p align="center">
  <img src="docs/screenshots/pipeline.png" width="100%" alt="pipeline">
</p>

---

## 🏗️ 系统架构: 一个入口, 云端本地双跑

所有入口都汇聚到 **`lidaxiao.py`**(自动识别运行环境),核心流水线在 **`monitor.py`**:

```
                     ┌─────────────────────────────────────────┐
   Actions 定时       │  ☁️ GitHub Actions (windows-latest)      │
   14:40 / 20:00      │  python lidaxiao.py --once [--force]    │
        ┌─────────────┤  ① 查认领标志: 本地在跑(≤30min) → 跳过   │
        │             │  ② 否则: 重置状态 → monitor → 发布报告   │
        │             └─────────────────────────────────────────┘
        │  互斥靠 gh variable LOCAL_DAEMON_CLAIMED_AT (认领标志)
        │             ┌─────────────────────────────────────────┐
        └─────────────┤  🖥️ 本地守护 (14:39 / 19:59, 早1分钟)     │
 local/local_daemon.py │  ① 云端已有今日报告?云端正在跑? → 跳过   │
   托盘版 / 计划任务     │  ② 否则: 写认领标志 → git pull 同步状态   │
                       │  ③ LIDAXIAO_LOCAL=1 跑 monitor (Ollama) │
                       │  ④ 报告+状态 git push [skip ci]          │
                       │  ⑤ ollama stop 释放内存 → 清认领标志      │
                       └─────────────────────────────────────────┘
                                          │
                                          ▼
                       reports/YYYY-MM-DD.md → (按BV合并) → docs/reports/
                                          │
        monitor.py 流水线 ────────────────┘
        检测 → 下载+完整性校验 → 抽帧OCR → 片尾取帧 → 视觉识别片尾
             → AI 话术解码(免费层→4级兜底) → 写日报(含观点连续性)
```

- **本地崩了怎么办**:认领标志 30 分钟自动过期,云端照常接管;云端挂了,本地检查到无报告会补跑。
- **状态永远一份**:`state/`(last_bvid / processed / 片尾识别缓存)**入库随仓库同步**,本地每次运行前 `git pull --ff-only`,云端本地互不重复分析。

---

## 📸 效果展示

**AI 生成的日报(真实内容, 2026-10-02《外资不要利用假期搞小动作》)**

<p align="center">
  <img src="docs/screenshots/report.png" width="85%" alt="report">
</p>

---

## 🚀 快速开始(3 步)

### 1️⃣ 准备依赖

```bash
# Python 3.12+ / Git / ffmpeg (放入 tools/ffmpeg/bin/ 或加入 PATH)

> 接手项目先读 [交接文档](docs/HANDOVER.md)（架构 / 运行 / 已知问题 / 约定）。
pip install -r requirements.txt
git clone https://github.com/menghuanshiguang/bilibili-downloader-cli.git _repo
```

> ⚠️ **bilidown CLI 版本下限 `e6e89a34`**：主路径 `download_to` 从这一版起才发 `Range: bytes=0-`。更旧的版本在机房 IP（GitHub runner）上会被 CDN 以 **403 / 567** 拒掉，只能靠 `lidaxiao/sources/bilibili.py` 的保底路径下下来——日志里出现 `✅ 兜底下载完成(Range)` 就说明 cli 是旧的。`monitor.yml:107` 用 `--depth 1` 跟 main，正常不会钉在旧版，自己克隆时注意别用陈旧的本地副本。

### 2️⃣ 配置(密钥可选,B站登录建议做)

```bash
# 🆓 AI 分析: 什么都不配也能跑 —— 免密免费层默认置顶 (mimo-v2.6-flash-free)
#    想走更稳的付费兜底链再配 (全部可选, 缺谁自动跳过谁):
echo "OPENCODE_GO_API_KEY=sk-xxxx" >> .env   # 主通道 OpenCode Zen Go
echo "DSV_TOKEN=xxxx" >> .env                # 网页版兜底: 文本 deepseek-chat-cli / 片尾识图 deepseek-vision-cli
echo "DEEPSEEK_API_KEY=sk-xxxx" >> .env      # 最后兜底 DeepSeek 官方

# B站扫码登录 (建议: 解锁高清 + 降低 412 风控)
python _setup/login_wait.py     # 浏览器打开二维码, B站App扫码
```

### 3️⃣ 运行

```bash
python lidaxiao.py --once          # ⭐ 统一入口: 手动跑一次 (自动识别云端/本地)
python lidaxiao.py --daemon        # 本地常驻守护 (14:39/19:59 到点自动跑)
python lidaxiao.py --once --force  # 强制处理最新视频 (豁免日期过滤, 按发布日写报告)

python monitor.py                  # 直接跑核心流水线 (无新视频秒退)
python monitor.py --check-vision   # 自检片尾视觉通道 (不下载不分析)
```

> 💻 **Windows 懒人包**:`run_monitor.cmd`(计划任务/手动包装,日志进 `data\run.log`)· `start_local_daemon.cmd`(本地守护,Ollama 环境已预设)· `local/local_daemon_tray.py`(**托盘版**:右下角常驻,菜单 立即运行/查看日志/退出)
>
> 💡 **更多用法**:批量补全历史字幕 `python local/batch_subtitles.py` · 发布规律分析 `python _setup/analyze_pubtimes.py` · 本地计划任务 `powershell -File _setup/install_schedule.ps1`(科学化 7 次/天)· 失败重跑 `python _setup/reset_failed_state.py`

---

## ⏰ 自动运行(GitHub Actions + 本地守护)

### 云端: 内置 CI,配好 Secret 即全自动

| Secret | 必需? | 说明 |
|--------|------|------|
| `BILIBILI_COOKIES_B64` | ✅ 必需 | B站登录 cookies(base64),下载/风控用 |
| `OPENCODE_GO_API_KEY` | 可选 | OpenCode Zen Go API(AI 主通道,模型 `deepseek-flash`) |
| `DSV_TOKEN` | 可选 | DeepSeek 网页版 token(文本第二兜底 `deepseek-chat-cli` + 片尾识图 `deepseek-vision-cli`) |
| `DEEPSEEK_API_KEY` | 可选 | DeepSeek 官方 API(最后兜底) |

> 🆓 **三个 AI 密钥一个都不配也能跑**:免密免费层(`opencode-zen-free`)默认开启,文本 + 识图全覆盖。`deepseek-chat-cli` / `deepseek-vision-cli` 仓库均已公开,Actions 直接克隆,无需 PAT。

```bash
gh secret set BILIBILI_COOKIES_B64 --repo <your>/lidaxiao-monitor
gh secret set OPENCODE_GO_API_KEY --repo <your>/lidaxiao-monitor   # 可选
```

- 每天 **北京时间 14:40 / 20:00** 自动运行;手动 `Run workflow` 可勾选 **`force`** 输入强制重处理最新视频
- 视觉自检改为**手动触发**:`workflow_dispatch` 跑 `vision-selftest.yml`,或本地 `python monitor.py --check-vision`;定时任务不再预检(少一次 dsv 调用、省约 60s,也少一次风控暴露)
- 状态走 Actions 缓存,无新视频秒退;报告 + `state/` 自动提交(`[skip ci]`),另上传 artifact 保留 14 天

### 本地: 守护进程与云端无缝错峰

```bash
python lidaxiao.py --daemon        # 或 python local/local_daemon.py · local/local_daemon_tray.py
```

- **时间错峰**:本地 14:39/19:59,比云端早 1 分钟;错过 slot 60 分钟内仍补跑
- **三方检查后才动手**:① 云端已有前天/昨天/今天的报告?② 云端 workflow 正在跑?③ 都没有 → 写认领标志(`gh variable set`,需先 `gh auth login`)→ 开跑
- **本地优先模型**:Ollama 本地大模型(`batiai/qwen3.6-35b:iq3`,MoE 40 专家下 CPU),零 API 成本;跑完自动 `ollama stop` 释放内存
- 报告/状态照常推送回仓库,与云端格式完全一致

---

## 🤖 模型选择与调用链(2026-10 更新)

**文本分析固定优先级**(`call_llm`,任一级失败/无密钥自动降级):

| # | 通道 | provider | 模型 | 密钥 |
|---|------|----------|------|------|
| 1 | 🆓 免密免费层(默认开启) | `opencode-zen-free` | `mimo-v2.6-flash-free` | **无需任何密钥** |
| 2 | OpenCode Zen Go | `opencode-go` | `deepseek-flash` | `OPENCODE_GO_API_KEY` |
| 3 | deepseek-chat-cli(网页版) | `deepseek-chat-cli` | 网页版会话 | `DSV_TOKEN` |
| 4 | DeepSeek 官方 API | `deepseek` | `deepseek-flash` | `DEEPSEEK_API_KEY` |
| — | 本地模式(`LIDAXIAO_LOCAL=1`) | `ollama` | `batiai/qwen3.6-35b:iq3` | 无需密钥 |

**片尾视觉固定优先级**(`vision_providers`,任一级失败/无密钥自动降级):

| # | 通道 | provider | 密钥 |
|---|------|----------|------|
| 1 | 🆓 MiMo 免密 | `opencode-zen-free` | **无需任何密钥** |
| 2 | OpenCode Zen Go | `opencode-go` | `OPENCODE_GO_API_KEY` |
| 3 | [deepseek-vision-cli](https://github.com/menghuanshiguang/deepseek-vision-cli)(网页版「识图模式」) | `deepseek-vision-cli` | `DSV_TOKEN` |
| 4 | DeepSeek 官方 API | `deepseek` | `DEEPSEEK_API_KEY` |

> 第 3 级由 `dsv.py` 驱动浏览器走 DeepSeek 网页版**识图模式**(真·多模态非 OCR),多帧一次会话发送、用后即删;iSH 用系统 `minis-browser-use`,Windows 用 playwright + 系统 Edge,零浏览器下载。`config.json` 里 `"vision_cli": {"enabled": false}` 可单独关闭。

> 🔧 **免密层协议细节**(实测自 `zouyuxuan122/dsh-our-free-model`):必须 `stream:true`(非流式 500/403);body 要带 bash/glob/grep/read 四个最小诱饵 tools + `tool_choice:none`;`reasoning_effort` 是空操作,思考与正文抢 `max_tokens` 所以预算靠顶满 `max_tokens`;免费额度按 session 计,`ses_`/`msg_` id 用 sha256 稳定派生。以上均已内置,无需配置。
>
> 🔧 **想关掉免费层**:`config.json` 里设 `"llm_free": {"enabled": false}` / `"vision_free": {"enabled": false}`。

**DeepSeek 官方模型名(2026-09 起)**:可用 `deepseek-flash`(=V4.1-Flash,唯一支持图像理解)、`deepseek-v4-pro`。旧名 `deepseek-chat`/`deepseek-reasoner`/`deepseek-v4-flash` 已下线,请求由 V4.1 Flash 承接。思考强度 `reasoningEffort`: `low`/`high`/`max`(`config.json` 配置,留空关闭)。

> 💰 省钱提示:官方 API 空闲时段(非工作日/非 9:00-12:00、14:00-18:00)半价;或者干脆全用免费层 + 本地 Ollama。

---

## 📂 目录结构

```
├── README.md                # 本文件 (含目录结构与根目录保留清单)
├── LICENSE                  # MIT
├── .gitignore               # 忽略 __pycache__ / data/ / reports/ 等运行期产物
├── .github/workflows/       # monitor.yml · monitor-ubuntu.yml · vision-selftest.yml · tests.yml
├── requirements.txt         # Python 依赖 (monitor.yml:41 直接 pip install -r 它)
├── run_monitor.cmd          # Windows 计划任务/手动包装 (日志 → data\run.log)
├── start_local_daemon.cmd   # Windows 本地守护启动 (Ollama 环境预设)
├── lidaxiao.py              # ⭐ 统一入口 (--once 云端/本地自适应, --daemon 本地守护)
├── monitor.py               # 编排 + 入口: 只剩 main / process_video (466 行)
├── config.json              # 配置 (UID/OCR/LLM五级通道/片尾视觉)
├── aoyuon_ocr.py            # 兼容 shim (1 行) → lidaxiao/ocr/engines/aoyuon.py
├── xiaomi_ocr.py            # 兼容 shim (1 行) → lidaxiao/ocr/engines/xiaomi.py
│
├── lidaxiao/                # 核心包: 依赖单向, common → ... → report → monitor
│   ├── common.py            # 路径 / 时区 / ffmpeg 定位
│   ├── config.py            # 配置读取与密钥加载
│   ├── textutil.py          # 日志 / 读写 / 归一化 / 水印过滤
│   ├── prompts.py           # 提示词与报告结构常量
│   ├── audit.py             # 审计整改的确定性修复 (纯函数)
│   ├── ocr/
│   │   ├── frames.py        # 抽帧 / 单帧 OCR / 字幕带探测
│   │   ├── pipeline.py      # 引擎选择与回退 / 逐帧扫描 / 帧去重
│   │   ├── engine.py        # RapidOCR 单例 (DirectML 优先)
│   │   ├── dedup.py         # 帧签名与游程压缩
│   │   └── engines/         # aoyuon.py (本地权重) · xiaomi.py (llama-server)
│   ├── sources/
│   │   └── bilibili.py      # API / 更新检测 / 下载 (Range 兜底)
│   ├── analyze/
│   │   └── llm.py           # LLM 调用 / CLI 通道 / 结果整形
│   ├── vision/
│   │   └── ending.py        # 片尾抽帧 + 视觉识别 + 缓存
│   ├── report/
│   │   └── render.py        # 渲染 / 合并 / 后处理
│   └── runtime/
│       └── state.py         # processed.txt + 并发锁
│
├── local/                   # 本地侧 (云端链路不 import)
│   ├── local_daemon.py      # 常驻定时 + 认领标志互斥 + 报告发布
│   ├── local_daemon_tray.py # 托盘版 (pystray)
│   ├── local_render.py      # 本地 Ollama 渲染通道
│   └── batch_subtitles.py   # 批量字幕提取 (多进程断点续传)
│
├── _setup/                  # 一次性/运维脚本 (登录、计划任务、周报、补处理)
├── tests/                   # 纯函数单测 + run_all.sh (三件套之一)
├── bench/                   # A/B 与帧样本
├── docs/                    # 交接文档 + docs/reports/ 日报 (自动发布)
├── _archive/workflows/      # 归档的 12 个实验性 workflow
├── skills/daxiao-strategy/  # 🧠 策略研判 skill (报告 → 定性 → 逐仓操作)
├── scripts/ · aoyuon/       # 辅助脚本 · vendor 上游模型代码与权重 (勿改)
├── state/                   # 共享状态 (入库): last_bvid / processed / 片尾缓存
├── data/                    # 运行时 (不入库): 视频 / 帧 / 日志 / 锁
└── reports/                 # 本地日报 (不入库, 发布时按 BV 合并进 docs/reports)
```

### 根目录保留清单(拆分后口径)

**`.py` 严格**:根目录只允许 `monitor.py`、`lidaxiao.py` 两个入口,加 `aoyuon_ocr.py` /
`xiaomi_ocr.py` 两个 **1 行兼容 shim**(旧引用 `from xiaomi_ocr import XiaomiOCR` 仍可用)。
业务代码一律在 `lidaxiao/`、`local/`、`_setup/`、`bench/` 等子目录里,根目录业务 `.py` 为 0。

**非 `.py` 走白名单**,只保留这几个:

| 文件 | 为什么必须在根目录 |
|---|---|
| `README.md` | 仓库首页 |
| `LICENSE` | MIT 许可证 |
| `.gitignore` | 忽略 `__pycache__`/`data/`/`reports/` 等运行期产物 |
| `requirements.txt` | `monitor.yml:41` 直接 `pip install -r requirements.txt`,搬走 CI 当场断 |
| `run_monitor.cmd` | Windows 计划任务/手动入口 |
| `start_local_daemon.cmd` | Windows 本地守护入口 |

不在白名单的散文件已归位:`agent_task.md`、`发布规律分析.md` → `docs/`;`audit_p2.py`、
`frame_dedup.py`、`batch_subtitles.py`、`local_*.py` → 包内或 `local/`。

### 对外可见变更(拆分后 `monitor` 不再导出的名字)

拆分原则是「只搬家不改逻辑」,但模块化必然让一部分名字离开 `monitor` 的命名空间。
仓库内已逐个 grep 确认 **0 消费者**;仓库外若有旧脚本按 `monitor.xxx` 取用,按下表换路径。

| 不再从 `monitor` 可见 | 现在去哪 |
|---|---|
| `win_to_msys` | **无替代,已删除**(Windows 路径转换随 bilidown 调用点并入 `lidaxiao/sources/bilibili.py`) |
| `BASH` / `BILIDOWN_SCRIPT` | `lidaxiao.sources.bilibili` |
| `DEFAULT_UID` / `load_deepseek_key` | `lidaxiao.config` |
| `FFMPEG` | `lidaxiao.common` |
| `LOCK_STALE_SECONDS` / `read_state_lines` / `_pid_alive` | `lidaxiao.runtime.state` |
| `get_ocr` / `_ocr`(单例) / `_ocr_video_impl` / `frame_sig` / `_sig_mad` / `dedupe_frames` | `lidaxiao.ocr.engine` / `lidaxiao.ocr.pipeline` |
| `tail_frames` / `load_ending_cache` / `save_ending_cache` / `_ending_cache_path` | `lidaxiao.vision.ending` |
| `vision_providers` / `_call_vision_once` / `_call_dsv` / `VISION_CLI_PROVIDER` | `lidaxiao.analyze.llm` |
| `ENDING_PROMPT_VER` | `lidaxiao.prompts` |
| stdlib 模块名 `base64` / `datetime` / `glob` / `hashlib` / `io` / `json` / `re` / `shutil` / `subprocess` / `urllib` | 已从 `monitor` 移除(拆分后无使用者),需要就自己 import |

**仍然从 `monitor` 可见**(兼容面,`bench/`、`_setup/`、旧脚本在用,共 31 个已实测):
`load_config`、`load_llm_key`、`RunLock`、`mark_processed`、`processed_bvids`、`processed_status_map`、
`is_success_status`、`is_from_baseline`、`read_ending_hint`、`ocr_video`、`merge_ts_lines`、`list_line`、
`ANALYZE_SYSTEM`、`ENDING_PROMPT`、`REPORT_SECTIONS`、`REPORT_TAIL`、`ffprobe_duration`、`extract_frames`、
`ocr_frame`、`probe_band_heuristic`、`probe_subtitle_band`、`filter_ocr_text`、`norm_text`、`read_text`、
`write_text`、`WORKDIR`、`CN_TZ`、`STATE_DIR`、`DISCLAIMER`、`FFMPEG_DIR`、`FFPROBE`。

## 🧪 测试与验收口径

**三件套**（每次改动都要跑）：

```bash
python3 -m py_compile monitor.py lidaxiao.py aoyuon_ocr.py xiaomi_ocr.py \
                       lidaxiao/*.py lidaxiao/*/*.py lidaxiao/*/*/*.py
python3 -m pyflakes monitor.py lidaxiao.py lidaxiao/ local/ | rg 'undefined name'
bash tests/run_all.sh
```

**本地判定是「不新增失败」，不是「全绿」。**
本机环境是 iOS arm64 + Python 3.15.2，numpy / Pillow 没有 cp315 轮子也编不出来，所以 `bash tests/run_all.sh` **永远有这 3 个固定失败**，与代码无关：

| 固定失败 | 原因 |
|---|---|
| `test_aoyuon_ocr.py` | `No module named 'numpy'` |
| `test_band.py` | `No module named 'PIL'` |
| `test_frame_dedup.py` | `No module named 'PIL'`（经 `lidaxiao/ocr/dedup.py`） |

判据：**失败集合恒为这 3 个**，passed 数只增不减（拆分收尾时 33 passed；加入 `tests/test_disclaimer.py` 后 47 passed / 3 FAILED）。不许为了让本地变绿去改测试、加 `try/except`、跳过用例。**「本地跑通测试」这句话不成立**，写交付物时别用。

**「全绿」由 `.github/workflows/tests.yml` 承担**：push 即跑 ubuntu-latest + Python 3.12（装了 numpy / pillow / opencv-python-headless / ripgrep）。最近一次绿 run 是 [`45235155031`](https://github.com/menghuanshiguang/lidaxiao-monitor/actions/runs/45235155031)（`0376f75`）；用例数在 ubuntu 上是 53 passed / 0 failed（`a224e65` 那次 run 的日志实测），加新测试后以 run 日志为准。里面两个**前置断言**（`pyflakes --version`、`command -v rg`）别删 —— 缺了它们 `run_all.sh` 有两条静默通过路径。

**业务链路**另用 `monitor.yml` 勾 `force=true` 跑一次，日志里要看到「主扫描完成」「视频处理完成」「报告已提交并推送」，且无 `Traceback`。

**报告对比口径**：行数用 `len(text.splitlines())`（末尾换行不算一行）；区块按 `【…】` **行内任意位置**数（按行首会得 0）；免责声明只报 `不构成投资建议` / `市场有巨大风险` **两列不报合计**。工具在工作区 `tools/factcheck.py`（不在仓库内）。

## 🧠 配套 Skill: daxiao-strategy

[`skills/daxiao-strategy`](./skills/daxiao-strategy/SKILL.md) 把日报转成**可执行决策**(观点 → 定性 → 操作):

- **五级宏观定调**:A 高度警惕 → B 谨慎防御 → C 温柔市/止跌 → D 反弹(非反转) → E 结构性看多
- **老登/小登风格循环理论** + 话术解码表("不是推荐"两种用法: 光杆点名=隐性强推; 报跌幅=真风险提示)
- **量能一票否决**:"没量=不是反转",缩量一律按反弹处理
- 流程:`market daxiao` 拉客观数据 → 读 `docs/reports/latest.md` → 定级 → 话术解码 → 逐仓动作

---

## ❓ FAQ

**Q: 一个密钥都没有,真的能跑吗?**
A: 能。免密免费层(`mimo-v2.6-flash-free`)默认置顶,文本分析和片尾识图都覆盖;配好 `BILIBILI_COOKIES_B64` 就能全流程无人值守。付费通道只是"更稳的兜底"。

**Q: 本地和云端会不会重复处理同一个视频?**
A: 不会,三重互斥:① 本地先跑并写 `gh variable` 认领标志,云端看到标志新鲜(≤30分钟)直接跳过;② 本地跑之前先查云端报告是否已存在、workflow 是否正在跑;③ `state/processed.txt` 入库同步,同 BV 不会分析两次。

**Q: "不是推荐"到底是什么?**
A: 李大霄的核心反话, 两种用法要分开读: ① 光杆点名"XX不是推荐"= 隐性强推(历史兑现过韩国/恒科/银行/券商); ② "报XX跌了X%, 不是推荐"= 字面意思, 即对该板块的**风险提示**(如海力士/三星/铠侠)。本项目 AI 逐条按语境判定, 标注 🔴警示/🟢看多/⚪中性/⚠️风险, 不会一刀切解读。

**Q: 片尾暗示是怎么读的?为什么不用 OCR?**
A: 片尾有两类 OCR 拿不到的东西:① 整屏密集文字(合规声明/荐书卡/数据面板),会被"数字过多=噪声"规则丢掉;② **图形动画**(地球≈地球顶、钻石≈钻石底、婴儿底、熊猫/熊玩偶≈熊市、牛≈牛市等标志性比喻)。所以用视觉模型读:片尾 45 秒取帧 → 签名去重 + 最远点采样选 ≤6 帧 → 输出【片尾口语/卡片/画面/画面暗示/数据/暗示】六行,按 BV 缓存进 `state/ending_hints.json`。设 `"vision": {"enabled": false}` 可关闭,主流程不受影响。

**Q: 什么叫"画面图形暗示"?**
A: 画面比喻比文字更能透露真实态度,刻意与文字暗示分列:

| 画面图形 | 含义 | 方向 |
|---|---|---|
| 地球 / 星球 / 地球仪 | **地球顶** | 🔴 高位见顶风险 |
| 钻石 / 宝石 | **钻石底** | 🟢 低位机会 |
| 婴儿 / 儿童 | **婴儿底** | 🟢 底部区域 |
| 山峰 / 登顶 | 顶部 / 高位 | 🔴 |
| 山谷 / 下坡 | 底部 / 低位 | 🟢 |
| 向上箭头 / 向下箭头 | 看多 / 看空 | 🟢 / 🔴 |
| 书 / 二维码等 | 不构成行情隐喻 | ⚪ 不附会 |

报告里单独置顶:`🎯 画面图形暗示: ...`;文字分析另加 `片尾图形:` 条目并要求体现在【操作含义】里。想重跑某视频:删掉 `state/ending_hints.json` 里对应条目(提示词版本号 `ENDING_PROMPT_VER` 升版会自动失效)。

**Q: `--force` 什么时候用?**
A: 强制重处理最新视频时用(`lidaxiao.py --once --force` / Actions 手动触发勾选 `force`)。会豁免"前天以来"的日期过滤,且报告按**视频发布日**命名、发布闸门走 mtime(6小时)兜底,旧视频也能正常归档发布。

**Q: 视频下载不完整会怎样?**
A: 用 B站元数据时长对比 `ffprobe` 实际时长,<97% 自动重下一次;仍残缺则报告打 `⚠️ 视频下载不完整`。这条校验必需——截断的 mp4 容器头仍报完整时长,ffmpeg 会提前结束,片尾窗口落在视频中间,读不到真正的片尾。

**Q: 为什么用 OCR 而不是官方字幕?**
A: 多数视频无官方字幕,且硬字幕(画面内文字)才是他真正展示的内容——点位、百分比、表格全在画面上。

**Q: cookies 失效了怎么办?**
A: `python _setup/login_wait.py` 重新扫码 → 转 base64 → `gh secret set BILIBILI_COOKIES_B64`。

**Q: 每天跑多少次最合理?**
A: 400 条样本统计:日均 3.67 条,高峰 10-15 时(40%)与 18-23 时(43%),中位间隔 3 小时 → 云端 2 次 + 本地守护 2 次 + 计划任务 7 次方案,依据见 [`发布规律分析.md`](./docs/发布规律分析.md)。

---

## 🧩 技术栈

[bilidown CLI](https://github.com/menghuanshiguang/bilibili-downloader-cli)(B站反爬/下载) · ffmpeg(抽帧/片尾取帧/时长校验) · RapidOCR + onnxruntime-directml(OCR) · AI: 🆓 OpenCode Zen 免密层(`mimo-v2.6-flash-free`) → [OpenCode Zen Go](https://opencode.ai)(`deepseek-flash`) → [deepseek-chat-cli](https://github.com/menghuanshiguang/deepseek-chat-cli) / [deepseek-vision-cli](https://github.com/menghuanshiguang/deepseek-vision-cli) → DeepSeek 官方 API · 本地 Ollama(`qwen3.6-35b`) · GitHub Actions + 本地守护进程

> **调用优先级**:文本 `免费层 → opencode-go → deepseek-chat-cli → DeepSeek 官方`;片尾识图 `免费层 → opencode-go → deepseek-vision-cli → DeepSeek 官方`。每一级失败/无密钥自动降级,日志里能看到实际用了谁。

## ⚖️ 免责声明

本项目所有分析均为对视频内容的**自动化提炼,不构成任何投资建议**。投资有风险,入市需谨慎。

## 📄 License

[MIT](./LICENSE) © 2026 [menghuanshiguang](https://github.com/menghuanshiguang)

---

<p align="center">
  <b>如果这个项目对你有帮助,点个 ⭐ Star 就是最大的支持!</b>
</p>
