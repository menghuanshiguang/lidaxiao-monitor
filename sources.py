"""数据源: B站更新检测 / API / 下载 (含 Range 兜底) —— 从 monitor.py 抽出"""
import json
import os
import re
import glob
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from common import FFMPEG_DIR, WORKDIR
from ocr import ffprobe_duration
from textutil import log, read_text

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SPACE_ITEM_RE = re.compile(r"^\s*(\d+)\.\s*\[([\d:]+)\]\s*(.+?)\s*$")
SPACE_BV_RE = re.compile(r"^\s*BV:\s*([A-Za-z0-9]+)")

BASH = os.environ.get("BILIDOWN_BASH", r"D:\Program Files\Git\bin\bash.exe")
BILIDOWN_SCRIPT = os.path.join(WORKDIR, "_repo", "bin", "bilidown")


def run_bilidown(cfg, args, timeout=600):
    """调用 bilidown CLI: 优先 v2.0+ Python 单文件版, 兼容旧 bash 版"""
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([FFMPEG_DIR, env.get("PATH", "")])
    py_script = os.path.join(WORKDIR, "_repo", "bin", "bilidown.py")
    if os.path.exists(py_script):
        cmd = [sys.executable, py_script] + args          # v2.0+: python bilidown.py
        log(f"运行: bilidown {' '.join(args)}")
    elif os.path.exists(BILIDOWN_SCRIPT):
        env["HOME"] = os.path.expanduser("~")
        extra = [os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Python",
                              "Python312", "Scripts")]
        env["PATH"] = os.pathsep.join(extra + [env["PATH"]])
        cmd = [BASH, BILIDOWN_SCRIPT] + args              # 旧版: bash bin/bilidown
        log(f"运行: bilidown(bash) {' '.join(args)}")
    else:
        log("❌ 未找到 bilidown: 请克隆 https://github.com/menghuanshiguang/bilibili-downloader-cli 到 _repo/")
        return 127, "", ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"bilidown 超时({timeout}s): {args}")
        return 124, "", ""
    return r.returncode, r.stdout, r.stderr

def load_cookies_dict():
    """从 data/cookies.txt 或 ~/.cache/bilibili-login-cookies.txt 读 Netscape cookies"""
    paths = [os.path.join(WORKDIR, "data", "cookies.txt"),
             os.path.join(os.path.expanduser("~"), ".cache",
                          "bilibili-login-cookies.txt")]
    ck = {}
    for p in paths:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        ck[parts[5]] = parts[6]
            if ck:
                break
    return ck

