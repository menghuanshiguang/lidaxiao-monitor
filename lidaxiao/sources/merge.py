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
import time
import unicodedata

from lidaxiao.common import CN_TZ

#: 同一条内容在两边发布的间隔实测是分钟级 (0.01h / 0.07h), 6 小时已很宽松。
WINDOW_HOURS = 6.0

#: 见模块 docstring 的校准过程。
SIM_THRESHOLD = 0.9

#: 早于该天数的微博条目一律不下 —— 置顶那条 2024-09-19 与历史回填都靠它挡。
MAX_AGE_DAYS = 7

#: 报告/state 里给微博条目的键前缀。B 站是 `(BV…)`, 微博是 `(wb<id>)`,
#: 报告里一眼分辨, 也不会被"按 BV 找同一条"的逻辑误认。
WEIBO_KEY_PREFIX = "wb"

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


# ------------------------------------------------------------------ 下载队列


def weibo_key(item):
    """微博条目的键: `wb` + mblog.id。没有 id 返回空串 (调用方据此跳过)。"""
    wid = str((item or {}).get("id") or "")
    return (WEIBO_KEY_PREFIX + wid) if wid else ""


def as_video_item(item):
    """微博条目 -> 与 B 站条目同形的"视频", 好让同一条处理链路直接吃。

    bvid 位放 `wb<id>`: 报告章节头、清单行、字幕文件名、片尾缓存键都用它,
    所以微博条目在磁盘上不会和任何 BV 撞名。
    """
    item = dict(item or {})
    dur = item.get("duration")
    dur = float(dur) if isinstance(dur, (int, float)) and dur > 0 else 0.0
    return {
        "source": "weibo",
        "id": str(item.get("id") or ""),
        "bvid": weibo_key(item),
        "title": str(item.get("title") or "").strip() or "（微博视频无标题）",
        "pubdate": item.get("created_at") or int(time.time()),
        "duration": dur,
        "dur_str": f"{int(dur // 60):02d}:{int(dur % 60):02d}" if dur else "",
        "video_url": item.get("video_url") or "",
        "weibo_url": item.get("weibo_url") or "",
    }


def build_queue(bili_items, weibo_items, seen=(), threshold=None, window_hours=None,
                max_age_days=MAX_AGE_DAYS, now=None):
    """两份列表 -> 本次下载队列 + 五数。口径 (督查 2026-11-26 定死):

        视频=M        微博视频进入判定的条数
        匹配跳过=D    被 B 站同一条覆盖; **同一条微博被多条 B 站命中只 +1**
        排除=P        置顶 / created_at 早于 max_age_days / 已处理过 (三者都不下载)
        进入队列=Q    Q = M - D - P
        合并后=K      K = N + Q   (N = 本次待处理 B 站条数, 由编排层给)

    判定顺序固定为 匹配 -> 置顶 -> 超窗 -> 已处理: 三个桶互斥, 同一条既被覆盖
    又置顶时只记 D 一次, 不会把一个条目算进两个数。
    """
    bili_items = list(bili_items or [])
    weibo_items = list(weibo_items or [])
    seen = {str(x) for x in (seen or [])}
    now = int(time.time() if now is None else now)
    cutoff = now - int(max_age_days) * 86400
    table = pair_table(bili_items, weibo_items, window_hours, threshold)

    queue, buckets = [], {"matched": 0, "pinned": 0, "stale": 0, "done": 0}
    for item, row in zip(weibo_items, table):
        if row["bvid"]:
            buckets["matched"] += 1
            continue
        ts = item.get("created_at")
        if item.get("pinned"):
            buckets["pinned"] += 1
        elif not isinstance(ts, (int, float)) or ts < cutoff:
            buckets["stale"] += 1                     # 时间不明也当下不了: 宁漏不错
        elif weibo_key(item) in seen:
            buckets["done"] += 1
        else:
            queue.append(item)

    m, d = len(weibo_items), buckets["matched"]
    p = buckets["pinned"] + buckets["stale"] + buckets["done"]
    # 跨度只按"现行窗口"算: 去掉置顶那条 2024-09-19 和历史回填(超窗)的,
    # 否则一条两年前的置顶把跨度撑到 19000h, 「无缺漏」的上限就成了假数。
    fresh = [i for i in weibo_items
             if not i.get("pinned") and isinstance(i.get("created_at"), (int, float))
             and i["created_at"] >= cutoff]
    return {
        "queue": queue,
        "pairs": table,
        "span": window_span(fresh),
        "counts": {"weibo_videos": m, "matched": d, "excluded": p,
                   "pinned": buckets["pinned"], "stale": buckets["stale"],
                   "done": buckets["done"], "queue": len(queue)},
    }


def format_weibo_counts(counts):
    """第一行: 微博侧的四个数, 排除再拆成两项 (和仍等于 P, 第三行可核)。

    已处理单独列出来, 是因为 cron 每 3 小时一轮而窗口约 17.6 小时 —— 同一条会在
    连续几轮里反复出现, 不排除就会每轮重下重析。
    """
    return (f"微博: 视频={counts.get('weibo_videos', 0)} "
            f"匹配跳过={counts.get('matched', 0)} "
            f"置顶/超窗排除={counts.get('pinned', 0) + counts.get('stale', 0)} "
            f"已处理排除={counts.get('done', 0)} "
            f"进入队列={counts.get('queue', 0)}")


def format_list_counts(bili_n, counts, cards=None, videos=None, ok=None, fail=None):
    """第二行: 目录口径的五个数 + 成功/失败。核对就是 K = N + Q 与 S + F = K。"""
    q = counts.get("queue", 0)
    k = int(bili_n) + q
    raw = ""
    if cards is not None:
        videos = 0 if videos is None else int(videos)
        # 原始条数在前, 括号里拆成视频/非视频: 「开播」通知也是 card, 只打 12 会与
        # 视频 9 条对不上账。非视频 = cards - videos, 不写死"开播"。
        raw = f"weibo={cards}({videos}视频/{max(int(cards) - videos, 0)}非视频) "
    line = (f"列表: bili={bili_n} {raw}合并后={k} "
            f"因重复跳过={counts.get('matched', 0)} "
            f"排除={counts.get('excluded', 0)} 队列={q}")
    if ok is not None or fail is not None:
        line += f" ｜ 下载/处理成功={ok or 0} 失败={fail or 0}"
    return line


def format_window(span):
    """窗口首末与跨度 —— 「无缺漏」的上限是「两轮成功 run 的间隔 < 这个跨度」。"""
    def iso(ts):
        if not isinstance(ts, (int, float)):
            return "?"
        return datetime_of(ts).strftime("%Y-%m-%d %H:%M")

    return (f"微博窗口(去置顶): 首 {iso(span.get('first'))} 末 {iso(span.get('last'))} "
            f"跨度 {span.get('hours')}h 条数 {span.get('count')}")


def datetime_of(ts):
    """epoch -> 北京时间 datetime (只给日志用)。"""
    import datetime

    return datetime.datetime.fromtimestamp(ts, CN_TZ)
