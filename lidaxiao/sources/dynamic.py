"""B站动态(图文/文字)抓取与解析 —— 阶段 1 探针结论已写进本文件

探针实测 (2026-11-24, UID 2137589551, /tmp/dynprobe/RESULTS.md):

1. 唯一可用端点: /x/polymer/web-dynamic/v1/feed/space
   老的 /x/dynamic/feed/dyn1 与 api.vc.bilibili.com space_history 都是 HTTP 404
   (站点 HTML, 不是接口错误), 不得作为兜底。
2. 必需头组合: UA + Referer + buvid3/buvid4 cookie + wbi 签名(w_rid/wts)。
   裸请求 -> HTTP 412(HTML 风控页); 有 buvid+Referer 但无 wbi -> 间歇 code -352;
   有 wbi 但无 buvid cookie -> HTTP 412。
3. 去重键是 items[].id_str。该端点**没有** dynamic_id 字段
   (items 顶层键: basic / id_str / modules / orig / type / visible)。
4. HTTP 200 + code 0 + items 0 是**限流软空**, 不是「没有动态」:
   实测间隔 2s/5s 全空, 15s/30s 恢复为 13 条, 20s 两次都 13 条。
   若把空页当成「今天没发动态」写进 state, 会把没抓到的动态永久标记为已处理,
   造成静默漏采。所以本模块把空页归为 status="empty", 调用方不得据此推进 state。
   total 字段恒为字符串 "0", 不能当完整性校验。
5. 因此: 两次请求间隔 >= 15s, 风控/限流重试上限 2 次, 禁止循环重试。
6. **正文形状由 features=itemOpusStyle 决定, 必须带这个参数**:
   同一 id 1273504300584140805, 不带参数 -> major.type=MAJOR_TYPE_DRAW, desc=null,
   正文一个字都没有; 带上参数 -> major.type=MAJOR_TYPE_OPUS,
   major.opus.summary.text 814 字, 图在 major.opus.pics[].url。
   所以按 major.type 分派解析: ARCHIVE(视频投稿) / OPUS(正文+图) / DRAW(纯图)。
   另注: detail 接口对同一 id 返回的形状和 feed 不一致(看不到那 814 字),
   复核必须以 feed/space 的原始响应为准。

本模块只做抓取/解析/去重三件事, 不接主流程; 没有网络调用时全部纯函数可离线测。
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from lidaxiao.common import STATE_DIR
from lidaxiao.textutil import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULT_UID = 2137589551
FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

FEED_FEATURES = "itemOpusStyle"   # 不带它正文在 opus 里看不到, 实测见文件头第 6 条
MIN_GAP_SECONDS = 15      # 实测 2s/5s 会回软空页, 15s 起恢复
MAX_RETRIES = 2           # 风控/限流重试上限
HTTP_TIMEOUT = 20
SEEN_FILE = "dynamics_seen.json"

# wbi 混淆表 (官方前端固定常量)
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

_last_request_at = 0.0


# ------------------------------------------------------------------ 网络


def _respect_gap(gap=None):
    """两次请求之间至少隔 gap 秒 (实测下限 15s, 服务端限流软空窗口)"""
    global _last_request_at
    gap = MIN_GAP_SECONDS if gap is None else gap
    if _last_request_at:
        wait = gap - (time.time() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
    _last_request_at = time.time()


def _http_get(url, headers, timeout=HTTP_TIMEOUT):
    """返回 (http_status, text)。网络异常折成 (-1, 描述), 不抛。"""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return exc.code, body
    except Exception as exc:                        # 超时 / DNS / 断网
        return -1, f"{type(exc).__name__}: {exc}"


def _json_or_none(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def _buvid_cookies():
    """取游客 buvid3/buvid4 (无 cookie 时必拿, 否则 412)"""
    status, text = _http_get(SPI_URL, {"User-Agent": UA})
    data = (_json_or_none(text) or {}).get("data") or {}
    b3, b4 = data.get("b_3"), data.get("b_4")
    if not b3:
        raise RuntimeError(f"取 buvid 失败 (HTTP {status})")
    return f"buvid3={b3}; buvid4={b4}"


def _login_cookies():
    """登录 cookie: 复用视频链路现成的 load_cookies_dict(), 不另写一套读取。

    412 的成因是**缺登录 cookie**, 与 IP 无关: 线上同组 UA/Referer/buvid/wbi 只加
    这一项, 412 立刻变 200/code=0。没有 cookie 文件时返回 {}, 请求照发(大概率 412),
    不抛异常 —— 本地没有那份文件也必须能跑。
    cookie 值一律不进日志、不进报告、不落盘第二份。
    """
    try:
        from lidaxiao.sources.bilibili import load_cookies_dict
        return load_cookies_dict() or {}
    except Exception as exc:      # 缺模块/读失败都降级, 不影响请求本身
        log(f"⚠️ 读取登录 cookie 失败(降级为不带): {type(exc).__name__}")
        return {}


def cookie_header(guest_cookie, login_cookies=None):
    """拼接 Cookie 头: 游客 buvid3/4 + 登录 cookie。纯函数, 便于离线测。"""
    parts = [guest_cookie or ""]
    for k, v in (login_cookies or {}).items():
        if k:
            parts.append(f"{k}={v}")
    return "; ".join(p for p in parts if p)


def _wbi_mixin_key():
    """nav -> wbi_img.img_url/sub_url -> 混淆出 mixin_key (未登录也能取到)"""
    status, text = _http_get(NAV_URL, {"User-Agent": UA,
                                       "Referer": "https://www.bilibili.com/"})
    wbi = (((_json_or_none(text) or {}).get("data") or {}).get("wbi_img")) or {}
    img, sub = wbi.get("img_url"), wbi.get("sub_url")
    if not img or not sub:
        raise RuntimeError(f"取 wbi_img 失败 (HTTP {status})")
    orig = (img.rsplit("/", 1)[-1].split(".")[0]
            + sub.rsplit("/", 1)[-1].split(".")[0])
    return "".join(orig[i] for i in _MIXIN_TAB if i < len(orig))[:32]


def sign_params(params, mixin_key, wts=None):
    """wbi 签名: 参数按 key 排序 -> 规范化 query -> md5(query + mixin_key)"""
    p = dict(params, wts=int(time.time()) if wts is None else wts)
    query = urllib.parse.urlencode(
        sorted(p.items()),
        quote_via=lambda s, *a, **k: urllib.parse.quote(s, safe=""))
    return query + "&w_rid=" + hashlib.md5((query + mixin_key).encode()).hexdigest()


# ------------------------------------------------------------------ 归类


def build_params(uid, limit=None):
    """feed/space 的查询参数(未签名)。features 是必需的, 否则正文丢失。"""
    params = {"host_mid": str(uid), "features": FEED_FEATURES}
    if limit:
        params["limit"] = str(limit)
    return params


def classify_payload(payload):
    """把一次 feed/space 响应归类。

    status 四种:
      ok    code=0 且 items 非空            —— 可用
      empty code=0 但 items 为空/缺失       —— **限流软空, 判「本次没拿到」**
      risk  code 为负 (如 -352) 或 HTTP 层 412 —— 风控
      error JSON 解析失败 / 其它异常        —— 未分类失败

    empty 与 ok 必须分开: 空页不能当「没有新动态」, 否则会静默漏采。
    """
    if isinstance(payload, str):
        payload = _json_or_none(payload)
    if not isinstance(payload, dict):
        return {"status": "error", "items": [], "code": None,
                "message": "响应不是 JSON"}
    code = payload.get("code")
    data = payload.get("data") or {}
    items = data.get("items")
    if code not in (0, "0"):
        return {"status": "risk", "items": [], "code": code,
                "message": str(payload.get("message"))}
    if not isinstance(items, list) or not items:
        return {"status": "empty", "items": [], "code": code,
                "message": str(payload.get("message"))}
    return {"status": "ok", "items": items, "code": code,
            "message": str(payload.get("message"))}


def is_usable(result):
    """只有 ok 才能推进 state / 渲染章节; empty/risk/error 一律「本次没拿到」。"""
    return bool(result) and result.get("status") == "ok"


def fetch_dynamics(cfg=None, uid=None, limit=None):
    """抓一页动态。返回 {status, items, code, message, attempts}。

    网络动作全在这里: buvid cookie -> wbi 密钥 -> 签名请求。
    间隔 >= MIN_GAP_SECONDS, 风控/限流最多重试 MAX_RETRIES 次;
    任一环节失败都返回 status != "ok", 不抛异常 (调用方按降级处理)。
    """
    cfg = cfg or {}
    dcfg = cfg.get("dynamic") or {}
    uid = uid or dcfg.get("uid") or DEFAULT_UID
    gap = dcfg.get("min_gap", MIN_GAP_SECONDS)

    last = {"status": "error", "items": [], "code": None,
            "message": "未发起请求", "attempts": 0}
    try:
        _respect_gap(gap)
        guest_cookie = _buvid_cookies()
        _respect_gap(gap)
        mixin = _wbi_mixin_key()
    except Exception as exc:
        last["message"] = f"准备请求失败: {exc}"
        return last

    params = build_params(uid, limit)
    login = _login_cookies()
    headers = {"User-Agent": UA,
               "Referer": f"https://space.bilibili.com/{uid}/dynamic",
               "Cookie": cookie_header(guest_cookie, login)}
    if login:
        log(f"🔑 动态请求带登录 cookie ({len(login)} 项)")

    for attempt in range(1, MAX_RETRIES + 2):
        _respect_gap(gap)
        status, text = _http_get(f"{FEED_URL}?{sign_params(params, mixin)}", headers)
        last["attempts"] = attempt
        if status == 412:
            last.update(status="risk", message="HTTP 412 风控页", code=None)
        else:
            result = classify_payload(text)
            result["http"] = status
            last = dict(result, attempts=attempt)
        if last["status"] == "ok":
            return last
        log(f"⚠️ 动态抓取第 {attempt} 次未拿到: {last['status']} {last['message']}")
        if attempt <= MAX_RETRIES:
            _respect_gap(gap)
    return last


# ------------------------------------------------------------------ 解析


def _dict(value):
    return value if isinstance(value, dict) else {}


def parse_dynamic(item):
    """单条动态 -> 扁平 dict; 拿不到 id_str 返回 None。

    正文按 major.type 分派 (实测三种形状):
      MAJOR_TYPE_ARCHIVE -> 视频投稿, 文字是 major.archive.title (desc 为 null)
      MAJOR_TYPE_OPUS    -> 正文在 major.opus.summary.text, 图在 major.opus.pics[]
      MAJOR_TYPE_DRAW    -> 纯图, 图在 major.draw.items[]
    desc 在 feed 里恒为 null, 这里顺势兜住不抛。
    """
    if not isinstance(item, dict):
        return None
    id_str = item.get("id_str")
    if not id_str:
        return None
    modules = _dict(item.get("modules"))
    author = _dict(modules.get("module_author"))
    md = _dict(modules.get("module_dynamic"))
    major = _dict(md.get("major"))
    archive = _dict(major.get("archive"))
    opus = _dict(major.get("opus"))
    draw = _dict(major.get("draw"))

    opus_text = (_dict(opus.get("summary")).get("text") or "").strip()
    images = [i.get("src") for i in (draw.get("items") or [])
              if isinstance(i, dict) and i.get("src")]
    images += [i.get("url") for i in (opus.get("pics") or [])
               if isinstance(i, dict) and i.get("url")]

    # 实测该端点的 pub_ts 是字符串("1788095424"), 统一转 int; 转不动就给 None
    try:
        pub_ts = int(author.get("pub_ts"))
    except (TypeError, ValueError):
        pub_ts = None
    jump = opus.get("jump_url") or archive.get("jump_url") or ""
    return {
        "id_str": str(id_str),
        "type": item.get("type") or "",
        "major_type": major.get("type") or "",
        "pub_ts": pub_ts,
        "pub_time": author.get("pub_time") or "",
        "author": author.get("name") or "",
        "text": ((_dict(md.get("desc")).get("text") or "").strip() or opus_text),
        "title": (archive.get("title") or opus.get("title") or "").strip(),
        "bvid": (archive.get("bvid") or "").strip(),
        "images": images,
        "url": f"https:{jump}" if jump.startswith("//") else
               (jump or f"https://t.bilibili.com/{id_str}"),
    }


def parse_items(items):
    """批量解析, 丢掉解析不出来的"""
    out = []
    for item in items or []:
        parsed = parse_dynamic(item)
        if parsed:
            out.append(parsed)
    return out


# ------------------------------------------------------------------ 幂等


def seen_path(cfg=None):
    base = ((cfg or {}).get("state_dir") or STATE_DIR)
    return os.path.join(base, SEEN_FILE)


def load_seen(cfg=None):
    """已见 id 列表。兼容两种落盘格式: 纯数组(旧) 与 {"last_dynamic_id","ids"}(新)。"""
    path = seen_path(cfg)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except Exception as exc:
        log(f"⚠️ 读已见动态失败({exc}), 按空处理")
        return []
    if isinstance(data, dict):
        data = data.get("ids") or []
    return [str(x) for x in data] if isinstance(data, list) else []


def load_last_id(cfg=None):
    """上一条已处理的 id_str; 没有就返回空串。幂等键之外只作状态记录。"""
    path = seen_path(cfg)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return ""
    return str(data.get("last_dynamic_id") or "") if isinstance(data, dict) else ""


def save_seen(seen, cfg=None, last_id=None):
    """落盘 {"last_dynamic_id": ..., "ids": [...]}; ids 排序后写, 便于 diff。"""
    path = seen_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"last_dynamic_id": str(last_id or ""),
               "ids": sorted({str(x) for x in (seen or [])})}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


def new_dynamics(cfg, items, seen=None):
    """按 id_str 去重, 返回没见过的动态 (已解析过的不重复解析)。

    seen 为 None 时从 state 读; 传入集合时只用传入的 (离线测试用)。
    同一批里重复出现同一个 id 也只留第一条。
    """
    known = set(seen) if seen is not None else set(load_seen(cfg))
    out = []
    for item in items or []:
        parsed = parse_dynamic(item) if isinstance(item, dict) and "modules" in item else item
        if not isinstance(parsed, dict):
            continue
        key = parsed.get("id_str")
        if not key or key in known:
            continue
        known.add(key)
        out.append(parsed)
    return out


def collect_dynamics(cfg=None):
    """抓一页并过滤掉已见项。**只读不写**: 这里绝不推进 state。

    阶段 4 的硬顺序是「渲染 → 报告写成功 → 才推进 state」, 所以本函数只负责
    "拿到什么", 落盘由调用方在报告写成功之后调 commit_dynamics 完成。
    返回 {status, new}: status 非 ok 时 new 恒为空。
    """
    cfg = cfg or {}
    try:
        dcfg = cfg.get("dynamic") or {}
        if not dcfg.get("enabled", True):
            log("动态: 已禁用 (config dynamic.enabled=false), 跳过")
            return {"status": "disabled", "new": []}

        result = fetch_dynamics(cfg, limit=dcfg.get("limit"))
        if not is_usable(result):
            log(f"⚠️ 动态本次没拿到 ({result.get('status')}: "
                f"{result.get('message')}), 不写 state、不出章节")
            return {"status": result.get("status"), "new": []}

        seen = load_seen(cfg)
        fresh = new_dynamics(cfg, result["items"], seen=set(seen))
        if not fresh:
            log("动态: 无新增")
        else:
            log(f"动态: 待处理 {len(fresh)} 条")
        return {"status": "ok", "new": fresh}
    except Exception as exc:                       # 兜底: 绝不影响视频链路
        log(f"⚠️ 动态处理异常 (已忽略): {type(exc).__name__}: {exc}")
        return {"status": "error", "new": []}


def commit_dynamics(cfg, dynamics):
    """**报告追加成功之后**才调用: 把 id 与 last_dynamic_id 落进 state。

    调用方保证顺序; 本函数不判断报告状态, 也不抓取。
    返回写入的 last_dynamic_id (没有新增时返回原值)。
    """
    items = [d for d in (dynamics or []) if isinstance(d, dict) and d.get("id_str")]
    if not items:
        return load_last_id(cfg)
    seen = load_seen(cfg)
    last_id = items[0]["id_str"]
    save_seen(seen + [d["id_str"] for d in items], cfg, last_id=last_id)
    log(f"动态: state 已推进 {len(items)} 条, last_dynamic_id={last_id}")
    return last_id
