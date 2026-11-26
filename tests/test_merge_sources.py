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

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
