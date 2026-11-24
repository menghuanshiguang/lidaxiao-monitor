# 交接文档 · 代码拆分收尾（已完成）

> 更新：2026-11-24 · 分支 `main` · 更新时 HEAD `5e6fc15`
> 配套：[HANDOVER.md](./HANDOVER.md)（项目怎么跑）· 根 [README.md](../README.md)（目录树 / 根目录保留清单）
> 本文所有数字都是对着仓库实测的，命令写在每节里；发现对不上，以命令输出为准，别信文字。

---

## 1. 结论

| 指标 | 拆分前 | 现在 |
|---|---|---|
| `monitor.py` | 2,639 行 | **464 行**（−82%） |
| `monitor.py` 顶层内容 | 下载 / OCR / 片尾识别 / LLM 分析 / 渲染 / 发布全在里面 | 只剩 `process_video`（L282–351）、`main`（L366–460，`add_argument` 5 处在里面）+ `if __name__` |
| 根目录业务 `.py` | 10 个 | **0 个**（只剩 2 个入口 + 2 个 1 行 shim） |
| 单文件最大 | `monitor.py` 2,639 | `local/local_daemon.py` 712（包内最大 `lidaxiao/analyze/llm.py` 535），全仓 `.py` 无一 ≥ 900 |

> 表内数字是 `d59a0b7`（代码侧最后一笔）的实测值。其后只有文档提交；行数变了就以 `git show HEAD:<path>` 数换行为准。

拆分原则是**只搬家不改逻辑**：搬家类 commit 里没有一处改行为。已知缺陷单独立项（第 10 节），不混进搬家 commit —— 这条在 P1–P5 每一笔上都核过：函数体逐字节相等，只有 import 与函数外注释跟着改。

---

## 2. 代码去哪了

### 2.1 包内 `lidaxiao/`（业务，依赖单向）

| 模块 | 行数 | 职责 |
|---|---|---|
| `common.py` | 27 | 路径 / 时区 / ffmpeg 定位 |
| `config.py` | 175 | 配置读取与默认值、LLM 密钥加载 |
| `textutil.py` | 58 | 日志 / 读写 / 归一化 / 水印过滤 |
| `prompts.py` | 62 | 提示词与报告结构常量 |
| `audit.py` | 259 | 审计整改的确定性修复（纯函数） |
| `ocr/frames.py` | 156 | 抽帧 / 单帧 OCR / 字幕带探测 |
| `ocr/pipeline.py` | 226 | 引擎选择与回退 / 逐帧扫描 / 帧去重 |
| `ocr/engine.py` | 20 | RapidOCR 单例（DirectML 优先） |
| `ocr/dedup.py` | 82 | 帧签名与游程压缩 |
| `ocr/engines/aoyuon.py` | 219 | 本地权重引擎（vendor 包装） |
| `ocr/engines/xiaomi.py` | 145 | llama-server 引擎 |
| `sources/bilibili.py` | 308 | API / 更新检测 / 下载（含 `Range` 保底） |
| `llmio.py` | 134 | LLM 通道公共件：模型名 / 思考参数 / 免密车道 / CLI 路径定位（`_warn_model`、`_apply_thinking`、`_find_cli_path`、`_free_derive_id`、`_free_lane_post`） |
| `analyze/llm.py` | 535 | LLM 调用 / CLI 通道 / 结果整形 |
| `vision/channel.py` | 242 | 片尾视觉通道：provider 选择 / 单次多图请求 / dsv CLI 调用 / 自检 |
| `vision/ending.py` | 136 | 片尾抽帧 + 缓存 + 调度视觉通道 |
| `report/render.py` | 258 | 渲染 / 合并 / 后处理 / 免责声明 |
| `runtime/state.py` | 137 | `processed.txt` + 并发锁 |

### 2.2 入口与本地侧

