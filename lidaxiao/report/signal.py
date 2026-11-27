"""每日操作建议 (docs/signal/): 归纳他当天说了什么, 所以当前该怎么对待仓位。

硬约束 (督查 2026-11-27 定死):
- **只许归纳, 不许另起市场判断**: 报告里没有的内容不许出现。每条 citations 必须能按
  `video` 键在当天报告里定位到真实存在的那一节, 且 `text` 是那一节正文的原话片段
  (去掉空白后做子串比对); 定位不到就整条丢掉并记一行日志。宁可少一条, 不许编。
- 引用核验 (验收员实测口径): `text` 与章节正文都先归一化(去空白/标点/符号, 留中文与
  数字)再做子串比对, 且 **原文至少 10 个字**; 命中不到或太短, 整条丢弃。不设长度门槛
  这条就成摆设 —— `会师`、`3500点` 这种碎词在真报告里也能命中。
- 三个状态: `ok` / `degraded`(LLM 挂了, 只放原话引用、不写归纳; 降级节正文本来就是空的,
  所以 citations/key_points **允许全空**, 空就是空) / `no_content`(当天没有任何内容,
  原样延续上一份 stance/action 并沿用上一份 `as_of`, 不许编)。
- `levels` 的每个点位、`assets` 的每个标的, 必须能在某条引用的正文里找到; 找不到就丢掉
  (宁缺毋滥)。这是「不许另起一套市场判断」唯一能机械判的形式。
- `docs/reports/` 只读: 本模块一个字节也不写它, 只写 `docs/signal/`。
- 输出里不放任何挂钟时间: `as_of` 固定为「报告日期 + 收盘时点」, 同样的报告跑两次必须
  逐字节相同, 没有例外。

`change` 用督查定的四档: 升级 / 维持 / 转向 / 无前值。四档里没有「降级」, 所以
强度下降(stance 不变)也记 `维持`, 真实前值写在 md 里给人看。
"""
import json
import os
import re

from lidaxiao.common import WORKDIR
from lidaxiao.report.render import parse_report
from lidaxiao.textutil import log, norm_text, read_text, write_text

#: 数据来源: 只有他本人当日视频的原话, 不掺任何外部判断
DISCLAIMER = "以上为对李大霄本人在当日视频中原话的归纳,不构成投资建议"

STANCES = ("看多", "看空", "中性", "警示")
ACTIONS = ("买入", "加仓", "持有", "观望", "减仓", "卖出")
HORIZONS = ("短线", "中线", "长线")
CHANGES = ("升级", "维持", "转向", "无前值")
STATUSES = ("ok", "degraded", "no_content")

#: 固定 schema, 顺序即输出顺序; 多出来的键一律不写(不许自由发挥)
FIELDS = ("date", "as_of", "stance", "action", "strength", "horizon", "assets",
          "levels", "key_points", "citations", "change", "coverage", "status",
          "disclaimer")

#: 收盘时点常量。`as_of` = 报告日期 + 它 —— 绝不能取 `now()`, 否则「两次跑逐字节相同」失效
CLOSE_TIME = "15:00"
#: 引用最短原文长度(归一化后). 少于这个数, 任何碎词都能在报告里命中, 等于没核。
MIN_QUOTE = 10

LIST_TIME_RE = re.compile(r"^-\s*\[(\d{2}:\d{2})\]\s*(.+?)\s*\(((?:BV|wb)\w+)\)")
HEAD_RE = re.compile(r"^##\s*视频\d*\s*[:：]\s*《(.+?)》\s*\(?((?:BV|wb)\w+)\)?\s*$")


def signal_dir():
    return os.path.join(WORKDIR, "docs", "signal")


def _path(name):
    return os.path.join(signal_dir(), name)


def report_sections(text):
    """当天报告的章节 -> [{key, title, time, body}], 只认真实存在的 `## 视频N:《…》 (KEY)`。"""
    text = text or ""
    parsed = parse_report(text)
    lines = text.splitlines()
    times = {}
    for ln in lines:
        m = LIST_TIME_RE.match(ln)
        if m:
            times[m.group(3)] = m.group(1)
    out = []
    for key, (s, e) in parsed["sections"].items():
        m = HEAD_RE.match(lines[s] if s < len(lines) else "")
        out.append({"key": key, "title": (m.group(1).strip() if m else ""),
                    "time": times.get(key, ""), "body": "\n".join(lines[s + 1:e])})
    # 按报告里的发布时间排序: 输出的确定性靠它, 不靠 dict 插入顺序
    out.sort(key=lambda s: (s["time"] or "99:99", s["key"]))
    return out


