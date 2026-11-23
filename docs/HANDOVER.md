# 交接文档 · lidaxiao-monitor

> 更新：2026-11-24 · 当前版本 0.2.0
> 仓库：`github.com/menghuanshiguang/lidaxiao-monitor`（公开）

---

## 1. 这是什么

自动跟踪 B 站 UP 主「李大霄」的新视频，下载 → 取字幕 → 用 LLM 做财务分析 → 生成日报并自动发布。

每天两次（北京时间 **14:40 / 20:00**）由 GitHub Actions 定时触发，处理当天新视频，产出：

- `docs/reports/YYYY-MM-DD.md`：当日日报（按视频章节合并）
- `docs/reports/latest.md`：最新日报
- `state/`：处理状态（哪个 BV 处理过、成功与否、片尾识别缓存）

产出通过 bot 自动 commit 回仓库，静态页面可直接读。

---

## 2. 现状速览

| 指标 | 数值 | 说明 |
|---|---|---|
| 单条视频耗时 | **≈ 45–75 秒** | 其中 OCR 10–15 秒 |
| OCR 引擎 | **Aoyuon-OCR 0.2** | 19.7M 参数 CRNN+CTC，CPU int8 |
| 字幕行级准确 | 92.5%（对老师严格口径）/ 94.1%（同帧对照） | 见 §8 |
| OCR 单帧 | 28–63 ms（整帧含检测，4 vCPU） | 旧的 Xiaomi-OCR 是 930 ms |
| 权重体积 | 19 MB | 旧的 Xiaomi 是 736 MB |
| 单测 | 53 条 / 6 个文件 | `tests/run_all.sh`，含静态检查 |
| 代码规模 | 约 2,600 行 | 已拆包，见 §3 |

---

## 3. 架构

```
lidaxiao/                    # 主包
├── common.py                路径 / 时区 / ffmpeg 定位
├── textutil.py              日志 / 读写 / 归一化 / 水印过滤
├── prompts.py               LLM 提示词与报告结构常量
├── ocr/
│   └── frames.py            抽帧 / 单帧 OCR / 字幕带定位
├── sources/
│   └── bilibili.py          更新检测 / B站 API / 下载（含 Range 兜底）
├── analyze/
│   └── llm.py               LLM 调用 / CLI 通道 / 结果整形
└── report/
    └── render.py            章节渲染 / 合并 / 后处理 / 片尾块

monitor.py                   # 编排 + CLI 入口（约 1000 行）
lidaxiao.py                  # 统一入口（云端/本地自适应，Actions 实际调它）
aoyuon_ocr.py                # Aoyuon 引擎封装（provides_boxes 接口）
xiaomi_ocr.py                # Xiaomi 引擎封装（备用，默认不启用）
audit_p2.py                  # 报告审计（排序、摘要、图文冲突等 17 条规则）
frame_dedup.py               # 帧去重（默认关闭，实测只值 3%）
aoyuon/                      # 上游推理代码 + 19MB 模型权重（vendor）
bench/                       # 评测脚本与固定测试帧（54 帧）
tests/                       # 单测 + run_all.sh
state/                       # 处理状态（入库，bot 会改）
docs/reports/                # 已发布日报（入库）
```

### 数据流

```
bilidown / Range 兜底下载 mp4
   → ffmpeg 按 ~2.18s 抽 160 帧
   → Aoyuon 整帧识别（自带行检测）→ 逐行文本
   → 去重（相似度 >0.85 视为同句）
   → LLM 分析（DeepSeek 等）→ 结构化报告
   → audit_p2 审计（排序 / 摘要 / 图文冲突）
   → upsert 到当日报告 → 合并进 docs/reports → git push
```

---

## 4. 怎么跑

### 4.1 云端（生产）

```bash
# 定时已配好，无需干预
gh workflow run monitor.yml -f force=true          # 强制重跑最新一条
gh workflow run monitor.yml -f ocr_engine=rapid    # 临时切回 RapidOCR
```

