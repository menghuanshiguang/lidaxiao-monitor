"""Local speech archive. Private files live outside the public checkout."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

CHINA = timezone(timedelta(hours=8))
BILI = "https://space.bilibili.com/2137589551/video"
VIDEO = "https://www.bilibili.com/video/"


def command(args, timeout=180, env=None):
    result = subprocess.run(
        list(map(str, args)), capture_output=True, encoding="utf-8",
        errors="replace", timeout=timeout, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        errors = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        detail = re.sub(r"https?://\S+", "[URL]", errors[-1]) if errors else ""
        raise RuntimeError(f"退出码 {result.returncode} {detail}")
    return result.stdout


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def timestamp(value):
    hours, remainder = divmod(value // 1000, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{value % 1000:03}"


def render_status(root, videos, failure):
    ordered = sorted(videos.values(), key=lambda v: (v.get("published", 0), v["observed"]), reverse=True)
    body = ["# 视频转写状态", "", f"更新时间 {datetime.now(CHINA).isoformat(timespec='seconds')}",
            f"B站发现状态 {failure or '查询成功'}", "抖音自动发现尚未配置", ""]
    dated = next((v for v in ordered if v.get("published")), None)
    if dated:
        body.append(f"已核实发布日期的最新视频 {dated['url']} · {dated['status']}")
    undated = [v for v in ordered if not v.get("published")]
    if undated:
        body.append("存在发布日期待确认的视频，最新视频尚无法确认。")
        body.extend(f"- {v['url']} · {v['status']}" for v in undated)
    completed = next((v for v in ordered if v.get("transcript")), None)
    if completed:
        body.append(f"最近的一条可读文字稿 [{completed['id']}]({completed['transcript']})")
    body.extend(["", "[完整索引](index.md)"])
    index = ["# 视频索引", ""]
    for video in ordered:
        date = datetime.fromtimestamp(video["published"], CHINA).isoformat() if video.get("published") else "发布日期待确认"
        transcript = video.get("transcript")
        status = f"[{video['status']}]({transcript})" if transcript else video["status"]
        index.append(f"- {date} · {video['platform']} · [{video['title']}]({video['url']}) · {status}")
    write(root / "index.md", "\n".join(index) + "\n")
    write(root / "latest.md", "\n".join(body) + "\n")


def speech(tool_root, media):
    sys.path.insert(0, str(tool_root))
    from bili_prep import transcribe
    return transcribe(media, logging.info)


def process(tool_root, root, video, downloader):
    with tempfile.TemporaryDirectory(dir=root, prefix="media-") as scratch:
        folder = Path(scratch)
        output = folder / "media.%(ext)s"
        info = json.loads(command(
            [*downloader, "--skip-download", "--dump-single-json", "--no-playlist", "--", video["url"]]
        ))
        published = info.get("timestamp")
        duration = info.get("duration")
        if not published or not duration:
            raise RuntimeError("平台缺少发布时间或时长")
        video.update(title=info["title"], published=published, duration=duration)
        command([*downloader, "--no-playlist", "-f", "bestaudio/best",
                 "-o", output, "--", video["url"]], timeout=600)
        media, = folder.glob("media.*")
        audio_duration = float(command([
            tool_root / "bin" / "ffprobe.exe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", media,
        ]).strip())
        if abs(audio_duration - duration) > max(2, duration * 0.02):
            raise RuntimeError(f"时长不符，音频 {audio_duration:.2f} 秒，平台 {duration} 秒")
        result = command(
            [sys.executable, "-X", "utf8", __file__, "--worker", tool_root, media], timeout=1800
        )
        cues = json.loads(result)
    if not cues:
        raise RuntimeError("未识别出语音")
    tail = max(0, round(audio_duration - cues[-1][1] / 1000, 1))
    date = datetime.fromtimestamp(published, CHINA)
    rel = f"{date:%Y-%m-%d}/{video['platform']}-{video['id']}.md"
    lines = [
        f"# {video['title']}", "", f"- 发布 {date.isoformat()}", f"- 平台 {video['platform']}",
        f"- 来源 {video['url']}", f"- 时长 {duration} 秒",
        f"- 完成 {datetime.now(CHINA).isoformat(timespec='seconds')}",
        "- 模型 SenseVoice Small INT8 CPU + Silero VAD",
        f"- 完整性 音频时长 {audio_duration:.2f} 秒，末段识别后 {tail} 秒无转写，片尾待听审",
        "- 待核对 数字、股票名称、指数、金额和日期均保留机器识别原文", "",
        "## 中文讲话", "",
    ]
    for start, end, value in cues:
        flag = " **数字待核对**" if re.search(r"\d|百分之|[万亿]元", value) else ""
        lines.append(f"- [{timestamp(start)} – {timestamp(end)}] {value}{flag}")
    write(root / rel, "\n".join(lines) + "\n")
    video.update(transcript=rel, status="已转写，待听审")


def poll(tool_root, root, videos, downloader, explicit, scan_limit):
    urls = list(explicit)
    failure = ""
    try:
        result = command([*downloader, "--flat-playlist", "--playlist-end", scan_limit,
                          "--dump-single-json", "--", BILI])
        for item in json.loads(result)["entries"]:
            if item and re.fullmatch(r"BV[0-9A-Za-z]{10}", item["id"]):
                urls.append(VIDEO + item["id"])
        if len(urls) == len(explicit):
            raise RuntimeError("账号列表未返回视频")
    except (RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        failure = str(exc).splitlines()[0]
        logging.warning("B站发现失败 %s", failure)
    for url in urls:
        match = re.fullmatch(r"https://www\.(bilibili\.com/video/BV[0-9A-Za-z]{10}|douyin\.com/video/\d+)", url)
        if not match:
            raise ValueError("仅接受规范的 B站或抖音单视频链接")
        ident = url.rsplit("/", 1)[-1]
        platform = "bilibili" if "bilibili.com" in url else "douyin"
        videos.setdefault(f"{platform}:{ident}", dict(
            id=ident, url=url, platform=platform, title=ident, status="待转写",
            observed=time.time(), attempts=0, retry_after=0,
        ))

    def persist():
        write(root / "state.json", json.dumps(videos, ensure_ascii=False, indent=2) + "\n")
        render_status(root, videos, failure)

    persist()
    for video in videos.values():
        if video.get("transcript") or video["retry_after"] > time.time() or (root / "pause").exists():
            continue
        try:
            process(tool_root, root, video, downloader)
            logging.info("%s 已转写", video["id"])
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            video["attempts"] += 1
            video["retry_after"] = time.time() + min(21600, 900 * 2 ** min(video["attempts"] - 1, 5))
            video["status"] = "转写失败，等待重试"
            logging.warning("%s %s", video["id"], str(exc).splitlines()[0])
        persist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool-root", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--scan-limit", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--worker", nargs=2, type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(speech(*args.worker), ensure_ascii=False))
        return
    if not args.tool_root or not args.archive or args.interval < 15 or args.scan_limit < 1:
        parser.error("需要 --tool-root、--archive，间隔至少 15 分钟，扫描数量大于零")
    root = args.archive.resolve()
    checkout = Path(__file__).resolve().parent
    if root.is_relative_to(checkout) or any((parent / ".git").exists() for parent in (root, *root.parents)):
        parser.error("全文归档必须位于 Git 工作区外")
    root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(root / "voice.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
    os.environ["PYTHONPATH"] = str(args.tool_root.resolve() / "deps")
    downloader = [sys.executable, "-X", "utf8", "-m", "yt_dlp", "--ignore-config", "--no-progress",
                  "--no-colors", "--socket-timeout", "20", "--retries", "1"]
    with (root / "voice.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            parser.exit(1, "该归档已由另一进程监控\n")
        state = root / "state.json"
        videos = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        while True:
            if not (root / "pause").exists():
                poll(args.tool_root.resolve(), root, videos, downloader, args.url, args.scan_limit)
            if args.once:
                break
            time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
