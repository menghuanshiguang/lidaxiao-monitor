"""微博接入的编排层接线 (离线; 取数与处理全部打桩, 不联网不写盘)。

钉的是督查那三条等式在**真实调用链**上成立, 不是只在一个纯函数里成立:
    Q = M - D - P ｜ K = N + Q ｜ S + F = K ｜ 报告新增节数 = S
顺带钉住两条硬约束:
    置顶 / 超窗 / 已处理 一律不进队列; 微博任何失败都降级, 绝不中断视频链路。
"""
import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import monitor as M  # noqa: E402
from lidaxiao.sources import merge as G  # noqa: E402
from lidaxiao.sources import weibo_plan as P  # noqa: E402

OK = FAIL = 0


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


T0 = 1795604547          # 2026-11-25 19:02:27 +08
CFG = {"weibo": {"enabled": True, "uid": "1645823934", "sim_threshold": 0.9,
                 "window_hours": 6, "max_age_days": 7}}

BILI_PAGE = [
    {"bvid": "BV1eWy2zEE5U", "title": "美股反弹的原因", "pubdate": T0 + 61800},
    {"bvid": "BV1uEtH6mEY6", "title": "提醒11月3日对美股的风险", "pubdate": T0 - 20278000},
]


def wb(wid, title, ts, **kw):
    return dict({"source": "weibo", "id": wid, "title": title, "created_at": ts,
                 "video_url": f"https://f.video.weibocdn.com/{wid}.mp4",
                 "weibo_url": f"https://m.weibo.cn/status/{wid}"}, **kw)


ITEMS = [
    wb("5369216985206630", "客户利益为重还是公司利益为重", T0),
    wb("5369222189809983", "投资前客观评估自己", T0 + 1260),
    wb("5369478827477034", "美股反弹的原因", T0 + 61800 + 240),     # B 站有
    wb("2635689", "2635与2689", 1726750200, pinned=True),          # 置顶
]
SNAP_OK = {"status": "ok", "items": ITEMS, "cards": 6, "videos": 4,
           "message": "", "http": 200, "meta": {"cards": 6, "total": 38820},
           "has_cookie": True}

print("== 取数失败一律降级 ==")
_orig_snap = P.timeline_snapshot
try:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        P.timeline_snapshot = lambda cfg: {"status": "risk", "items": [], "cards": 0,
                                          "videos": 0, "message": "HTTP 432 风控拒绝(身份无效)",
                                          "has_cookie": False}
        snap = P.fetch_snapshot(CFG)
    logtxt = buf.getvalue()
    chk("432 -> 空快照, 不抛", snap["items"] == [] and snap["status"] == "risk", snap)
    chk("失败日志点明身份与不写 state", "HTTP 432" in logtxt and "不写 state" in logtxt, logtxt)
    chk("失败日志只有一行", logtxt.count("\n") == 1, logtxt)

    def _boom(cfg):
        raise RuntimeError("网络挂了")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        P.timeline_snapshot = _boom
        snap2 = P.fetch_snapshot(CFG)
    chk("取数抛异常也不外抛", snap2["items"] == [], snap2)
    chk("异常也只留一行", buf.getvalue().count("\n") == 1, buf.getvalue())

    # 失败时统计全 0, 编排仍然给出可核的等式 (上面那个 _boom 桩还在, 不会真去请求)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _s, empty_plan = P.weibo_round(CFG, BILI_PAGE, set())
    c = empty_plan["counts"]
    chk("没拿到微博时五数全 0", c["weibo_videos"] == 0 and c["queue"] == 0, c)
finally:
    P.timeline_snapshot = _orig_snap

