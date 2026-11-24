"""动态请求的登录 cookie 单测(离线): 412 的成因是缺登录 cookie, 与 IP 无关。

钉三件事:
1. 复用视频链路现成的 load_cookies_dict() 读 runner 上那份 cookie, 不另写读取
2. Cookie 头 = 游客 buvid3/4 + 登录 cookie; 日志只报项数, 绝不打印值
3. 没有 cookie 文件时必须降级为不带 cookie 照跑, 不抛异常
"""
import contextlib
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from lidaxiao.sources import dynamic as D  # noqa: E402
from lidaxiao.sources import bilibili as B  # noqa: E402

OK = FAIL = 0


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


print("== 登录 cookie ==")
_COOKIE_SECRET = "SESSDATA_SECRET_VALUE_9f3a7c"
_ck_wd = tempfile.mkdtemp()
os.makedirs(os.path.join(_ck_wd, "data"))
with open(os.path.join(_ck_wd, "data", "cookies.txt"), "w", encoding="utf-8") as fh:
    fh.write("# Netscape HTTP Cookie File\n")
    fh.write(".bilibili.com\tTRUE\t/\tTRUE\t1893456000\tSESSDATA\t"
             + _COOKIE_SECRET + "\n")
    fh.write(".bilibili.com\tTRUE\t/\tTRUE\t1893456000\tbili_jct\tJCT123\n")
_wd_saved = B.WORKDIR
B.WORKDIR = _ck_wd
try:
    _ck = D._login_cookies()
    chk("假 cookie 文件能读出来", set(_ck) == {"SESSDATA", "bili_jct"}, list(_ck))
    _hdr = D.cookie_header("buvid3=B3; buvid4=B4", _ck)
    chk("Cookie 头 = 游客 buvid + 登录 cookie",
        _hdr.startswith("buvid3=B3; buvid4=B4; ") and _COOKIE_SECRET in _hdr,
        _hdr[:40] + "...")
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        D.log(f"🔑 动态请求带登录 cookie ({len(_ck)} 项)")
    chk("日志只报项数, 不含 cookie 值",
        _COOKIE_SECRET not in _buf.getvalue() and "2 项" in _buf.getvalue(),
        _buf.getvalue().strip())
finally:
    B.WORKDIR = _wd_saved

# 没有 cookie 文件 -> 不炸, 降级为不带
_empty_ck = tempfile.mkdtemp()
B.WORKDIR = _empty_ck
try:
    chk("没有 cookie 文件 -> 返回空 dict 不抛", D._login_cookies() == {})
    chk("空 dict 时 Cookie 头只剩 buvid",
        D.cookie_header("buvid3=B3; buvid4=B4", {}) == "buvid3=B3; buvid4=B4")
    chk("None 也不炸", D.cookie_header("buvid3=B3", None) == "buvid3=B3")
finally:
    B.WORKDIR = _wd_saved

# 请求侧接线: fetch_dynamics 拼头时确实带上了登录 cookie, 且日志不含值
_headers_seen = []
_orig_http = D._http_get
_orig_gap = D._respect_gap


def _fake_http(url, headers=None, timeout=15):
    """按端点伪造: buvid / wbi / feed 各回各的, 好让请求真的走到拼头那一步。"""
    if url == D.SPI_URL:
        return 200, '{"code": 0, "data": {"b_3": "B3", "b_4": "B4"}}'
    if url == D.NAV_URL:
        return 200, ('{"code": 0, "data": {"wbi_img": {"img_url": '
                     '"https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png", '
                     '"sub_url": "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png"}}}')
    _headers_seen.append(headers or {})
    return 200, '{"code": 0, "data": {"items": [], "has_more": false, "offset": ""}}'


B.WORKDIR = _ck_wd
D._respect_gap = lambda gap: None
D._http_get = _fake_http
try:
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        D.fetch_dynamics({"dynamic": {"enabled": True, "uid": "1", "min_gap": 0}}, limit=1)
    _ckhdr = [h.get("Cookie", "") for h in _headers_seen]
    chk("请求头里带上了登录 cookie",
        any(_COOKIE_SECRET in c for c in _ckhdr), [c[:30] for c in _ckhdr])
    chk("请求日志里不含 cookie 值", _COOKIE_SECRET not in _buf.getvalue(),
        _buf.getvalue()[-120:])
finally:
    D._http_get = _orig_http
    D._respect_gap = _orig_gap
    B.WORKDIR = _wd_saved

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