def quote_ok(sections, key, quote):
    """引用必须落在该节正文里, 且原文至少 MIN_QUOTE 个字(归一化后算)。

    归一化用 norm_text(去空白/标点/符号), 正文与引用同一把尺子: 模型抄原文时把
    全角逗号换成半角不会因此被误判, 而自己编的一句话仍然过不去。
    """
    q = norm_text(quote)
    if len(q) < MIN_QUOTE:
        return False
    return any(s["key"] == key and q in norm_text(s["body"]) for s in sections)


def _time_of(sections, key):
    for s in sections:
        if s["key"] == key:
            return s["time"]
    return ""


def _pick_quote(body, limit=80):
    """从一节正文里取一句原话: 优先【一句话摘要】, 否则第一行有内容的正文。"""
    lines = [l.strip() for l in (body or "").splitlines()]
    for i, l in enumerate(lines):
        if l.startswith("【一句话摘要】"):
            q = l.split("】", 1)[1].strip() if "】" in l else ""
            if q:
                return q[:limit]
            # 报告里这个标记单独占一行, 正文在下一行
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith((">", "#", "【")):
                    return nxt[:limit]
            return ""
    for l in lines:
        if l and not l.startswith(">") and not l.startswith("#"):
            return l[:limit]
    return ""


def _as_of(date, prev, has_content):
    """报告日期 + 收盘时点; 当天没内容时沿用上一份的(验收员要求写上一份的日期)。"""
    if has_content:
        return f"{date} {CLOSE_TIME}"
    return (prev or {}).get("as_of") or f"{date} {CLOSE_TIME}"


def _change(prev, stance, strength):
    if not prev:
        return "无前值"
    if prev.get("stance") != stance:
        return "转向"
    try:
        return "升级" if int(strength) > int(prev.get("strength") or 0) else "维持"
    except (TypeError, ValueError):
        return "维持"


def _level_list(raw):
    out = []
    for lv in raw or []:
        if not isinstance(lv, dict):
            continue
        asset = str(lv.get("asset") or "").strip()
        try:
            level = float(lv.get("level"))
        except (TypeError, ValueError):
            continue
        if not asset:
            continue
        out.append({"asset": asset, "level": int(level) if level == int(level) else level,
                    "kind": str(lv.get("kind") or "支撑").strip() or "支撑"})
    return out[:8]


def _clean_payload(sections, payload, dropped):
    """把 LLM 的 JSON 过一遍白名单与可定位性; 关键字段不合格 -> None(走 degraded)。"""
    if not isinstance(payload, dict):
        return None
    stance = str(payload.get("stance") or "").strip()
    action = str(payload.get("action") or "").strip()
    if stance not in STANCES or action not in ACTIONS:
        log(f"⚠️ 建议: LLM 返回的 stance/action 不在白名单 ({stance!r}/{action!r}), 按 degraded 处理")
        return None
    try:
        strength = int(payload.get("strength") or 1)
    except (TypeError, ValueError):
        strength = 1
    strength = max(0, min(5, strength))
    horizon = str(payload.get("horizon") or "").strip()
    if horizon not in HORIZONS:
        horizon = "短线"
    assets = []
    for a in payload.get("assets") or []:
        a = str(a).strip()
        if a and a not in assets:
            assets.append(a)
    citations = []
    for c in payload.get("citations") or []:
        if not isinstance(c, dict):
            dropped.append(("?", "citations 里不是对象"))
            continue
        key = str(c.get("video") or "").strip()
        text = str(c.get("text") or "").strip()
        if not key or not text:
            dropped.append((key or "?", "缺 video 或 text"))
            continue
        if not quote_ok(sections, key, text):
            dropped.append((key, text[:40]))
            continue
        citations.append({"text": text, "video": key,
                          "time": _time_of(sections, key) or str(c.get("time") or "")})
    # 点位与标的必须能落到某条引用的正文里, 落不到的丢掉: 这是「不许另起市场判断」的机械判据
    blob = " ".join(norm_text(c["text"]) for c in citations)
    kept_assets = []
    for a in assets:
        if norm_text(a) and norm_text(a) in blob:
            kept_assets.append(a)
        else:
            dropped.append((a, "标的在引用正文里找不到"))
    levels = []
    for lv in _level_list(payload.get("levels")):
        if norm_text(lv["asset"]) in blob and norm_text(str(lv["level"])) in blob:
            levels.append(lv)
        else:
            dropped.append((lv["asset"], f"点位 {lv['level']} 在引用正文里找不到"))
    assets = kept_assets
    if not citations:
        # 白名单过关、但一条引用都落不到报告上: 没有依据就不许有结论(宁缺毋滥)。
        # 按 degraded 处理 —— 只留能从报告正文直接取到的原话, stance/action 沿用上一份。
        # 八条判据里没有一条直接管这个, 但 `status=ok` 配零引用等于把一句无出处的话
        # 标成"有据可查", 那是八条判据想防的东西换了个位置。
        log("⚠️ 建议: 没有一条引用能落到报告正文上, 整份按 degraded 处理")
        return None
    key_points = [str(k).strip() for k in (payload.get("key_points") or []) if str(k).strip()]
    return {"stance": stance, "action": action, "strength": strength, "horizon": horizon,
            "assets": assets[:6], "levels": levels,
            "key_points": key_points[:5], "citations": citations[:8]}


