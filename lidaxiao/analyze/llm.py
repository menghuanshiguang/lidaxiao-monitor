"""LLM 分析层: 提示词构造 / 云端调用 / CLI 通道 / 结果整形 —— 从 monitor.py 抽出"""
import base64
import datetime
import glob
import hashlib
import io
import json
import os
import shutil
import re
import subprocess
import sys
import time

from lidaxiao.common import CN_TZ, WORKDIR
from lidaxiao.prompts import ANALYZE_SYSTEM, REPORT_SECTIONS, REPORT_TAIL
from lidaxiao.textutil import log, read_text
from lidaxiao.config import FREE_PROVIDER, load_llm_key  # noqa: E402

VISION_CLI_PROVIDER = "deepseek-vision-cli"

RETIRED_MODEL_MAP = {
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4-flash-free": "deepseek-flash",
}

def vision_providers(cfg):
    """视觉 provider 优先级(固定): MiMo 免密 -> opencode-go -> deepseek-vision-cli -> DeepSeek 官方 API

    deepseek-vision-cli (dsv.py) 走 DeepSeek 网页版「识图模式」, 用登录 token,
    不消耗 API 额度; 多帧一次会话发送, 用后即删。
    """
    out = []
    for k in ("vision_free", "vision", "vision_cli", "vision_fallback"):
        p = cfg.get(k)
        if not p or not p.get("enabled", True):
            continue
        sig = (p.get("provider"), p.get("base_url"), p.get("model"))
        if any((q.get("provider"), q.get("base_url"), q.get("model")) == sig for q in out):
            continue
        out.append(p)
    return out

