"""数据源: 李大霄微博视频号时间线 (m.weibo.cn) —— 阶段 1 探针结论已写进本文件

探针实测 (2026-11-26, uid 1645823934, 浏览器 network log + Azure runner):

1. 真实端点两条, 顺序写死:
   GET https://m.weibo.cn/api/config                 (先打这个: 下发 _T_WM / XSRF-TOKEN)
   GET https://m.weibo.cn/api/container/getIndex?type=uid&value=<uid>&containerid=107603<uid>
   必须头: 移动 UA + Referer https://m.weibo.cn/u/<uid> +
           X-Requested-With: XMLHttpRequest + MWeibo-Pwa: 1
2. 身份: `.weibo.cn` 域上的**访客** SUB/SUBP (登录态 MLOGIN=0 也算访客)。
   只从环境变量读, 不打印、不落盘。
   实测分层: 无 cookie -> HTTP 432 空 body;
             自铸访客身份 (passport genvisitor2/incarnate) -> 200 但 ok=-100;
             浏览器铸的访客 SUB -> 200 ok=1 (Azure runner 上已复现)。
3. 免登录只给**第一页 12 条**(2 条置顶 + 10 条最新), 覆盖约 17.6 小时;
   page=2 / since_id 全部 302 到 passport 登录页。cron 每 3 小时一轮足够, 不翻页。
4. 只有 page_info.type == "video" 的条目算视频; 直链在同一响应里:
   page_info.urls.mp4_720p_mp4 (缺则 mp4_hd_mp4 -> mp4_ld_mp4)
   page_info.media_info.stream_url 亦可用。链接约 3600 秒过期, 必须当次下载。
5. 下载直链必须带 Referer https://m.weibo.cn/ (去掉 -> 403, 146 B); HEAD 也 403。
6. 去重键是 mblog.id。置顶条目每页都出现, 按 id 判重即不会当成新条目。

本模块只做「解析 / 去重 / 取数编排」, 不接主流程, 不读文件、不直连:
网络动作全部经注入的 fetch 完成, 所以纯函数部分可离线单测。
"""
import datetime
import json
import os
import re
import urllib.error
import urllib.request

from lidaxiao.textutil import log

DEFAULT_UID = "1645823934"

#: 移动 Safari UA。与 Referer/指纹参数必须是同一套, 自相矛盾的组合会被判风险。
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")

CONFIG_URL = "https://m.weibo.cn/api/config"
TIMELINE_URL = "https://m.weibo.cn/api/container/getIndex"

#: 下视频用的 Referer。缺它 CDN 直接 403。
MEDIA_REFERER = "https://m.weibo.cn/"

#: cookie 只从这里读; 值绝不写日志。
COOKIE_ENV_NAMES = ("WEIBO_COOKIE", "WEIBO_SUB", "LIDAXIAO_WEIBO_COOKIE")

#: 直链档位, 从高到低。
URL_PREFERENCE = ("mp4_720p_mp4", "mp4_hd_mp4", "mp4_ld_mp4")

HTTP_TIMEOUT = 20
TAG_RE = re.compile(r"<[^>]+>")


def containerid(uid=DEFAULT_UID):
    """时间线容器 id: 107603 + uid (探针从页面请求原样取到)。"""
    return "107603" + str(uid)


def page_referer(uid=DEFAULT_UID):
    return f"https://m.weibo.cn/u/{uid}"


def read_cookie(env=None):
    """从环境变量取 cookie 头。空串表示没有身份, 调用方照常请求但预期 432/-100。"""
    env = os.environ if env is None else env
    for name in COOKIE_ENV_NAMES:
        value = (env.get(name) or "").strip()
        if value:
            # Secret 里可能带 "Cookie:" 前缀, 去掉, 但要保留 "SUB=x; SUBP=y" 本身
            return re.sub(r"^(?i:cookie)\s*:\s*", "", value)
    return ""