| 文件 | 行数 | 职责 |
|---|---|---|
| `monitor.py` | 464 | 编排 + 入口 |
| `lidaxiao.py` | 69 | 统一入口（`--once` / `--daemon`，自动识别云端/本地） |
| `aoyuon_ocr.py` / `xiaomi_ocr.py` | 各 1 | **兼容 shim**，只做转发，保证 `from xiaomi_ocr import XiaomiOCR` 仍可用 |
| `local/local_daemon.py` | 712 | 常驻定时 + 认领标志互斥 + 报告发布 |
| `local/local_render.py` | 324 | 本地 Ollama 渲染通道 |
| `local/local_daemon_tray.py` | 88 | 托盘版（pystray） |
| `local/batch_subtitles.py` | 263 | 批量字幕（多进程断点续传） |

> 行数是 `2de9fdd` 之后的实测值（`git show HEAD:<path>` 数换行）。`monitor.py` 会随第 10 节的结构债继续变，抄表前先 `git log -1`。

### 2.3 依赖方向（硬约束）

```
common → textutil → prompts → ocr → sources → analyze → report → monitor
```

- 新模块 `config` 与 `common` 同层，只可 import `common`。
- `sources` 依赖 `ocr` 的 `ffprobe_duration`，不许反过来。
- **反向边已消除**：视觉通道原先实现在 `lidaxiao/analyze/llm.py`，`vision/ending.py` 只能反向 import 它（P3 时临时裁定允许这一条）。`2de9fdd` 把视觉实现搬进 `lidaxiao/vision/channel.py`；`_warn_model` / `_apply_thinking` / `_find_cli_path` 这类**文本与视觉共用的件下沉到 `lidaxiao/llmio.py`**，没有搬进 `vision` —— 搬进 vision 只会把反向边换个方向。
- 硬约束（永久，无例外）：`analyze` 不得 import `vision`；`vision` 不得 import `sources` / `report` / `monitor`。
- 自检：
  ```bash
  python3 - <<'PY'
  import glob
  hits=[(p,i,l.rstrip()) for p in glob.glob('lidaxiao/vision/**/*.py',recursive=True)
        for i,l in enumerate(open(p,encoding='utf-8'),1) if 'lidaxiao.analyze' in l]
  print(hits or 'NO_REVERSE_EDGE')
  PY
  ```

---

## 3. 提交对照（回溯与回滚用）

基线：`d9ce664`（拆分前最后一次线上报告提交）。

| 阶段 | commit | 内容 |
|---|---|---|
| P1 | `f188010` | `config.py`：`load_config` + 密钥加载 |
| P2 | `3140e12` | `ocr/pipeline.py` + `ocr/engine.py` |
| P3 | `18de5b0` | `vision/ending.py` |
| P4 | `784f38e` | `runtime/state.py` |
| P5 | `55f9ccf` | `audit_p2.py` → `lidaxiao/audit.py` |
| P5 | `a1e3841` | `frame_dedup.py` → `lidaxiao/ocr/dedup.py` |
| P5 | `035f0f5` | `batch_subtitles.py` → `local/` |
| P5 | `67c6629` | 两个 OCR 引擎迁入 `lidaxiao/ocr/engines/`，根目录留 1 行 shim |
| P5 | `0f747ee` | 12 个实验性 workflow → `_archive/workflows/` |
| P5 | `8066cd0` | 清死引用与重复 import |
| P5 | `ff37735` | `local_daemon` / `local_render` / `local_daemon_tray` → `local/` |
| P5 | `530e0f2` | `list_line` 进 `render.py`，`monitor` 只剩 `main` + `process_video` |
| P5 | `866de78` | README 目录树按拆分后重写 |
| P5 | `7547b06` | 新增 `tests.yml`（「Actions 全绿」的载体） |
| 文档 | `ffafd00` | 两份散文件归位 `docs/`，README 补根目录保留清单 |
| 修正 | `608e50a` | 合并线上报告提交（拆分后首次对账） |
| 修正 | `215102a` | `tests.yml` 两处 step name 含冒号导致 YAML 解析失败 |
| 修正 | `a224e65` | `tests.yml` 补 `opencv-python-headless`（`test_aoyuon_ocr` 要 cv2） |
| 修正 | `a24505a` | README 补「对外可见变更」一节 |
| 修正 | `5e6fc15` | `render.py` 免责声明改归一化匹配（全角逗号不再重复追加） |
| 结构债 | `2de9fdd` | 视觉通道搬进 `vision/channel.py`，共用件下沉 `llmio.py`，消除 `vision → analyze` 反向边 |
| 结构债 | `d59a0b7` | 撤掉 `monitor` 的 25 条 imported-but-unused 再导出，消费者改直接 import |

