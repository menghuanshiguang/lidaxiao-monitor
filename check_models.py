#!/usr/bin/env python3
"""模型自检: 用真实密钥实测 DeepSeek 官方通道(文本 + 视觉), 打印实际请求体与返回。

不打印密钥本身。作为一次性验证脚本, 验证完即从仓库删除。
用法: DEEPSEEK_API_KEY=xxx python check_models.py
"""
import importlib.util
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
D = os.path.dirname(os.path.abspath(__file__))


def load_monitor():
    spec = importlib.util.spec_from_file_location("mon", os.path.join(D, "monitor.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["mon"] = m
    spec.loader.exec_module(m)
    return m


def main():
    mon = load_monitor()
    cfg = mon.load_config(os.path.join(D, "config.json"))

    print("=" * 70)
    print("配置中的模型名")
    print("=" * 70)
    for sec in ("llm", "llm_fallback", "vision", "vision_fallback"):
        p = cfg.get(sec, {})
        print("  %-16s provider=%-12s model=%-16s effort=%s"
              % (sec, p.get("provider"), p.get("model"), p.get("reasoningEffort", "-")))

    # ---------- 文本通道 ----------
    print()
    print("=" * 70)
    print("文本通道: DeepSeek 官方 API")
    print("=" * 70)
    pcfg = dict(cfg["llm_fallback"])
    key = mon.load_llm_key("deepseek") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("  ❌ 没有 DEEPSEEK_API_KEY")
        return 1

    payload = {"model": pcfg["model"], "messages": [], "max_tokens": pcfg.get("max_tokens", 4000),
               "stream": False, "temperature": pcfg.get("temperature", 0.3)}
    mon._apply_thinking(payload, pcfg)
    print("  实际请求体关键字段:")
    for k in ("model", "temperature", "thinking", "reasoning_effort", "max_tokens"):
        print("     %-18s %s" % (k, payload.get(k, "<未发送>")))
    try:
        out = mon._call_llm_once(pcfg, key,
                                 "你是简洁的中文助手, 只回答被问的内容, 不要客套。",
                                 "用一句话说明什么是A股。")
        print("  ✅ 返回 %d 字: %s" % (len(out), out[:200].replace("\n", " ")))
    except Exception as e:
        print("  ❌ 调用失败: %s: %s" % (type(e).__name__, str(e)[:400]))
        return 1

    # ---------- 视觉通道(官方兜底) ----------
    print()
    print("=" * 70)
    print("视觉通道: DeepSeek 官方 API (片尾识图)")
    print("=" * 70)
    vcfg = dict(cfg["vision_fallback"])
    print("  provider=%s model=%s" % (vcfg.get("provider"), vcfg.get("model")))
    frames = []
    for d in sorted(__import__("glob").glob(os.path.join(D, "data", "frames", "*"))):
        fs = sorted(__import__("glob").glob(os.path.join(d, "*.jpg")))
        if fs:
            frames = fs[-1:]
            break
    if not frames:
        print("  没有现成帧, 合成一张测试图")
        tmp = os.path.join(D, "data", "vision_preflight.png")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (640, 360), "white")
        ImageDraw.Draw(im).text((40, 160), "VISION CHECK 2026", fill="black")
        im.save(tmp)
        frames = [tmp]
    try:
        text = mon._call_vision_once(vcfg, key, "这张图里有什么文字? 直接回答, 不要解释。",
                                     frames[:1])
        print("  ✅ 返回 %d 字: %s" % (len(text), text[:200].replace("\n", " ")))
    except Exception as e:
        print("  ❌ 调用失败: %s: %s" % (type(e).__name__, str(e)[:400]))
        return 1

    print()
    print("=" * 70)
    print("全部通道实测通过 ✅")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
