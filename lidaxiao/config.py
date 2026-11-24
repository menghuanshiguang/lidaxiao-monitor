"""配置与密钥加载 (从 monitor.py / lidaxiao/analyze/llm.py 抽出)

依赖只有 lidaxiao.common —— 本模块在依赖链最底层 (common -> config -> ...),
不允许反向 import 任何上层模块。
"""
import json
import os

from lidaxiao.common import WORKDIR

DEFAULT_UID = "2137589551"          # 李大霄

LLM_PROVIDER_KEY_ENVS = {
    "opencode-go": ("OPENCODE_GO_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "deepseek-chat-cli": ("DSV_TOKEN", "DEEPSEEK_CHAT_CLI_TOKEN"),
    "deepseek-vision-cli": ("DSV_TOKEN",),
}

LLM_KEY_ENVS = ("OPENCODE_GO_API_KEY", "DEEPSEEK_API_KEY", "DSV_TOKEN")

FREE_PROVIDER = "opencode-zen-free"        # 旧: 直连 Zen 免密车道(指纹头)
FREE_GATEWAY_PROVIDER = "free-gateway"     # 新: 本机 dsh-opencode-free 网关(OpenAI 兼容, 无鉴权)


def _env_or_envfile(name):
    """从环境变量或工作目录 .env 文件读取单个密钥"""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    envf = os.path.join(WORKDIR, ".env")
    if os.path.exists(envf):
        with open(envf, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                # 精确匹配变量名, 避免 DSV_TOKEN 误匹配 DSV_TOKEN2 等同前缀键
                key, val = line.split("=", 1)
                if key.strip() == name:
                    return val.strip().strip("'\"")
    return ""


def load_llm_key(provider=None):
    """密钥优先级: 环境变量 > .env 文件; provider=None 时按 OPENCODE_GO_API_KEY > DEEPSEEK_API_KEY 顺序"""
    if provider == FREE_PROVIDER:
        return "public"        # 免密车道固定 token, 走 OpenCode 指纹头校验
    if provider == FREE_GATEWAY_PROVIDER:
        return "local"         # 本机网关无鉴权, 这个值不会被发出去
    names = LLM_PROVIDER_KEY_ENVS.get(provider, LLM_KEY_ENVS) if provider else LLM_KEY_ENVS
    for name in names:
        v = _env_or_envfile(name)
        if v:
            return v
    return ""


def load_deepseek_key():
    """向后兼容别名 (旧脚本/旧密钥名仍可用)"""
    return load_llm_key()


def load_config(path=None):
    cfg = {
        "uid": DEFAULT_UID,
        "limit": 10,                    # 每次最多检查/处理多少条
        "frame_interval": 0.0,          # 0=自适应 (时长/240, 限1~4秒)
        "ocr_confidence": 0.5,
        "min_subtitle_chars": 30,       # 字幕文本少于该长度视为"无字幕"
        "llm_free": {                           # 免费车道: 本机 dsh-opencode-free 网关 (OpenAI 兼容, 无鉴权)
            "enabled": True,
            "provider": FREE_GATEWAY_PROVIDER,
            "base_url": "http://127.0.0.1:18787/v1",
            "model": "",                        # 空 = 运行时 GET /v1/models 取第一个 -free (不写死)
            "temperature": 0.3,
            "max_tokens": 32768,                # 与旧直连车道一致: 思考关不掉, 正文要留足
            "timeout": 300,
        },
        "llm": {
            "provider": "opencode-go",          # OpenCode Zen Go (https://opencode.ai/zen/go)
            "base_url": "https://opencode.ai/zen/go/v1",
            "model": "deepseek-flash",
            "reasoningEffort": "max",           # 思考强度: low/high/max (旧值 minimal/medium/xhigh/ultra 会被官方映射)
            "temperature": 0.3,
            "max_tokens": 8000,
        },
        "llm_fallback": {                       # OpenCode Go 失败时的 DeepSeek 兜底
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            # 官方最新模型名: deepseek-flash (DeepSeek-V4.1-Flash)。
            # 旧名 deepseek-chat / deepseek-v4-flash 已下线, 请求会被路由到 V4.1 Flash。
            "model": "deepseek-flash",
            "reasoningEffort": "medium",        # 思考强度: low/high/max (旧名 medium 会被抬到 high)
            "temperature": 0.3,                 # 思考模式下官方 API 会忽略该参数
            "max_tokens": 4000,
        },
        "llm_cli": {                            # 第二兜底: deepseek-chat-cli (DeepSeek 网页版登录 token)
            "provider": "deepseek-chat-cli",
            "repo": "https://github.com/menghuanshiguang/deepseek-chat-cli.git",
            "path": "_repo/deepseek-chat-cli",
            "token_env": "DSV_TOKEN",
            "timeout": 300,
        },
        "llm_ollama": {                         # 本地模式: Ollama (无 key)
            "provider": "ollama",
            "base_url": "http://localhost:11434/v1",
            "model": "batiai/qwen3.6-35b:iq3",
            "temperature": 0.3,
            "max_tokens": 8000,
            "timeout": 600,
        },
        "llm_local": {                          # 本地权重 (llama.cpp OpenAI 兼容), 校验不过自动回退云端
            "enabled": False,
            "provider": "local",
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "minicpm5-2b",
            "temperature": 0.3,
            "max_tokens": 3000,
            "timeout": 900,
            "enable_thinking": False,           # MiniCPM5 混合思考: 关掉防止全花在 reasoning 上
            "validate": True,                   # 结构校验(6 段 + 结尾声明), 不过就回退
        },
        "vision_free": {                        # 片尾识别第一层: MiMo 免费视觉车道 (无 key)
            "enabled": True,
            "provider": "opencode-zen-free",
            "base_url": "https://opencode.ai/zen/v1",
            "model": "mimo-v2.6-flash-free",
            "max_tokens": 8000,                 # 思考不可关, 需留足思考 token
            "timeout": 240,
        },
        "vision": {                             # 片尾画面识别 (deepseek-flash 视觉能力)
            "enabled": True,
            "provider": "opencode-go",
            "base_url": "https://opencode.ai/zen/go/v1",
            "model": "deepseek-flash",          # 官方唯一支持图像理解的模型
            "max_tokens": 6000,                 # 思考模式下需留足思考token, 否则返回空
            "timeout": 240,
            "tail_seconds": 45,                 # 片尾扫描窗口(秒)
            "max_frames": 6,                    # 送模型的最大帧数(覆盖不同场景)
            "max_width": 1280,                  # 帧降采样宽度(省token)
            "jpeg_quality": 85,
        },
        "vision_cli": {                         # 片尾识别第三级: deepseek-vision-cli (DeepSeek 网页版「识图模式」)
            "enabled": True,
            "provider": "deepseek-vision-cli",
            "repo": "https://github.com/menghuanshiguang/deepseek-vision-cli.git",
            "path": "_repo/deepseek-vision-cli",
            "token_env": "DSV_TOKEN",
            "model": "deepseek-vision-web",     # 仅报告展示用 (网页版无模型名)
            "timeout": 240,                     # 回答等待上限(秒), 经 DSV_TIMEOUT 传给 dsv.py
        },
        "vision_fallback": {                    # 视觉兜底: DeepSeek 官方 API (同一模型)
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
            "max_tokens": 6000,
            "timeout": 240,
        },
        "dl_retries": 1,                # 下载失败额外重试次数
        "llm_retries": 3,
    }
    p = path or os.path.join(WORKDIR, "config.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            user = json.load(f)
        # 先深合并嵌套段再整体更新: cfg.update(user) 会把嵌套 dict 整体替换掉,
        # 导致后面的 cfg[k].update(user[k]) 变成 no-op, 用户配置缺省字段的默认值全部丢失
        for k in ("llm_free", "llm", "llm_fallback", "llm_cli", "llm_ollama", "llm_local",
                  "vision_free", "vision", "vision_cli", "vision_fallback"):
            if isinstance(user.get(k), dict):
                cfg[k].update(user.pop(k))
        cfg.update(user)
    # 本地模式开关: 环境变量 LIDAXIAO_LOCAL=1 或 config.json local_ollama=true
    cfg["local_ollama"] = bool(user.get("local_ollama", False)) if os.path.exists(p) else False
    if os.environ.get("LIDAXIAO_LOCAL") == "1":
        cfg["local_ollama"] = True
    return cfg