`force=true` 会删掉 `data/subtitles/<last_bvid>.txt` 并绕过日期过滤重跑。

### 4.2 本地

```bash
python lidaxiao.py --once       # 跑一次（自动判断本地/云端）
python lidaxiao.py --daemon     # 常驻守护（Windows 托盘另见 local_daemon_tray.py）
python monitor.py --once --force
```

### 4.3 测试

```bash
bash tests/run_all.sh
# 先跑 pyflakes 的 undefined name 检查，再跑 6 个单测文件
```

---

## 5. 配置（`config.json`）

| 键 | 当前值 | 说明 |
|---|---|---|
| `ocr_engine` | `aoyuon` | 可选 `aoyuon` / `xiaomi` / `rapid` |
| `frame_interval` | `0.0` | 0 = 自适应（时长/160，限 1–4 秒） |
| `ocr_confidence` | `0.5` | 低于此值的框丢弃 |
| `min_subtitle_chars` | `30` | 低于此字数触发兜底扫描 |
| `ocr_dedup` | `false` | 帧去重，实测收益仅 3%（省 9 秒但会截字） |
| `aoyuon` | `threads=4` | 其余字段保留兼容，v0.2 已内置分块 |
| `xiaomi_ocr` | `band=auto` 等 | 仅 `ocr_engine=xiaomi` 时生效 |
| `llm` / `vision` | 见文件 | 模型与 provider 配置 |

---

## 6. 密钥与环境

GitHub Secrets（**不要写进代码或日志**）：

| Secret | 用途 |
|---|---|
| `BILIBILI_COOKIES_B64` | B 站登录 cookies（base64），下载必需 |
| `OPENCODE_GO_API_KEY` | 主 LLM 通道 |
| `DEEPSEEK_API_KEY` | 分析/视觉备用通道 |
| `DSV_TOKEN` | 片尾识图 CLI 通道（未配置则自动跳过） |

工作流里另有 `GH_TOKEN`（自动注入，用于 git push）。

---

## 7. 数据与状态

| 路径 | 是否入库 | 说明 |
|---|---|---|
| `state/processed.txt` | ✅ | 处理过的 BV 与状态 |
| `state/last_bvid.txt` | ✅ | 最近处理的 BV |
| `state/ending_hints.json` | ✅ | 片尾识别缓存（按 BV） |
| `docs/reports/*.md` | ✅ | 已发布日报 |
| `data/` | ❌（缓存） | 视频、帧、字幕，Actions cache |
| `reports/` | ❌ | 当日临时报告，运行时生成后合并 |

**注意**：`reports/` 不入库，每次 run 从空开始；发布时按 BV 合并进 `docs/reports`。这是历史 bug 的高发区（见 §11）。

---

## 8. 质量基线与复现

固定测试集：`bench/frames/BV1x3y8zpEGD/`（54 帧，每 3 帧取 1）。

```bash
gh workflow run ocr-ab.yml
# 同帧对比 Xiaomi 与 Aoyuon：逐帧一致率、CER、延迟
```

当前基线（2026-11-24）：

| 项 | 数值 |
|---|---|
| 逐帧完全一致 | 94.12% |
| CER | 3.05% |
| Aoyuon p50（整帧） | 28–63 ms（随 runner 波动） |
| Xiaomi p50（字幕带） | 419–933 ms |

上游模型评测（`aoyuon-ocr` 仓库 `distill/eval.md`）：test 8,346 行 **92.5% 严格口径 / CER 0.87%**。

---

## 9. 已知问题

### 9.1 上游模型（已提 issue，等待修）

| # | 问题 | 影响 |
|---|---|---|
| aoyuon-ocr#15 | 宽度超 `max_width` 时**横向压扁**而非裁切 | 31% 的行受影响，最长压扁 41%，长句尾字丢失 |
| #16 | 字幕带输入被判为单行，双行带丢第二行 | 调用方直接喂带时会静默掉 40 个点 |
| #17 | 1080p 比 480p 掉 9.6 个点 | 换高分辨率源时质量下降且无报错 |
| #18 | 训练标签被归一化，推理侧用启发式改回 | 标点正确率与训练目标不一致 |
| #19 | 只有行级 score，无逐字置信度 | 无法只校验数字等关键字符 |
| #20 | 包内 9.4MB LM 默认不启用 | 体积浪费、决策依据缺失 |

