# -*- coding: utf-8 -*-
"""Xiaomi-OCR-0 字幕识别测速。

对 bench/visionset/*.jpg 逐张跑 OCR，记录单帧耗时（冷启动/热态分开），
可选跑 RapidOCR 做基线对比。
"""
import glob
import json
import os
import re
import subprocess
import sys
import time

LLAMA_DIR = os.environ.get("LLAMA_DIR", "")
MODEL = os.environ.get("OCR_MODEL", "models/xiaomi-ocr0-q4km.gguf")
MMPROJ = os.environ.get("OCR_MMPROJ", "models/xiaomi-ocr0-mmproj.gguf")
PROMPT = "Extract the text in the image."
N_PREDICT = int(os.environ.get("OCR_N_PREDICT", "128"))

LOG_PAT = re.compile(
    r"^(\s*)(llama|ggml|mtmd|load_|print_info|init:|main:|clip_|sched|"
    r"build:|system_info|sampling|generate:|chat_template|using |"
    r"image |encoding|decoding|n_|common_|\.\.\.|$)"
    r"|^\s*[\d.]+\s+(I|W|E)\s",
    re.M)


def find_bin(name):
    for cand in (name, name.replace("-mtmd-cli", "-cli")):
        p = os.path.join(LLAMA_DIR, cand)
        if os.path.exists(p):
            return p, cand != name
    raise SystemExit(f"找不到 {name} (LLAMA_DIR={LLAMA_DIR})")


def clean(out):
    keep = []
    for ln in out.splitlines():
        s = ln.strip()
        if not s or LOG_PAT.match(ln):
            continue
        if re.match(r"^[\d.]+\s*(ms|s) per", s):
            continue
        keep.append(s)
    return "\n".join(keep).strip()


def run_one(img, use_mmproj=True):
    exe, _fb = find_bin("llama-mtmd-cli")
    cmd = [exe, "-m", MODEL, "--image", img, "-p", PROMPT,
           "-n", str(N_PREDICT), "--temp", "0", "-t", "4", "-c", "2048"]
    if use_mmproj and os.path.exists(MMPROJ):
        cmd[3:3] = ["--mmproj", MMPROJ]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    dt = time.time() - t0
    txt = clean((r.stdout or "") + "\n" + (r.stderr or ""))
    return dt, txt


def rapidocr_baseline(imgs):
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as e:
        return None, f"(RapidOCR 不可用: {e})"
    engine = RapidOCR()
    rows = []
    for p in imgs:
        t0 = time.time()
        res, _ = engine(p)
        dt = time.time() - t0
        txt = "\n".join(x[1] for x in (res or []))
        rows.append((os.path.basename(p), dt, txt))
    return rows, ""


def crop_band(src, dst, y0=0.70, y1=0.97):
    """裁出字幕条: 生产管线只把底部字幕带送 OCR"""
    from PIL import Image
    im = Image.open(src)
    w, h = im.size
    im.crop((0, int(h * y0), w, int(h * y1))).save(dst, quality=92)
    return dst


def bench_set(imgs, tag, use_crop=False):
    rows = []
    for i, p in enumerate(imgs):
        path = p
        if use_crop:
            dst = os.path.join("/tmp", f"crop_{i}.jpg")
            path = crop_band(p, dst)
        try:
            dt, txt = run_one(path)
        except Exception as e:
            print(f"  [{tag}] {p} 失败: {e}")
            continue
        rows.append((os.path.basename(p), dt, txt))
        print(f"  [{tag}] {os.path.basename(p)} {dt:.2f}s {len(txt)}字: {txt[:40]!r}", flush=True)
    return rows


def main():
    imgs = sorted(glob.glob("bench/visionset/*.jpg"))
    if not imgs:
        raise SystemExit("bench/visionset 为空")
    rows = bench_set(imgs, "全帧")
    rows_c = bench_set(imgs, "字幕条", use_crop=True)
    fails = []

    warm = [r[1] for r in rows[1:]] or [0]
    avg_warm = sum(warm) / len(warm)
    cold = rows[0][1] if rows else 0

    rrows, rerr = rapidocr_baseline(imgs) if os.environ.get("WITH_RAPIDOCR") else ([], "")
    ravg = (sum(r[1] for r in rrows) / len(rrows)) if rrows else 0
    warm_c = [r[1] for r in rows_c[1:]] or [0]
    avg_c = sum(warm_c) / len(warm_c)
    cold_c = rows_c[0][1] if rows_c else 0

    lines = ["# Xiaomi-OCR-0 字幕识别测速", "",
             f"- 模型: `{os.path.basename(MODEL)}` + `{os.path.basename(MMPROJ)}`",
             f"- 测试帧: {len(imgs)} 张 (bench/visionset, 480x846 硬字幕)",
             f"- 冷启动首帧: **{cold:.2f}s** (含加载)",
             f"- 热态平均: **{avg_warm:.2f}s/帧**",
             f"- 4 分钟视频(240 帧)估算: **{avg_warm * 240 / 60:.1f} 分钟**", "",
             f"- 裁字幕条后: 冷 {cold_c:.2f}s / 热 **{avg_c:.2f}s/帧** → 240 帧约 "
             f"{avg_c * 240 / 60:.1f} 分钟", ""]
    if rrows:
        lines += [f"- RapidOCR 基线: **{ravg:.2f}s/帧** → 240 帧约 {ravg*240/60:.1f} 分钟", ""]
    elif rerr:
        lines += [f"- {rerr}", ""]
    lines += ["| 帧 | 耗时 | 识别字数 | 输出预览 |", "|---|---|---|---|"]
    for name, dt, txt in rows:
        prev = txt.replace("|", "/").replace("\n", " ")[:70]
        lines.append(f"| {name} | {dt:.2f}s | {len(txt)} | {prev} |")
    if fails:
        lines += ["", "## 失败", ""] + [f"- {f}" for f in fails]
    lines += ["", "## 字幕条识别结果", "", "| 帧 | 耗时 | 文字 |", "|---|---|---|"]
    for name, dt, txt in rows_c:
        lines.append(f"| {name} | {dt:.2f}s | {txt.replace('|','/').replace(chr(10),' ')[:90]} |")
    if rrows:
        lines += ["", "## RapidOCR 对照", "", "| 帧 | 耗时 | 文字 |", "|---|---|---|"]
        for name, dt, txt in rrows:
            lines.append(f"| {name} | {dt:.2f}s | {txt.replace('|','/').replace(chr(10),' ')[:70]} |")
    out = "\n".join(lines) + "\n"
    os.makedirs("bench/out", exist_ok=True)
    open("bench/out/ocr-bench.md", "w", encoding="utf-8").write(out)
    print("\n" + out)
    json.dump({"cold": cold, "warm_avg": avg_warm, "crop_avg": avg_c,
               "rapidocr_avg": ravg,
               "rows": [(n, round(d, 3)) for n, d, _ in rows]},
              open("bench/out/ocr-bench.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
