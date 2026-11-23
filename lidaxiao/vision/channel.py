"""片尾视觉通道: provider 选择 / 单次多图请求 / dsv CLI 调用 / 自检

从 analyze/llm.py 搬入 (原 vision -> analyze 临时反向边就此消除)。
函数体与搬入前逐字节相同; analyze 不再 import 本模块。
"""
import base64
import glob
import io
import os
import shutil
import subprocess
import sys
import time

from lidaxiao.common import WORKDIR
from lidaxiao.config import FREE_PROVIDER, load_llm_key
from lidaxiao.llmio import (
    _apply_thinking, _find_cli_path, _free_lane_post, _warn_model)
from lidaxiao.textutil import log

VISION_CLI_PROVIDER = "deepseek-vision-cli"


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


def _find_dsv_path(pcfg):
    """定位 deepseek-vision-cli 的 dsv.py"""
    return _find_cli_path(pcfg, "dsv.py", "deepseek-vision-cli")


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
