"""微博视频下载 (离线): 头、落盘、半截文件处置。

CDN 只认 Referer, 去掉就是 403 —— 这条不是可选项, 所以单测直接断言请求头里带着它;
半截 mp4 比明确失败更难发现 (容器头仍报完整时长), 所以这里也断言不完整时不留成品。
"""
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import lidaxiao.sources.weibo_video as V  # noqa: E402

OK = FAIL = 0


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


class FakeResp:
    def __init__(self, body, length=None):
        if length is None:
            length = len(body)
        self.headers = {"Content-Length": str(length)}
        self._buf = io.BytesIO(body)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(body, length=None, seen=None):
    def _open(req, timeout=None):
        if seen is not None:
            seen.append(req)
        return FakeResp(body, length)
    return _open


ITEM = {"source": "weibo", "id": "5369222189809983", "bvid": "wb5369222189809983",
        "title": "书中内容解读", "video_url": "https://f.video.weibocdn.com/a.mp4"}

_tmp = tempfile.mkdtemp()
_orig_workdir = V.WORKDIR
_orig_open = V.urllib.request.urlopen
V.WORKDIR = _tmp
try:
    print("== 头: 必须带 Referer ==")
    seen = []
    V.urllib.request.urlopen = fake_urlopen(b"x" * 4096, seen=seen)
    path = V.download_weibo_video({"dl_retries": 0}, ITEM)
    chk("下载成功返回 mp4 路径", path and path.endswith(".mp4"), path)
    chk("文件存在且字节数对得上",
        os.path.exists(path) and os.path.getsize(path) == 4096, path)
    chk("请求头带 Referer=https://m.weibo.cn/",
        seen and seen[0].get_header("Referer") == V.media_headers()["Referer"], seen[:1])
    chk("Referer 值就是 m.weibo.cn 根路径",
        V.media_headers()["Referer"] == "https://m.weibo.cn/", V.media_headers())
    chk("落盘目录是 downloads/wb<id>",
        os.path.dirname(path) == os.path.join(_tmp, "downloads", "wb5369222189809983"),
        path)
    chk("不留 .part 半截文件",
        not [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".part")],
        os.listdir(os.path.dirname(path)))

    print("== 已有成品不重下 ==")
    seen2 = []
    V.urllib.request.urlopen = fake_urlopen(b"y" * 10, seen=seen2)
    again = V.download_weibo_video({"dl_retries": 0}, ITEM)
    chk("第二次直接复用", again == path and seen2 == [], (again, len(seen2)))
    chk("复用后字节没变", os.path.getsize(path) == 4096, os.path.getsize(path))

    print("== 半截文件 ==")
    item2 = dict(ITEM, id="1111111111111111", bvid="wb1111111111111111")
    V.urllib.request.urlopen = fake_urlopen(b"z" * 100, length=999)   # 声明 999 只给 100
    got = V.download_weibo_video({"dl_retries": 0}, item2)
    chk("字节数不足判失败", got is None, got)
    d2 = os.path.join(_tmp, "downloads", "wb1111111111111111")
    left = os.listdir(d2) if os.path.isdir(d2) else []
    chk("失败后不留 .part / .mp4", left == [], left)
    chk("找不到成品时返回 None", V.find_downloaded_weibo("wb1111111111111111") is None)

    print("== 直链缺失 / 没有 id ==")
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bad = V.download_weibo_video({"dl_retries": 0}, {"id": "2222", "video_url": ""})
    chk("直链缺失不下", bad is None and "直链缺失" in buf.getvalue(), buf.getvalue())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        noid = V.download_weibo_video({"dl_retries": 0}, {"video_url": "https://x/a.mp4"})
    chk("没有 id 直接拒绝", noid is None and "没有 id" in buf.getvalue(), buf.getvalue())
finally:
    V.WORKDIR = _orig_workdir
    V.urllib.request.urlopen = _orig_open

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
