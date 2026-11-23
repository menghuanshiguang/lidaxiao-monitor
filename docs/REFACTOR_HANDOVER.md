# 交接文档 · 代码拆分工作（进行中）

> 更新：2026-11-24 00:50 · 分支 `main` · 负责人：待指派
> 配套阅读：[HANDOVER.md](./HANDOVER.md)（项目本身怎么跑）

---

## 1. 为什么拆

拆分前：`monitor.py` **2,639 行**，一个文件里混着下载、OCR、片尾识别、LLM 分析、报告渲染、发布六件事。
带来的实际问题：

- 改一处要读一整屏，容易连带影响别处
- 这周修过的 4 个 bug（今日摘要漏条、章节顺序、标记被丢、Windows 编码）全部出在这个文件里
- 新功能（语音通道）没有地方放

**目标**：`monitor.py` 只剩编排（目标 < 600 行），每个模块单一职责，且**行为完全不变**。

---

## 2. 进度：已完成 6/8

`monitor.py`：**2,639 → 1,035 行（-61%）**

| 模块 | 行数 | 职责 | 状态 |
|---|---|---|---|
| `lidaxiao/common.py` | 26 | 路径 / 时区 / ffmpeg 定位 | ✅ |
| `lidaxiao/textutil.py` | 58 | 日志 / 读写 / 归一化 / 水印过滤 | ✅ |
| `lidaxiao/prompts.py` | 62 | 提示词与报告结构常量 | ✅ |
| `lidaxiao/ocr/frames.py` | 156 | 抽帧 / 单帧 OCR / 字幕带探测 | ✅ |
| `lidaxiao/sources/bilibili.py` | 308 | API / 更新检测 / 下载（Range 兜底） | ✅ |
| `lidaxiao/analyze/llm.py` | 905 | LLM 调用 / CLI 通道 / 结果整形 | ✅ |
| `lidaxiao/report/render.py` | 233 | 渲染 / 合并 / 后处理 / 片尾块 | ✅ |
| `monitor.py` | 1,035 | 编排 + 入口 | 🚧 待续 |

### 当前目录结构

```
lidaxiao/
├── common.py          textutil.py       prompts.py
├── ocr/frames.py
├── sources/bilibili.py
├── analyze/llm.py
└── report/render.py
monitor.py             ← 还剩 8 个可抽出的块
```

---

## 3. monitor.py 里还剩什么

按函数大小排序，前 12 个：

| 函数 | 行数 | 建议归属 | 说明 |
|---|---|---|---|
| `_ocr_video_impl` | 122 | `lidaxiao/ocr/pipeline.py` | 抽帧 + 扫描 + 去重 + 兜底，**最大的待抽块** |
| `load_config` | 115 | `lidaxiao/config.py` | 配置读取与默认值 |
| `main` | 95 | 留在 monitor（入口） | 保持不动 |
| `process_video` | 70 | 留在 monitor（编排） | 保持不动 |
| `read_ending_hint` | 53 | `lidaxiao/vision/ending.py` | 片尾缓存读取 |
| `tail_frames` | 37 | `lidaxiao/vision/ending.py` | 片尾抽帧 |
| `ocr_video` | 35 | `lidaxiao/ocr/pipeline.py` | 引擎选择与回退 |
| `dedupe_frames` | 31 | `lidaxiao/ocr/pipeline.py` | 帧去重（默认关） |
| `mark_processed` | 17 | `lidaxiao/runtime/state.py` | 状态写入 |
| `get_ocr` | 14 | `lidaxiao/ocr/engine.py` | 引擎工厂 |
| `read_state_lines` | 9 | `lidaxiao/runtime/state.py` | 状态读取 |
| `load_ending_cache` | 8 | `lidaxiao/vision/ending.py` | 缓存加载 |

其余为工具函数与常量，合计约 370 行不参与拆分（入口、编排、状态）。

---

## 4. 剩余工作清单（按依赖从松到紧）

### P1 · `config.py`（约 150 行）

抽 `load_config` + 密钥加载（`analyze/llm.py` 里的 `load_llm_key` / `_env_or_envfile` 应一并挪进来，
现在它们分散在两处，配置相关逻辑应该只有一个家）。

**注意**：`load_config` 里有大量默认值，**不要顺手改值**，只搬家。

### P2 · `ocr/pipeline.py` + `ocr/engine.py`（约 200 行）

- `ocr_video` / `_ocr_video_impl` / `dedupe_frames` → `pipeline.py`
- `get_ocr` → `engine.py`
- 顺带把 `aoyuon_ocr.py`、`xiaomi_ocr.py` 移进 `lidaxiao/ocr/engines/`，根目录留 1 行 shim 保证旧引用可用

### P3 · `vision/ending.py`（约 100 行）

片尾识图相关：`read_ending_hint` / `tail_frames` / `load_ending_cache`，以及 `analyze/llm.py`
里的视觉通道可以后续合并进来（现在是分开的，合并时注意别打断调用顺序）。

### P4 · `runtime/state.py`（约 50 行）

`read_state_lines` / `mark_processed` / `processed_bvids` / 并发锁。