def headers_for(uid=DEFAULT_UID, cookie=""):
    """时间线请求头。cookie 为空则不带 Cookie 头。"""
    h = {
        "User-Agent": MOBILE_UA,
        "Referer": page_referer(uid),
        "X-Requested-With": "XMLHttpRequest",
        "MWeibo-Pwa": "1",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def media_headers():
    """取视频直链时的头。只带 Referer —— CDN 只认它, cookie 无用。"""
    return {"User-Agent": MOBILE_UA, "Referer": MEDIA_REFERER}


def _dict(value):
    return value if isinstance(value, dict) else {}


def parse_created_at(text):
    """'Thu Nov 25 19:02:27 +0800 2026' -> epoch 秒; 认不出返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = datetime.datetime.strptime(text.strip(), "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None
    return int(dt.timestamp())


def pick_video_url(page_info):
    """从 page_info 取最高可用直链; 一个都没有返回空串。"""
    urls = _dict(_dict(page_info).get("urls"))
    for key in URL_PREFERENCE:
        value = urls.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    stream = _dict(_dict(page_info).get("media_info")).get("stream_url")
    return stream if isinstance(stream, str) and stream.startswith("http") else ""


def is_pinned(card):
    """置顶条目的两种标记: 卡片的 profile_type_id, 或 mblog.title.text == '置顶'。"""
    card = _dict(card)
    if str(card.get("profile_type_id") or "").startswith("proweibotop_"):
        return True
    title = _dict(_dict(card.get("mblog")).get("title")).get("text")
    return str(title or "") == "置顶"


def plain_text(html):
    """微博正文是 HTML 片段; 报告里只留文字。"""
    return TAG_RE.sub("", str(html or "")).replace("\u200b", "").strip()


def parse_card(card):
    """单张 card -> 视频条目 dict; 不是视频返回 None。

    判据只用 page_info.type == "video" —— 「开播」通知等条目没有 page_info。
    """
    card = _dict(card)
    mblog = _dict(card.get("mblog"))
    if not mblog or not mblog.get("id"):
        return None
    page_info = _dict(mblog.get("page_info"))
    if str(page_info.get("type") or "") != "video":
        return None
    url = pick_video_url(page_info)
    if not url:                       # 有 page_info 却取不到直链: 当作不可用, 宁漏不错
        return None
    duration = _dict(page_info.get("media_info")).get("duration")
    return {
        "source": "weibo",
        "id": str(mblog["id"]),
        "mid": str(mblog.get("mid") or mblog["id"]),
        "created_at": parse_created_at(mblog.get("created_at")),
        "created_text": str(mblog.get("created_at") or ""),
        "title": str(page_info.get("title") or "").strip(),
        "text": plain_text(mblog.get("text")),
        "video_url": url,
        "duration": float(duration) if isinstance(duration, (int, float)) else None,
        "play_count": str(page_info.get("play_count") or ""),
        "pinned": is_pinned(card),
        "weibo_url": f"https://m.weibo.cn/status/{mblog['id']}",
    }


def parse_items(payload):
    """响应 -> 视频条目列表(保持响应顺序; 置顶条目也在其中, 由 id 去重)。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return []
    cards = _dict(_dict(payload).get("data")).get("cards")
    if not isinstance(cards, list):
        return []
    items = []
    for card in cards:
        item = parse_card(card)
        if item:
            items.append(item)
    return items


def timeline_meta(payload):
    """cardlistInfo 摘要, 用于日志/核对(不含任何身份信息)。"""
    info = _dict(_dict(_dict(payload).get("data")).get("cardlistInfo"))
    cards = _dict(_dict(payload).get("data")).get("cards")
    return {"containerid": str(info.get("containerid") or ""),
            "total": info.get("total"),
            "cards": len(cards) if isinstance(cards, list) else 0,
            "page": info.get("page")}


def classify_payload(payload):
    """响应归类。status 五种:

    ok       ok=1 且有 cards            —— 可用
    empty    ok=1 但 cards 为空         —— 本次没拿到新页, 按「没拿到」处理
    login    ok=-100                    —— 身份不被认(未登录/访客身份无效)
    risk     HTTP 432 / 无 ok 字段      —— 风控层拒绝
    error    不是 JSON                  —— 未分类失败
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {"status": "error", "items": [], "ok": None, "login": None,
                    "message": "响应不是 JSON", "meta": {}}
    if not isinstance(payload, dict):
        return {"status": "error", "items": [], "ok": None, "login": None,
                "message": "响应不是 JSON", "meta": {}}
    ok = payload.get("ok")
    data = _dict(payload.get("data"))
    cards = data.get("cards")
    meta = timeline_meta(payload)
    if ok == -100 or str(ok) == "-100":
        return {"status": "login", "items": [], "ok": ok, "login": payload.get("login"),
                "message": "ok=-100 (身份不被认, 需访客/登录 cookie)", "meta": meta}
    if ok is None:
        return {"status": "risk", "items": [], "ok": ok, "login": payload.get("login"),
                "message": str(payload.get("msg") or "无 ok 字段"), "meta": meta}
    if not isinstance(cards, list) or not cards:
        return {"status": "empty", "items": [], "ok": ok, "login": payload.get("login"),
                "message": "cards 为空", "meta": meta}
    items = parse_items(payload)
    return {"status": "ok", "items": items, "ok": ok, "login": payload.get("login"),
            "message": "", "meta": meta}


def is_usable(result):
    """只有 ok 才能推进 state / 渲染章节。"""
    return bool(result) and result.get("status") == "ok"


def new_items(items, seen=()):
    """按 mblog.id 去重, 保持输入顺序。置顶条目重复出现不算新。"""
    seen = {str(x) for x in (seen or ())}
    fresh, out = set(), []
    for item in items or []:
        key = str((item or {}).get("id") or "")
        if not key or key in seen or key in fresh:
            continue
        fresh.add(key)
        out.append(item)
    return out


# ------------------------------------------------------------------ 取数


def _http_fetch(url, headers, timeout=HTTP_TIMEOUT):
    """默认 fetch: 返回 (http_status, text)。不抛异常, 失败给 (None, "")。

    注入点在这里 —— 单测用假 fetch 顶掉, 模块本身不直连。
    """
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return exc.code, body
    except Exception:
        return None, ""


def fetch_timeline(cfg=None, uid=None, fetch=None, cookie=None):
    """先 /api/config 再 getIndex。返回 {status, items, ok, login, message, meta, http}。

    身份只从环境变量读; 日志只写「有没有身份」, 不写值。
    """
    cfg = cfg or {}
    wcfg = cfg.get("weibo") or {}
    uid = str(uid or wcfg.get("uid") or DEFAULT_UID)
    fetch = fetch or _http_fetch
    cookie = read_cookie() if cookie is None else cookie
    headers = headers_for(uid, cookie)

    # 顺序写死: config 先跑, 它下发 _T_WM / XSRF-TOKEN —— 少了它 getIndex 会被判风控。
    cstatus, _cbody = fetch(CONFIG_URL, headers, HTTP_TIMEOUT)
    if cstatus != 200:
        return {"status": "risk", "items": [], "ok": None, "login": None,
                "message": f"/api/config HTTP {cstatus}", "meta": {}, "http": cstatus}

    url = (f"{TIMELINE_URL}?type=uid&value={uid}&containerid={containerid(uid)}")
    status, body = fetch(url, headers, HTTP_TIMEOUT)
    if status == 432:
        return {"status": "risk", "items": [], "ok": None, "login": None,
                "message": "HTTP 432 风控拒绝(身份无效)", "meta": {}, "http": status}
    result = classify_payload(body)
    result["http"] = status
    return result


def collect_weibo_videos(cfg=None, uid=None, fetch=None, seen=()):
    """取一页并把已见项过滤掉。**只读不写**: 不落盘、不推进 state。

    任何失败都降级成空列表 + 一行日志, 绝不向上抛 —— 微博失败不许影响视频链路。
    """
    cfg = cfg or {}
    try:
        wcfg = cfg.get("weibo") or {}
        if not wcfg.get("enabled", True):
            log("微博: 已禁用 (config weibo.enabled=false), 跳过")
            return []
        has_cookie = bool(read_cookie())
        result = fetch_timeline(cfg, uid=uid, fetch=fetch)
        if not is_usable(result):
            # 失败只留一行, 且点明身份是否注入 —— 432 与 -100 的处置不同。
            log(f"⚠️ 微博本次没拿到 ({result.get('status')}: {result.get('message')}, "
                f"身份={'已注入' if has_cookie else '无'}), 不写 state、不出章节")
            return []
        meta = result.get("meta") or {}
        fresh = new_items(result["items"], seen)
        log(f"微博: 身份={'已注入' if has_cookie else '无'} cards={meta.get('cards')} "
            f"total={meta.get('total')} 视频={len(result['items'])} 新增={len(fresh)}")
        return fresh
    except Exception as exc:                       # 兜底: 绝不影响视频链路
        log(f"⚠️ 微博处理异常 (已忽略): {type(exc).__name__}: {exc}")
        return []