> ⚠️ 这个仓库的 `git` 是 libgit2 实现，**路径限定的 `git log`/`git diff` 会静默返回空**（`git log --oneline -- lidaxiao/` 只出 1 笔，实际 20+ 笔）。要按路径回溯，用 `git log --oneline d9ce664..HEAD` 全量列出来人工筛，或 `git show <rev>:<path>` 取内容。

---

## 4. 验证规范（三件套 + 线上）

| 检查 | 命令 | 抓什么 | 判据 |
|---|---|---|---|
| 编译 | `python3 -m py_compile <files>` | 语法错误 | EXIT=0 |
| 静态 | `python3 -m pyflakes ... \| rg 'undefined name'` | 编译能过、运行时才炸的缺 import | 0 条 |
| 单测 | `bash tests/run_all.sh` | 纯函数逻辑回归 | **失败集合不变**（见下） |
| 线上 | ubuntu：`.github/workflows/tests.yml`；业务链路：`monitor.yml -f force=true` | 真实依赖与真实网络 | tests.yml 绿 + 日志无 `Traceback` |

### 4.1 本地测试的正确说法

本机是 iOS arm64 + Python 3.15.2，numpy / Pillow 没有 cp315 轮子也编不出来。所以 `bash tests/run_all.sh` **永远有 3 个固定失败**，与代码无关：

| 固定失败 | 原因 |
|---|---|
| `test_aoyuon_ocr.py` | `No module named 'numpy'` |
| `test_band.py` | `No module named 'PIL'` |
| `test_frame_dedup.py` | `No module named 'PIL'`（经 `lidaxiao/ocr/dedup.py`） |

- **判据**：失败集合恒为这 3 个，passed 数只增不减。拆分收尾时 33 passed；加入 `tests/test_disclaimer.py` 后 47 passed / 3 FAILED。
- **不许**为了让本地变绿去改测试、加 `try/except`、跳过用例。
- **「本地跑通测试」这句话不成立**，写交付物时别用。

### 4.2 「全绿」的权威在 Actions

`tests.yml` 只跑单测与静态检查，push 即跑 ubuntu-latest + Python 3.12（装了 numpy / pillow / opencv-python-headless / ripgrep）。最近一次绿 run：`45235155031`（`0376f75`）；用例数在 ubuntu 上是 53（`a224e65` 那次日志实测），加新测试后以 run 日志为准。

里面有两个**前置断言**，别删：`python3 -m pyflakes --version` 与 `command -v rg`。缺了它们 `run_all.sh` 有两条静默通过路径 —— pyflakes 未安装时它打印「跳过」算通过；`rg` 不存在时 `| rg 'undefined name'` 返回空也算通过。

### 4.3 历史教训

拆分时 `textutil.py` 漏了 `import os`，py_compile / pyflakes / 单测前三项全过，线上跑到「字幕已保存」才炸。所以线上那一项不能省。

---

## 5. 报告对比口径（别再各用一套）

对比拆分前后日报，必须口径一致，否则数字对不上账。

