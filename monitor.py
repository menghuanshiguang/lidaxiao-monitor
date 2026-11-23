#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
李大霄视频自动监控分析系统 - 主脚本 (Windows)
====================================================
功能:
  1. 更新检测  : bilidown space 获取 UP主最新视频, 与 data/last_bvid.txt 对比
  2. 视频下载  : bilidown dl 下载 480P mp4 (失败重试1次)
  3. 字幕提取  : ffmpeg 抽帧 + RapidOCR 硬字幕识别 (自适应字幕区域 + 相邻帧去重)
  4. 片尾识别  : ffmpeg 取片尾帧 + 视觉链(MiMo免密 → opencode-go → deepseek-vision-cli 网页版识图 → DeepSeek官方)
                读"片尾暗示" (结束语/荐书卡/合规声明卡/数据面板; OCR 会丢弃的整屏密集文字)
  5. AI 分析   : OpenCode Zen Go API (deepseek-flash) 对字幕+片尾做"李大霄话术解码"分析
  6. 每日报告  : reports/YYYY-MM-DD.md (当天已有报告则追加)
  7. 状态幂等  : data/last_bvid.txt + data/processed.txt + 并发锁
                (片尾识别结果缓存于 state/ending_hints.json, 随仓库同步)

用法:
  python monitor.py            # 正常运行 (无新视频秒退)
  python monitor.py --force    # 强制重新处理最新视频
  python monitor.py --uid 2137589551 --limit 10
  python monitor.py --config config.json
  python monitor.py --check-vision   # 仅自检片尾视觉通道 (手动自检用)

