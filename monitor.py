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
import datetime
import os
import sys
import time

# ---------- 常量 ----------
# 只用 monitor 自己真要用的常量。CN_TZ/DISCLAIMER/FFMPEG_DIR/FFPROBE 在 monitor 内
# 已无引用, 外部消费者 (bench/run_bench.py 的 m.CN_TZ) 已改从 lidaxiao.common 直接 import。
from lidaxiao.common import CN_TZ, STATE_DIR, WORKDIR  # noqa: E402

try:
    from lidaxiao import audit as _audit
except Exception:  # pragma: no cover - 模块缺失时不影响主流程
    _audit = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# monitor 自己只用 log/read_text/write_text; filter_ocr_text 与 norm_text 的唯一
# 外部消费者 local/batch_subtitles.py 已改为从 lidaxiao.textutil 直接 import。
from lidaxiao.textutil import (  # noqa: E402
    log, read_text, sweep_temp_files, write_text)

from lidaxiao.config import (  # noqa: E402
    load_config, load_llm_key)

# OpenCode Zen 免密车道: 固定公开 token, 不需要任何环境变量
# 片尾视觉 CLI 车道: DeepSeek 网页版「识图模式」(走登录 token, 不需要 API key)



# ---------------- 并发锁 ----------------




# ---------------- bilidown 调用 ----------------
# _direct_download / run_bilidown / expected_duration 在 monitor 内与全仓都无消费点,
# 随再导出一起删; 需要时从 lidaxiao.sources.bilibili 取。
from lidaxiao.sources.bilibili import (  # noqa: E402
    download_video, ensure_complete_download, fetch_video_info, find_downloaded_mp4,
    get_space_videos)
# 动态监测: 与视频链路完全独立的一条支线, 失败只记日志
from lidaxiao.sources.dynamic import (  # noqa: E402
    collect_dynamics, commit_dynamics)
# 微博视频号: B 站没有的视频从这里补。取数/合并/下载都在 sources 下, 这里只编排。
from lidaxiao.report.crossreplace import apply_replacements, save_handoff  # noqa: E402
from lidaxiao.sources.merge import (  # noqa: E402
    as_video_item, format_list_counts)
from lidaxiao.sources.weibo_plan import weibo_round  # noqa: E402
from lidaxiao.sources.weibo_video import (  # noqa: E402
    download_weibo_video, find_downloaded_weibo)


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
# 抽帧/带探测的再导出面已撤: 唯一的仓内消费者 local/batch_subtitles.py 改从
# lidaxiao.ocr.frames 直接 import, tests/test_band.py 改从 lidaxiao.ocr.frames
# import probe_band_heuristic。probe_subtitle_band 的消费者在 ocr/pipeline.py,
# 与本处无关。

# merge_ts_lines 的外部消费者 local/batch_subtitles.py 已改从 lidaxiao.ocr.pipeline import
from lidaxiao.ocr.pipeline import ocr_video  # noqa: E402

# _setup/week_process.py 仍按 m.RunLock / m.mark_processed / m.processed_bvids 引用, 保留再导出
from lidaxiao.runtime.state import (  # noqa: E402
    RunLock, is_from_baseline, is_success_status, mark_processed,
    processed_bvids, processed_status_map)








# 频道水印: 整帧扫描(片尾补扫/兜底)会把左上角“李大霄 bilibili”读进字幕,
# 这些词在口播正文里不会出现, 直接剥掉; 中文名只在其后紧跟拉丁字母时才算水印,
# 避免误伤正文里提到的“李大霄”。












# prompts 的四个再导出已撤: bench/run_bench.py 用 ANALYZE_SYSTEM、
# bench/legacy/run_vision.py 用 ENDING_PROMPT, 都改为从 lidaxiao.prompts 直接 import;
# REPORT_SECTIONS / REPORT_TAIL 全仓无外部消费者。
from lidaxiao.vision.ending import read_ending_hint  # noqa: E402


















# =====================================================================
# OpenCode Zen 免密车道 (Bearer public + OpenCode 客户端指纹 + 流式)
# 协议实测自 zouyuxuan122/dsh-our-free-model:
#   - 非流式必挂(500/403), 只有 stream:true 稳定 200
#   - body 必须带 bash/glob/grep/read 四个最小 tools + tool_choice:none, 否则 403
#   - reasoning_effort 发了是空操作, 思考与正文抢 max_tokens -> 预算靠 max_tokens 顶
#   - 免费额度按 session 计: ses_/msg_ id 用 sha256 稳定派生, 不能每请求新铸
# =====================================================================