| 指标 | 定义 | 坑 |
|---|---|---|
| 行数 | `len(text.splitlines())` | `split('\n')` 会多 1（文件末尾有换行）。这五个样本都因此多算过 |
| 章节 | `^##\s` | —— |
| 区块 | `【([^】\n]+)】`，**行内任意位置** | 按行首口径数会得 **0**（这些日报的区块全在行内），会被误判成「区块丢了」 |
| 免责声明 | `不构成投资建议`、`市场有巨大风险` **两列分开报，不报合计** | 合计会让人误以为有第三类声明 |
| 字符串匹配 | 精确子串，不做归一化、区分大小写 | 大小写/标点归一化会让两个不同表述混成一个 |

基线五样本（拆分前 `b25a4be` 与三次 force 及对照组）：字节 5105 / 4773 / 6537 / 6594 / 6002，行数 64 / 62 / 65 / 70 / 67，免责两列 2/1/1/1/1 与 2/2/2/2/2，区块 12 / 12 / 14 / 13 / 13。

工具在工作区的 `tools/factcheck.py`（**不在仓库里**；仓库 `.gitignore` 里的 `/tools/` 是仓库内同名目录，别混为一谈）。`--selftest` 21 项，`conventions` 字段随每次 JSON 输出带出上面这套口径。

### 5.1 三次 force 跑与 baseline 的真实差异（归因写在这里，别重新调查一遍）

- **结构一致**：12 个 `【…】` 段全部在；`【观点连续性】` 在，只是标题与内容被并成一行（章节序列一致、仅行内合并，不是缺章节）。
- **`仓位`**：baseline 是 `3成仓`，新产物全是 `不适用`。这不是纯模型随机 —— `render.py` 的兜底是「解析出的仓位里没有数字，或含长期持有/适度/合理/注意 → 写死 `不适用`」，baseline 当时匹配上了，后来模型换了格式触发兜底。属**代码兜底的最终形态 + 模型输出格式触发**。
- **免责声明条数**：baseline 2 处、新产物 1 处，是 `ensure_disclaimer` 只认半角 `,` 的缺陷被模型的全角 `，` 触发（已在 `5e6fc15` 改成归一化匹配）。
- **LLM 段落**：三次新 run 相对 baseline 的均距 0.717–0.752，对照组 0.747，属同一波动带。

---

## 6. 已知陷阱

| 陷阱 | 说明 |
|---|---|
| **`WORKDIR` 指向错** | 包内模块 `__file__` 是 `lidaxiao/xxx.py`，仓库根要取**父目录**：`dirname(dirname(abspath(__file__)))`。踩过一次，表现为找不到 `config.json` |
| **循环 import** | 依赖方向见 2.3，反向引用直接 ImportError |
| **`set -e` 下的探测行** | 跑 `.sh` 脚本做测试时，`YTDLP="$(command -v yt-dlp)"` 这类探测在工具缺失时会整脚本退出；用副本把这一行改掉再测，别改被验证的代码 |
| **pyflakes 抓不到的** | 路径计算错、变量名拼错但恰好有同名、默认值被顺手改 —— 只能靠日志 |
| **不要动 `aoyuon/`** | vendor 目录（上游模型代码 + 权重），要改去 `aoyuon-ocr` 仓库提 issue |
| **`reports/` 与 `state/`** | 由 bot 自动推送，不手工改。`state/` 入库并随仓库同步，是两个运行通道互不重复分析的基础 |
| **`data/` 与 `reports/` 克隆后不存在** | 运行时目录，程序自己建，`.gitignore` 忽略。README 目录树里列它们是提示用途，不是「缺文件」 |
| **`__pycache__`** | 已忽略；根目录出现它不算违规 |
| **「拿已发布产物」验不出这个 bug** | 免责声明重复追加的复现：落盘的 `.md` 里已经含有 `ensure_disclaimer` 追加的半角那份，`DISCLAIMER in text` 精确匹配会命中，旧逻辑看起来也「不追加」——于是验出「没病」。要复现必须先把模型原文复原（删掉追加的那行半角声明）再喂旧逻辑，才会得到 2 处；`baseline-reports/2026-11-23.md` 的 `L52` 是模型写的全角、`L64` 才是追加的半角。凡是「看着能验、其实验不出」的场景，第一件事是问清输入是模型原文还是落盘产物 |
| **libgit2 的路径限定 git 命令** | 本仓库当前 git 实现下 `git log/diff <rev> -- <path>` 会静默返回空，见第 3 节 |
| **`: > file` 与 chmod** | 本机沙箱只能跑 Python 子进程，`chmod +x` 不生效、外部二进制 spawn 不出来；依赖这类手段的验证要在 CI 上做 |
| **坏 uid 与限流软空在日志里同形** | 动态接口对不存在的 uid 返回的也是 `code=0 + items=[]`，被判成 `empty`，与真正的限流软空一字不差。运维时不能靠「空」判断 uid 配错，得看这个 uid 有没有正常取到过 13 条。两者都**不写 state、不出章节**，所以漏采风险一样 |
| **动态监测在 `finally` 里，每次 run 都会打一次接口** | `process_dynamics` 放在 `main()` 的 `finally`：三处提前 `return`（无新视频、首次运行后已处理、无待处理）以及视频列表拉不到 `return 1` 的路径**都会执行**。单次最多 15s×2 次重试。这是有意设计（动态独立于视频链），不是漏判；代价是没新视频的日子也会多一次网络往返 |

