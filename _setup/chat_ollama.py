# -*- coding: utf-8 -*-
"""Ollama 交互对话客户端：自动带上 num_cpu_moe / num_batch / num_ubatch / cache_ram"""
import json
import os
import sys

import requests

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "http://localhost:11434/api/chat"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import ctypes
except ImportError:
    ctypes = None


def _disable_quickedit():
    """关闭控制台 QuickEdit, 防止鼠标点击选中文本后输出冻结"""
    if ctypes is None:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10
        ENABLE_EXTENDED_FLAGS = 0x0080
        ENABLE_QUICK_EDIT_MODE = 0x0040
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
            kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


def load_model_and_options():
    with open(os.path.join(WORKDIR, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    ollama = cfg.get("llm_ollama", {})
    model = ollama.get("model", "batiai/qwen3.6-35b:iq3")
    options = {}
    mapping = {
        "n_cpu_moe": "num_cpu_moe",
        "num_batch": "num_batch",
        "num_ubatch": "num_ubatch",
        "cache_ram": "cache_ram",
    }
    for cfg_key, api_key in mapping.items():
        val = ollama.get(cfg_key)
        if val is None or val == "":
            continue
        # 兼容 "30" 这种字符串数字, Ollama 要求整数
        try:
            val = int(val)
        except (TypeError, ValueError):
            pass
        options[api_key] = val
    options["num_predict"] = ollama.get("max_tokens", 8000)
    return model, options


def main():
    _disable_quickedit()
    model, options = load_model_and_options()
    print("=" * 60)
    print("Ollama 对话客户端")
    print(f"模型: {model}")
    print(f"参数: {options}")
    print("输入内容直接对话；输入 q / exit 退出")
    print("=" * 60)

    history = []
    while True:
        try:
            user = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break
        if not user:
            continue
        if user.lower() in ("q", "quit", "exit"):
            print("再见")
            break
        history.append({"role": "user", "content": user})
        payload = {
            "model": model,
            "messages": history,
            "stream": False,
            "options": options,
        }
        print("模型> ", end="", flush=True)
        try:
            r = requests.post(URL, json=payload, timeout=1800)
            r.raise_for_status()
            content = (r.json().get("message", {}).get("content") or "").strip()
            print(content)
            history.append({"role": "assistant", "content": content})
        except requests.exceptions.HTTPError as e:
            detail = ""
            if e.response is not None:
                detail = e.response.text[:500]
            print(f"\n[错误] {e} {detail}")
            history.pop()
        except Exception as e:
            print(f"\n[错误] {e}")
            history.pop()


if __name__ == "__main__":
    sys.exit(main())