退出码: 0=正常(含无更新)  1=出错
"""
import argparse
import base64
import datetime
import hashlib
import io
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request

# ---------- 常量 ----------
from lidaxiao.common import (  # noqa: E402
    CN_TZ, DISCLAIMER, STATE_DIR, WORKDIR)

try:
    from lidaxiao import audit as _audit
except Exception:  # pragma: no cover - 模块缺失时不影响主流程
    _audit = None
# bash 路径可用环境变量 BILIDOWN_BASH 覆盖 (如 WSL/GitBash 安装在不同位置)
BASH = os.environ.get("BILIDOWN_BASH", r"D:\Program Files\Git\bin\bash.exe")
BILIDOWN_SCRIPT = os.path.join(WORKDIR, "_repo", "bin", "bilidown")
from lidaxiao.common import (  # noqa: E402
    CN_TZ, FFMPEG, FFMPEG_DIR, FFPROBE, STATE_DIR, WORKDIR)





try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# 以下名字在 monitor.py 内已不再直接使用, 保留 import 是给 bench/ 与旧脚本的
# 再导出面 (实测有 bench/ab_same_frames.py 等按 m.filter_ocr_text / m.norm_text 调用)
from lidaxiao.textutil import (  # noqa: E402
    filter_ocr_text, log, norm_text, read_text, write_text)


from lidaxiao.config import (  # noqa: E402
    load_config, load_llm_key)


# OpenCode Zen 免密车道: 固定公开 token, 不需要任何环境变量
# 片尾视觉 CLI 车道: DeepSeek 网页版「识图模式」(走登录 token, 不需要 API key)








# =====================================================================
# 工具函数
# =====================================================================
def win_to_msys(path):
    """C:\\a\\b -> /c/a/b (bilidown bash 脚本要求 POSIX 绝对路径)"""
    p = path.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(/.*)$", p)
    if m:
        return "/" + m.group(1).lower() + m.group(2)
    return p








# ---------------- 并发锁 ----------------




# ---------------- bilidown 调用 ----------------
from lidaxiao.sources.bilibili import (  # noqa: E402
    _direct_download, download_video, ensure_complete_download, expected_duration,
    fetch_video_info, find_downloaded_mp4, get_space_videos, run_bilidown)


# ---------------- B站 API (view 信息) ----------------






# =====================================================================
# 1. 更新检测
# =====================================================================




# =====================================================================
# 2. 视频下载
# =====================================================================










# =====================================================================
# 3. 字幕提取 (ffmpeg 抽帧 + RapidOCR)
# =====================================================================
# 同上: extract_frames / ocr_frame / probe_band_heuristic / probe_subtitle_band 的使用者
# 已随函数搬去 ocr/pipeline.py, 这里保留是为了 bench 与旧脚本的 m.xxx 引用不断
from lidaxiao.ocr.frames import (  # noqa: E402
    extract_frames, ffprobe_duration, ocr_frame, probe_band_heuristic,
    probe_subtitle_band)

# merge_ts_lines 在 monitor 内已无调用点, 但 local/batch_subtitles.py 用 m.merge_ts_lines,
# 必须继续从 monitor 可见
from lidaxiao.ocr.pipeline import (  # noqa: E402
    merge_ts_lines, ocr_video)

# _setup/week_process.py 仍按 m.RunLock / m.mark_processed / m.processed_bvids 引用, 保留再导出
from lidaxiao.runtime.state import (  # noqa: E402
    RunLock, is_from_baseline, is_success_status, mark_processed,
    processed_bvids, processed_status_map)








# 频道水印: 整帧扫描(片尾补扫/兜底)会把左上角“李大霄 bilibili”读进字幕,
# 这些词在口播正文里不会出现, 直接剥掉; 中文名只在其后紧跟拉丁字母时才算水印,
# 避免误伤正文里提到的“李大霄”。












from lidaxiao.prompts import (ANALYZE_SYSTEM, ENDING_PROMPT, REPORT_SECTIONS, REPORT_TAIL)  # noqa: E402
from lidaxiao.vision.ending import read_ending_hint  # noqa: E402


















# =====================================================================
# OpenCode Zen 免密车道 (Bearer public + OpenCode 客户端指纹 + 流式)
# 协议实测自 zouyuxuan122/dsh-our-free-model:
#   - 非流式必挂(500/403), 只有 stream:true 稳定 200
#   - body 必须带 bash/glob/grep/read 四个最小 tools + tool_choice:none, 否则 403
#   - reasoning_effort 发了是空操作, 思考与正文抢 max_tokens -> 预算靠 max_tokens 顶
#   - 免费额度按 session 计: ses_/msg_ id 用 sha256 稳定派生, 不能每请求新铸
# =====================================================================












from lidaxiao.analyze.llm import (  # noqa: E402
    _apply_thinking, _warn_model, analyze_subtitles, build_dsc_prompt, call_llm,
    check_vision, load_history, tighten_analysis, validate_report)


# =====================================================================
# 4. AI 分析
# =====================================================================


# 官方已下线的模型名 -> 现行模型名 (旧名仍可调用, 但请求由 V4.1 Flash 承接)
# 思考强度取值; 官方另接受 minimal/medium/xhigh/ultra (映射到 low/high/max)

















































# =====================================================================
# 5. 每日报告
# =====================================================================
from lidaxiao.report.render import (  # noqa: E402
    build_section, ensure_disclaimer, postprocess_analysis, upsert_report)












# =====================================================================
# 主流程
# =====================================================================
def process_video(cfg, api_key, video):
    """处理单个视频: 下载->字幕->分析->报告; 返回 (ok, report_path, note)"""
    bvid = video["bvid"]
    if not video.get("pubdate"):
        info = fetch_video_info(bvid)
        if info:
            video.update(info)
        else:
            video["pubdate"] = int(time.time())   # 兜底: 用当前时间
    log(f"---- 处理视频: {video.get('title', '')} ({bvid}) ----")

    # 下载
    mp4 = find_downloaded_mp4(bvid)
    if not mp4:
        mp4 = download_video(cfg, bvid)
    mp4, dl_warn = ensure_complete_download(cfg, video, mp4)
    if not mp4:
        note = f"下载失败(重试后仍失败), 已跳过: {bvid}"
        log("❌ " + note)
        return (False, None, note)

    # 字幕
    sub_path = os.path.join(WORKDIR, "data", "subtitles", f"{bvid}.txt")
    subtitle_text = read_text(sub_path) if os.path.exists(sub_path) else ""
    if not subtitle_text:
        subtitle_text, stats = ocr_video(cfg, mp4, bvid)

    # 片尾画面识别 (deepseek-flash 视觉; 放在抽帧之后可直接复用帧; 失败不影响主流程)
    ending = read_ending_hint(cfg, video, mp4)

    if not subtitle_text:
        log("无字幕, 跳过分析")
        section, sl = build_section(cfg, video, "", None, None, ending=ending, warn=dl_warn)
        rp = upsert_report(video, section, sl, list_line(video))
        return (True, rp, "无字幕,跳过分析")

    # 分析
    history = load_history(cfg)
    analysis = None
    last_err = ""
    for attempt in range(1, cfg["llm_retries"] + 1):
        try:
            log(f"AI分析中 (第{attempt}次)...")
            analysis = analyze_subtitles(cfg, api_key, video, subtitle_text, history,
                                         ending=ending)
            # 防御: 任何 provider 返回超时/未获取回答都视为失败, 不写入报告
            if "超时未获取回答" in analysis or "未获取回答" in analysis:
                raise ValueError("LLM 返回超时未获取回答")
            analysis = ensure_disclaimer(analysis)
            analysis = postprocess_analysis(analysis)
            break
        except Exception as e:
            last_err = str(e)
            log(f"AI分析失败(第{attempt}次): {last_err}")
            time.sleep(5 * attempt)
    if analysis is None:
        note = f"AI分析失败: {last_err}"
        log("❌ " + note)
        section, sl = build_section(cfg, video, subtitle_text, None, None,
                                    ending=ending, warn=dl_warn)
        section = section + f"\n> ⚠️ AI分析失败: {last_err}\n"
        rp = upsert_report(video, section, sl, list_line(video))
        return (False, rp, note)

    # 写报告
    section, summary_line = build_section(cfg, video, subtitle_text, analysis, None,
                                          ending=ending, warn=dl_warn)
    rp = upsert_report(video, section, summary_line, list_line(video))
    log(f"✅ 视频处理完成: {bvid}")
    return (True, rp, "ok")


def list_line(video):
    t = datetime.datetime.fromtimestamp(video.get("pubdate", time.time()), CN_TZ)
    return f"- [{t.strftime('%H:%M')}] {video['title']} ({video['bvid']}) {video.get('dur_str', '')}"












def main():
    ap = argparse.ArgumentParser(description="李大霄视频自动监控分析系统")
    ap.add_argument("--force", action="store_true",
                    help="强制重新处理最新视频(即使已处理过)")
    ap.add_argument("--uid", default=None, help="UP主UID")
    ap.add_argument("--limit", type=int, default=None, help="每次最多检查的视频数")
    ap.add_argument("--config", default=None, help="配置文件路径")
    ap.add_argument("--check-vision", action="store_true",
                    help="仅自检片尾视觉模型可用性后退出(不下载/不分析)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.check_vision:
        return 0 if check_vision(cfg) else 1
    if args.uid:
        cfg["uid"] = args.uid
    if args.limit:
        cfg["limit"] = args.limit
    api_key = load_llm_key()
    free_ok = (cfg.get("llm_free") or {}).get("enabled", True)
    if not cfg.get("local_ollama") and not api_key and not free_ok:
        log("❌ 未找到 LLM 密钥 (OPENCODE_GO_API_KEY / DEEPSEEK_API_KEY, 检查 .env 或环境变量)")
        return 1
    if not api_key and free_ok:
        log("ℹ️ 无本地密钥, 仅免费层可用 (opencode-zen-free)")

    lock = RunLock(os.path.join(WORKDIR, "data", "monitor.lock"))
    if not lock.acquire():
        return 0
    try:
        # 1. 检测更新
        videos = get_space_videos(cfg, cfg["limit"])
        if not videos:
            log("❌ 无法获取UP主视频列表(风控或网络问题)")
            return 1
        # 只处理 baseline(前天)及之后发布的视频, 更早的忽略 (--force 豁免: 始终取最新)
        if not args.force:
            videos = [v for v in videos if is_from_baseline(v)]
            if not videos:
                log("前天以来暂无新视频, 退出")
                return 0
        latest = videos[0]
        last_file = os.path.join(STATE_DIR, "last_bvid.txt")
        last_bvid = read_text(last_file).strip()
        done = processed_bvids()
        status_map = processed_status_map()

        # 前天以来所有视频都已成功处理时直接退出; 有失败/部分成功/未处理则继续
        if not args.force and all(
            v["bvid"] in done and is_success_status(status_map.get(v["bvid"]))
            for v in videos
        ):
            log(f"无新视频 (前天以来均已处理), 退出")
            return 0

        # 2. 确定候选: force=仅最新; 首次运行=仅最新;
        #    常规=前天以来未处理/失败/部分成功的视频 (按时间从旧到新)
        if args.force:
            candidates = [latest]
            log("--force 模式: 强制处理最新视频")
        elif not last_bvid:
            candidates = [latest]
            log("首次运行, 处理最新视频")
        else:
            cand = [v for v in videos
                    if v["bvid"] not in done or not is_success_status(status_map.get(v["bvid"]))]
            candidates = list(reversed(cand))        # 旧的在前
            if not candidates:
                log("无新视频 (最新已处理), 退出")
                return 0
            log(f"发现 {len(candidates)} 个待处理视频")

        # 3. 逐个处理
        touched = []
        for v in candidates:
            ok, rp, note = process_video(cfg, api_key, v)
            status = "ok" if ok else ("error" if rp is None else "partial")
            if note == "无字幕,跳过分析":
                status = "no-subtitle"
            mark_processed(v["bvid"], v.get("title", ""), status)
            if rp and rp not in touched:
                touched.append(rp)
            log(f"状态: {v['bvid']} -> {status} ({note})")

        # 4. 更新状态文件
        write_text(last_file, latest["bvid"] + "\n")
        log(f"状态已更新: last_bvid={latest['bvid']}")

        # 5. 输出确认
        if touched:
            for rp in touched:
                print(f"✅ 今日报告: {rp}")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