def _call_vision_once(pcfg, api_key, prompt, images):
    """向一个视觉端点发多图请求, 返回文本"""
    import requests
    _warn_model(pcfg)
    url = pcfg["base_url"].rstrip("/") + "/chat/completions"
    content = [{"type": "text", "text": prompt}]
    for p in images:
        content.append({"type": "image_url", "image_url": {
            "url": frame_data_url(p, pcfg.get("max_width", 1280), pcfg.get("jpeg_quality", 85))}})
    base = {"model": pcfg["model"], "messages": [{"role": "user", "content": content}],
            "stream": False}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if pcfg.get("provider") == FREE_PROVIDER:
        # 免密车道: 非流式必挂, 走流式分支 (思考不可关, max_tokens 留足)
        return _free_lane_post(pcfg, {
            "model": pcfg["model"],
            "messages": [{"role": "user", "content": content}],
            "max_tokens": pcfg.get("max_tokens", 8000),
        }, pcfg.get("timeout", 240))

    def post(payload):
        r = requests.post(url, json=payload, headers=headers,
                          timeout=pcfg.get("timeout", 240))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    payload = dict(base, max_tokens=pcfg.get("max_tokens", 6000))
    if pcfg.get("temperature") is not None:
        payload["temperature"] = pcfg["temperature"]
    # 思考模式参数统一由 _apply_thinking 决定 (官方: thinking + reasoning_effort)
    _apply_thinking(payload, pcfg)
    j = post(payload)
    ch = (j.get("choices") or [{}])[0]
    text = (ch.get("message", {}).get("content") or "").strip()
    if not text:
        # deepseek-flash 默认开启思考: 思考 token 吃满 max_tokens 时会返回空内容
        # -> 显式关闭思考重试一次
        log("⚠️ 视觉模型返回空内容(思考token可能耗尽), 关闭思考重试...")
        try:
            j2 = post(dict(base, max_tokens=2000, thinking={"type": "disabled"}))
            text = ((j2.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        except Exception as e:
            raise RuntimeError(f"视觉模型返回空内容, 关思考重试失败: {e}")
        if not text:
            raise RuntimeError("视觉模型返回空内容")
    return text

try:
    from lidaxiao import audit as _audit
except Exception:
    _audit = None


def check_vision(cfg):
    """自检视觉通道: 打印每个 provider 的可用性 (手动排障用, 定时任务不再调用)"""
    vcfg = cfg.get("vision") or {}
    if not vcfg.get("enabled", True):
        log("片尾视觉已禁用 (config.json vision.enabled=false), 跳过预检")
        return True
    ok_any = False
    frames = []
    for d in sorted(glob.glob(os.path.join(WORKDIR, "data", "frames", "*"))):
        fs = sorted(glob.glob(os.path.join(d, "*.jpg")))
        if fs:
            frames = fs[-1:]
            break
    if not frames:
        # 无现成帧时用一张合成图自检 (640x360, 足够大以通过格式校验)
        try:
            from PIL import Image, ImageDraw
            tmp = os.path.join(WORKDIR, "data", "vision_preflight.png")
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            im = Image.new("RGB", (640, 360), "white")
            ImageDraw.Draw(im).text((40, 160), "VISION CHECK 2026", fill="black")
            im.save(tmp)
            frames = [tmp]
        except Exception as e:
            log(f"❌ 无法生成预检图: {e}")
            return False
    for pcfg in vision_providers(cfg):
        provider = pcfg.get("provider", "")
        key = load_llm_key(provider)
        if not key:
            log(f"⏭ 视觉预检 {provider}: 无密钥")
            continue
        try:
            t0 = time.time()
            if provider == VISION_CLI_PROVIDER:
                text = _call_dsv(pcfg, key, "这张图里有什么文字? 直接回答, 不要解释。",
                                 frames[:1])
            else:
                text = _call_vision_once(pcfg, key, "这张图里有什么文字? 直接回答, 不要解释。",
                                         frames[:1])
            log(f"✅ 视觉预检 {provider} ({pcfg.get('model', '')}) 可用 "
                f"{time.time() - t0:.1f}s: {text[:60]!r}")
            ok_any = True
        except Exception as e:
            log(f"❌ 视觉预检 {provider} ({pcfg.get('model', '')}) 不可用: {e}")
    if not ok_any:
        log("⚠️ 没有可用的视觉 provider (片尾识别将跳过, 主流程不受影响)")
    return ok_any

def _call_ollama_native(pcfg, system, user):
    """Ollama 走原生 /api/chat, 确保 num_gpu/ngl 等参数真正生效"""
    import requests
    base = pcfg["base_url"].rstrip("/")
    if base.endswith("/v1"):
        api_url = base[:-3] + "/api/chat"
    else:
        api_url = base + "/api/chat"
    options = {}
    option_map = {
        "n_cpu_moe": "num_cpu_moe",
        "num_batch": "num_batch",
        "num_ubatch": "num_ubatch",
        "cache_ram": "cache_ram",
    }
    for cfg_key, api_key in option_map.items():
        val = pcfg.get(cfg_key)
        if val is None or val == "":
            continue
        # 兼容 "30" 这种字符串数字, Ollama 要求整数
        try:
            val = int(val)
        except (TypeError, ValueError):
            pass
        options[api_key] = val
    options["num_predict"] = pcfg.get("max_tokens", 4000)
    options["temperature"] = pcfg.get("temperature", 0.3)
    payload = {
        "model": pcfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": options,
    }
    r = requests.post(api_url, json=payload, timeout=pcfg.get("timeout", 180))
    try:
        r.raise_for_status()
    except requests.HTTPError:
        raise RuntimeError(f"Ollama API {r.status_code}: {r.text[:500]}")
    j = r.json()
    return (j.get("message", {}).get("content") or "").strip()

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

def _call_llm_once(pcfg, api_key, system, user):
    """向单个 LLM provider 发起一次 Chat Completions 请求"""
    if pcfg.get("mode") == "json":
        from local import local_render
        return local_render.run(pcfg, user)
    if pcfg.get("provider") == "ollama":
        return _call_ollama_native(pcfg, system, user)
    import requests
    _warn_model(pcfg)
    url = pcfg["base_url"].rstrip("/") + "/chat/completions"
    if pcfg.get("provider") == FREE_PROVIDER:
        # 免密车道: 非流式必挂, 走流式 + 指纹头 + decoy tools
        return _free_lane_post(pcfg, {
            "model": pcfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": pcfg.get("max_tokens", 4000),
        }, pcfg.get("timeout", 180))
    payload = {
        "model": pcfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": pcfg.get("max_tokens", 4000),
        "stream": False,
    }
    # 先按普通模式放 temperature, 再由思考模式参数决定是否移除
    payload["temperature"] = pcfg.get("temperature", 0.3)
    if pcfg.get("provider") in ("local", "llama") and pcfg.get("enable_thinking") is False:
        # MiniCPM5 混合思考: 关掉, 否则容易把 token 全花在 reasoning 上、正文为空
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    _apply_thinking(payload, pcfg)
    # Ollama: 透传 MoE/批处理/缓存参数, 等价于 llama-server 的对应 flag
    if pcfg.get("provider") == "ollama":
        ollama_options = {}
        option_map = {
            "n_cpu_moe": "num_cpu_moe",
            "num_batch": "num_batch",
            "num_ubatch": "num_ubatch",
            "cache_ram": "cache_ram",
        }
        for cfg_key, api_key in option_map.items():
            val = pcfg.get(cfg_key)
            if val:
                ollama_options[api_key] = val
        if ollama_options:
            payload["options"] = ollama_options
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = requests.post(url, json=payload, timeout=pcfg.get("timeout", 180), headers=headers)
    r.raise_for_status()
    j = r.json()
    msg = j["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        reason = msg.get("reasoning_content") or ""
        raise RuntimeError(f"返回空 content (reasoning {len(reason)} chars)")
    return content

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

def _find_dsc_path(pcfg):
    """定位 deepseek-chat-cli 的 dsc.py"""
    return _find_cli_path(pcfg, "dsc.py", "deepseek-chat-cli")

def _find_dsv_path(pcfg):
    """定位 deepseek-vision-cli 的 dsv.py"""
    return _find_cli_path(pcfg, "dsv.py", "deepseek-vision-cli")

def _repo_slug(repo):
    s = repo.rstrip("/").removesuffix(".git")
    parts = s.split("/")
    return "/".join(parts[-2:])

def _clone_dsc(pcfg):
    """克隆 deepseek-chat-cli (私有仓库, 优先 gh 认证, 其次 git + token)"""
    repo = pcfg.get("repo", "https://github.com/menghuanshiguang/deepseek-chat-cli.git")
    rel = pcfg.get("path", "_repo/deepseek-chat-cli")
    dest = rel if os.path.isabs(rel) else os.path.join(WORKDIR, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # 1) gh repo clone (本地已 gh 登录时最稳)
    try:
        subprocess.run(["gh", "repo", "clone", _repo_slug(repo), dest],
                       check=True, capture_output=True, timeout=180)
        return True
    except Exception as e:
        log(f"⚠️ gh clone 失败: {e}")
    # 2) git clone + token
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        try:
            token = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                   text=True, timeout=30).stdout.strip()
        except Exception:
            token = ""
    try:
        if token:
            auth_url = repo.replace("https://", f"https://oauth2:{token}@")
            subprocess.run(["git", "clone", "--depth", "1", auth_url, dest],
                           check=True, capture_output=True, timeout=180)
        else:
            subprocess.run(["git", "clone", "--depth", "1", repo, dest],
                           check=True, capture_output=True, timeout=180)
        return True
    except Exception as e:
        log(f"⚠️ git clone 失败: {e}")
    return False

def _clone_dsv(pcfg):
    """克隆 deepseek-vision-cli (公开仓库, 零额外依赖: iSH 用系统 minis-browser-use / Windows 用 playwright+Edge)"""
    repo = pcfg.get("repo", "https://github.com/menghuanshiguang/deepseek-vision-cli.git")
    rel = pcfg.get("path", "_repo/deepseek-vision-cli")
    dest = rel if os.path.isabs(rel) else os.path.join(WORKDIR, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        subprocess.run(["git", "clone", "--depth", "1", repo, dest],
                       check=True, capture_output=True, timeout=180)
        return True
    except Exception as e:
        log(f"⚠️ git clone deepseek-vision-cli 失败: {e}")
    return False

def _call_dsc(pcfg, token, prompt):
    """调用 deepseek-chat-cli (dsc.py): 网页版登录 token 走浏览器对话"""
    dsc_py = _find_dsc_path(pcfg)
    if not dsc_py:
        log("⏬ deepseek-chat-cli 未找到, 尝试克隆...")
        if not _clone_dsc(pcfg):
            raise RuntimeError("deepseek-chat-cli 克隆失败")
        dsc_py = _find_dsc_path(pcfg)
        if not dsc_py:
            raise RuntimeError("克隆后仍未找到 dsc.py")

    tok = (token or "").strip().lstrip("\ufeff")
    if not tok:
        # 兜底: 直接读仓库自带的 .dsv_token
        repo_tok = os.path.join(os.path.dirname(dsc_py), ".dsv_token")
        if os.path.exists(repo_tok):
            with open(repo_tok, "r", encoding="utf-8-sig") as f:
                tok = f.read().strip()
    if not tok:
        raise RuntimeError("deepseek-chat-cli: 未找到 DSV_TOKEN")

    # dsc.py 固定读 ~/.dsv_token, 把系统变量写进去
    token_path = os.path.expanduser("~/.dsv_token")
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(tok)

    # 根因修复: 每次调用使用全新浏览器 profile, 避免连续多次后 dsc 检测退化导致超时
    import uuid
    profile_dir = os.path.join(WORKDIR, "data", "dsc_profiles", uuid.uuid4().hex)
    os.makedirs(profile_dir, exist_ok=True)

    cmd = [sys.executable, os.path.abspath(dsc_py), prompt]
    log(f"调用 deepseek-chat-cli ({os.path.basename(dsc_py)})...")
    # dsc.py 支持 DSC_TIMEOUT 环境变量控制回答等待时长; DSV_EDGE_PROFILE 指定本次全新 profile
    env = dict(os.environ)
    env["DSC_TIMEOUT"] = str(pcfg.get("timeout", 300))
    env["DSV_EDGE_PROFILE"] = profile_dir
    try:
        res = subprocess.run(cmd, cwd=os.path.dirname(dsc_py), capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=pcfg.get("timeout", 300) + 60, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("deepseek-chat-cli 调用超时")
    finally:
        # 用完即删, 避免 data/dsc_profiles 无限膨胀
        shutil.rmtree(profile_dir, ignore_errors=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"deepseek-chat-cli 失败 exit={res.returncode}: {(res.stderr or res.stdout)[-500:]}")
    out = (res.stdout or "").strip()
    if not out:
        raise RuntimeError("deepseek-chat-cli 返回空内容")
    # dsc.py 超时时会输出 "(超时未获取回答)" 且退出码为 0, 必须识别为失败并继续兜底
    if "超时未获取回答" in out or "未获取回答" in out:
        raise RuntimeError("deepseek-chat-cli 超时未获取回答")
    return out

def _call_dsv(pcfg, token, prompt, images):
    """调用 deepseek-vision-cli (dsv.py): DeepSeek 网页版「识图模式」, 多帧一次会话发送

    images: 本地图片路径列表 (帧文件), 与 prompt 一起作为 dsv.py 参数。
    """
    dsv_py = _find_dsv_path(pcfg)
    if not dsv_py:
        log("⏬ deepseek-vision-cli 未找到, 尝试克隆...")
        if not _clone_dsv(pcfg):
            raise RuntimeError("deepseek-vision-cli 克隆失败")
        dsv_py = _find_dsv_path(pcfg)
        if not dsv_py:
            raise RuntimeError("克隆后仍未找到 dsv.py")

    tok = (token or "").strip().lstrip("\ufeff")
    if not tok:
        # 兜底: 直接读仓库自带的 .dsv_token
        repo_tok = os.path.join(os.path.dirname(dsv_py), ".dsv_token")
        if os.path.exists(repo_tok):
            with open(repo_tok, "r", encoding="utf-8-sig") as f:
                tok = f.read().strip()
    if not tok:
        raise RuntimeError("deepseek-vision-cli: 未找到 DSV_TOKEN")

    # dsv.py 固定读 ~/.dsv_token, 把系统变量写进去 (与 deepseek-chat-cli 共用同一登录态)
    with open(os.path.expanduser("~/.dsv_token"), "w", encoding="utf-8") as f:
        f.write(tok)

    # 每次调用使用全新浏览器 profile, 避免连续多次调用后检测退化导致超时 (与 dsc 同策略)
    import uuid
    profile_dir = os.path.join(WORKDIR, "data", "dsv_profiles", uuid.uuid4().hex)
    os.makedirs(profile_dir, exist_ok=True)

    cmd = [sys.executable, os.path.abspath(dsv_py)]
    cmd += [os.path.abspath(p) for p in images]
    cmd.append(prompt)
    log(f"调用 deepseek-vision-cli ({os.path.basename(dsv_py)}, {len(images)}帧)...")
    # dsv.py 支持 DSV_TIMEOUT 控制回答等待时长; DSV_EDGE_PROFILE 指定本次全新 profile (Windows)
    env = dict(os.environ)
    env["DSV_TIMEOUT"] = str(pcfg.get("timeout", 240))
    env["DSV_EDGE_PROFILE"] = profile_dir
    try:
        res = subprocess.run(cmd, cwd=os.path.dirname(dsv_py), capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=pcfg.get("timeout", 240) + 90, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("deepseek-vision-cli 调用超时")
    finally:
        # 用完即删, 避免 data/dsv_profiles 无限膨胀
        shutil.rmtree(profile_dir, ignore_errors=True)
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip()[-500:]
        if res.returncode == 4:
            raise RuntimeError(f"deepseek-vision-cli: DSV_TOKEN 无效/缺失, 需重新登录: {tail}")
        if res.returncode == 5:
            raise RuntimeError(f"deepseek-vision-cli: 另一个 dsv 正在运行(锁占用): {tail}")
        raise RuntimeError(f"deepseek-vision-cli 失败 exit={res.returncode}: {tail}")
    out = (res.stdout or "").strip()
    if not out:
        raise RuntimeError("deepseek-vision-cli 返回空内容")
    # dsv.py 超时/被拒时会以退出码 0 输出提示文本, 必须识别为失败并继续兜底
    if "超时未获取回答" in out or "未获取回答" in out:
        raise RuntimeError("deepseek-vision-cli 超时未获取回答")
    if "DeepSeek拒绝处理" in out:
        raise RuntimeError(f"deepseek-vision-cli 内容被拒: {out[:200]}")
    return out

def validate_report(text, reference=""):
    """本地小模型结构校验: 7 段 + 结尾声明都要在, 否则视为不合格回退云端

    reference 传入原始 prompt(含字幕), 用来抽查数字是否来自本期内容,
    防止小模型把历史/别期的数据张冠李戴。
    """
    t = (text or "").strip()
    miss = [s for s in REPORT_SECTIONS if s not in t]
    if len(t) < 400:
        miss.append("内容过短")
    if REPORT_TAIL not in t:
        miss.append("缺结尾声明")
    # 复读提示词 / 模板占位没替换
    for bad in ("每条必须包含原文数据", "格式: N.", "【输出模板】", "3-8条"):
        if bad in t:
            miss.append("复读提示词")
            break
    if re.search(r"(?m)^N\.\s", t):
        miss.append("模板占位N未替换")
    # 数字溯源: 输出里的多位数应当能在本期 prompt(字幕)里找到
    if reference:
        nums = [n for n in re.findall(r"\d+(?:\.\d+)?(?:%|万亿|亿|万)?", t)
                if len(n.rstrip("%万亿亿点")) >= 2]
        if nums:
            hit = sum(1 for n in set(nums) if n in reference)
            if hit / len(set(nums)) < 0.5:
                miss.append(f"数据不在本期字幕({hit}/{len(set(nums))})")
    return (not miss), miss

def _local_llm_alive(pcfg, timeout=3):
    """本地 llama.cpp 探活: 没起就不进 provider 链, 免得每次白等 900s 超时"""
    import requests
    base = (pcfg.get("base_url") or "").rstrip("/")
    if not base:
        return False
    root = base[:-3] if base.endswith("/v1") else base
    # llama.cpp 有 /health; Ollama 只有 /v1/models 或 /api/tags
    for url in (root + "/health", base.rstrip("/") + "/models", root + "/api/tags"):
        try:
            if requests.get(url, timeout=timeout).status_code < 400:
                return True
        except Exception:
            continue
    return False

def call_llm(cfg, api_key, system, user, dsc_prompt=None):
    """按顺序尝试 LLM provider; 本地模式(cfg.local_ollama)只走 Ollama qwen2.5:7b

    文本优先级(固定): opencode-go -> deepseek-chat-cli -> DeepSeek 官方 API
    (即 cfg.llm -> cfg.llm_cli -> cfg.llm_fallback, 顺序由 providers 列表决定)
    """
    if cfg.get("local_ollama"):
        providers = [cfg.get("llm_ollama")]
    else:
        providers = []
        local = cfg.get("llm_local")
        if (local and local.get("enabled") and _local_llm_alive(local)
                and local.get("position", "first") == "first"):
            providers.append(local)            # 本地优先: 校验不过自动回落云端
        free = cfg.get("llm_free")
        if free and free.get("enabled", True):
            providers.append(free)             # 免费层最前, 挂了自动落到下面三级
        providers.append(cfg.get("llm"))
        cli = cfg.get("llm_cli")
        if cli and cli.get("provider") != cfg.get("llm", {}).get("provider"):
            providers.append(cli)
        fallback = cfg.get("llm_fallback")
        if fallback and fallback.get("provider") != cfg.get("llm", {}).get("provider"):
            providers.append(fallback)
        if (local and local.get("enabled") and local.get("position") == "last"
                and _local_llm_alive(local)):
            providers.append(local)            # 可选: position=last 时退居兜底
    NO_KEY_PROVIDERS = {"ollama", "local", "llama"}
    last_err = ""
    for pcfg in providers:
        if not pcfg:
            continue
        provider = pcfg.get("provider", "opencode-go")
        key = load_llm_key(provider) or api_key
        if not key and provider not in NO_KEY_PROVIDERS:
            last_err = f"{provider}: 未找到 API key/token"
            log(f"⚠️ LLM {provider}: {last_err}, 跳过")
            continue
        try:
            if provider == "deepseek-chat-cli":
                log(f"调用 LLM {provider}...")
                out = _call_dsc(pcfg, key, dsc_prompt or f"{system}\n\n{user}")
            else:
                log(f"调用 LLM {provider} ({pcfg.get('model', '')})...")
                out = _call_llm_once(pcfg, key, system, user)
            # 任何 provider 返回超时/未获取回答都视为失败, 继续下一个兜底
            if "超时未获取回答" in out or "未获取回答" in out:
                raise RuntimeError(f"{provider} 返回超时未获取回答")
            if provider in ("local", "llama") and pcfg.get("validate", True):
                ref = user
                ok, miss = validate_report(out, ref)
                if not ok:
                    tag = "本地模型" if provider in ("local", "llama") else provider
                    log(f"⚠️ {tag} 输出不完整({', '.join(miss)}), 换下一个通道")
                    last_err = f"{provider}: 校验未过 {miss}"
                    continue
            return out
        except Exception as e:
            last_err = f"{provider}: {e}"
            log(f"❌ LLM {provider} 失败: {e}")
    raise RuntimeError(last_err or "没有可用的 LLM provider")

def load_history(cfg, days=5):
    """取最近 days 天的报告原文(截断), 供观点连续性对比"""
    rdir = os.path.join(WORKDIR, "reports")
    if not os.path.isdir(rdir):
        return ""
    files = sorted(glob.glob(os.path.join(rdir, "*.md")))
    if not files:
        return ""
    cutoff = datetime.datetime.now(CN_TZ) - datetime.timedelta(days=days)
    parts = []
    for f in files:
        try:
            d = datetime.datetime.strptime(os.path.basename(f)[:10], "%Y-%m-%d")
            if d.replace(tzinfo=CN_TZ) >= cutoff:
                parts.append(read_text(f))
        except ValueError:
            pass
    return "\n\n".join(parts)[-6000:]

def clean_subtitle_for_prompt(subtitle_text):
    """去掉 [MM:SS] 时间戳和换行, 用中文逗号连接, 适合 C 端对话"""
    import re
    lines = []
    for ln in (subtitle_text or "").splitlines():
        s = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", ln).strip()
        if s:
            lines.append(s)
    return "，".join(lines)

def build_dsc_prompt(video, subtitle_text, history, ending=None):
    """构造给 deepseek-chat-cli 的自然 C 端提示词(保持原输出格式)"""
    date = datetime.datetime.fromtimestamp(video.get("pubdate", 0), CN_TZ).strftime("%Y-%m-%d")
    clean_sub = clean_subtitle_for_prompt(subtitle_text)[:12000]
    ending_block = ""
    if ending and (ending.get("text") or "").strip():
        ending_block = ("\n视频片尾画面(视觉识别, 视频最后几十秒的结束语/卡片):\n"
                        f"{ending['text'].strip()[:1500]}\n")
    return f"""你好，请以资深财经分析的角度，帮我分析一下李大霄这条视频的内容，并按下面的 Markdown 格式输出（全部使用简体中文）：

视频日期：{date}
视频标题：{video['title']}
视频时长：{video.get('dur_str', '')}

视频字幕（已去掉时间戳）：
{clean_sub}
{ending_block}
最近 5 天的历史报告（供你对比观点是否延续/升级/新增/反转）：
{history or "(无历史报告)"}

请按以下格式输出：
【一句话摘要】用一句话概括本期视频的核心结论
【核心观点】3-8条, 每条必须包含原文数据(点位/百分比/日期等), 格式: N. 观点 (原文数据: ...)
【关键数据清单】列出视频中出现的具体数据: 美债收益率/巴菲特指标/见顶时间表/指数点位/成交量等, 格式: - 项目: 数值（必须带单位; 纯日期/"周六周日"/"康波周期"不算数据; 没有写"本期无量化数据"）
【暗示提取】逐条列出视频中带暗示性质的话术并判断方向, 每条标注: 🔴警示 / 🟢看多 / ⚪中性 / ⚠️风险; reason 必须引用该条原文的关键要素, 禁止多条复用同一句。注意"不是推荐"是李大霄的核心反话, 要逐条看语境: 光杆点名型(主动拎出某标的、无跌幅铺垫, 如历史兑现的韩国/恒科/银行/券商)多为隐性推荐, 按标的属性给方向; 报跌幅型("XX跌了X%, 不是推荐", 尤其海外行情/板块复盘语境)就是字面意思——风险提示该板块, 标🔴警示, 绝不写成"关注价值/超跌机会"; 片尾免责声明里的"并非推荐"标⚪中性。禁止一律套用"实则暗示关注", 禁止虚构"暗示意味最强"之类强度评语。若上面给了片尾画面, 末尾按顺序补两条: 一条以"片尾:"开头(文字/卡片信号), 一条以"片尾图形:"开头(画面图形隐喻, 按"形态 → 含义 → 方向"写, 方向用 🔴高位风险/看空、🟢低位机会/看多)
【市场定性】反弹/反转/见顶/调整/防御/无明确判断 之一 + 依据。依据必须引用本期字幕里的点位/涨跌幅/量能/估值等具体证据; 纯理念/生活/投教类且无数值证据时必须写"无明确判断, 本期无数值依据"。注意他的核心框架: "没量=不是反转"。
【操作含义】三行, 必须可执行:
  动作: 加仓/减仓/持有/观望/不适用 之一
  仓位: 具体比例(如"3成仓") 或 "不适用"
  触发: 可执行条件(如"放量站上3200点") 或 "不适用"
  禁止"适度/合理/注意/长期持有"这类无法执行的词。其中方向、仓位、风险里的关键词（如看多/看空/观望、加仓/减仓、进攻/防御等）请用 ~关键词~ 这种波浪号标记标出（例如 ~防御~、~看空~、~减仓~），我会在后处理时转成加粗。
【反照抄】历史报告仅用于【观点连续性】, 严禁把历史报告的结论/措辞/案例搬进本期摘要、定性或操作。
【观点连续性】与历史报告对比: 标注 延续/升级/新增/反转。如态度升级(如"警惕→高度警惕")必须重点标出。无历史报告则写"首次记录,无历史对比"。

最后必须另起一行输出：以上为李大霄个人观点提炼,不构成投资建议"""

def _tnorm(t):
    return re.sub(r"[\s,，。.!！?？、]", "", t or "")

def _tdedup(items, key):
    out = []
    for it in items:
        k = key(it)
        if not k:
            continue
        for i, prev in enumerate(out):
            if k == key(prev):
                if len(it) > len(prev):
                    out[i] = it
                break
        else:
            out.append(it)
    return out

def tighten_analysis(text, subtitle="", history=""):
    """审计收敛: 去重 / 数据规范 / 操作三件套 / 定性证据与照抄 / 去视觉泄漏"""
    t = (text or "").strip()
    t = re.sub(r"\s*[(（]视觉[:：][^)）]*[)）]", "", t)

    def blk(name):
        return re.search(rf"【{name}】(.*?)(?=【|\Z)", t, re.S)

    m = blk("核心观点")
    if m:
        rows = re.findall(r"(?m)^\s*\d+\.\s*(.+)$", m.group(1))
        if rows:
            kept = _tdedup(rows, lambda x: _tnorm(re.split(r"[(（]原文数据", x)[0])[:40])
            bod = "\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(kept, 1)) + "\n"
            t = t[:m.start(1)] + bod + t[m.end(1):]

    m = blk("暗示提取")
    if m:
        rows = re.findall(r"(?m)^\s*\d+\.\s*(.+)$", m.group(1))
        if rows:
            seen, kept = set(), []
            for r in rows:
                q = re.search(r"“(.+?)”", r)
                k = _tnorm(q.group(1)) if q else _tnorm(r)[:30]
                if k in seen:
                    continue
                seen.add(k)
                kept.append(re.sub(r"[:：]\s*(李大霄的提醒|明确提醒某个风险点.*)$", "", r))
            bod = "\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(kept, 1)) + "\n"
            t = t[:m.start(1)] + bod + t[m.end(1):]

    m = blk("关键数据清单")
    if m:
        rows = [r.strip().rstrip(",，") for r in re.findall(r"(?m)^\s*-\s*(.+)$", m.group(1))]
        good = []
        for r in rows:
            if not re.search(r"\d", r) or re.fullmatch(r"\d{4}年.*", r):
                continue
            if re.match(r"^(周六|周日|康波)", r) or (":" not in r and "：" not in r):
                continue
            good.append(r)
        good = _tdedup(good, lambda x: _tnorm(x))
        bod = "\n" + ("\n".join(f"- {x}" for x in good) if good else "- 本期无量化数据") + "\n"
        t = t[:m.start(1)] + bod + t[m.end(1):]

    m = blk("市场定性")
    if m:
        val = m.group(1).strip()
        kinds = ("反弹", "反转", "见顶", "调整", "防御", "无明确判断")
        kind = next((k for k in kinds if k in val[:12]), "")
        has_ev = bool(re.search(r"\d+(\.\d+)?(%|点|万亿|亿|万|倍)", subtitle or ""))
        reason = re.sub(r"^.*?[:：]", "", val).strip()
        copied = bool(history) and len(reason) >= 8 and re.sub(r"[\s，。,.、]", "", reason) in re.sub(r"[\s，。,.、]", "", history)
        if not has_ev:
            val = "无明确判断：本期无点位/涨跌幅/量能等数值依据"
        elif copied:
            val = "无明确判断：定性理由与历史报告重复(疑似照抄)，本期无独立依据"
        elif not kind:
            val = "无明确判断：" + (reason or "依据不足")
        t = t[:m.start(1)] + "\n" + val + "\n" + t[m.end(1):]

    m = blk("操作含义")
    if m:
        body = m.group(1)
        d = re.search(r"方向[*\s]*[:：]\s*(.+)", body)
        pz = re.search(r"仓位[*\s]*[:：]\s*(.+)", body)
        rk = re.search(r"风险[*\s]*[:：]\s*(.+)", body)
        act = (d.group(1).strip() if d else "")
        if "观望" in act: act = "观望"
        elif "空" in act: act = "减仓"
        elif "多" in act: act = "持有"
        elif act in ("加仓","减仓","持有","不适用"): pass
        else: act = "不适用"
        pos = (pz.group(1).strip() if pz else "不适用")
        if not re.search(r"\d", pos) or re.search(r"(长期持有|适度|合理|注意)", pos):
            pos = "不适用"
        trig = (rk.group(1).strip() if rk else "不适用")
        if re.search(r"(适度|合理|注意)", trig):
            trig = "不适用"
        t = t[:m.start(1)] + f"\n- **动作**: {act}\n- **仓位**: {pos}\n- **触发**: {trig}\n" + t[m.end(1):]

    if _audit:
        t = _audit.fix_point_context(t)
        t = _audit.fix_continuity(t, history)
    return re.sub(r"\n{3,}", "\n\n", t).strip()

def analyze_subtitles(cfg, api_key, video, subtitle_text, history, ending=None):
    date = datetime.datetime.fromtimestamp(video.get("pubdate", 0), CN_TZ).strftime("%Y-%m-%d")
    ending_block = ""
    if ending and (ending.get("text") or "").strip():
        ending_block = ("\n【片尾画面(视觉识别, 视频最后几十秒)】\n"
                        f"{ending['text'].strip()[:2000]}\n")
    user = f"""【视频信息】
日期: {date}
标题: {video['title']}
时长: {video.get('dur_str', '')}

【字幕文本】
{subtitle_text[:12000]}
{ending_block}
【历史报告(最近5天, 供观点连续性对比)】
{history or "(无历史报告)"}

请按模板输出分析。"""
    sys_prompt = ANALYZE_SYSTEM
    dsc_prompt = build_dsc_prompt(video, subtitle_text, history, ending=ending)
    out = call_llm(cfg, api_key, sys_prompt, user, dsc_prompt=dsc_prompt)
    return tighten_analysis(out, subtitle_text, history)

THINKING_EFFORTS = ("low", "high", "max")



def frame_data_url(path, max_width=1280, quality=85):
    """帧 -> data:image/jpeg;base64 (降采样省 token)"""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        if max_width and im.width > max_width:
            h = max(1, round(im.height * max_width / im.width))
            im = im.resize((max_width, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

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