def bili_api_get(url, params=None, referer=None):
    ck = load_cookies_dict()
    qs = urllib.parse.urlencode(params or {})
    full = url + ("?" + qs if qs else "")
    req = urllib.request.Request(full, headers={
        "User-Agent": UA,
        "Referer": referer or "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Cookie": "; ".join(f"{k}={v}" for k, v in ck.items()),
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"B站API请求失败 {url}: {e}")
        return None

def fetch_video_info(bvid):
    """GET /x/web-interface/view -> title/pubdate/duration"""
    j = bili_api_get("https://api.bilibili.com/x/web-interface/view",
                     {"bvid": bvid})
    if j and j.get("code") == 0 and j.get("data"):
        d = j["data"]
        return {
            "title": d.get("title", ""),
            "pubdate": d.get("pubdate", 0),
            "duration": d.get("duration", 0),
        }
    return None

def get_space_videos(cfg, count):
    """bilidown space -> [{bvid,title,dur_str,pubdate}] 最新在前"""
    rc, out, err = run_bilidown(cfg, ["space", cfg["uid"], str(count)], timeout=120)
    if rc != 0:
        log(f"space 命令失败 rc={rc}\nstderr: {err[-500:]}")
        return []
    videos = []
    cur = None
    for line in out.splitlines():
        m = SPACE_ITEM_RE.match(line)
        if m:
            cur = {"bvid": "", "title": m.group(3).strip(), "dur_str": m.group(2)}
            videos.append(cur)
            continue
        m2 = SPACE_BV_RE.match(line)
        if m2 and cur is not None:
            cur["bvid"] = m2.group(1)
    result = [v for v in videos if v["bvid"]]
    # 补 pubdate: 日期过滤需要 (bilidown space 不返回时间)
    for v in result:
        info = fetch_video_info(v["bvid"])
        if info:
            v["pubdate"] = info.get("pubdate", 0)
            v["title"] = info.get("title", v.get("title", ""))
            v["duration"] = info.get("duration", 0)
        else:
            v["pubdate"] = 0
    return result

def find_downloaded_mp4(bvid):
    d = os.path.join(WORKDIR, "downloads", bvid)
    if not os.path.isdir(d):
        return None
    for f in sorted(glob.glob(os.path.join(d, "*.mp4"))):
        if not os.path.basename(f).startswith("."):
            return f
    return None

def _direct_download(bvid):
    """直链兜底下载: B站 CDN 对机房 IP 要求带 Range 头, 否则 567/403

    走 playurl(platform=html5) 的 durl 单文件(480p, 音视频已合并),
    用 Range 流式落盘, 不需要 ffmpeg 合并。
    """
    import requests
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    try:
        spi = s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=10).json()
        b3 = (spi.get("data") or {}).get("b_3")
        if b3:
            s.cookies.set("buvid3", b3, domain=".bilibili.com")
        view = s.get("https://api.bilibili.com/x/web-interface/view",
                     params={"bvid": bvid}, timeout=15).json()
        cid = view["data"]["cid"]
        j = s.get("https://api.bilibili.com/x/player/playurl",
                  params={"bvid": bvid, "cid": cid, "qn": 32, "fnval": 1,
                          "platform": "html5", "high_quality": 1}, timeout=15).json()
        durl = ((j.get("data") or {}).get("durl") or [])
        if not durl:
            log(f"兜底下载: API 未返回 durl (code={j.get('code')} {j.get('message')})")
            return None
        url = durl[0]["url"]
        outdir = os.path.join(WORKDIR, "downloads", bvid)
        os.makedirs(outdir, exist_ok=True)
        dest = os.path.join(outdir, f"{bvid}.mp4")
        tmp = dest + ".part"
        r = s.get(url, headers={"Range": "bytes=0-"}, stream=True, timeout=60)
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        # 206 部分内容: 必须确认 Range 覆盖了整个文件, 否则会把半截视频当成功
        expect = total
        if r.status_code == 206:
            crange = (r.headers.get("Content-Range") or "").strip()
            m_range = re.fullmatch(r"bytes\s+0-(\d+)/(\d+)", crange)
            if not m_range:
                log(f"兜底下载: 206 但 Content-Range 异常 ({crange!r}), 丢弃")
                return None
            end, full = int(m_range.group(1)), int(m_range.group(2))
            if end + 1 != full:
                log(f"兜底下载: 只覆盖 {end + 1}/{full}B, 丢弃")
                return None
            expect = full
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)
        size = os.path.getsize(tmp)
        if size < 200 * 1024:
            os.remove(tmp)
            log(f"兜底下载: 文件过小 {size}B, 丢弃")
            return None
        if expect and size != expect:
            os.remove(tmp)
            log(f"兜底下载: 长度不符 {size}/{expect}B, 丢弃")
            return None
        os.replace(tmp, dest)
        log(f"✅ 兜底下载完成(Range): {dest} {size/1048576:.1f}MB"
            + (f" / {total/1048576:.1f}MB" if total else ""))
        return dest
    except Exception as e:
        msg = re.sub(r"https?://\S+", "[URL]", str(e))
        log(f"兜底下载失败: {type(e).__name__}: {msg}")
        return None

