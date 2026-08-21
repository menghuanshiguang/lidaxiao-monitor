# -*- coding: utf-8 -*-
"""Ollama 交互对话客户端：自动带上 num_cpu_moe / num_batch / num_ubatch / cache_ram"""
import json
import os
import sys

import requests

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "http://localhost:11434/v1/chat/completions"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
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
        if val:
            options[api_key] = val
    return model, options


def main():
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
            content = r.json()["choices"][0]["message"]["content"].strip()
            print(content)
            history.append({"role": "assistant", "content": content})
        except Exception as e:
            print(f"\n[错误] {e}")
            history.pop()


if __name__ == "__main__":
    sys.exit(main())
