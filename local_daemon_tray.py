#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地常驻监控托盘版: 右下角显示后台, 支持 立即运行/查看日志/退出"""
import argparse
import os
import threading
import time

import pystray
from PIL import Image, ImageDraw

import local_daemon as daemon

STOP = threading.Event()


def make_icon_image():
    img = Image.new("RGB", (64, 64), "#1f6feb")
    d = ImageDraw.Draw(img)
    try:
        d.text((18, 14), "李", fill="white")
    except Exception:
        d.rectangle((16, 16, 48, 48), fill="white")
    return img


def daemon_loop(times, grace_minutes, config):
    last_beat = time.time()
    daemon.update_local_flag(True)
    try:
        while not STOP.is_set():
            now = daemon.datetime.datetime.now(daemon.CN_TZ)
            nxt = daemon.next_run(now, times)
            if now >= nxt:
                daemon.run_slot(argparse.Namespace(config=config, grace_minutes=grace_minutes))
                continue
            time.sleep(30)
            if time.time() - last_beat >= daemon.HEARTBEAT_SECONDS:
                daemon.update_local_flag(True)
                last_beat = time.time()
    finally:
        daemon.update_local_flag(False)


def run_once(config):
    daemon.run_slot(argparse.Namespace(config=config, grace_minutes=0))


def on_run_once(icon, item):
    threading.Thread(target=run_once, args=(None,), daemon=True).start()


def on_open_log(icon, item):
    try:
        os.startfile(daemon.LOG_PATH)
    except Exception as e:
        daemon.log(f"打开日志失败: {e}")


def on_exit(icon, item):
    daemon.log("托盘退出")
    STOP.set()
    icon.stop()


def main():
    ap = argparse.ArgumentParser(description="本地常驻监控托盘版")
    ap.add_argument("--times", default=",".join(daemon.DEFAULT_TIMES),
                    help="运行时间(北京时间, 逗号分隔), 默认 14:40,20:00")
    ap.add_argument("--grace-minutes", type=int, default=10, help="云端观察宽限分钟数")
    ap.add_argument("--config", default=None, help="传给 monitor.py 的配置文件")
    args = ap.parse_args()
    times = [t.strip() for t in args.times.split(",") if t.strip()]

    daemon.log("本地托盘监控启动")
    threading.Thread(target=daemon_loop,
                     args=(times, args.grace_minutes, args.config),
                     daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("立即运行一次", on_run_once),
        pystray.MenuItem("打开日志", on_open_log),
        pystray.MenuItem("退出", on_exit),
    )
    icon = pystray.Icon("lidaxiao_monitor", make_icon_image(), "李大霄本地监控", menu)
    icon.run()


if __name__ == "__main__":
    main()
