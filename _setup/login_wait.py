#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站扫码登录辅助脚本 (Windows)
- 生成二维码 HTML(自动刷新,最多 30 分钟),用户用 B站 App 扫码确认
- 轮询登录状态,成功后把 cookies 保存为 Netscape 格式:
    1) %USERPROFILE%/.cache/bilibili-login-cookies.txt  (bilidown 默认读取位置)
    2) data/cookies.txt                                  (监控脚本读取位置)
用法: python _setup/login_wait.py
"""
import base64
import io
import os
import sys
import time

import requests
import qrcode

# Windows 控制台默认 GBK, 强制 UTF-8 输出, 避免 emoji 打印崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 工作目录
HOME = os.path.expanduser("~")
COOKIE_DEFAULT = os.path.join(HOME, ".cache", "bilibili-login-cookies.txt")
COOKIE_BACKUP = os.path.join(BASE, "data", "cookies.txt")
QR_HTML = os.path.join(BASE, "login_qr.html")
STATUS_LOG = os.path.join(BASE, "data", "login_status.log")

os.makedirs(os.path.dirname(COOKIE_DEFAULT), exist_ok=True)
os.makedirs(os.path.dirname(COOKIE_BACKUP), exist_ok=True)
os.makedirs(os.path.dirname(STATUS_LOG), exist_ok=True)


def log(msg):
    line = time.strftime("[%H:%M:%S] ") + msg
    print(line, flush=True)
    with open(STATUS_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_qr_html(qr_url, path):
    img = qrcode.make(qr_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>B站扫码登录 - 李大霄监控</title>
<style>
body{{font-family:sans-serif;text-align:center;background:#f5f5f5;padding:20px}}
.card{{background:#fff;max-width:440px;margin:40px auto;padding:30px;border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,.1)}}
h2{{color:#fb7299}} p{{color:#666}}
img{{width:320px;height:320px;border:1px solid #eee;border-radius:10px}}
.tip{{font-size:13px;color:#999;margin-top:10px}}
</style></head>
<body><div class="card">
<h2>B站扫码登录</h2>
<p>用 <b>B站 App</b> 扫一扫,确认登录</p>
<img src="data:image/png;base64,{b64}"/>
<p class="tip">登录成功后 cookies 自动保存,本页可关闭</p>
<p class="tip" id="t"></p>
</div>
<script>
let n=0;
setInterval(()=>{{document.getElementById('t').textContent='本二维码已生成 '+ (++n) + ' 秒,失效后请刷新本页重新获取';}},1000);
</script></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def netscape_dump(session, path):
    """把 session cookies 写成 Netscape 格式文件"""
    lines = ["# Netscape HTTP Cookie File"]
    for c in session.cookies:
        domain = c.domain
        if domain.startswith("."):
            include_sub = "TRUE"
        else:
            include_sub = "FALSE"
        secure = "TRUE" if c.secure else "FALSE"
        expires = int(c.expires) if c.expires else 0
        lines.append("\t".join([domain, include_sub, c.path, secure, str(expires), c.name, c.value]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    deadline = time.time() + 30 * 60  # 最长 30 分钟
    log("开始 B站扫码登录流程(最长30分钟,二维码自动刷新)")
    while time.time() < deadline:
        # 1. 获取二维码
        try:
            r = s.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate", timeout=10)
            d = r.json()["data"]
            qrcode_key, qr_url = d["qrcode_key"], d["url"]
        except Exception as e:
            log(f"获取二维码失败: {e}, 5秒后重试")
            time.sleep(5)
            continue
        write_qr_html(qr_url, QR_HTML)
        log(f"二维码已生成: {QR_HTML} (请用B站App扫码)")
        # 2. 轮询, 每个二维码最多 100 秒
        polled = 0
        while polled < 100 and time.time() < deadline:
            time.sleep(3)
            polled += 3
            try:
                r = s.get("https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                          params={"qrcode_key": qrcode_key}, timeout=10)
                j = r.json()
                code = j["data"]["code"]
            except Exception as e:
                log(f"轮询异常: {e}")
                continue
            if code == 0:
                # 先兑换并保存 cookies, 再打印成功信息(避免中途崩溃丢失登录态)
                cross = j["data"].get("url", "")
                if cross:
                    try:
                        s.get(cross, timeout=15)
                    except Exception as e:
                        print(f"[{time.strftime('%H:%M:%S')}] crossDomain 请求异常: {e}", flush=True)
                netscape_dump(s, COOKIE_DEFAULT)
                netscape_dump(s, COOKIE_BACKUP)
                names = [c.name for c in s.cookies]
                log("✅ 扫码确认成功, 登录完成!")
                log(f"✅ cookies 已保存: {COOKIE_DEFAULT}")
                log(f"   备份: {COOKIE_BACKUP}  含cookie: {names}")
                return 0
            elif code == 86038:
                log("二维码已失效, 重新生成...")
                break
            elif code == 86090:
                log("已扫码, 请在手机上点击确认...")
            else:
                pass  # 等待扫码
        else:
            continue
    log("❌ 登录超时(30分钟), 请重新运行 _setup/login_wait.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())