---

## 7. 验收标准（本轮逐条达成，证据位置在右）

| # | 条件 | 实测 |
|---|---|---|
| 1 | `monitor.py` < 600 行，只剩 `main` / `process_video` / 入口参数解析 | 464 行；顶层 `process_video` L282–351、`main` L366–460（`add_argument` 5 处在 `main` 内）、`if __name__` |
| 2 | 每个模块 < 900 行 | 全仓 `.py` 最大 `local/local_daemon.py` 712（包内最大 `lidaxiao/analyze/llm.py` 535），无一 ≥ 900 |
| 3 | 三件套 | `py_compile` 0；`undefined name` 0；本地 47 passed / 3 FAILED（固定那 3 个）；ubuntu 由 `tests.yml` 全绿 |
| 4 | 连续 3 次 force 线上成功，报告内容与拆分前一致 | run `45232765826` / `45233639404` / `45234161587` 全 success，日志均有「主扫描完成」「视频处理完成」「报告已提交并推送」，无 `Traceback`；差异归因见 5.1 |
| 5 | 根目录只剩入口 + 白名单 | `.py` 只有 `monitor.py` / `lidaxiao.py` / 两个 1 行 shim；非 `.py` 只有 `README.md` / `LICENSE` / `.gitignore` / `requirements.txt` / `run_monitor.cmd` / `start_local_daemon.cmd` / `config.json` |
| 6 | README 目录结构与实际一致 | 树内条目逐个在盘上（`data/` 为运行时目录，见第 6 节） |
| 7 | 行为完全不变 | `b25a4be:monitor.py` 的 30 个顶层符号：27 个逐字节相同、2 个只差懒加载 import 路径、1 个（`win_to_msys`）全仓零引用后删除 |

> 拆分验收的原始报告与命令输出在工作区 `../reports/`：`§8-终表.md`、`P1`–`P5` 的独立验收报告、`P5-收尾与拆分-验证报告.md`。

---

## 8. 回滚

每一步都是独立 commit，出问题：

```bash
git revert <commit>                       # 单步回滚
git reset --hard <上一个绿色 commit>      # 一步退回
```

但有两条耦合，**必须同进同退**：

