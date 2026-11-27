"""微博取数层单测 (离线, 用探针真实响应裁剪的 fixture, 不联网)。

fixture 来源: /home/dsh/Documents/deepseek-harness/lidaxiao/reports/weibo-p1.json
(2026-11-26 浏览器抓的 ok=1 原始响应, 68890 B), 这里只留 4 张 card:
  5080410967245171  2024-09-19 20:10 置顶视频
  5337721547785683  2026-08-30 21:10 置顶视频
  5369483480797474  2026-11-26 12:41 「开播」通知(无 page_info, 必须被排除)
  5369216985206630  2026-11-25 19:02 视频

重点钉五件事:
1. 只认 page_info.type == "video" —— 「开播」条目不能算视频。
2. 直链必须按 mp4_720p → hd → ld 兜底, 取不到就当这条不可用。
3. 去重键是 mblog.id, 同页重复抓不算新(置顶条目每页都会出现)。
4. 身份只从环境变量读, 日志里不许出现 cookie 值。
5. 任何失败只降级成空列表 + 一行日志, 不抛异常。
"""
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lidaxiao.common import CN_TZ  # noqa: E402
from lidaxiao.sources import weibo as W  # noqa: E402

# 硬保证: 本文件进程内禁网。任何一次真实连接都会抛 RuntimeError ——
# 57 条用例全绿就等于「过程中一次网都没上」, 不靠人看有没有打桩。
import socket as _socket  # noqa: E402


def _no_net(*a, **k):
    raise RuntimeError("单测禁止联网")


_socket.socket = _no_net
_socket.create_connection = _no_net

OK = FAIL = 0
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "weibo")


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


print("== 常量与请求头 ==")
chk("containerid 是 107603+uid", W.containerid() == "1076031645823934", W.containerid())
chk("containerid 跟随 uid", W.containerid("123") == "107603123", W.containerid("123"))
_h = W.headers_for("1645823934", "SUB=x")
chk("Referer 是用户主页", _h["Referer"] == "https://m.weibo.cn/u/1645823934", _h["Referer"])
chk("必带 X-Requested-With / MWeibo-Pwa",
    _h.get("X-Requested-With") == "XMLHttpRequest" and _h.get("MWeibo-Pwa") == "1", _h)
chk("UA 是移动端", "iPhone" in _h["User-Agent"], _h["User-Agent"])
chk("无 cookie 时不带 Cookie 头", "Cookie" not in W.headers_for("1", ""))
chk("下载头只有 Referer", W.media_headers()["Referer"] == "https://m.weibo.cn/", W.media_headers())

print("== 解析 fixture ==")
payload = fixture("timeline_page1.json")
items = W.parse_items(payload)
chk("4 张 card 里只有 3 条视频(开播被排除)", len(items) == 3, [i["id"] for i in items])
chk("保持响应顺序",
    [i["id"] for i in items] == ["5080410967245171", "5337721547785683", "5369216985206630"],
    [i["id"] for i in items])
chk("两条置顶被识别", [i["pinned"] for i in items] == [True, True, False],
    [i["pinned"] for i in items])
_v = items[2]
chk("标题取 page_info.title", _v["title"] == "客户利益为重还是公司利益为重", _v["title"])
chk("直链是 720p", "mp4_720p" in _v["video_url"], _v["video_url"][:80])
chk("直链宿主是 weibocdn", "weibocdn.com" in _v["video_url"], _v["video_url"][:80])
chk("时长解析成秒", _v["duration"] and round(_v["duration"]) == 224, _v["duration"])
chk("source 标成 weibo", _v["source"] == "weibo", _v["source"])
chk("带原文链接", _v["weibo_url"] == "https://m.weibo.cn/status/5369216985206630", _v["weibo_url"])
chk("正文去掉了 HTML 标签", "<a" not in _v["text"] and "#" in _v["text"], _v["text"][:60])

print("== 时间解析 ==")
chk("'+0800' 时间戳换算正确",
    W.parse_created_at("Wed Nov 25 19:02:27 +0800 2026") == 1795604547,
    W.parse_created_at("Wed Nov 25 19:02:27 +0800 2026"))
chk("按 +08 还原是 2026-11-25 19:02",
    __import__("datetime").datetime.fromtimestamp(
        W.parse_created_at("Wed Nov 25 19:02:27 +0800 2026"), CN_TZ
    ).strftime("%Y-%m-%d %H:%M") == "2026-11-25 19:02")
chk("认不出的时间返回 None", W.parse_created_at("昨天") is None)
chk("空时间返回 None", W.parse_created_at("") is None)

print("== 直链档位兜底 ==")
chk("只有 hd 时取 hd",
    W.pick_video_url({"urls": {"mp4_hd_mp4": "http://h/x.mp4"}}) == "http://h/x.mp4")
chk("只有 ld 时取 ld",
    W.pick_video_url({"urls": {"mp4_ld_mp4": "http://l/x.mp4"}}) == "http://l/x.mp4")
chk("720p 优先于 hd",
    W.pick_video_url({"urls": {"mp4_hd_mp4": "http://h/x", "mp4_720p_mp4": "http://p/x"}}) == "http://p/x")
chk("urls 空时退回 media_info.stream_url",
    W.pick_video_url({"urls": {}, "media_info": {"stream_url": "http://s/x"}}) == "http://s/x")
chk("全空返回空串", W.pick_video_url({}) == "")

