#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地小模型两步法: JSON 抽取 + 代码模板渲染

小模型只负责"读懂字幕、抽信息"，报告结构由这里保证，
所以不会再出现缺段、复读提示词、N. 占位这类问题。
"""
import json
import re

import requests

EXTRACT_SYSTEM = """你是财经视频内容抽取器。只依据用户给出的【字幕文本】抽取信息，【历史报告】仅用于最后一项对比。
严格输出一个 JSON 对象（不要代码块、不要任何解释），字段如下：
{
  "summary": "一句话结论",
  "points": [{"claim": "观点", "data": "原文数据(点位/百分比/日期等原话片段)"}],
  "metrics": ["数据项: 数值"],
  "hints": [{"quote": "原话", "direction": "🔴警示 或 🟢看多 或 ⚪中性 或 ⚠️风险", "reason": "判断依据"}],
  "market": "反弹/反转/见顶/调整/防御 之一，并给依据",
  "action": {"direction": "看多/看空/观望", "position": "仓位建议", "risk": "风险点"},
  "continuity": "与历史对比: 延续/升级/新增/反转 + 说明；无历史则写'首次记录,无历史对比'"
}
要求：
- points 3-8 条，每条尽量带原文数据；
- hints 逐条列出带暗示的话术，注意"不是推荐"要结合语境判断：光杆点名多为隐性推荐，报跌幅型是风险提示，片尾免责声明算中性；
- 只写字幕里真实出现的数据，绝不虚构；
- 下面每条字段都必须填本期具体内容，禁止把说明文字（如"之一""仓位建议""风险点"）原样抄进结果。

格式示例（内容仅为示范，实际要按本期字幕填写）：
{"summary":"本期围绕政策定调，强调长期资金入市",
 "points":[{"claim":"长钱入市已成体系","data":"净买入超1.5万亿元"}],
 "metrics":["A股总市值: 突破100万亿元"],
 "hints":[{"quote":"不是推荐","direction":"⚪中性","reason":"片尾合规声明，非行情暗示"}],
 "market":"防御：有政策利好但反复提示外部联动风险",
 "action":{"direction":"观望","position":"维持中性仓位，不加杠杆","risk":"外部市场传导与情绪过热"},
 "continuity":"延续：与昨日报告一致，仍以防御为主"}"""


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


def render(o):
    """把 JSON 渲染成生产报告格式(保证七段齐全)"""
    def s(k, default=""):
        v = o.get(k)
        return str(v).strip() if v is not None else default

    out = [f"【一句话摘要】{s('summary') or '（模型未给出摘要）'}", "", "【核心观点】"]
    pts = _as_list(o.get("points"))
    for i, p in enumerate(pts[:8], 1):
        if isinstance(p, dict):
            claim, data = str(p.get("claim", "")).strip(), str(p.get("data", "")).strip()
            out.append(f"{i}. {claim}" + (f" (原文数据: {data})" if data else ""))
        else:
            out.append(f"{i}. {str(p).strip()}")
    if not pts:
        out.append("1. （模型未给出观点）")

    out += ["", "【关键数据清单】"]
    mets = _as_list(o.get("metrics"))
    out += [f"- {str(m).strip()}" for m in mets] or ["- 本期未提取到具体数据"]

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
            f"- **方向**: {str(act.get('direction', '观望')).strip()}",
            f"- **仓位**: {str(act.get('position', '维持现状')).strip()}",
            f"- **风险**: {str(act.get('risk', '见上述风险提示')).strip()}"]

    out += ["", f"【观点连续性】{s('continuity') or '首次记录,无历史对比'}", "",
            "以上为李大霄个人观点提炼,不构成投资建议"]
    return "\n".join(out)



PLACEHOLDER_MARKERS = ("之一", "仓位建议", "风险点", "看多/看空", "或", "具体内容")


def _is_placeholder(o):
    act = o.get("action") if isinstance(o.get("action"), dict) else {}
    vals = [str(o.get("market", "")), str(act.get("direction", "")),
            str(act.get("position", "")), str(act.get("risk", ""))]
    return any(v.strip() in ("", "仓位建议", "风险点", "看多/看空/观望")
               or "之一" in v or "看多/看空" in v for v in vals)


def _heuristic_fill(o):
    """模型照抄占位时, 用暗示条目的方向分布兜底补齐"""
    hints = [h for h in (o.get("hints") or []) if isinstance(h, dict)]
    bull = sum(1 for h in hints if "看多" in str(h.get("direction", "")))
    bear = sum(1 for h in hints if any(k in str(h.get("direction", "")) for k in ("警示", "风险", "看空")))
    act = o.get("action") if isinstance(o.get("action"), dict) else {}
    if "之一" in str(o.get("market", "")) or not str(o.get("market", "")).strip():
        o["market"] = ("防御：警示条目多于看多项，整体偏谨慎" if bear >= bull
                       else "反弹：看多条目占优，但仍需量能确认")
    if "看多/看空" in str(act.get("direction", "")) or not act.get("direction"):
        act["direction"] = "观望" if bear >= bull else "看多"
    if not act.get("position") or act.get("position") in ("仓位建议",):
        act["position"] = "维持中性仓位，不加杠杆"
    if not act.get("risk") or act.get("risk") in ("风险点",):
        act["risk"] = "外部市场传导、情绪过热与杠杆风险"
    o["action"] = act
    return o


def run(pcfg, user_text):
    """调本地 llama-server 做 JSON 抽取并渲染成报告"""
    base = (pcfg.get("base_url") or "").rstrip("/")
    payload = {
        "model": pcfg.get("model", "local"),
        "messages": [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": user_text[:12000]},
        ],
        "temperature": pcfg.get("temperature", 0.2),
        "max_tokens": pcfg.get("max_tokens", 1600),
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
        raise RuntimeError("本地 JSON 抽取返回空 content")
    o = _parse(content)
    if _is_placeholder(o):
        o = _heuristic_fill(o)
    return render(o)
