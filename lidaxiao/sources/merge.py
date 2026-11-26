"""跨源合并: 同一内容优先 B 站, 微博只补 B 站没有的。

纯函数, 不联网、不读盘 —— 两份列表由调用方取好传进来。合并结果用于决定
"本次到底下载谁", 报告里每条内容只出现一次。

阈值怎么来的 (2026-11-26, 真实数据: B 站 400 条窗口 + 微博 12 条):

    Δt ≤ 6h 的候选对一共只有 2 对, 相似度都是 1.000
        《提醒11月3日对美股的风险》→ BV1uEtH6mEY6, Δt 0.01h, sim 1.000
        《美股反弹的原因》        → BV1eWy2zEE5U, Δt 0.07h, sim 1.000
    所有"像但不是同一条"的最高分都在窗外:
        0.833 《反弹的原因》 (Δt 1147h) / 0.714 《美股崩盘的原因》 (118h)
        0.667 《稳市组合拳持续发力》 (344h) —— 与《稳市场措施持续发力》话题相同、
              但相隔 14 天, 是两条不同视频
    6 小时窗内没有 0.6~0.99 的中间样本, 所以阈值在这份数据上无法被"拟合"出来;
    0.6 与 0.9 在现有数据上结果相同。取 0.9 是朝保守方向: 万一将来"同话题不同视频"
    进了 6 小时窗 (上面 0.667 那对就是样本), 0.6 会把两条并成一条 —— 那是**缺漏**,
    比重复更难发现。阈值可经 config weibo.sim_threshold 覆盖。
"""
import re
import unicodedata

#: 同一条内容在两边发布的间隔实测是分钟级 (0.01h / 0.07h), 6 小时已很宽松。
WINDOW_HOURS = 6.0

#: 见模块 docstring 的校准过程。
SIM_THRESHOLD = 0.9

TAG_RE = re.compile(r"#[^#]{1,30}#")
SPACE_RE = re.compile(r"[\s\u3000]+")
PUNCT_RE = re.compile(r"[，。、；：！？,.;:!?\"'“”‘’（）()\[\]【】<>《》\-—…·|/\\]")


def normalize_title(text):
    """归一化: 全角转半角(NFKC) -> 去 hashtag -> 去空白/标点 -> 小写。"""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = TAG_RE.sub("", t)
    t = SPACE_RE.sub("", t)
    t = PUNCT_RE.sub("", t)
    return t.lower()


def title_similarity(a, b):
    """归一化标题的相似度 [0,1]; 相等直接给 1.0。"""
    import difflib

    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _hours(ts_a, ts_b):
    if not isinstance(ts_a, (int, float)) or not isinstance(ts_b, (int, float)):
        return None
    return abs(float(ts_a) - float(ts_b)) / 3600.0


def match_item(weibo_item, bili_items, window_hours=None, threshold=None):
    """给一条微博找它的 B 站同款; 找不到返回 None。

    判定(两条分支都必须落在时间窗内):
      a) 归一化标题完全相等 且 Δt ≤ window_hours
      b) Δt ≤ window_hours 且 相似度 ≥ threshold
    a 也加时间窗, 是拿真实数据换来的: B 站 400 条里**同名不同作品有 8 对**,
    最远相隔 1683h (《反弹的原因》两个 BV), 也有 25~26h 的。只按标题相等匹配,
    会把几个月前的旧片和今天的微博并成一条 —— 那是缺漏, 比重复难发现。
    命中多条的取时间最近的一条: 这是"同一条微博被多条 B 站命中"的唯一出口,
    调用方据此只把 D 加 1。
    """
    window = WINDOW_HOURS if window_hours is None else float(window_hours)
    limit = SIM_THRESHOLD if threshold is None else float(threshold)
    best = None
    for bili in bili_items or []:
        sim = title_similarity(weibo_item.get("title"), bili.get("title"))
        dt = _hours(weibo_item.get("created_at"), bili.get("pubdate"))
        if dt is None or dt > window:
            continue                      # 时间对不上就不算同一条(见 docstring 的 8 对同名)
        if sim >= 1.0:
            reason = "title-equal"
        elif sim >= limit:
            reason = "title-similar"
        else:
            continue
        key = (0 if reason == "title-equal" else 1, dt if dt is not None else 9e9, -sim)
        if best is None or key < best[0]:
            best = (key, {"bvid": bili.get("bvid"), "title": bili.get("title"),
                          "reason": reason, "similarity": round(sim, 4),
                          "dt_hours": None if dt is None else round(dt, 3)})
    return best[1] if best else None


