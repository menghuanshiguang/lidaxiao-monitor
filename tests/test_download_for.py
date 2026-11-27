"""`monitor.download_for` 的形状契约 (离线, 不联网不下载)。

这一层是 2026-11-26 线上事故的现场: `download_for` 把整个 video dict 传给了
`bilibili.download_video(cfg, bvid)`, 后者第一步就 `os.path.join(WORKDIR, "downloads", bvid)`,
TypeError 立刻抛出。后果不是"这一条视频失败", 而是**队列第一支就把整轮打死**、报告全没产出,
而 568 条用例一条都没踩到 —— 因为它们把 `process_video` 整个打桩了。

所以这里的两条假函数刻意照抄生产形状: `download_video` 真的去做 `os.path.join`,
`download_weibo_video` 真的去读 item["id"]。形状传错就当场炸, 不靠断言口头约定。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import monitor as M  # noqa: E402
from lidaxiao.common import WORKDIR  # noqa: E402
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


BILI = {"bvid": "BV1eWy2zEE5U", "title": "美股反弹的原因", "pubdate": 1795666724}
WB = G.as_video_item({"id": "5369222189809983", "title": "书中内容解读",
                      "created_at": 1795604547, "duration": 151.0,
                      "video_url": "https://f.video.weibocdn.com/a.mp4"})

_orig = (M.find_downloaded_mp4, M.download_video,
         M.find_downloaded_weibo, M.download_weibo_video)
try:
    print("== B 站分支: 第二参必须是 bvid 字符串 ==")
    seen = {}

    def real_shape_download(cfg, bvid, retries=None):
        # 照抄 bilibili.py:212 —— 传 dict 在这一行就 TypeError
        outdir = os.path.join(WORKDIR, "downloads", bvid)
        seen["arg"] = bvid
        seen["dir"] = outdir
        return os.path.join(outdir, "x.mp4")

    M.find_downloaded_mp4 = lambda bvid: None
    M.download_video = real_shape_download
    got = M.download_for({"dl_retries": 0}, BILI)
    chk("不抛 TypeError", True)
    chk("传给 download_video 的是字符串", isinstance(seen.get("arg"), str), seen)
    chk("传的正是 bvid", seen.get("arg") == "BV1eWy2zEE5U", seen)
    chk("产出路径来自 bvid 目录",
        got == os.path.join(WORKDIR, "downloads", "BV1eWy2zEE5U", "x.mp4"), got)

    print("== B 站分支: 已有成品就不再下 ==")
    called = []
    M.find_downloaded_mp4 = lambda bvid: "/tmp/already.mp4"
    M.download_video = lambda cfg, bvid, retries=None: called.append(bvid) or "/tmp/new.mp4"
    chk("直接复用已有 mp4", M.download_for({}, BILI) == "/tmp/already.mp4")
    chk("没有重复下载", called == [], called)

    print("== 微博分支: 要的是整个 item, 键是 wb<id> ==")
    got_key = []

    def real_shape_weibo(cfg, item):
        got_key.append(item.get("bvid") or G.weibo_key(item))
        return os.path.join(WORKDIR, "downloads", G.weibo_key(item), "y.mp4")

    M.find_downloaded_weibo = lambda key: None
    M.download_weibo_video = real_shape_weibo
    path = M.download_for({}, WB)
    chk("微博分支拿到的是 wb<id>", got_key == ["wb5369222189809983"], got_key)
    chk("落盘在 wb<id> 目录", os.path.basename(os.path.dirname(path)) == "wb5369222189809983",
        path)
    chk("微博条目不会被当成 B 站下载",
        M.find_downloaded_mp4(BILI["bvid"]) == "/tmp/already.mp4", "B 站假函数被微博分支调到")

    print("== 两条分支的判据只看 source ==")
    hits = []
    M.find_downloaded_mp4 = lambda bvid: None
    M.download_video = lambda cfg, bvid, retries=None: hits.append(("bili", bvid)) or "/tmp/b.mp4"
    M.find_downloaded_weibo = lambda key: hits.append(("weibo-seen", key)) or None
    M.download_weibo_video = lambda cfg, item: hits.append(("weibo-dl", item.get("id"))) or "/tmp/w.mp4"
    no_source = M.download_for({}, {"bvid": "BVx", "title": "没有 source 字段"})
    chk("没有 source 字段按 B 站处理",
        no_source == "/tmp/b.mp4" and hits == [("bili", "BVx")], (no_source, hits))
finally:
    (M.find_downloaded_mp4, M.download_video,
     M.find_downloaded_weibo, M.download_weibo_video) = _orig

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
