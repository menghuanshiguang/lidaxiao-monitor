#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地小模型分段法: 抽取分成 3 次调用, 每次只产出小 JSON, 由代码渲染成报告

① 观点 + 数据清单   ② 暗示提取(带判断口径示例)   ③ 市场定性 + 操作含义 + 连续性
好处: 每次任务小、约束强, 2B 模型也能稳定完成; 格式由 render() 保证。
"""
import json
import re

import requests

# ---------------- ① 观点 + 数据 ----------------
A_SYSTEM = """你是财经视频信息抽取器。只依据用户给的【字幕文本】抽取，输出一个 JSON（不要解释、不要代码块）：
{"summary": "一句话结论", "points": [{"claim": "观点", "data": "原文数据"}], "metrics": ["数据项: 数值"]}
规则：
- points 3-8 条；data 必须是字幕里真实出现的数字/日期原话，禁止用字幕时间戳(如 00:44)冒充；
- metrics 只列具体数值类信息，没有就给空数组；
- 不要评价，不要给操作建议。"""

# ---------------- ② 暗示提取 ----------------
FEWSHOT = """判断口径示例（只学口径，不要照抄内容）：
1. “XX跌了5%，不是推荐” → 🔴警示｜报跌幅型，属风险提示，绝不是低吸信号
2. “港股通没有杠杆” → 🟢看多｜光杆点名型，隐性认可
3. 片尾“市场有巨大风险，投资需谨慎” → ⚪中性｜合规免责声明
4. “没量，那还是反弹，不一定是反转” → 🔴警示｜量能框架下的否定判断"""

B_SYSTEM = f"""你是财经话术分析器。只分析【字幕文本】里带暗示性的话术，输出一个 JSON（不要解释）：
{{"hints": [{{"quote": "原话片段", "direction": "🔴警示 或 🟢看多 或 ⚪中性 或 ⚠️风险", "reason": "判断依据"}}]}}
规则：
- 3-8 条，quote 必须是字幕原话；
- 必须结合语境，不要一律标看多；纯陈述政策/数据、无方向含义的标 ⚪中性；
{FEWSHOT}"""

# ---------------- ③ 定性 + 操作 ----------------
C_SYSTEM = """你是投资研究报告定稿器。只依据用户给的观点与暗示条目，输出一个 JSON（不要解释、不要代码块）：
{"market": "市场定性", "action": {"direction": "操作方向", "position": "仓位建议", "risk": "风险点"}, "continuity": "观点连续性"}

填写要求：
- market 从 反弹/反转/见顶/调整/防御 里选一个，后接一句依据；
- direction 从 看多/看空/观望 里选一个；
- position、risk 各写一句本期具体内容；
- continuity 写 延续/升级/新增/反转 并说明，没有历史就写「首次记录,无历史对比」。

