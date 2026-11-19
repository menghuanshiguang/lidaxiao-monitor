#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地小模型分段法 + 枚举约束

思路: 字段边界交给 schema(json_schema/enum) 和解码器, 而不是指望模型自觉。
① 观点+数据   ② 暗示(方向枚举)   ③ 定性(枚举) + 操作(方向枚举)
每段都带 enum + 数量约束, 解不出来就重试一次, 再不行抛错回落云端。
"""
import json
import re
import time

import requests

# ---------------- JSON Schema(带枚举边界) ----------------
DIRECTIONS = ["🔴警示", "🟢看多", "⚪中性", "⚠️风险"]
MARKET_KINDS = ["反弹", "反转", "见顶", "调整", "防御"]
ACTIONS = ["看多", "看空", "观望"]
CONTINUITY = ["延续", "升级", "新增", "反转", "首次记录"]

SCHEMA_A = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 20},
        "points": {
            "type": "array", "minItems": 3, "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {"claim": {"type": "string", "minLength": 10},
                               "data": {"type": "string"}},
                "required": ["claim", "data"],
            },
        },
        "metrics": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
    },
    "required": ["summary", "points", "metrics"],
}

SCHEMA_B = {
    "type": "object",
    "properties": {
        "hints": {
            "type": "array", "minItems": 3, "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string", "minLength": 6},
                    "direction": {"type": "string", "enum": DIRECTIONS},
                    "reason": {"type": "string", "minLength": 4},
                },
                "required": ["quote", "direction", "reason"],
            },
        }
    },
    "required": ["hints"],
}

SCHEMA_C = {
    "type": "object",
    "properties": {
        "market_kind": {"type": "string", "enum": MARKET_KINDS},
        "market_basis": {"type": "string", "minLength": 6},
        "action": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ACTIONS},
                "position": {"type": "string", "minLength": 4},
                "risk": {"type": "string", "minLength": 4},
            },
            "required": ["direction", "position", "risk"],
        },
        "continuity_kind": {"type": "string", "enum": CONTINUITY},
        "continuity_note": {"type": "string"},
    },
    "required": ["market_kind", "market_basis", "action", "continuity_kind"],
}

# ---------------- 提示词(只讲怎么想, 边界交给 schema) ----------------
A_SYSTEM = """只依据用户给的【字幕文本】抽取信息，输出 JSON。
summary 一句话结论；points 3-8 条，每条 claim 是观点、data 是字幕里真实出现的数字/日期原话（不要用时间戳）；metrics 只放具体数值。
不要评价，不要建议。"""

B_SYSTEM = """判断【字幕文本】里带暗示性的话术，输出 JSON。
每条给 quote(字幕原话)、direction、reason(依据)。
direction 只能取这四个之一：🔴警示 / 🟢看多 / ⚪中性 / ⚠️风险。
判断口径：
- 光杆点名某标的、无跌幅铺垫 → 🟢看多（隐性推荐）
- “XX跌了X%，不是推荐”这类报跌幅 → 🔴警示（风险提示，不是低吸）
- 片尾免责声明 → ⚪中性
- 明确提醒某个风险点（杠杆、情绪、外部传导）→ ⚠️风险
不要一律标看多；纯陈述政策/数据的标 ⚪中性。"""

C_SYSTEM = """根据用户给的观点与暗示条目做定稿，输出 JSON。
market_kind 只能取：反弹 / 反转 / 见顶 / 调整 / 防御，market_basis 写一句依据。
action.direction 只能取：看多 / 看空 / 观望。
continuity_kind 只能取：延续 / 升级 / 新增 / 反转 / 首次记录。
position、risk、continuity_note 各写一句本期具体内容，不要写说明文字。"""


# ---------------- 请求 ----------------
def _post(pcfg, system, user, schema, max_tokens):
    base = (pcfg.get("base_url") or "").rstrip("/")
    payload = {
        "model": pcfg.get("model", "local"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user[:12000]}],
        "temperature": pcfg.get("temperature", 0.2),
        "max_tokens": max_tokens,
        "stream": False,
    }
    # 只有 llama.cpp 认这个参数, Ollama 会当成未知字段
    if pcfg.get("enable_thinking") is False and "11434" not in base:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    # 首选 json_schema(枚举在解码阶段生效), 服务端不支持则退回 json_object
    for fmt in ({"type": "json_schema",
                 "json_schema": {"name": "out", "strict": True, "schema": schema}},
                {"type": "json_object"}):
        payload["response_format"] = fmt
        r = requests.post(base + "/chat/completions", json=payload,
                          timeout=pcfg.get("timeout", 900))
        if r.status_code < 400:
            msg = (r.json().get("choices") or [{}])[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            if content:
                return _parse(content)
            raise RuntimeError("本地返回空 content")
    raise RuntimeError(f"本地请求失败 HTTP {r.status_code}")


def _clean_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    if t.startswith("{") and t.endswith("}"):
        return t
    m = re.search(r"\{.*\}", t, re.S)
    return m.group(0) if m else t


def _parse(text):
    return json.loads(_clean_json(text))


# ---------------- 枚举校验(解码没兜住时在这一层兜) ----------------
DIR_SYNONYMS = {
    "🔴": "🔴警示", "警示": "🔴警示", "看空": "🔴警示", "空": "🔴警示",
    "🟢": "🟢看多", "看多": "🟢看多", "多": "🟢看多",
    "⚪": "⚪中性", "中性": "⚪中性",
    "⚠️": "⚠️风险", "风险": "⚠️风险", "⚠": "⚠️风险",
}
MARKET_SYNONYMS = {
    "反弹": "反弹", "反转": "反转", "见顶": "见顶", "调整": "调整", "防御": "防御",
    "上涨": "反弹", "下跌": "调整", "高位": "见顶", "底部": "反弹", "观望": "防御",
}
ACTION_SYNONYMS = {"看多": "看多", "多": "看多", "买入": "看多",
                   "看空": "看空", "空": "看空", "卖出": "看空",
                   "观望": "观望", "中性": "观望", "持有": "观望"}
CONT_SYNONYMS = {k: k for k in CONTINUITY} | {"无历史": "首次记录", "首次": "首次记录"}


def _enum(v, allowed, table):
    s = str(v or "").strip()
    if s in allowed:
        return s
    return table.get(s) or next((a for a in allowed if a in s), None)


def _check_a(o):
    errs = []
    pts = [p for p in (o.get("points") or []) if isinstance(p, dict)
           and str(p.get("claim", "")).strip()]
    o["points"] = pts
    if len(pts) < 3:
        errs.append(f"points 只有 {len(pts)} 条(需 3-8)")
    if not str(o.get("summary", "")).strip():
        errs.append("summary 为空")
    return errs


def _check_b(o):
    errs, good = [], []
    for h in (o.get("hints") or []):
        if not isinstance(h, dict):
            continue
        q = str(h.get("quote", "")).strip()
        d = _enum(h.get("direction"), DIRECTIONS, DIR_SYNONYMS)
        r = str(h.get("reason", "")).strip()
        if not q:
            continue
        if not d:
            errs.append(f"direction 非法: {h.get('direction')!r}")
            continue
        good.append({"quote": q, "direction": d, "reason": r or "见原文语境"})
    o["hints"] = good
    if len(good) < 3:
        errs.append(f"hints 只有 {len(good)} 条(需 3-8)")
    dirs = {h["direction"] for h in good}
    if len(good) >= 3 and len(dirs) < 2:
        errs.append(f"方向过于单一(全为 {list(dirs)[0] if dirs else '?'})，必须区分看多/警示/中性")
    return errs


def _check_c(o, hints=None):
    errs = []
    hs = hints or []
    bull = sum(1 for h in hs if "看多" in str(h.get("direction", "")))
    bear = sum(1 for h in hs if any(k in str(h.get("direction", "")) for k in ("警示", "风险", "看空")))
    mk = _enum(o.get("market_kind"), MARKET_KINDS, MARKET_SYNONYMS)
    act = o.get("action") if isinstance(o.get("action"), dict) else {}
    ad = _enum(act.get("direction"), ACTIONS, ACTION_SYNONYMS)
    ck = _enum(o.get("continuity_kind"), CONTINUITY, CONT_SYNONYMS) or "首次记录"
    if not mk:
        errs.append(f"market_kind 非法: {o.get('market_kind')!r}")
    if not ad:
        errs.append(f"action.direction 非法: {act.get('direction')!r}")
    for k in ("position", "risk"):
        if not str(act.get(k, "")).strip():
            errs.append(f"action.{k} 为空")
    if not str(o.get("market_basis", "")).strip():
        errs.append("market_basis 为空")
    o["market_kind"], o["action"] = mk or "", {
        "direction": ad or "", "position": act.get("position", ""),
        "risk": act.get("risk", "")}
    o["continuity_kind"] = ck
    # 业务一致性: 定性/操作不能和暗示方向相反
    if mk == "反弹" and bear > bull:
        errs.append(f"market_kind=反弹 但暗示里警示({bear})多于看多({bull})")
    if mk == "防御" and bull > bear:
        errs.append(f"market_kind=防御 但暗示里看多({bull})多于警示({bear})")
    if ad == "看多" and bear > bull:
        errs.append(f"action.direction=看多 但暗示以警示为主({bear} vs {bull})")
    if ad == "看空" and bull > bear + 1:
        errs.append(f"action.direction=看空 但暗示以看多为主({bull} vs {bear})")
    return errs


def _run_stage(pcfg, system, user, schema, checker, max_tokens, label):
    """跑一段: 校验不过就把错误喂回去重试一次"""
    t0 = time.time()
    print(f"[local] {label} ...", flush=True)
    o = _post(pcfg, system, user, schema, max_tokens)
    errs = checker(o)
    if errs:
        print(f"[local] {label} 不合格({'; '.join(errs)}), 重试", flush=True)
        o2 = _post(pcfg, system,
                   user + f"\n\n上一次输出不合格：{'; '.join(errs)}。请严格按枚举取值重出。",
                   schema, max_tokens)
        errs2 = checker(o2)
        if errs2:
            raise RuntimeError(f"{label} 两次都不合格: {errs2}")
        o = o2
    print(f"[local] {label} 完成 {time.time()-t0:.0f}s", flush=True)
    return o


def _subtitle(user_text):
    m = re.search(r"【字幕文本】\s*\n(.*?)\n\s*【历史报告", user_text, re.S)
    return (m.group(1) if m else user_text).strip()[:12000]


def _history(user_text):
    m = re.search(r"【历史报告[^】]*】\s*\n(.*?)(?:\n\s*请按|\Z)", user_text, re.S)
    return (m.group(1).strip() if m else "")[:1500]


def run(pcfg, user_text):
    sub, hist = _subtitle(user_text), _history(user_text)

    a = _run_stage(pcfg, A_SYSTEM, f"【字幕文本】\n{sub}", SCHEMA_A, _check_a, 1000, "1/3 观点+数据")
    b = _run_stage(pcfg, B_SYSTEM, f"【字幕文本】\n{sub}", SCHEMA_B, _check_b, 800, "2/3 暗示")

    brief = json.dumps({"summary": a.get("summary"), "points": a.get("points"),
                        "hints": b.get("hints")}, ensure_ascii=False)[:4000]
    c = _run_stage(pcfg, C_SYSTEM,
                   f"【观点与暗示】\n{brief}\n\n【历史报告摘要】\n{hist or '(无历史报告)'}",
                   SCHEMA_C, lambda o: _check_c(o, b.get("hints")), 500, "3/3 定性+操作")
    return render(a, b, c)


# ---------------- 渲染 ----------------
def _strip_ts(t):
    return re.sub(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*", "", str(t or "")).strip()


def _as_list(v):
    return v if isinstance(v, list) else ([v] if v else [])


def render(a, b, c):
    out = [f"【一句话摘要】{str(a.get('summary', '')).strip()}", "", "【核心观点】"]
    pts = _as_list(a.get("points"))
    for i, p in enumerate(pts[:8], 1):
        claim = str(p.get("claim", "")).strip()
        data = _strip_ts(p.get("data", ""))
        out.append(f"{i}. {claim}" + (f" (原文数据: {data})" if data else ""))
    if not pts:
        out.append("1. （未提取到观点）")

    out += ["", "【关键数据清单】"]
    mets = _as_list(a.get("metrics"))
    out += [f"- {_strip_ts(m)}" for m in mets] or ["- 本期未提取到具体数据"]

    out += ["", "【暗示提取】"]
    hints = _as_list(b.get("hints"))
    for i, h in enumerate(hints[:10], 1):
        out.append(f"{i}. “{h['quote']}” → {h['direction']}：{h.get('reason', '')}")
    if not hints:
        out.append("1. 本期无明显暗示性话术")

    kind = c.get("market_kind") or "调整"
    basis = str(c.get("market_basis", "")).strip()
    out += ["", f"【市场定性】**{kind}**" + (f"：{basis}" if basis else "")]

    act = c.get("action") or {}
    out += ["", "【操作含义】",
            f"- **方向**: {act.get('direction') or '观望'}",
            f"- **仓位**: {str(act.get('position', '')).strip() or '维持现状'}",
            f"- **风险**: {str(act.get('risk', '')).strip() or '见上述风险提示'}"]

    ck = c.get("continuity_kind") or "首次记录"
    note = str(c.get("continuity_note", "")).strip()
    out += ["", f"【观点连续性】**{ck}**" + (f"：{note}" if note else ""), "",
            "以上为李大霄个人观点提炼,不构成投资建议"]
    return "\n".join(out)