def _raw_citations(sections, limit=6):
    """LLM 不可用时的降级内容: 只放原话, 不做任何归纳。

    同样受 MIN_QUOTE 约束 —— 降级不等于可以放松证据门槛, 短于 10 字的片段
    (如一节正文只有「新」)宁可不写, 空就空着。
    """
    out = []
    for s in sections:
        q = _pick_quote(s["body"])
        if q and len(norm_text(q)) >= MIN_QUOTE and norm_text(q) in norm_text(s["body"]):
            out.append({"text": q, "video": s["key"], "time": s["time"]})
    return out[:limit]


def build_signal(date, report_text, payload=None, prev=None, dropped=None):
    """-> 固定 schema 的建议 dict。

    payload 为空 = LLM 不可用 -> degraded(只放原话); 报告没有任何视频节 -> no_content
    (原样延续上一份 stance/action/as_of, 不许编)。
    """
    dropped = dropped if dropped is not None else []
    sections = report_sections(report_text)
    coverage = {"videos": sum(1 for s in sections if s["key"].startswith("BV")),
                "weibo": sum(1 for s in sections if s["key"].startswith("wb"))}
    prev = prev or None
    base = {"date": date, "as_of": _as_of(date, prev, bool(sections)),
            "stance": (prev or {}).get("stance") or "中性",
            "action": (prev or {}).get("action") or "观望",
            "strength": (prev or {}).get("strength") or 0,
            "horizon": (prev or {}).get("horizon") or "短线",
            "assets": [], "levels": [], "key_points": [], "citations": [],
            "coverage": coverage, "disclaimer": DISCLAIMER}
    if not sections:
        # 当天没内容: 只延续 stance/action/strength/horizon 与上一份的 as_of;
        # assets/levels 一律留空 —— 没有引用正文就没有可核的依据(宁缺毋滥)。
        base.update({"status": "no_content", "assets": [], "levels": [],
                     "change": "无前值" if not prev else "维持"})
        if not prev:
            log("⚠️ 建议: 当天没有内容且没有上一份可延续, stance 记中性/观望")
        return {k: base[k] for k in FIELDS}
    cleaned = _clean_payload(sections, payload, dropped) if payload is not None else None
    if cleaned is None:
        base.update({"status": "degraded", "citations": _raw_citations(sections),
                     "change": _change(prev, base["stance"], base["strength"])})
        return {k: base[k] for k in FIELDS}
    base.update(cleaned)
    base["change"] = _change(prev, base["stance"], base["strength"])
    base["status"] = "ok"
    return {k: base[k] for k in FIELDS}


# ---------------------------------------------------------------- 渲染 (md / json)
def render_md(sig, sections=None):
    titles = {s["key"]: s["title"] for s in (sections or [])}
    st = {"ok": "正常", "degraded": "降级(LLM 不可用, 仅原话)",
          "no_content": "当天无内容"}[sig["status"]]
    out = [f"# 📌 操作建议 {sig['date']}", ""]
    if sig["status"] == "no_content":
        out += [f"- 结论: 当日无视频内容, 延续上一份 —— **{sig['stance']}** / {sig['action']}",
                f"- 数据截止: {sig['as_of']}（沿用上一份）"]
    else:
        out += [f"- 结论: **{sig['stance']}** / {sig['action']}"
                f"（强度 {sig['strength']}，{sig['horizon']}）",
                f"- 数据截止: {sig['as_of']}",
                f"- 与前一日: {sig['change']}"]
    out += [f"- 覆盖: 视频 {sig['coverage']['videos']} 条 / 微博 {sig['coverage']['weibo']} 条",
            f"- 状态: {st}", ""]
    if sig["key_points"]:
        out += ["## 今日依据（每条都能落到原话）", ""]
        for i, k in enumerate(sig["key_points"], 1):
            out.append(f"{i}. {k}")
        out.append("")
    if sig["citations"]:
        out += ["## 原话引用", ""]
        for c in sig["citations"]:
            t = titles.get(c["video"]) or ""
            head = f"视频《{t}》({c['video']})" if t else f"({c['video']})"
            out.append(f"- {head} [{c['time'] or '--:--'}]")
            out.append(f"  > {c['text']}")
        out.append("")
    if sig["assets"]:
        out += ["## 关注标的", ""] + [f"- {a}" for a in sig["assets"]] + [""]
    if sig["levels"]:
        out += ["## 位置", ""] + [f"- {lv['asset']} {lv['kind']} {lv['level']}"
                                  for lv in sig["levels"]] + [""]
    out.append(sig["disclaimer"])
    return "\n".join(out).rstrip() + "\n"