格式示例（只示范怎么填，内容要换成当期的）：
{"market":"防御：政策面偏暖，但反复提示外部联动与量能不足","action":{"direction":"观望","position":"维持中性仓位，不加杠杆","risk":"外部市场传导、情绪过热与杠杆资金"},"continuity":"首次记录,无历史对比"}"""


def _post(pcfg, system, user, max_tokens):
    base = (pcfg.get("base_url") or "").rstrip("/")
    payload = {
        "model": pcfg.get("model", "local"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user[:12000]}],
        "temperature": pcfg.get("temperature", 0.2),
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    if pcfg.get("enable_thinking") is False:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    r = requests.post(base + "/chat/completions", json=payload,
                      timeout=pcfg.get("timeout", 900))
    r.raise_for_status()
    msg = (r.json().get("choices") or [{}])[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    if not content:
        raise RuntimeError("分段调用返回空 content")
    return _parse(content)


def _clean_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    if t.startswith("{") and t.endswith("}"):
        return t
    m = re.search(r"\{.*\}", t, re.S)
    return m.group(0) if m else t


def _parse(text):
    return json.loads(_clean_json(text))


def _as_list(v):
    if isinstance(v, list):
        return v
    return [v] if v else []


def _subtitle(user_text):
    m = re.search(r"【字幕文本】\s*\n(.*?)\n\s*【历史报告", user_text, re.S)
    return (m.group(1) if m else user_text).strip()[:12000]


def _history(user_text):
    m = re.search(r"【历史报告[^】]*】\s*\n(.*?)(?:\n\s*请按|\Z)", user_text, re.S)
    return (m.group(1).strip() if m else "")[:1500]


PLACEHOLDER = ("选一个", "一句话", "说明；", "反弹/反转", "市场定性", "操作方向", "仓位建议", "风险点")


def _bad(v):
    v = str(v or "")
    return (not v.strip()) or any(m in v for m in PLACEHOLDER)


def _fix_c(c, hints):
    """第三段被模型照抄占位时, 用暗示方向分布兜底"""
    hs = [h for h in (hints or []) if isinstance(h, dict)]
    bull = sum(1 for h in hs if "看多" in str(h.get("direction", "")))
    bear = sum(1 for h in hs if any(k in str(h.get("direction", "")) for k in ("警示", "风险", "看空")))
    act = c.get("action") if isinstance(c.get("action"), dict) else {}
    if _bad(c.get("market")):
        c["market"] = ("防御：警示类条目多于看多项，整体偏谨慎" if bear > bull
                       else "反弹：看多类条目偏多，但仍需量能确认")
    if _bad(act.get("direction")):
        act["direction"] = "观望" if bear >= bull else "看多"
    if _bad(act.get("position")):
        act["position"] = "维持中性仓位，不加杠杆"
    if _bad(act.get("risk")):
        act["risk"] = "外部市场传导、情绪过热与杠杆风险"
    if _bad(c.get("continuity")):
        c["continuity"] = "首次记录,无历史对比"
    c["action"] = act
    return c


def run(pcfg, user_text):
    sub = _subtitle(user_text)
    hist = _history(user_text)

    a = _post(pcfg, A_SYSTEM, f"【字幕文本】\n{sub}", 1000)
    b = _post(pcfg, "你是财经话术分析器。\n" + B_SYSTEM,
              f"【字幕文本】\n{sub}", 800)

    brief = json.dumps({"summary": a.get("summary"), "points": a.get("points"),
                        "hints": b.get("hints")}, ensure_ascii=False)[:4000]
    c_user = f"【观点与暗示】\n{brief}\n\n【历史报告摘要】\n{hist or '(无历史报告)'}"
    c = _post(pcfg, C_SYSTEM, c_user, 500)
    c = _fix_c(c, b.get("hints"))

    return render({**a, **b, **c})


# ---------------- 渲染 ----------------
def _strip_ts(t):
    """去掉模型顺手带上的字幕时间戳, 如 '00:44 100万亿元'"""
    return re.sub(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*", "", str(t or "")).strip()


def render(o):
    def s(k, default=""):
        v = o.get(k)
        return str(v).strip() if v is not None else default

    out = [f"【一句话摘要】{s('summary') or '（未给出摘要）'}", "", "【核心观点】"]
    pts = _as_list(o.get("points"))
    for i, p in enumerate(pts[:8], 1):
        if isinstance(p, dict):
            claim = str(p.get("claim", "")).strip()
            data = _strip_ts(p.get("data", ""))
            out.append(f"{i}. {claim}" + (f" (原文数据: {data})" if data else ""))
        else:
            out.append(f"{i}. {str(p).strip()}")
    if not pts:
        out.append("1. （未提取到观点）")

    out += ["", "【关键数据清单】"]
    mets = _as_list(o.get("metrics"))
    out += [f"- {_strip_ts(m)}" for m in mets] or ["- 本期未提取到具体数据"]

    out += ["", "【暗示提取】"]
    hints = _as_list(o.get("hints"))
    for i, h in enumerate(hints[:10], 1):
        if isinstance(h, dict):
            q = str(h.get("quote", "")).strip()
            d = str(h.get("direction", "⚪中性")).strip() or "⚪中性"
            r = str(h.get("reason", "")).strip()
            out.append(f"{i}. “{q}” → {d}" + (f"：{r}" if r else ""))
        else:
            out.append(f"{i}. {str(h).strip()}")
    if not hints:
        out.append("1. 本期无明显暗示性话术")

    out += ["", f"【市场定性】{s('market') or '未给出'}"]

    act = o.get("action") if isinstance(o.get("action"), dict) else {}
    out += ["", "【操作含义】",
            f"- **方向**: {str(act.get('direction', '观望')).strip() or '观望'}",
            f"- **仓位**: {str(act.get('position', '维持现状')).strip() or '维持现状'}",
            f"- **风险**: {str(act.get('risk', '见上述风险提示')).strip() or '见上述风险提示'}"]

    out += ["", f"【观点连续性】{s('continuity') or '首次记录,无历史对比'}", "",
            "以上为李大霄个人观点提炼,不构成投资建议"]
    return "\n".join(out)