# 只留 monitor 自己要调的两个。_apply_thinking/_warn_model/build_dsc_prompt/call_llm/
# tighten_analysis/validate_report 在 monitor 内均无调用点: call_llm 的消费者
# _setup/weekly_summary.py、validate_report 的消费者 bench/legacy/* 已改为直接 import。
from lidaxiao.analyze.llm import analyze_dynamic, analyze_subtitles, load_history  # noqa: E402
# check_vision 随视觉通道搬去 vision/, analyze 不再导出它
from lidaxiao.vision import check_vision  # noqa: E402


# =====================================================================
# 4. AI 分析
# =====================================================================


# 官方已下线的模型名 -> 现行模型名 (旧名仍可调用, 但请求由 V4.1 Flash 承接)
# 思考强度取值; 官方另接受 minimal/medium/xhigh/ultra (映射到 low/high/max)

















































# =====================================================================
# 5. 每日报告
# =====================================================================
from lidaxiao.report.render import (  # noqa: E402
    append_dynamics, build_section, ensure_disclaimer, list_line,
    marked_dynamic_ids, postprocess_analysis, report_date_of, report_path,
    upsert_report)












# =====================================================================
# 主流程
# =====================================================================
def download_for(cfg, video):
    """按来源下载; 失败给 None, 由 process_video 判定"下载失败"并跳过。

    参数形状踩过坑: B 站那支第二参是 **bvid 字符串**(内部 path.join), 传 dict 立刻
    TypeError 并崩在队列第一支(2026-11-26 一轮零报告); 微博那支相反, 要整个 item。
    """
    bvid = video["bvid"]
    if (video or {}).get("source") == "weibo":
        return find_downloaded_weibo(bvid) or download_weibo_video(cfg, video)
    return find_downloaded_mp4(bvid) or download_video(cfg, bvid)


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

    # 下载 (B 站: bilidown + 直链兜底; 微博: 直链 + Referer)
    mp4 = download_for(cfg, video)
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














def run_queue(cfg, api_key, queue, touched, isolate=False):
    """逐个处理 -> (成功, 失败)。

    S 的口径 = 报告里真的多出一节: 分析失败但章节已落盘的 partial 也算 S(第三个
    等式按节数算), 只有 rp 为 None(下载就失败)才计入 F。isolate=True 只给微博补
    充队列用 —— 一条炸了不影响其它条目; B 站那条照旧上抛, 不偷偷吞掉。
    """
    ok_n = fail_n = 0
    for v in queue:
        try:
            ok, rp, note = process_video(cfg, api_key, v)
            status = "ok" if ok else ("error" if rp is None else "partial")
            if note == "无字幕,跳过分析":
                status = "no-subtitle"
            mark_processed(v["bvid"], v.get("title", ""), status)
            if rp and rp not in touched:
                touched.append(rp)
            if rp:
                ok_n += 1
            else:
                fail_n += 1
            log(f"状态: {v['bvid']} -> {status} ({note})")
        except Exception as exc:
            if not isolate:
                raise
            fail_n += 1
            log(f"⚠️ 微博条目处理异常 (已忽略): {v.get('bvid')} "
                f"{type(exc).__name__}: {exc}")
    return ok_n, fail_n


def dynamics_target_date(touched, today=None):
    """动态写进哪份日报 —— 两条规则:

    1. 本次 run 处理过视频: 用**那份视频报告**的日期(多份取最新)。线上 2026-11-24
       02:37 那次处理的是 11-23 发布的视频, 报告是 2026-11-23.md; 按 now() 取日期
       会另建一份 2026-11-24.md 平行骨架(只有 今日摘要/视频清单 空壳), 与视频报告分家。
    2. 本次没处理视频(列表拿到了、只是没新视频): 用**今天**, 允许新建 —— 动态就是
       当天发的, 不该往几天前的报告里塞。不往旧日报追加(按日期读就是错位内容)。
    """
    dates = sorted(d for d in (report_date_of(p) for p in (touched or [])) if d)
    if dates:
        return dates[-1]
    return today or datetime.datetime.now(CN_TZ).strftime("%Y-%m-%d")