def pair_table(bili_items, weibo_items, window_hours=None, threshold=None):
    """逐条微博 -> 匹配到的 bvid 或 None, 附判定理由与相似度实值。

    这是核对匹配对不对用的表: 四个数自洽证明不了配对正确, 这张表才能。
    """
    window = WINDOW_HOURS if window_hours is None else float(window_hours)
    rows = []
    for w in weibo_items or []:
        hit = match_item(w, bili_items, window_hours, threshold)
        # 未命中的行也要给"窗内最像的那条"与其实值 —— 核对时才能判断是放对了还是放错了。
        best = None
        for bili in bili_items or []:
            dt = _hours(w.get("created_at"), bili.get("pubdate"))
            if dt is None or dt > window:
                continue
            sim = title_similarity(w.get("title"), bili.get("title"))
            key = (dt, -sim)
            if best is None or key < best[0]:
                best = (key, bili, sim, dt)
        rows.append({
            "weibo_id": str(w.get("id") or ""),
            "weibo_title": w.get("title", ""),
            "weibo_created_at": w.get("created_at"),
            "bvid": (hit or {}).get("bvid"),
            "bili_title": (hit or {}).get("title"),
            "reason": (hit or {}).get("reason", "unmatched"),
            "similarity": (hit or {}).get("similarity", 0.0),
            "dt_hours": (hit or {}).get("dt_hours"),
            "nearest_bvid": best[1].get("bvid") if best else None,
            "nearest_similarity": round(best[2], 4) if best else None,
            "nearest_dt_hours": round(best[3], 3) if best else None,
        })
    return rows


def merge_sources(bili_items, weibo_items, window_hours=None, threshold=None):
    """合并两份列表 -> 本次要处理的内容 + 四个计数。

    口径(验收员定死, 不要改):
      N = B 站本次**待处理**条数(不是 400 窗口)
      M = 微博进入判定的条数
      D = 被 B 站覆盖而丢掉的微博条数; **同一条微博被多条 B 站命中只 +1**
      K = N + (M - D)
    微博侧窗口外的历史条目补不回来, 不是 D, 也不进 M —— 见 window_span。
    """
    bili_items = list(bili_items or [])
    weibo_items = list(weibo_items or [])
    table = pair_table(bili_items, weibo_items, window_hours, threshold)
    matched = [r for r in table if r["bvid"]]
    survivors = [w for w, r in zip(weibo_items, table) if not r["bvid"]]
    n, m, d = len(bili_items), len(weibo_items), len(matched)
    return {
        "items": bili_items + survivors,
        "bili_items": bili_items,
        "weibo_new": survivors,
        "pairs": table,
        "counts": {"bili": n, "weibo": m, "merged": n + (m - d), "skipped": d},
    }


def format_counts(counts, ok=None, fail=None):
    """督查定的日志格式; ok/fail 给了就一并打出来(S+F=K 才可核)。"""
    line = (f"列表: bili={counts.get('bili', 0)} weibo={counts.get('weibo', 0)} "
            f"合并后={counts.get('merged', 0)} 因重复跳过={counts.get('skipped', 0)}")
    if ok is not None or fail is not None:
        line += f" ｜ 下载/处理成功={ok or 0} 失败={fail or 0}"
    return line


def window_span(items):
    """微博窗口的首末时间与跨度小时数 —— 「无缺漏」的上限靠它, 不是 17.6 小时这个数。"""
    stamps = [i.get("created_at") for i in (items or [])
              if isinstance(i.get("created_at"), (int, float))]
    if not stamps:
        return {"first": None, "last": None, "hours": None, "count": 0}
    return {"first": min(stamps), "last": max(stamps),
            "hours": round((max(stamps) - min(stamps)) / 3600.0, 2),
            "count": len(stamps)}
