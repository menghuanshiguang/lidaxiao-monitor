"""微博视频直链下载 —— 微博这条线上唯一会写盘、唯一直连 CDN 的模块。

探针结论 (2026-11-26) 决定了这里的每一行:

1. 直链就在时间线响应里 (`page_info.urls.mp4_720p_mp4` → `mp4_hd` → `mp4_ld`,
   兜底 `media_info.stream_url`), **不需要二次解析**。
2. 链接约 3600 秒过期, 所以只能当次取当次下 —— 绝不把 URL 存进 state, 也不跨
   run 复用。下载失败不落任何"已处理"标记, 下一轮重新拉时间线拿新链接。
3. CDN 只认 `Referer: https://m.weibo.cn/`: 去掉 → 403 (146 B), `HEAD` 也 403。
   头从 `weibo.media_headers()` 取, 与取数层同一套, 不在这里另写一份。
4. 先写 `<key>.mp4.part`, 收完字节数对得上才 `os.replace` 成正式文件: 截断的 mp4
   容器头仍报完整时长, 会让 ffmpeg 提前结束、字幕不全, 比明确失败更难发现。
"""
import os
import time
import urllib.error
import urllib.request

from lidaxiao.common import WORKDIR
from lidaxiao.sources.merge import weibo_key
from lidaxiao.sources.weibo import media_headers
from lidaxiao.textutil import log

#: 单块读大小。微博短视频普遍 10~30 MB, 1 MB 一块够省内存。
CHUNK = 1 << 20
TIMEOUT = 60


def weibo_download_dir(key):
    return os.path.join(WORKDIR, "downloads", key)


def find_downloaded_weibo(key):
    """已有的完整 mp4 (只认 .mp4, `.part` 半截文件不会被当成成品)。"""
    if not key:
        return None
    d = weibo_download_dir(key)
    if not os.path.isdir(d):
        return None
    path = os.path.join(d, f"{key}.mp4")
    return path if os.path.exists(path) else None


def _fetch_to_file(url, key, attempt=0):
    """带 Referer 拉一遍直链 -> 完整 mp4 路径; 任何失败返回 None (清掉半截文件)。"""
    outdir = weibo_download_dir(key)
    final = os.path.join(outdir, f"{key}.mp4")
    part = final + ".part"
    try:
        os.makedirs(outdir, exist_ok=True)
        req = urllib.request.Request(url, headers=media_headers())
        got = 0
        expected = 0
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            try:
                expected = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                expected = 0
            with open(part, "wb") as fh:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
        if got <= 0:
            raise ValueError("响应 0 字节")
        if expected and got < expected:
            raise ValueError(f"只收到 {got}/{expected} B")
        os.replace(part, final)
        log(f"✅ 微博下载完成: {final} ({got} B)")
        return final
    except urllib.error.HTTPError as exc:
        log(f"微博下载失败(第{attempt + 1}次) HTTP {exc.code}: {url[:60]}")
    except Exception as exc:
        log(f"微博下载失败(第{attempt + 1}次) {type(exc).__name__}: {exc}")
    try:
        os.remove(part)                      # 半截文件必须清, 免得下次被当成成品
    except OSError:
        pass
    return None


def download_weibo_video(cfg, item, retries=None):
    """下载一条微博视频 -> 本地 mp4 路径 或 None。

    直链随响应一起给, 过期即失效, 所以每轮重试都**重新取当前 item 里的 URL**
    (调用方每轮都重新拉时间线, 拿到的是新链接)。
    """
    cfg = cfg or {}
    key = weibo_key(item)
    if not key:
        log("❌ 微博条目没有 id, 无法下载")
        return None
    existing = find_downloaded_weibo(key)
    if existing:
        log(f"视频已存在, 跳过下载: {existing}")
        return existing
    retries = int(cfg.get("dl_retries", 1) if retries is None else retries)
    attempts = 1 + max(retries, 0)
    url = str((item or {}).get("video_url") or "")
    if not url.startswith("http"):
        log(f"❌ 微博直链缺失: {key}")
        return None
    for i in range(attempts):
        path = _fetch_to_file(url, key, i)
        if path:
            return path
        if i + 1 < attempts:
            time.sleep(5)
    log(f"❌ 微博下载全部失败: {key}")
    return None
