"""微博这一轮的编排: 取一页 -> 跨源合并 -> 队列 + 口径日志。

拆成单独模块的理由很实在: monitor.py 有 "< 600 行, 只留 main/process_video" 的硬预算
(docs/REFACTOR_HANDOVER.md 第 10 节), 而"怎么判同一条内容、排出什么数"是要长期打磨的
规则, 放在 monitor 里既挤预算也不好单测。

两条硬约束在这里落地:
1. 先取两份列表再决定下载谁 —— B 站优先, 微博只补 B 站没有的 (规则在 sources/merge.py)。
2. 微博任何失败都降级成空队列 + 一行日志, 绝不向上抛, 不中断视频链路。
"""
from lidaxiao.sources.merge import build_queue, format_weibo_counts, format_window
from lidaxiao.sources.weibo import timeline_snapshot
from lidaxiao.textutil import log

#: 微博没拿到时的空快照形状 (字段与 timeline_snapshot 一致, 编排层不用再判 None)。
EMPTY_SNAPSHOT = {"status": "error", "items": [], "cards": 0, "videos": 0,
                  "message": "", "http": None, "meta": {}, "has_cookie": False}


def fetch_snapshot(cfg, fetch=None):
    """取一页微博快照。失败只留一行日志, 绝不抛。"""
    try:
        snap = timeline_snapshot(cfg, fetch=fetch) if fetch else timeline_snapshot(cfg)
    except Exception as exc:
        log(f"⚠️ 微博处理异常 (已忽略): {type(exc).__name__}: {exc}")
        return dict(EMPTY_SNAPSHOT, message=str(exc))
    status = snap.get("status")
    if status == "disabled":
        log("微博: 已禁用 (config weibo.enabled=false), 跳过")
    elif status != "ok":
        # 失败要能一眼分清是风控(432)、身份(-100) 还是别的, 且不写 state、不出章节。
        log(f"⚠️ 微博本次没拿到 ({status}: {snap.get('message')}, "
            f"身份={'已注入' if snap.get('has_cookie') else '无'}), 不写 state、不出章节")
    else:
        log(f"微博: 身份={'已注入' if snap.get('has_cookie') else '无'} "
            f"cards={snap.get('cards')} 视频={snap.get('videos')} "
            f"total={(snap.get('meta') or {}).get('total')}")
    return snap


def weibo_round(cfg, bili_items, seen=(), fetch=None):
    """这一轮微博下什么 -> (snapshot, plan), 顺带把口径日志打全。

    bili_items 用**本次 B 站列表原样**, 不是"待处理候选": 判断"这条内容 B 站有没有"
    要按列表看 —— 已经处理过的 B 站条目同样算覆盖, 否则同一条内容会被微博再下一遍。

    plan["counts"]: weibo_videos=M, matched=D, excluded=P(=置顶+超窗+已处理),
                    queue=Q; plan["span"] 是现行窗口的首末与跨度。
    """
    wcfg = (cfg or {}).get("weibo") or {}
    snap = fetch_snapshot(cfg, fetch)
    plan = build_queue(bili_items, snap.get("items") or [], seen=seen,
                       threshold=wcfg.get("sim_threshold"),
                       window_hours=wcfg.get("window_hours"),
                       max_age_days=wcfg.get("max_age_days", 7))
    log(format_weibo_counts(plan["counts"]))
    log(format_window(plan["span"]))
    return snap, plan