print("== 队列: 先取两份列表再决定下载谁 ==")
_orig_proc = M.process_video
_orig_mark = M.mark_processed
_orig_snap2 = P.timeline_snapshot
try:
    P.timeline_snapshot = lambda cfg: SNAP_OK       # 取数打桩: 这一段只验编排
    # 每条落到自己的报告路径: touched 去重是按路径的, 同路径会把 5 条合成 1 条
    M.process_video = lambda cfg, key, video: (True, f"/tmp/{video['bvid']}.md", "ok")
    saved = []
    M.mark_processed = lambda bvid, title, status: saved.append((bvid, status))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        snap = {"status": "ok", "items": ITEMS, "cards": 6, "videos": 4,
                "meta": {"cards": 6, "total": 38820}, "has_cookie": True}
        snap2, plan = P.weibo_round(CFG, BILI_PAGE, set())
        bili_queue = [{"bvid": f"BVt{i}", "title": f"B站待处理{i}", "pubdate": T0}
                      for i in range(3)]
        n = len(bili_queue)                     # N = 本次待处理 B 站条数
        touched = []
        okn, failn = M.run_queue(CFG, "K", bili_queue, touched)
        okw, failw = M.run_queue(CFG, "K", [M.as_video_item(i) for i in plan["queue"]],
                                 touched, isolate=True)
        okn, failn = okn + okw, failn + failw
        line = M.format_list_counts(n, plan["counts"], snap["cards"], snap["videos"],
                                    okn, failn)
        line1 = G.format_weibo_counts(plan["counts"])
        wspan = G.format_window(plan["span"])
    c = plan["counts"]
    chk("M=4 条视频", c["weibo_videos"] == 4, c)
    chk("D=1 (那条 B 站有)", c["matched"] == 1, c)
    chk("P=1 (置顶)", c["excluded"] == 1 and c["pinned"] == 1, c)
    chk("Q=2 (只补 B 站没有的两条)", c["queue"] == 2, c)
    chk("Q = M - D - P", c["weibo_videos"] - c["matched"] - c["excluded"] == c["queue"], c)
    chk("K = N + Q", f"合并后={n + c['queue']}" in line, line)
    chk("S + F = K", okn + failn == n + c["queue"], (okn, failn, n, c["queue"]))
    chk("报告新增节数 = S", len(touched) == okn, (len(touched), okn))
    chk("队列里没有置顶那条",
        all(v["bvid"] != "wb2635689" for v in [M.as_video_item(i) for i in plan["queue"]]))
    chk("队列条目键是 wb<id>",
        [v["bvid"] for v in [M.as_video_item(i) for i in plan["queue"]]]
        == ["wb5369216985206630", "wb5369222189809983"],
        [v["bvid"] for v in [M.as_video_item(i) for i in plan["queue"]]])
    chk("第一行是微博四数", line1.startswith("微博: 视频=4 匹配跳过=1"), line1)
    chk("第二行是列表口径", line.startswith("列表: bili=3"), line)
    chk("第二行带成功/失败", f"下载/处理成功={okn} 失败={failn}" in line, line)
    chk("窗口行带首末与跨度", wspan.startswith("微博窗口(去置顶): 首 ") and "跨度" in wspan,
        wspan)

    print("== 已处理不重下 ==")
    done = {"wb5369216985206630"}
    _s3, plan2 = P.weibo_round(CFG, BILI_PAGE, done)
    chk("已处理的掉出队列", plan2["counts"]["queue"] == 1, plan2["counts"])
    chk("记进 done 桶", plan2["counts"]["done"] == 1, plan2["counts"])

    print("== 一条微博炸了不影响别的 ==")
    def _half(cfg, key, video):
        if video["bvid"].endswith("5369216985206630"):
            raise RuntimeError("处理炸了")
        return (True, "/tmp/r2.md", "ok")
    M.process_video = _half
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        touched2 = []
        okn2, failn2 = M.run_queue(
            CFG, "K", [M.as_video_item(i) for i in plan["queue"]], touched2, isolate=True)
    chk("异常那条计失败, 另一条照跑", (okn2, failn2) == (1, 1), (okn2, failn2))
    chk("异常有日志", "微博条目处理异常" in buf.getvalue(), buf.getvalue())
finally:
    M.process_video = _orig_proc
    M.mark_processed = _orig_mark
    P.timeline_snapshot = _orig_snap2

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
