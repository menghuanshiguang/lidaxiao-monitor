#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""李大霄监控统一入口: 本地/云端同一份代码自适应

用法:
  python lidaxiao.py --once       # 云端或本地手动一次(自动识别环境)
  python lidaxiao.py --daemon     # 本地常驻守护
"""
import argparse
import os
import sys

import local_daemon as ld


def is_cloud():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def run_once(args):
    """一次执行: 云端模式检查本地认领; 本地模式走完整 slot"""
    if is_cloud():
        if ld.claim_is_fresh():
            ld.log("本地已认领(≤30分钟), 云端跳过")
            return 0
        ld.log("云端开始处理")
        ld.reset_local_state()
        ld.run_monitor(args, local=False)
        ld.publish_reports(args, tag="cloud")
        return 0
    else:
        ld.run_slot(args)
        return 0


def main():
    ap = argparse.ArgumentParser(description="李大霄监控统一入口 (本地/云端自适应)")
    ap.add_argument("--once", action="store_true", help="执行一次后退出(自动识别环境)")
    ap.add_argument("--daemon", action="store_true", help="本地常驻守护")
    ap.add_argument("--times", default=",".join(ld.DEFAULT_TIMES),
                    help="守护运行时间(北京时间, 逗号分隔)")
    ap.add_argument("--config", default=None, help="配置文件")
    ap.add_argument("--grace-minutes", type=int, default=0, help="兼容参数")
    args = ap.parse_args()

    if args.once:
        return run_once(args)
    if args.daemon:
        # 复用 local_daemon 的守护循环
        daemon_argv = [sys.argv[0]]
        if args.times:
            daemon_argv += ["--times", args.times]
        if args.config:
            daemon_argv += ["--config", args.config]
        if args.grace_minutes:
            daemon_argv += ["--grace-minutes", str(args.grace_minutes)]
        sys.argv = daemon_argv
        return ld.main()
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
