# -*- coding: utf-8 -*-
"""Xiaomi-OCR-0 提速实验: 常驻 server + 图像 token 上限 + 生成上限。

用法: python bench/bench_ocr_fast.py --tag base --extra "" 
      由 workflow 循环调用, 每个配置起一个 server, 测完杀掉。
"""
import argparse
import base64
import glob
import json
import os
import statistics
import tempfile
import subprocess
import time
import urllib.request

MODEL = os.environ.get("OCR_MODEL", "models/xiaomi-ocr0-q4km.gguf")
MMPROJ = os.environ.get("OCR_MMPROJ", "models/xiaomi-ocr0-mmproj.gguf")
LLAMA_DIR = os.environ.get("LLAMA_DIR", "")
PORT = int(os.environ.get("PORT", "8099"))
PROMPT = "Extract the text in the image. Output only the text."
REPEAT = int(os.environ.get("REPEAT", "3"))


def start_server(extra):
    exe = os.path.join(LLAMA_DIR, "llama-server")
    if not os.path.exists(exe) and os.path.exists(exe + ".exe"):
        exe += ".exe"
    cmd = [exe, "-m", MODEL, "--mmproj", MMPROJ, "-c", "4096",
           "--host", "127.0.0.1", "--port", str(PORT), "--jinja", "--no-webui"]
    if extra:
        cmd += extra
    log = open(os.path.join(tempfile.gettempdir(), f"server_{PORT}.log"), "w")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
            return p
        except Exception:
            time.sleep(1)
    raise SystemExit("server 起不来, 见 /tmp/server_%d.log" % PORT)


def ocr(img, max_tokens):
    b64 = base64.b64encode(open(img, "rb").read()).decode()
    body = {
        "model": "x",
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": PROMPT}]}],
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    dt = time.time() - t0
    txt = (d["choices"][0]["message"].get("content") or "").strip()
    txt = txt.split("<im_start/>")[0].split("</im_start/>")[0].strip()
    usage = d.get("usage", {})
    return dt, txt, usage.get("completion_tokens", 0)


def crop_srcs():
    from PIL import Image
    out = []
    for i, p in enumerate(sorted(glob.glob("bench/visionset/*.jpg"))):
        im = Image.open(p)
        w, h = im.size
        d = os.path.join(tempfile.gettempdir(), f"v2_{i}.jpg")
        im.crop((0, int(h * 0.70), w, int(h * 0.97))).save(d, quality=92)
        out.append((os.path.basename(p), d))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--extra", default="")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--full", action="store_true", help="用整帧而不是字幕条")
    a = ap.parse_args()
    extra = a.extra.split() if a.extra.strip() else []

    srcs = [(n, p) for n, p in
            ((os.path.basename(x), x) for x in sorted(glob.glob("bench/visionset/*.jpg")))] \
        if a.full else crop_srcs()

    srv = start_server(extra)
    try:
        times, texts = [], []
        for rep in range(REPEAT):
            for name, path in srcs:
                dt, txt, ct = ocr(path, a.max_tokens)
                if rep > 0:            # 首轮含预热, 不计入
                    times.append(dt)
                texts.append((rep, name, dt, txt, ct))
        med = statistics.median(times) if times else 0
        avg = sum(times) / len(times) if times else 0
        line = (f"| {a.tag} | {a.max_tokens} | {avg:.2f} | {med:.2f} "
                f"| {min(times):.2f} | {max(times):.2f} | {avg*240/60:.1f} |")
        print("\nRESULT", line, flush=True)
        with open("bench/out/ocr-fast.md", "a", encoding="utf-8") as f:
            f.write(line + "\n")
            for rep, name, dt, txt, ct in texts:
                f.write(f"<!-- {a.tag} r{rep} {name} {dt:.2f}s {ct}tok {txt[:80]!r} -->\n")
                print(f"  r{rep} {name} {dt:.2f}s {ct}tok {txt[:60]!r}", flush=True)
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
