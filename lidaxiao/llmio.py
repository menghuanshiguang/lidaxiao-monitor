"""LLM 通道公共件: 模型名/思考参数/免密车道/CLI 路径定位

这些是文本通道 (analyze) 与视觉通道 (vision) 共用的底层动作, 从 analyze/llm.py 下沉而来。
放在 analyze 与 vision 之下, 让两边都能 import 而不产生 analyze<->vision 的反向依赖。
函数体与下沉前逐字节相同, 只有所在文件变了。
"""
import hashlib
import json
import os
import time

from lidaxiao.common import WORKDIR
from lidaxiao.textutil import log

RETIRED_MODEL_MAP = {
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4-flash-free": "deepseek-flash",
}


THINKING_EFFORTS = ("low", "high", "max")


def _warn_model(pcfg):
    """旧模型名提醒(不阻断): 官方已下线这些名字, 请求会被路由到 V4.1 Flash"""
    m = (pcfg.get("model") or "").strip()
    if m in RETIRED_MODEL_MAP:
        log(f"⚠️ 模型名 `{m}` 已下线, 建议改为 `{RETIRED_MODEL_MAP[m]}` (--config 或 config.json)")


def _apply_thinking(payload, pcfg):
    """思考模式参数 (官方文档: OpenAI 格式 {"thinking":{"type":"enabled/disabled"}}
    + {"reasoning_effort":"low/high/max"})。

    只有配了 reasoningEffort 才显式打开思考(并省略 temperature —— 思考模式下
    官方 API 不支持 temperature, 传了也不生效)。没配时原样保留, 保证
    OpenCode Zen Go / Ollama 等旧通道行为完全不变。
    """
    effort = (pcfg.get("reasoningEffort") or "").strip()
    if not effort:
        return payload
    if effort in ("off", "none", "disabled", "false"):
        payload["thinking"] = {"type": "disabled"}
        payload.setdefault("temperature", pcfg.get("temperature", 0.3))
        return payload
    if effort not in THINKING_EFFORTS:
        # 旧值兼容: minimal/medium/xhigh/ultra 官方会自动映射, 这里仅提示
        log(f"ℹ️ reasoningEffort=`{effort}` 非官方新取值 {THINKING_EFFORTS}, 官方会自动映射")
    payload["thinking"] = {"type": "enabled"}
    payload["reasoning_effort"] = effort
    payload.pop("temperature", None)
    return payload


def _find_cli_path(pcfg, fname, dirname):
    """定位 CLI 仓库的入口脚本 (dsc.py / dsv.py)"""
    rel = pcfg.get("path", "_repo/" + dirname)
    cands = []
    if os.path.isabs(rel):
        cands.append(rel)
    else:
        cands.append(os.path.join(WORKDIR, rel))
    for extra in ("_repo/" + dirname, "tools/" + dirname, dirname):
        cands.append(os.path.join(WORKDIR, extra))
    for d in cands:
        f = os.path.join(d, fname)
        if os.path.isfile(f):
            return f
    return None


FREE_DECOY_TOOLS = [
    {"type": "function", "function": {"name": n, "description": "d",
     "parameters": {"type": "object", "properties": {}}}}
    for n in ("bash", "glob", "grep", "read")
]


def _free_derive_id(prefix, seed):
    """sha256 派生稳定 id: 前6字节hex + 后14字节base62 (与插件同构)"""
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    return prefix + h[:6].hex() + "".join(FREE_B62[b % 62] for b in h[6:20])


FREE_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _free_lane_post(pcfg, payload, timeout):
    """免密车道流式 POST, 返回拼接好的正文 (跳过 reasoning 增量)"""
    import requests
    seed = "lidaxiao-monitor\0" + (pcfg.get("model") or "free")
    headers = {
        "Authorization": "Bearer public",
        "User-Agent": "opencode/1.18.31",
        "x-opencode-client": "desktop",
        "x-opencode-session": _free_derive_id("ses_", seed),
        "x-opencode-request": _free_derive_id(
            "msg_", seed + "\0" + str(time.time()) + str(os.getpid())),
        "x-opencode-project": "global",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    body = dict(payload, stream=True, tools=FREE_DECOY_TOOLS, tool_choice="none")
    url = pcfg["base_url"].rstrip("/") + "/chat/completions"
    r = requests.post(url, json=body, headers=headers, timeout=timeout, stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    r.encoding = "utf-8"          # iter_lines 默认 latin-1, 中文正文会乱码
    parts, finish = [], ""
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        s = line[5:].strip()
        if s == "[DONE]":
            break
        try:
            j = json.loads(s)
        except ValueError:
            continue
        for ch in j.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError(f"免密车道返回空正文 (finish={finish})")
    if finish == "length":
        raise RuntimeError(f"免密车道正文被 max_tokens={payload.get('max_tokens')} 截断")
    return text