### 9.2 本仓库待办

| 优先级 | 事项 |
|---|---|
| P0 | **失败告警**：job 失败 / 报告为空 / 视频数异常时推通知（现在静默） |
| P0 | **缓存拆分**：`data/` 一个 key 装所有东西，Ubuntu 与 Windows 会互相挤掉 |
| P1 | **去重阈值**：`>0.85` 可能吞掉"美股跌了"和"美股跌了3%"这类真字幕 |
| P1 | **抽帧上限**：固定 160 帧，20 分钟长视频会退化成 7.5 秒/帧 |
| P2 | **数字溯源**：报告里的数字应能回溯到字幕原文 |
| P2 | **摘要覆盖断言**：`摘要行数 == 视频章节数`，防回归 |
| P2 | **重构收尾**：引擎封装入包、`local_*.py` 归拢到 `local/`、归档 15 个实验性 workflow |

---

## 10. 开发约定

1. **改完必须本地验证**
   ```bash
   bash tests/run_all.sh          # pyflakes + 53 条单测
   python3 -c "import sys;sys.path.insert(0,'.');import monitor"
   ```
2. **提交前必须跑一次线上 `force`**，确认流水线端到端通（约 1–2 分钟）。历史上有多次“单测全过、线上炸”。
3. **按模块提交**，一次只改一件事；重构与功能分开。
4. **不要直接改 `docs/reports/` 与 `state/`**：这些由 bot 自动推送，手改会在下次 run 被覆盖或产生冲突。
5. **不接受外部 PR 直接改主干**：曾有过陌生账号的 PR，处理方式是“看内容、抄思路、不合代码”。

---

## 11. 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| 运行报 “没有帧” / “没有 reports 目录” | Actions cache 被别的 OS 的 run 挤掉；`data/` 缓存按 OS 隔离 |
| 下载失败 `567/403` | B 站 CDN 对机房 IP 限流；已有 Range 兜底，但需校验 `Content-Range` 完整性 |
| 报告缺视频 / 顺序乱 | `reports/` 每次从空开始，靠 `_merge_report` 合并；曾因 `latest` 取“最后写入”而非“最新日期”出错 |
| 字幕末尾缺字 | 多半是宽度超 `max_width` 被压扁（§9.1 #15），不是模型认不出 |
| 双行字幕只出一行 | 字幕带被整条当一行（§9.1 #16） |
| `WORKDIR` 指向错误 | 包化后 `common.py` 里的路径要取**包目录的父目录** |
| 单测过但线上炸 | `import` 缺失这类问题编译查不出来；已加 pyflakes 检查，务必保留 |

---

## 12. 接手第一周建议

1. 读 `lidaxiao/` 六个模块 + `monitor.py` 主流程（约 1 小时能通读）
2. 跑一次 `bash tests/run_all.sh`，再跑一次 `gh workflow run monitor.yml -f force=true`，对着日志走一遍数据流
3. 修 **P0 的失败告警**（改动小、收益立刻可见）
4. 拆缓存 key（避免再次出现“跑一次发现没帧”）
5. 跟进上游 `aoyuon-ocr` 的 issue #15/#16/#17 —— 这三条决定字幕质量天花板

---

## 13. 资源

- 上游模型仓库：`github.com/menghuanshiguang/aoyuon-ocr`（私有，含 `distill/eval.md` 完整评测）
- 模型权重：`aoyuon/model/`（19MB，随仓库分发；如嫌大，可移到 Release 资产）
- 评测脚本：`bench/ab_same_frames.py`（同帧 A/B）、`bench/color_robust.py`（换色鲁棒性）、`bench/run_ocr_bench.py`
- 历史报告：`docs/reports/`
