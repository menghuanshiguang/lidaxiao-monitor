# -*- coding: utf-8 -*-
"""Xiaomi-OCR-0 (0.8B) 常驻推理客户端。

设计要点:
  * llama-server 由本模块拉起并在进程生命周期内常驻 —— GitHub Actions 每个 step
    结束会杀掉后台进程, 所以不能跨 step 启动, 必须和调用方同进程。
  * 任何失败都返回空结果并累计; 连续失败达到阈值后 `dead=True`, 调用方回退 RapidOCR。
  * 输出与 RapidOCR 兼容: [(box, text, score)], box 是 4 点坐标。
"""
import atexit
import base64
import json
import os
import subprocess
import time
import urllib.request

PROMPT_DEFAULT = "Extract the text in the image. Output only the text."
CUTS = ("<im_start", "</im_start", "<|im_end|>", "<|endoftext|>")


def _clean(t):
    if not t:
        return ""
    t = t.replace("<think>", "").replace("</think>", "").strip()
    for c in CUTS:
        i = t.find(c)
        if i >= 0:
            t = t[:i]
    return t.strip()


class XiaomiOCR:
    provides_boxes = True      # 返回值已是 (y, x, text, score), 不走 RapidOCR 的解析分支

    def __init__(self, conf, log=print):
        self.conf = conf or {}
        self.log = log
        self.proc = None
        self.dead = False
        self.fails = 0
        self.port = int(self.conf.get("port", 8099))
        self.max_tokens = int(self.conf.get("max_tokens", 192))
        self.prompt = self.conf.get("prompt") or PROMPT_DEFAULT
        self.timeout = float(self.conf.get("timeout", 180))
        self.base = f"http://127.0.0.1:{self.port}"

    # ---------- 生命周期 ----------
    def start(self):
        bin_ = self.conf.get("server_bin", "")
        model = self.conf.get("model", "")
        mmproj = self.conf.get("mmproj", "")
        if not os.path.exists(bin_):
            alt = bin_[:-4] if bin_.endswith(".exe") else bin_ + ".exe"
            if os.path.exists(alt):
                bin_ = alt
        if not (os.path.exists(bin_) and os.path.exists(model) and os.path.exists(mmproj)):
            self.log(f"Xiaomi-OCR: 文件缺失 bin={bin_} model={model} mmproj={mmproj}")
            return False
        th = int(self.conf.get("threads", 0) or 0)
        cmd = [bin_, "-m", model, "--mmproj", mmproj,
               "-c", str(self.conf.get("ctx", 2048))]
        if th > 0:                      # <=0 表示交给 llama.cpp 按物理核数自决
            cmd += ["-t", str(th)]
        cmd += [
               "--host", "127.0.0.1", "--port", str(self.port),
               "--jinja", "--no-webui", "-np", "1"]
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, **kw)
        except Exception as e:
            self.log(f"Xiaomi-OCR 启动异常: {e}")
            return False
        t0 = time.time()
        while time.time() - t0 < 120:
            if self.proc.poll() is not None:
                self.log("Xiaomi-OCR 进程提前退出")
                return False
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=2):
                    self.log(f"✅ Xiaomi-OCR 就绪 ({time.time()-t0:.1f}s)")
                    atexit.register(self.stop)
                    return True
            except Exception:
                time.sleep(1.0)
        self.log("Xiaomi-OCR 启动超时")
        self.stop()
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    # ---------- 推理 ----------
    def __call__(self, img_bgr):
        """img: cv2 BGR ndarray -> [(box, text, score)]"""
        if self.dead or self.proc is None:
            return []
        try:
            import cv2
            ok, buf = cv2.imencode(".jpg", img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if not ok:
                return []
            b64 = base64.b64encode(buf.tobytes()).decode()
            body = {
                "model": "ocr",
                "temperature": 0,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": self.prompt}]}],
            }
            req = urllib.request.Request(
                self.base + "/v1/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                d = json.load(r)
            txt = _clean((d["choices"][0]["message"].get("content") or ""))
            self.fails = 0
        except Exception as e:
            self.fails += 1
            if self.fails >= 3:
                self.dead = True
                self.log(f"Xiaomi-OCR 连续失败 {self.fails} 次, 回退 RapidOCR: {e}")
            return []
        h, w = img_bgr.shape[:2]
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        out = []
        for i, ln in enumerate(lines):
            y = int(i * h / max(1, len(lines)))
            out.append((y, 0, ln, 1.0))     # 与 ocr_frame 的 (y, x, text, score) 对齐
        return out