def process_dynamics(cfg, api_key, video_ok, touched=None):
    """视频链路之后处理 B 站动态, 返回真正写进报告的条数。

    顺序是硬约束, 不许颠倒: 抓取 -> 出简版 -> 追加报告 -> **报告写成功之后**才推进 state。
    判重也不只靠 state: append_dynamics 按报告里的 <!-- dynamic:id --> 标记跳过。

    state 只推进真正写进报告的那些条: 一条没写进去还推进, 那些动态就被永久标成
    已处理、再也不会进报告(静默漏采)。视频列表失败时 allow_create=False,
    append_dynamics 返回 0, 这里就一条都不推进。

    touched: 本次 run 写过的报告路径, 用来决定动态落进哪份日报(见 dynamics_target_date)。
    """
    collected = collect_dynamics(cfg)
    new = collected.get("new") or []
    if not new:
        return 0
    # 硬约束: 视频列表没拿到 -> 一律不落盘、不推进 state。
    # 必须由这个条件本身来拦, 不能靠 allow_create=False 或「报告文件在不在」:
    # allow_create 只挡新建, 挡不住往已存在的旧报告里追加(验收员实测过:
    # 「列表失败 + 旧报告存在」既写进旧报告、又推进 state 2 条)。
    if not video_ok:
        log(f"⚠️ 视频列表未拿到, 动态 {len(new)} 条本次不落盘、不推进 state(下次正常 run 再补)")
        return 0
    today = datetime.datetime.now(CN_TZ).strftime("%Y-%m-%d")
    date_str = dynamics_target_date(touched, today)
    src = "本次处理的视频报告" if any(report_date_of(p) for p in (touched or [])) else "当天(没新视频)"
    log(f"ℹ️ 动态写入目标: reports/{date_str}.md (来源: {src})")

    # 已发布的条目原样复用: 报告里已有 <!-- dynamic:id --> 的, 不再调 LLM、也不会被改写
    # (否则第二次 run 会把已发布的分析按模型随机性重写一遍, 节 sha 漂移, 还白花额度)。
    published = marked_dynamic_ids(read_text(report_path(date_str), default=""))
    todo = [d for d in new if str(d.get("id_str") or "") not in published]
    if len(todo) < len(new):
        log(f"ℹ️ 报告里已有 {len(new) - len(todo)} 条动态, 原样复用不重分析")
    entries = [{"dynamic": d, "analysis": analyze_dynamic(cfg, api_key, d)}
               for d in todo]
    added = append_dynamics(date_str, entries, allow_create=True) if entries else 0
    if added == len(todo):
        # 已发布的那批也算已处理: 它们本来就在报告里
        commit_dynamics(cfg, new)
    else:
        log(f"⚠️ 动态只写入 {added}/{len(todo)} 条, 不推进 state "
            f"(没落盘的不会标成已处理)")
    return added


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
    # 上一跑若在交接之后、发布之前崩了, data/ 里会留下待删列表。它不是本轮的,
    # 拿锁后立刻清掉, 免得发布段拿着陈旧决定去删节。
    save_handoff([], 0)
    # 中断残留识别: 进程若在 os.replace 之前被杀, 会留下 <name>.tmp.<pid> ——
    # 主文件是完整的旧版, 那种 tmp 只是垃圾。拿锁后清掉够旧的(600s), 不抢正在写的。
    for _leftover in sweep_temp_files(STATE_DIR, os.path.join(WORKDIR, "reports")):
        log(f"🧹 清掉中断残留: {_leftover}")
    # 视频列表是否成功拿到: 只有拿到才允许动态去**新建**当日日报。
    # 列表拉取失败(return 1)那条路径也在 finally 里, 若它新建一份空日报,
    # 等于告诉读者"当天真的没有视频", 而真实情况是根本没拉到。
    video_ok = False
    touched = []          # 本次 run 写过的报告; finally 里的动态要用它决定落哪份
    try:
        # 1. 检测更新
        space = get_space_videos(cfg, cfg["limit"])
        if not space:
            log("❌ 无法获取UP主视频列表(风控或网络问题)")
            return 1
        video_ok = True
        # 只处理 baseline(前天)及之后发布的视频, 更早的忽略 (--force 豁免: 始终取最新)
        videos = space if args.force else [v for v in space if is_from_baseline(v)]
        latest = videos[0] if videos else None
        last_file = os.path.join(STATE_DIR, "last_bvid.txt")
        last_bvid = read_text(last_file).strip()
        done = processed_bvids()
        status_map = processed_status_map()

        # 2. 确定 B 站候选: force=仅最新; 首次=仅最新; 常规=前天以来未处理/失败/部分成功。
        #    这里**不提前 return 0**: 微博有自己的队列, B 站没新视频时也得往下走。
        if args.force:
            candidates = [latest] if latest else []
            log("--force 模式: 强制处理最新视频")
        elif not last_bvid:
            candidates = [latest] if latest else []
            log("首次运行, 处理最新视频")
        else:
            cand = [v for v in videos
                    if v["bvid"] not in done or not is_success_status(status_map.get(v["bvid"]))]
            candidates = list(reversed(cand))        # 旧的在前
            if candidates:
                log(f"发现 {len(candidates)} 个待处理视频")
            elif not videos:
                log("前天以来暂无新视频")
            else:
                log("无新视频 (前天以来均已处理)")

        # 2.5 微博: **先取两份列表再决定下载谁**。同一条内容优先走 B 站下载,
        #     微博只补 B 站没有的; 置顶/超窗/已处理的一律不进队列。
        wsnap, plan = weibo_round(cfg, space, done)
        wqueue = [as_video_item(i) for i in plan["queue"]]
        # 池 = 本次 B 站列表条数(= 跨源配对能看到的全部 B 站条目)。它必须大于微博
        # 窗口内他 B 站的发布量, 否则窗口外的旧片会被当成"微博独有"再下一遍。
        log(f"队列: bili={len(candidates)} weibo={len(wqueue)} "
            f"合计={len(candidates) + len(wqueue)} ｜ B站池={len(space)}")
        # 开跑前先打一次六数: 队列第一支就崩的那次, 这行到死都没印出来(2026-11-26)。
        log(format_list_counts(len(candidates), plan["counts"],
                               wsnap.get("cards"), wsnap.get("videos")))

        # 3. 逐个处理: B 站队列在前 (这段与接入微博前逐字相同), 微博补在后
        succeeded, failed = run_queue(cfg, api_key, candidates, touched)
        ok_w, fail_w = run_queue(cfg, api_key, wqueue, touched, isolate=True)
        succeeded, failed = succeeded + ok_w, failed + fail_w

        # 4. 更新状态文件 (只推进 B 站游标; 微博按 id 去重, 没有游标)
        if candidates and latest:
            write_text(last_file, latest["bvid"] + "\n")
            log(f"状态已更新: last_bvid={latest['bvid']}")
        # 往轮发过的微博节, 这轮由 B 站重出 -> 删旧节, 同一内容只留 B 站那条。
        # 这里只**判定**要删哪些; 真正删在发布段对合并后的报告做(见 crossreplace 头注)。
        drops, _skipped = apply_replacements(plan.get("replaced"), processed_status_map())
        save_handoff(drops, succeeded)
        log(f"跨源替换(待删微博节)={len(drops)}")
        log(format_list_counts(len(candidates), plan["counts"],
                               wsnap.get("cards"), wsnap.get("videos"),
                               succeeded, failed))
        log(f"报告节(队列): 追加={succeeded}")

        # 5. 输出确认
        if touched:
            for rp in touched:
                print(f"✅ 今日报告: {rp}")
        return 0
    finally:
        # 6. 动态监测 (视频链路之后)。顺序是硬约束, 不许颠倒:
        #    抓取 -> 出简版 -> 追加报告 -> **报告写成功之后**才推进 state。
        #    反过来的话, 报告写失败会把动态永久标成已处理, 就是静默漏采换个位置重演。
        #    判重也不只靠 state: append_dynamics 按报告里的 <!-- dynamic:id --> 标记跳过。
        try:
            process_dynamics(cfg, api_key, video_ok, touched)
        except Exception as exc:            # 兜底: 动态任何异常都不改上面的返回码
            log(f"⚠️ 动态处理异常 (已忽略): {type(exc).__name__}: {exc}")
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
