"""跨源合并单测 (离线, 不联网; 数据用真实标题, 不编假结构)。

钉六件事:
1. 标题相等也必须落在时间窗内 —— B 站 400 条里同名不同作品有 8 对, 最远 1683h。
2. 相似度阈值 0.9: 真实数据里《稳市场措施持续发力》vs《稳市组合拳持续发力》= 0.667,
   落在窗内若按 0.6 会并成一条 —— 那是缺漏。
3. 一条微博被多条 B 站命中, D 只加 1。
4. N + M - D = K 恒等; S + F = K 与报告节数的关系由编排层保证。
5. 日志六个数按督查的格式打出来。
6. 窗口跨度按首末 created_at 算, 不是写死 17.6 小时。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lidaxiao.sources import merge as G  # noqa: E402

OK = FAIL = 0


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


T0 = 1795604547                    # 2026-11-25 19:02:27 +08


def bili(bvid, title, ts, **kw):
    return dict({"bvid": bvid, "title": title, "pubdate": ts}, **kw)


def weibo(wid, title, ts):
    return {"source": "weibo", "id": wid, "title": title, "created_at": ts}


print("== 标题归一化 ==")
chk("去 hashtag", G.normalize_title("美股反弹的原因 #李大霄#") == "美股反弹的原因")
chk("全角转半角", G.normalize_title("（测试）") == "测试", G.normalize_title("（测试）"))
chk("去空白与标点", G.normalize_title("稳市场，措施 持续发力") == "稳市场措施持续发力")
chk("书名号不影响", G.normalize_title("《稳市场的重要举措》") == "稳市场的重要举措")
chk("大小写归一", G.normalize_title("ETF 怎么看") == "etf怎么看")

print("== 相似度 ==")
chk("完全相同给 1.0", G.title_similarity("美股反弹的原因", "美股反弹的原因") == 1.0)
chk("真实对上的一对 = 1.0",
    G.title_similarity("提醒11月3日对美股的风险", "提醒11月3日对美股的风险") == 1.0)
_s = G.title_similarity("稳市场措施持续发力", "稳市组合拳持续发力")
chk("同话题不同片约 0.667（低于阈值）", 0.6 < _s < 0.7, _s)
chk("空标题给 0", G.title_similarity("", "x") == 0.0)

print("== 匹配: 真实的正例与反例 ==")
BI = [
    bili("BV1uEtH6mEY6", "提醒11月3日对美股的风险", T0 - 20278000 + 0, ),
    bili("BV1eWy2zEE5U", "美股反弹的原因", T0 + 61800),
    bili("BV1PKWj62ExA", "稳市组合拳持续发力", T0 - 14 * 86400),
    bili("BV1yqp46GEiW", "反弹的原因", T0 - 1683 * 3600),
]
W1 = weibo("5369216985206630", "客户利益为重还是公司利益为重", T0)
W2 = weibo("5369478827477034", "美股反弹的原因", T0 + 61800 + 240)
W3 = weibo("5369239026270845", "稳市场措施持续发力", T0 + 86400 * 2)

hit = G.match_item(W2, BI)
chk("同标题同时间 -> 命中且理由 title-equal",
    hit and hit["bvid"] == "BV1eWy2zEE5U" and hit["reason"] == "title-equal", hit)
chk("命中带相似度实值", hit and hit["similarity"] == 1.0, hit)
chk("不同内容 -> 未匹配", G.match_item(W1, BI) is None, G.match_item(W1, BI))
chk("同话题 0.667 且相隔 14 天 -> 不并（阈值的意义）",
    G.match_item(W3, BI) is None, G.match_item(W3, BI))
chk("同标题但相隔 1683h -> 不并",
    G.match_item(weibo("x1", "反弹的原因", T0), BI) is None)

print("== 时间窗边界 ==")
W_WIN = weibo("x2", "美股反弹的原因", T0 + 61800 + 5 * 3600)       # 差 5h
W_OUT = weibo("x3", "美股反弹的原因", T0 + 61800 + 7 * 3600)       # 差 7h
chk("Δt=5h 命中", G.match_item(W_WIN, BI) is not None)
chk("Δt=7h 不命中", G.match_item(W_OUT, BI) is None)
chk("自定义窗口 8h 时 7h 能命中",
    G.match_item(W_OUT, BI, window_hours=8) is not None)

print("== 阈值可覆盖(不写死) ==")
# 同一对标题，但把微博时间挪进 5 小时窗：默认 0.9 不并，降到 0.6 就并 —— 证明阈值真在起作用。
W3_IN = weibo("x4", "稳市场措施持续发力", T0 - 14 * 86400 + 5 * 3600)
chk("窗内 0.667：默认阈值不并", G.match_item(W3_IN, BI) is None, G.match_item(W3_IN, BI))
chk("窗内 0.667：阈值降到 0.6 就并（阈值不是装饰）",
    G.match_item(W3_IN, BI, threshold=0.6) is not None,
    G.match_item(W3_IN, BI, threshold=0.6))

print("== 一条微博被多条 B 站命中, D 只加 1 ==")
BI2 = [bili("BV1eWy2zEE5U", "美股反弹的原因", T0 + 61800),
       bili("BV1zzz000001", "美股反弹的原因", T0 + 61800 + 600)]
res = G.merge_sources(BI2, [weibo("w1", "美股反弹的原因", T0 + 61800 + 120)])
chk("K = 2（两条 B 站都保留）", res["counts"]["merged"] == 2, res["counts"])
chk("D = 1（微博只算被覆盖一次）", res["counts"]["skipped"] == 1, res["counts"])
chk("微博那条没进处理列表", all("w1" != str(i.get("id")) for i in res["items"]), res["items"])

print("== merge: 四数恒等 ==")
BILI3 = [bili("BV1eWy2zEE5U", "美股反弹的原因", T0 + 61800),
         bili("BV1X6ynzrEkU", "本轮黄金波动的原因", T0 - 52 * 3600)]
WEI3 = [weibo("5369478827477034", "美股反弹的原因", T0 + 61800 + 240),
        weibo("5369222189809983", "投资前客观评估自己", T0)]
r3 = G.merge_sources(BILI3, WEI3)
c = r3["counts"]
chk("N=2 M=2 D=1", (c["bili"], c["weibo"], c["skipped"]) == (2, 2, 1), c)
chk("K = N + (M - D) = 3", c["merged"] == 3, c)
chk("恒等式成立", c["bili"] + c["weibo"] - c["skipped"] == c["merged"])
chk("处理列表 = B 站在前 + 未匹配微博在后",
    [i.get("bvid") or i.get("id") for i in r3["items"]]
    == ["BV1eWy2zEE5U", "BV1X6ynzrEkU", "5369222189809983"],
    [i.get("bvid") or i.get("id") for i in r3["items"]])
chk("B 站条目原样不动", r3["items"][:2] == BILI3)
chk("微博条目带 source=weibo", r3["items"][2]["source"] == "weibo")

print("== 日志格式 ==")
line = G.format_counts(c, ok=2, fail=1)
chk("六个数都在", line == "列表: bili=2 weibo=2 合并后=3 因重复跳过=1 ｜ 下载/处理成功=2 失败=1", line)
chk("不给 S/F 时只打四个数",
    G.format_counts(c) == "列表: bili=2 weibo=2 合并后=3 因重复跳过=1",
    G.format_counts(c))

print("== 窗口跨度 ==")
span = G.window_span(WEI3)
chk("首末取 min/max", span["first"] == T0 and span["last"] == T0 + 61800 + 240, span)
chk("跨度按小时算", span["hours"] == round((61800 + 240) / 3600.0, 2), span)
chk("空列表不炸", G.window_span([]) == {"first": None, "last": None, "hours": None, "count": 0})
chk("忽略没有时间的条目", G.window_span([{"id": "x"}, weibo("y", "t", T0)])["count"] == 1)

print("== 配对表 ==")
rows = G.pair_table(BILI3, WEI3)
chk("每条微博一行", len(rows) == 2, len(rows))
chk("命中行带 bvid/理由/相似度",
    rows[0]["bvid"] == "BV1eWy2zEE5U" and rows[0]["reason"] == "title-equal"
    and rows[0]["similarity"] == 1.0, rows[0])
chk("未命中行写 unmatched", rows[1]["bvid"] is None and rows[1]["reason"] == "unmatched", rows[1])

print("== 下载队列: M/D/P/Q 五数 (督查口径) ==")
PINNED = {"source": "weibo", "id": "2635689", "title": "2635与2689",
          "created_at": 1726750200, "pinned": True}          # 置顶, 2024-09-19
QUEUE_W = [
    weibo("5369216985206630", "客户利益为重还是公司利益为重", T0),
    weibo("5369222189809983", "投资前客观评估自己", T0 + 1260),
    weibo("5369478827477034", "美股反弹的原因", T0 + 61800 + 240),   # 命中 BV1eWy2zEE5U
    PINNED,
    weibo("5369239026270845", "黄金的判断", T0 - 30 * 86400),        # 30 天前, 超窗
]
plan = G.build_queue(BI, QUEUE_W, now=T0 + 61800 + 3600)
c = plan["counts"]
chk("M = 微博视频条数", c["weibo_videos"] == 5, c)
chk("D = 被 B 站覆盖的 1 条", c["matched"] == 1, c)
chk("P = 置顶 1 + 超窗 1", c["excluded"] == 2 and c["pinned"] == 1 and c["stale"] == 1, c)
chk("Q = M - D - P", c["queue"] == 2, c)
chk("置顶那条不进队列", all(i.get("id") != "2635689" for i in plan["queue"]))
chk("超窗那条不进队列", all(i.get("id") != "5369239026270845" for i in plan["queue"]))
chk("队列里只有 B 站没有的两条",
    [i["id"] for i in plan["queue"]] == ["5369216985206630", "5369222189809983"],
    plan["queue"])

done_plan = G.build_queue(BI, QUEUE_W, seen={"wb5369216985206630"}, now=T0 + 61800 + 3600)
chk("已处理过的微博不进队列", done_plan["counts"]["queue"] == 1, done_plan["counts"])
chk("已处理记在 done 桶, 仍算进 P",
    done_plan["counts"]["done"] == 1 and done_plan["counts"]["excluded"] == 3,
    done_plan["counts"])

print("== 等式: K = N + Q, Q = M - D - P ==")
for label, pl, n in (("常规", plan, 3), ("有已处理", done_plan, 0)):
    cn = pl["counts"]
    q = cn["weibo_videos"] - cn["matched"] - cn["excluded"]
    chk(f"{label}: Q = M - D - P", q == cn["queue"], cn)
    line = G.format_list_counts(n, cn, 12, 9)
    chk(f"{label}: 日志里 K = N + Q",
        f"合并后={n + cn['queue']}" in line and f"队列={cn['queue']}" in line, line)

print("== 同一条微博被多条 B 站命中, D 只 +1 ==")
BI2 = [bili("BV1eWy2zEE5U", "美股反弹的原因", T0 + 61800),
       bili("BV1uEtH6mEY6", "美股反弹的原因", T0 + 61800 - 1800)]
dup = G.build_queue(BI2, [weibo("5369478827477034", "美股反弹的原因", T0 + 61800 + 240)],
                    now=T0 + 61800 + 3600)
chk("两条 B 站命中同一条微博, D 仍为 1", dup["counts"]["matched"] == 1, dup["counts"])
chk("它不会进队列", dup["counts"]["queue"] == 0, dup["counts"])

print("== 微博条目 -> 视频链路能吃的结构 ==")
asv = G.as_video_item({"id": "5369222189809983", "title": "书中内容解读",
                       "created_at": T0, "duration": 151.6,
                       "video_url": "https://f.video.weibocdn.com/x.mp4",
                       "weibo_url": "https://m.weibo.cn/status/5369222189809983"})
chk("bvid 位是 wb<id>", asv["bvid"] == "wb5369222189809983", asv)
chk("pubdate 取 created_at", asv["pubdate"] == T0, asv)
chk("时长换成 mm:ss", asv["dur_str"] == "02:31", asv["dur_str"])
chk("source 标 weibo", asv["source"] == "weibo", asv)
chk("没标题不炸", G.as_video_item({"id": "1"})["title"] == "（微博视频无标题）")
chk("没有 id 的键为空", G.weibo_key({}) == "", G.weibo_key({}))

print("== 日志格式 ==")
line1 = G.format_weibo_counts(c)
chk("第一行四个数都在",
    "视频=5" in line1 and "匹配跳过=1" in line1 and "置顶/超窗排除=2" in line1
    and "进入队列=2" in line1, line1)
line2 = G.format_list_counts(3, c, 12, 9, 2, 1)
chk("第二行带成功/失败", "下载/处理成功=2 失败=1" in line2, line2)
chk("第二行原始条数拆视频/非视频", "weibo=12(9视频/3非视频)" in line2, line2)
chk("窗口跨度只算现行窗口 (去置顶、去超窗回填)",
    plan["span"]["count"] == 3 and 17 < plan["span"]["hours"] < 18, plan["span"])

print("== 时间形状: int 与 ISO 字符串都必须认 (错形状不许静默) ==")
ISO_A = "2026-11-25T19:02:27+08:00"
ISO_B = "2026-11-26T12:18:44+08:00"
chk("ISO 与 int 同一时刻等价",
    G.epoch_of(ISO_A) == T0 and G.epoch_of(T0) == float(T0), G.epoch_of(ISO_A))
chk("ISO 与 int 求差为 0", G._hours(T0, ISO_A) == 0.0)
chk("认不出给 None (不炸)", G.epoch_of("昨天") is None and G.epoch_of(None) is None)
chk("裸时间按北京时间", G.epoch_of("2026-11-25 19:02:27") == float(T0),
    G.epoch_of("2026-11-25 19:02:27"))

# 真实导出的形状: B 站 pubdate 是 int, 微博 created_at 是 ISO 字符串。
# 只认数字会让整张表静默判"未匹配"(再被超窗规则变成全 stale 排除) —— 那是漏采。
ISO_BI = [bili("BV1uEtH6mEY6", "提醒11月3日对美股的风险", T0 - 20278000),
          bili("BV1eWy2zEE5U", "美股反弹的原因", T0)]
ISO_W = [{"source": "weibo", "id": "5369478827477034", "title": "美股反弹的原因",
          "created_at": ISO_A},                                   # 与 B 站同一分钟
         {"source": "weibo", "id": "5369483480797474", "title": "美股反弹的原因",
          "created_at": ISO_B}]                                   # 17h 后, 不该并
iso_hit = G.match_item(ISO_W[0], ISO_BI)
chk("ISO 形状喂进去也能匹配上(不能静默 0 匹配)",
    iso_hit and iso_hit["similarity"] == 1.0, iso_hit)
iso_plan = G.build_queue(ISO_BI, ISO_W, now=T0 + 61800 + 3600)
chk("ISO 形状不被全判 stale",
    iso_plan["counts"]["queue"] == 1 and iso_plan["counts"]["stale"] == 0,
    iso_plan["counts"])
chk("ISO 形状下 D=1", iso_plan["counts"]["matched"] == 1, iso_plan["counts"])
chk("ISO 形状也能换成 pubdate 数字",
    isinstance(G.as_video_item(ISO_W[0])["pubdate"], int), G.as_video_item(ISO_W[0]))

# 残余形状 (验收员 2026-11-26 点出): 数字字符串与毫秒时间戳, 同样不许静默判 stale
chk("数字字符串认成时间", G.epoch_of("1795604547") == float(T0), G.epoch_of("1795604547"))
chk("数字字符串求差不炸", G._hours(T0, "1795604547") == 0.0)
chk("毫秒时间戳归一到秒", G.epoch_of(1795604547000) == float(T0))
chk("微秒时间戳归一到秒", G.epoch_of(1795604547000000) == float(T0))
chk("毫秒必须与 B 站配上, 否则会被当成独有重下",
    G.build_queue(ISO_BI, [dict(ISO_W[0], created_at=1795604547000)],
                  now=T0 + 61800 + 3600)["counts"]["queue"] == 0
    and G.build_queue(ISO_BI, [dict(ISO_W[0], created_at=1795604547000)],
                      now=T0 + 61800 + 3600)["counts"]["matched"] == 1,
    G.build_queue(ISO_BI, [dict(ISO_W[0], created_at=1795604547000)],
                  now=T0 + 61800 + 3600)["counts"])
chk("布尔不当时间 (True 不是 1 秒)", G.epoch_of(True) is None)

# inf / nan / 1e999: 能穿过 float() 与大小比较, 却会让下游 int() 抛 —— 那是在
# 监控链路正中间炸 (as_video_item 在 main 里先于视频队列执行), 所以必须在这里判掉。
for _bad in ("inf", "-inf", "nan", "1e999", float("inf"), float("nan")):
    chk(f"{_bad!r} 认不出 (不当时间)", G.epoch_of(_bad) is None, G.epoch_of(_bad))
junk = G.as_video_item({"id": "1", "title": "x", "created_at": float("nan"),
                        "duration": float("inf")})
chk("非有限值不会让 as_video_item 抛", isinstance(junk["pubdate"], int), junk)
chk("非有限时长归 0 (不印 00:00 假长度)",
    junk["duration"] == 0.0 and junk["dur_str"] == "", junk)
chk("认不出时间时 pubdate 落在当前时刻附近",
    abs(junk["pubdate"] - int(__import__("time").time())) < 5, junk["pubdate"])
plan_bad = G.build_queue(ISO_BI, [{"source": "weibo", "id": "9", "title": "x",
                                   "created_at": float("nan")}], now=T0)
chk("坏时间的条目按 stale 排除, 不进队列",
    plan_bad["counts"]["queue"] == 0 and plan_bad["counts"]["stale"] == 1,
    plan_bad["counts"])

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
