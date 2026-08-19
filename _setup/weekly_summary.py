#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成近一周总结报告 (基于 reports/ 下最近 N 天的日报)"""
import datetime
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor as m

CN = datetime.timezone(datetime.timedelta(hours=8))


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    cutoff_date = (datetime.datetime.now(CN) - datetime.timedelta(days=days)).date()
    rdir = os.path.join(m.WORKDIR, "reports")
    files = []
    for f in sorted(glob.glob(os.path.join(rdir, "*.md"))):
        try:
            d = datetime.datetime.strptime(os.path.basename(f)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d >= cutoff_date and not os.path.basename(f).startswith("weekly"):
            files.append(f)
    if not files:
        print("范围内无报告")
        return 1
    print(f"汇总 {len(files)} 天日报: {[os.path.basename(f)[:10] for f in files]}")

    reports = []
    for f in files:
        text = m.read_text(f)
        # 只保留每个视频的分析主体
        reports.append(f"### 报告 {os.path.basename(f)[:10]}\n{text}")

    today = datetime.datetime.now(CN).strftime("%Y-%m-%d")
    start = os.path.basename(files[0])[:10]
    end = os.path.basename(files[-1])[:10]
    system = """你是资深财经分析助手。请基于提供的"李大霄视频日报"(每日AI分析)生成一份**周度总结报告**,要求:
1. 【本周观点主线】用时间线梳理本周观点演变(每天的核心判断,标注升级/反转/延续)
2. 【核心数据回顾】列出本周反复出现的关键数据(点位/指标/事件)
3. 【话术信号汇总】汇总本周"不是推荐"类暗示与风险警示(按🟢看多/🔴警示/⚠️风险归类)
4. 【近期操作建议】分三部分写清楚:
   - 方向判断:当前市场处于什么阶段(反弹/反转/见顶/防御),依据是什么
   - 仓位建议:具体仓位水平与加减仓条件(什么信号出现才加仓/减仓)
   - 风险清单:近期最需要防范的风险点(具体到事件/数据)
5. 【下周关注】下周需要重点跟踪的事件与信号

要求: 简体中文、条理清晰、操作建议具体可执行, 不模棱两可。结尾固定一行: 以上为李大霄个人观点提炼,不构成投资建议"""

    api_key = m.load_llm_key()
    if not api_key:
        print("无 LLM 密钥 (OPENCODE_GO_API_KEY / DEEPSEEK_API_KEY)")
        return 1
    cfg = m.load_config()
    user = "以下是本周(起始日 " + start + ")每日分析报告:\n\n" + "\n\n".join(reports)
    print(f"调用 {cfg['llm']['model']} 生成周报...")
    out = m.call_llm(cfg, api_key, system, user)
    if "不构成投资建议" not in out:
        out += "\n\n以上为李大霄个人观点提炼,不构成投资建议"

    weekly = os.path.join(m.WORKDIR, "reports", f"weekly_{start}_{end}.md")
    header = f"# 📅 李大霄周度总结 {start} ~ {end}\n\n> 由近 {len(files)} 天日报自动汇总生成于 {today}\n\n---\n\n"
    m.write_text(weekly, header + out + "\n")
    print(f"周报已生成: {weekly}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