1. `67c6629` 同时做了两件事：两个引擎文件迁入 `lidaxiao/ocr/engines/`，根目录留 1 行 shim。只回滚一侧，`from aoyuon_ocr import ...` 或新路径 import 必断一边。
2. `530e0f2` 把 `list_line` 搬进 `render.py` 并删掉 `monitor` 的实现，同一笔里 `monitor` 只剩 `main`/`process_video`。回滚它会把 `render.py` 的调用点变成悬空。

回滚到 P2 之前（即 `3140e12` 之前）时，还要连 `batch_subtitles.py` / `bench/` 的 import 路径一起退，它们是按新路径改过的。

本次交接**保留了**两个 1 行 shim（`aoyuon_ocr.py` / `xiaomi_ocr.py`），旧引用不会断；但 shim 只做转发，不承载逻辑，删掉它不会影响包内代码。

---

## 9. 交接时一起带走

- 代码侧最后 HEAD：`d59a0b7`；其后只有文档提交（`git log -5` 可见）
- 固定测试集：`bench/frames/BV1x3y8zpEGD/`（54 帧，A/B 用）
- 上游模型仓库：`aoyuon-ocr`（私有），`distill/eval.md` 有完整评测口径
- 报告对比工具：工作区 `tools/factcheck.py`（`--selftest` 21 项，口径见第 5 节）
- 验收痕迹：工作区 `../reports/` 下的独立验收报告与 `§8-终表.md`

---

## 10. 本轮之外，还挂着的

拆分不等于没问题。以下都是**已知、有证据、独立成单**的事项，别以为拆完就干净了。

### P0 · 影响线上产出

1. **`deepseek-chat-cli` 通道 token 每次失效**：日志固定出现 `dsc token 失效`，四次通道只剩 `deepseek-flash` 活，白等一轮超时。要么换 `DSV_TOKEN`，要么把这条死通道从 `vision_providers` / 分析链里摘掉。
2. **下载主源 `akamaized` 仍 403**：现在能跑是靠备用源 `bilivideo` —— cli 加了 `Range: bytes=0-` 之后它才接受请求。`html5 durl` 单文件旁路只在本地 monkeypatch 下验过，**线上从未触发**，真实可用性未证。
3. **免责声明重复追加**：已改为归一化匹配（`5e6fc15`，配 `tests/test_disclaimer.py` 14 条）。仍待一次线上 run 确认 baseline 那 2 处降到 1 处。

### P1 · 结构债

4. ~~**`vision → analyze` 反向边**~~ **已消除**（`2de9fdd`）：视觉实现搬进 `lidaxiao/vision/channel.py`（242 行），文本与视觉共用的件下沉到 `lidaxiao/llmio.py`（134 行），`analyze/llm.py` 863 → 535 行。复核命令见第 2.3 节。
5. ~~**`monitor.py` 的 imported-but-unused 再导出**~~ **已完成**（`d59a0b7`）：`pyflakes monitor.py` 的 imported-but-unused 从 **25 条清到 0**，`monitor.py` 464 行。处理分三类，不是一刀切：
   - **仓内有消费者，改从新模块 import（12 条）**：`local/batch_subtitles.py`、`bench/run_bench.py`、`bench/legacy/*`、`_setup/weekly_summary.py`、`tests/test_band.py` —— 只换 import 路径，断言与用例数未动；
   - **必须留在 `monitor`**：`_setup/week_process.py` 用的 `RunLock` / `mark_processed` / `processed_bvids`（外部脚本入口），那个文件零改动；
   - **全仓无消费者，直接删（13 条）**：换路径见 README「对外可见变更」表。

### P2 · 卫生

6. **`bilibili-downloader-cli` 没有自测**，且 `monitor.yml:107` 每次 `git clone --depth 1` 跟 main，没有钉版本 —— 上游一改，线上行为跟着变。
7. **PAT**：群里明文贴过的那个按决定不换（记档在案）；密钥库里 `GH_TOKEN` 的**描述与事实不符**（写的是「新的 GitHub PAT」），只能在密钥管理页改。