### P5 · 收尾

- `audit_p2.py` → `lidaxiao/report/audit.py`
- `frame_dedup.py` → `lidaxiao/ocr/dedup.py`
- `local_daemon.py` / `local_render.py` / `local_daemon_tray.py` → `local/`
- `.github/workflows/` 里 12 个实验性 workflow（llm-bench\*、vlm-bench、probe、ocr-bench 等）移到 `_archive/workflows/`
- README 的目录结构章节更新（现在还是拆分前的）

---

## 5. 操作手册（怎么搬一个模块）

**原则：只搬家，不改逻辑。** 重构和修 bug 分成两个 commit。

```bash
# 1) 用 ast 精确抽取（避免手抄出错）
python3 - <<'PY'
import ast
src = open('monitor.py', encoding='utf-8').read()
tree = ast.parse(src)
WANT = ["函数名1", "函数名2"]          # 要搬的函数
lines = src.splitlines(keepends=True)
defs, spans = {}, []
for n in tree.body:
    if isinstance(n, ast.FunctionDef) and n.name in WANT:
        defs[n.name] = ast.get_source_segment(src, n)
        spans.append((n.lineno, n.end_lineno))
# 写新模块（手工补 import 头）
open('lidaxiao/xxx.py', 'w', encoding='utf-8').write(head + "\n\n".join(defs.values()))
# 从 monitor.py 删除对应行，并在原位置加 import
PY

# 2) 编译 + 静态检查（这一步会抓出 90% 的问题）
python3 -m py_compile monitor.py lidaxiao/xxx.py
python3 -m pyflakes monitor.py lidaxiao/**/*.py | grep 'undefined name'

# 3) 单测
bash tests/run_all.sh

# 4) 线上验证（必做）
gh workflow run monitor.yml -f force=true
# 等 1–2 分钟，看日志里 主扫描完成 / 报告已提交，确认无 Traceback
```

---

## 6. 验证规范（三件套，缺一不可）

| 检查 | 命令 | 能抓什么 |
|---|---|---|
| 编译 | `python3 -m py_compile` | 语法错误 |
| **静态检查** | `python3 -m pyflakes`（只看 `undefined name`） | **编译能过、运行时才炸的缺失 import** |
| 单测 | `bash tests/run_all.sh` | 纯函数逻辑回归（53 条） |
| **线上** | `gh workflow run monitor.yml -f force=true` | 真实链路（网络、模型、写文件） |

**历史教训**：拆分时 `textutil.py` 漏了 `import os`，前三项全过，线上跑到「字幕已保存」才报错。
所以第四项不能省。

---

## 7. 已知陷阱

| 陷阱 | 说明 |
|---|---|
| **`WORKDIR` 指向错误** | 包内模块的 `__file__` 是 `lidaxiao/xxx.py`，仓库根要取**父目录**：`dirname(dirname(abspath(__file__)))`。踩过一次，表现为找不到 `config.json` |
| **循环 import** | 依赖方向必须是 `common → textutil → prompts → ocr → sources → analyze → report → monitor`。反向引用会直接 ImportError |
| **依赖顺序** | `sources` 依赖 `ocr.ffprobe_duration`（不要反过来） |
| **pyflakes 抓不到的** | 路径计算错、变量名拼错但恰好存在同名、默认值被顺手改了 —— 只能靠日志验证 |
| **不要动 `aoyuon/`** | 这是 vendor 目录（上游模型代码 + 权重），要改去 `aoyuon-ocr` 仓库提 issue |
| **`reports/` 与 `state/`** | 由 bot 自动推送，不要手工改 |

---

## 8. 验收标准

- [ ] `monitor.py` < 600 行，且只包含：`main` / `process_video` / 入口参数解析
- [ ] 每个模块 < 900 行（`analyze/llm.py` 目前 905，可再拆 `channels.py`）
- [ ] `bash tests/run_all.sh` 全绿，pyflakes 无 `undefined name`
- [ ] **连续 3 次** `force` 线上运行成功，且报告内容与拆分前一致
- [ ] 根目录只剩：`monitor.py` `lidaxiao.py` `config.json` + 目录
- [ ] README 的目录结构与实际一致

---

## 9. 回滚

每一步都是独立 commit，出问题直接：

```bash
git revert <commit>      # 单步回滚
git reset --hard <上一个绿色 commit>
```

**没有保留 shim**，所以回滚必须整步回退，不能只回一部分。

如果团队更希望渐进式，可以给 `monitor.py` 加 shim（`from lidaxiao.xxx import *`），
但代价是根目录会长期保留一层转发文件——本次交接选择了“干净优先”。

---

## 10. 交接时请一起带走

- 当前 commit：`b44938f`（docs: 新增交接文档）
- 拆分过程中的 6 次提交记录：`git log --oneline -- lidaxiao/ monitor.py`
- 固定测试集：`bench/frames/BV1x3y8zpEGD/`（54 帧，A/B 用）
- 上游模型仓库：`aoyuon-ocr`（私有），其 `distill/eval.md` 有完整评测口径