def download_video(cfg, bvid, retries=None):
    """bilidown dl <BV> video <outdir> mp4 480 1, 返回本地mp4路径或None"""
    retries = cfg["dl_retries"] if retries is None else retries
    outdir = os.path.join(WORKDIR, "downloads", bvid)
    attempts = 1 + retries
    for i in range(attempts):
        existing = find_downloaded_mp4(bvid)
        if existing:
            log(f"视频已存在, 跳过下载: {existing}")
            return existing
        rc, out, err = run_bilidown(
            cfg, ["dl", bvid, "video", outdir, "mp4", "480", "1"],
            timeout=900)
        ok = rc == 0
        if not ok:
            log(f"下载失败(第{i+1}/{attempts}次) rc={rc}\n"
                f"stdout: {out[-600:]}\nstderr: {err[-600:]}")
            if i + 1 < attempts:
                time.sleep(5)
            continue
        existing = find_downloaded_mp4(bvid)
        if existing:
            # 成功时也记下 CDN 回退过程: 哪条直链不可达、最终由哪个备用源供流
            for line in out.splitlines():
                if "直链失败" in line or "备用源可用" in line:
                    log(line.strip())
            log(f"✅ 下载完成: {existing}")
            return existing
        log(f"bilidown 返回成功但未找到mp4 (第{i+1}次)\n{out[-300:]}\n{err[-300:]}")
    log("bilidown 全部失败, 改用直链兜底(Range)")
    return _direct_download(bvid)

def expected_duration(video):
    """期望时长(秒): B站 API duration 优先, 其次解析 dur_str ("06:21")"""
    d = float(video.get("duration") or 0)
    if d > 0:
        return d
    parts = []
    for x in (video.get("dur_str") or "").split(":"):
        x = x.strip()
        if x.isdigit():
            parts.append(int(x))
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0.0

def ensure_complete_download(cfg, video, mp4, tol=0.97):
    """下载完整性校验: 实际时长明显短于B站元数据 -> 重下一次。

    截断的 mp4(容器头仍报完整时长)会让 ffmpeg 提前结束, 抽帧数变少,
    直接后果是字幕不全、片尾窗口落在视频中间(读不到真正的片尾)。
    返回 (mp4, warn): warn 非空表示最终仍残缺, 需要写进报告提醒。
    """
    if not mp4 or not os.path.exists(mp4):
        return mp4, ""
    exp = expected_duration(video)
    got = ffprobe_duration(mp4)
    if not exp or not got or got >= exp * tol:
        return mp4, ""

    log(f"⚠️ 下载疑似不完整: 实际 {got:.0f}s / 预期 {exp:.0f}s "
        f"({got / exp * 100:.0f}%) -> 重新下载")
    bad = mp4 + ".partial"
    try:
        os.replace(mp4, bad)          # 挪走, 让 download_video 重新下载
    except Exception as e:
        warn = f"视频下载不完整(实际 {got:.0f}s / 预期 {exp:.0f}s), 内容可能缺失"
        log(f"⚠️ 无法移走残缺文件({e}), 不再重下; {warn}")
        return mp4, warn

    again = download_video(cfg, video["bvid"])
    got2 = ffprobe_duration(again) if again else 0.0
    if again and got2 > got:
        try:
            os.remove(bad)
        except Exception:
            pass
        if got2 >= exp * tol:
            log(f"✅ 重新下载完整: {got2:.0f}s / 预期 {exp:.0f}s")
            return again, ""
        warn = f"视频下载仍不完整(实际 {got2:.0f}s / 预期 {exp:.0f}s), 内容可能缺失"
        log("⚠️ " + warn)
        return again, warn

    # 重下反而更短/失败 -> 恢复原文件
    if again:
        try:
            os.remove(again)
        except Exception:
            pass
    if os.path.exists(bad):
        try:
            os.replace(bad, mp4)
        except Exception:
            pass
    warn = f"视频下载不完整(实际 {got:.0f}s / 预期 {exp:.0f}s), 内容可能缺失"
    log("⚠️ " + warn)
    return mp4, warn