print("== 响应分类 ==")
r = W.classify_payload(payload)
chk("ok=1 有 cards -> ok", r["status"] == "ok", r)
chk("meta.total 取到 38820", str(r["meta"]["total"]) == "38820", r["meta"])
chk("ok=1 cards 空 -> empty",
    W.classify_payload({"ok": 1, "data": {"cards": []}})["status"] == "empty")
chk("ok=-100 -> login",
    W.classify_payload({"ok": -100, "url": "https://passport.weibo.com/sso/signin"})["status"] == "login")
chk("无 ok 字段 -> risk", W.classify_payload({"msg": "blocked"})["status"] == "risk")
chk("不是 JSON -> error", W.classify_payload("<html>432</html>")["status"] == "error")
chk("is_usable 只认 ok",
    W.is_usable(r) and not W.is_usable({"status": "login"}) and not W.is_usable(None))

print("== 去重 ==")
fresh = W.new_items(items, seen=[])
chk("首轮 3 条都算新", len(fresh) == 3, len(fresh))
chk("同页再抓一轮 0 条新", W.new_items(items, seen=[i["id"] for i in items]) == [])
chk("置顶重复不算新、非置顶照旧算",
    [i["id"] for i in W.new_items(items, seen=["5080410967245171", "5337721547785683"])]
    == ["5369216985206630"])
chk("同一页里重复 id 只留一条",
    len(W.new_items(items + items, seen=[])) == 3)
chk("没有 id 的条目被丢掉", W.new_items([{"title": "无 id"}], seen=[]) == [])

print("== 取数编排(注入 fetch, 不联网) ==")
calls = []


def fake_fetch(status=200, body=None):
    def _f(url, headers, timeout=20):
        calls.append((url, dict(headers)))
        if "api/config" in url:
            return 200, '{"ok":1,"data":{"login":false}}'
        return status, body if body is not None else json.dumps(payload, ensure_ascii=False)
    return _f


res = W.fetch_timeline({"weibo": {"uid": "1645823934"}}, fetch=fake_fetch(), cookie="SUB=abc")
chk("真正请求了两条 URL", len(calls) == 2, [c[0][:40] for c in calls])
chk("顺序是 config 先", "api/config" in calls[0][0], calls[0][0][:60])
chk("第二条是 getIndex 且带 containerid",
    "containerid=1076031645823934" in calls[1][0], calls[1][0][:90])
chk("结果 ok 且有 3 条视频", res["status"] == "ok" and len(res["items"]) == 3, res["status"])
chk("cookie 进请求头", calls[1][1].get("Cookie") == "SUB=abc", calls[1][1].get("Cookie"))
chk("没有 cookie 时不带 Cookie 头",
    W.fetch_timeline({}, fetch=fake_fetch(), cookie="") and True)

_r432 = W.fetch_timeline({}, fetch=fake_fetch(status=432, body=""), cookie="")
chk("HTTP 432 -> risk", _r432["status"] == "risk", _r432)
_r500 = W.fetch_timeline({}, fetch=lambda u, h, t=20: (500, ""), cookie="")
chk("config 500 -> risk 且不抛", _r500["status"] == "risk", _r500)

print("== 降级: 失败只返回空列表 ==")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    out = W.collect_weibo_videos({}, fetch=fake_fetch())
logtxt = buf.getvalue()
chk("成功路径返回 3 条", len(out) == 3, len(out))
chk("日志写了 cards/total/新增", "cards=4" in logtxt and "新增=3" in logtxt, logtxt[-200:])

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    boom = W.collect_weibo_videos({}, fetch=lambda u, h, t=20: (_ for _ in ()).throw(RuntimeError("网络挂了")))
chk("fetch 抛异常也不外抛", boom == [], boom)
chk("异常降级有且只有一行日志", buf.getvalue().count("\n") == 1, buf.getvalue())

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    blocked = W.collect_weibo_videos({}, fetch=fake_fetch(status=432, body=""))
chk("432 降级为空列表", blocked == [], blocked)
chk("432 日志点明没拿到", "没拿到" in buf.getvalue(), buf.getvalue())

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    off = W.collect_weibo_videos({"weibo": {"enabled": False}}, fetch=fake_fetch())
chk("config 关闭时不请求", off == [] and calls.count(None) == 0, off)

print("== 身份: 只从环境变量读, 不打印 ==")
os.environ["WEIBO_SUB"] = "SUB=secretvalue; SUBP=other"
chk("read_cookie 读到值", W.read_cookie() == "SUB=secretvalue; SUBP=other", W.read_cookie())
os.environ["WEIBO_COOKIE"] = "Cookie: SUB=fromheader"
chk("去掉 Cookie: 前缀", W.read_cookie() == "SUB=fromheader", W.read_cookie())
del os.environ["WEIBO_COOKIE"]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    W.collect_weibo_videos({}, fetch=fake_fetch())
chk("日志里没有 cookie 值", "secretvalue" not in buf.getvalue(), buf.getvalue())
chk("日志只写身份有没有", "身份=已注入" in buf.getvalue(), buf.getvalue())
del os.environ["WEIBO_SUB"]

print("== 禁网保证(默认 fetch 也不许真的出去) ==")
chk("默认 _http_fetch 在禁网环境下降级为 (None, '')",
    W._http_fetch("https://m.weibo.cn/api/config", {}) == (None, ""),
    W._http_fetch("https://m.weibo.cn/api/config", {}))
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _noinj = W.collect_weibo_videos({})
chk("不注入 fetch 时也返回空列表、不抛", _noinj == [], _noinj)
chk("不注入 fetch 时只留一行日志", _buf.getvalue().count("\n") == 1, _buf.getvalue())

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