def render_json(sig):
    return json.dumps({k: sig[k] for k in FIELDS}, ensure_ascii=False, indent=2) + "\n"


def render_index(dates):
    links = "\n".join(f"- [{d}](./{d}.md)" for d in sorted(dates, reverse=True))
    return "# 📌 操作建议索引\n\n" + links + "\n"


def write_signal(sig, sections=None):
    """写四份: <date>.md / latest.md / latest.json / index.md; 返回目录。"""
    d = signal_dir()
    os.makedirs(d, exist_ok=True)
    md = render_md(sig, sections)
    write_text(_path(f"{sig['date']}.md"), md)
    write_text(_path("latest.md"), md)
    write_text(_path("latest.json"), render_json(sig))
    dates = [f[:-3] for f in os.listdir(d) if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", f)]
    write_text(_path("index.md"), render_index(dates))
    return d


def load_prev():
    """上一份建议; 坏了就当没有(宁可写 degraded, 不拿坏数据延续)。"""
    try:
        data = json.loads(read_text(_path("latest.json"), "") or "{}")
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("date"):
        return None
    return data


# ---------------------------------------------------------------- 生成 (LLM)
SYSTEM = (
    "你把李大霄当日视频的发言归纳成操作提示。铁律: "
    "① 只许归纳给定报告里已有的内容, 不许加入报告里没有的判断、数据、标的或点位; "
    "② 每条 citations.text 必须是报告某一节里逐字出现的原话片段, video 必须是该节 id; "
    "③ stance 只能取 看多/看空/中性/警示, action 只能取 买入/加仓/持有/观望/减仓/卖出; "
    "④ 只输出一个 JSON 对象, 不要解释, 不要代码块标记。"
)

USER_TMPL = """当日报告:
{report}

可用章节 id: {ids}

输出 JSON, 字段与取值固定:
{{"stance":"看多|看空|中性|警示","action":"买入|加仓|持有|观望|减仓|卖出",
"strength":1,"horizon":"短线|中线|长线","assets":["A股"],
"levels":[{{"asset":"上证","level":3455,"kind":"支撑"}}],
"key_points":["结论, 不超过 5 条"],
"citations":[{{"text":"报告里的原话片段","video":"上列 id 之一","time":"09:35"}}]}}
"""


def _parse_json(text):
    if not text:
        return None
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text.strip())
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def ask_llm(cfg, api_key, report_text, sections):
    """调一次 LLM 要 JSON; 任何失败都返回 None(上层按 degraded 处理)。"""
    from lidaxiao.analyze.llm import call_llm
    ids = ", ".join(s["key"] for s in sections) or "(无)"
    user = USER_TMPL.format(report=report_text[:8000], ids=ids)
    try:
        raw = call_llm(cfg, api_key, SYSTEM, user)
    except Exception as e:
        log(f"⚠️ 建议: LLM 调用异常 {type(e).__name__}: {e}")
        return None
    data = _parse_json(raw)
    if data is None:
        log("⚠️ 建议: LLM 没返回可解析的 JSON, 按 degraded 处理")
    return data


def generate(date, report_text, cfg=None, api_key=None, prev=None, payload=None,
             use_llm=True):
    """生成一份建议(不写盘) -> (sig, sections, dropped)。

    use_llm=False 或 cfg 缺失时不调模型, 直接走 degraded。
    """
    sections = report_sections(report_text)
    dropped = []
    if payload is None and use_llm and sections and cfg is not None:
        payload = ask_llm(cfg, api_key, report_text, sections)
    sig = build_signal(date, report_text, payload=payload, prev=prev, dropped=dropped)
    for key, why in dropped:
        log(f"↩️ 建议: 丢弃引用 {key}: {why}")
    return sig, sections, dropped
