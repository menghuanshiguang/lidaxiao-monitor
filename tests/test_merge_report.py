"""local/local_daemon.py 的 _merge_report 单测(离线, 不联网)。

钉住一个会丢内容的缺陷: 早先只按 `## 视频N:` 保旧章节, 同一天重跑同一 BV 且动态
0 新增时, 已发布的「## 李大霄动态」连同收尾声明会被合并静默吃掉, 而 state 里那些
id 仍是「已见」, 当天补不回来。

local_daemon.py 顶部 import requests(本机没有), 这里给个占位模块再导入。
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.modules.setdefault("requests", types.ModuleType("requests"))

from lidaxiao.report import render as R  # noqa: E402
from local.local_daemon import _merge_report  # noqa: E402

OK = FAIL = 0


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


def dyn_block(i, ts=None):
    item = {"id_str": f"12423494582696000{i:02d}", "pub_ts": ts or 1788095424,
            "major_type": "MAJOR_TYPE_OPUS", "text": f"第 {i} 条原文"}
    return R.dynamic_md(item, f"第 {i} 条简版")


N = 13
_dyn = "\n\n".join(dyn_block(i) for i in range(1, N + 1))
OLD = ("""# 📺 李大霄视频日报 2026-11-23

## 今日摘要
- 视频《一支》: 摘要一
- 视频《两支》: 摘要二

## 视频清单
- [09:00] 一支 (BV1aa00001)
- [15:00] 两支 (BV1bb00002)

## 视频1: 《一支》 (BV1aa00001)

【一句话摘要】一

""" + R.DISCLAIMER + """

## 视频2: 《两支》 (BV1bb00002)

【一句话摘要】二

""" + R.DISCLAIMER + """

## 李大霄动态

""" + _dyn + "\n\n" + R.DISCLAIMER + "\n")

# 新报告: 同一 BV 重跑(视频正文换了措辞), 没有动态节(动态 0 新增 / 412 失败时就是这样)
NEW = ("""# 📺 李大霄视频日报 2026-11-23

## 今日摘要
- 视频《一支》: 摘要一(新)
- 视频《两支》: 摘要二

## 视频清单
- [09:00] 一支 (BV1aa00001)
- [15:00] 两支 (BV1bb00002)

## 视频1: 《一支》 (BV1aa00001)

【一句话摘要】一(新措辞)

""" + R.DISCLAIMER + """

## 视频2: 《两支》 (BV1bb00002)

【一句话摘要】二

""" + R.DISCLAIMER + "\n")

print("== 合并: 旧报告有动态节, 新报告只有视频 ==")
merged = _merge_report(NEW, OLD)
chk("动态节还在", "## 李大霄动态" in merged, merged[-300:])
chk("13 条标记都在", merged.count("<!-- dynamic:") == N, merged.count("<!-- dynamic:"))
chk("动态节内容逐块原样", all(dyn_block(i) in merged for i in range(1, N + 1)),
    "有块被改写")
chk("末行是免责声明",
    [l for l in merged.splitlines() if l.strip()][-1] == R.DISCLAIMER,
    [l for l in merged.splitlines() if l.strip()][-2:])
chk("视频正文用新报告的(没被旧版覆盖)", "一(新措辞)" in merged and "一(新措辞)" not in OLD)
chk("两个视频节都在", merged.count("## 视频") == 3, merged.count("## 视频"))
chk("动态节排在视频章节之后",
    merged.index("## 李大霄动态") > merged.rindex("## 视频2:"), "节序不对")

print("== 合并: 新旧报告都带动态节(新报告只有本轮新增的 2 条) ==")
NEW_DYN = NEW.rstrip() + "\n\n## 李大霄动态\n\n" + "\n\n".join(
    dyn_block(i) for i in (1, 2)) + "\n\n" + R.DISCLAIMER + "\n"
m2 = _merge_report(NEW_DYN, OLD)
chk("不重复出两份动态节", m2.count("## 李大霄动态") == 1, m2.count("## 李大霄动态"))
# 2026-11-26 线上: 新报告只带本轮新抓的 1 条, 合并时「新覆盖旧」把旧的 13 条整段删了。
chk("动态节按 id 取并集(13 条一条不少)", m2.count("<!-- dynamic:") == N,
    m2.count("<!-- dynamic:"))
chk("已有块逐字节原样", all(dyn_block(i) in m2 for i in range(1, N + 1)), "有块被改写")
chk("动态节只有一条收尾声明",
    m2.split("## 李大霄动态")[1].count(R.DISCLAIMER) == 1,
    m2.split("## 李大霄动态")[1].count(R.DISCLAIMER))

print("== 合并: 旧报告没有动态节(不能凭空造) ==")
m3 = _merge_report(NEW, NEW)
chk("没有动态节就还是没有", "## 李大霄动态" not in m3, m3[-200:])
chk("视频节不受影响", m3.count("## 视频") == 3, m3.count("## 视频"))

print("== 合并: 旧报告里的第三个视频(本次没重跑)仍保留 ==")
_sec3 = ("## 视频3: 《三支》 (BV1cc00003)\n\n【一句话摘要】三\n\n"
         + R.DISCLAIMER + "\n\n")
OLD3 = OLD.replace("## 李大霄动态", _sec3 + "## 李大霄动态", 1)
m4 = _merge_report(NEW, OLD3)
chk("旧报告独有视频节被保留", "《三支》" in m4, m4[:200])
chk("动态节同时保留", "## 李大霄动态" in m4 and m4.count("<!-- dynamic:") == N,
    m4.count("<!-- dynamic:"))
chk("章节重新编号不重复",
    len([l for l in m4.splitlines() if l.startswith("## 视频")]) == 4,
    [l for l in m4.splitlines() if l.startswith("## 视频")])

print("== 合并: 微博章节按 wb<id> 合并, 不许被搬到文末或叠出第二份 ==")
_WB_HEAD = ("## 视频1:《书中内容解读》 (wb5369222189809983)\n\n"
            "> source=weibo ｜ 微博 id=5369222189809983\n\n【一句话摘要】读书\n\n"
            + R.DISCLAIMER + "\n")
WB_NEW = ("# 📺 李大霄视频日报 2026-11-25\n\n## 今日摘要\n- 视频《书中内容解读》: 读书\n\n"
          "## 视频清单\n- [19:02] 书中内容解读 (wb5369222189809983) 02:31 ｜ source=weibo\n\n"
          + _WB_HEAD)
WB_OLD = ("# 📺 李大霄视频日报 2026-11-25\n\n## 今日摘要\n- 视频《书中内容解读》: 读书(旧)\n\n"
          "## 视频清单\n- [19:02] 书中内容解读 (wb5369222189809983) 02:31 ｜ source=weibo\n\n"
          "## 视频1:《书中内容解读》 (wb5369222189809983)\n\n"
          "> source=weibo ｜ 微博 id=5369222189809983\n\n【一句话摘要】读书(旧措辞)\n\n"
          + R.DISCLAIMER + "\n")
m5 = _merge_report(WB_NEW, WB_OLD)
chk("微博章节只有一份", m5.count("(wb5369222189809983)") == 2,
    m5.count("(wb5369222189809983)"))
chk("内容用新报告那版", "读书(旧措辞)" not in m5, m5[-300:])
chk("仍是视频章节, 没被当成独立小节搬到文末",
    len([l for l in m5.splitlines() if l.startswith("## 视频")]) == 2,
    [l for l in m5.splitlines() if l.startswith("## 视频")])
chk("source=weibo 标记还在", "source=weibo" in m5, m5[:300])

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
