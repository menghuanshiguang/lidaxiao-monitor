"""微博条目的报告落盘 (离线, 不联网)。

钉四件事:
1. 微博章节头与清单行必须带 `wb<id>` 与 `source=weibo` —— 报告里要能和 B 站分辨。
2. 重跑同一条微博不得产生第二个章节 (章节键认识 `wb<id>`, 否则每跑一次多一节)。
3. 排序: 微博与 B 站同一天的内容按发布时间统一升序、统一重编号, 谁都不丢。
4. B 站章节一个字节都不该变 —— 不许为微博在现有报告里留下痕迹。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lidaxiao.report import render as R  # noqa: E402

OK = FAIL = 0


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


T_WB = 1795604547          # 2026-11-25 19:02:27 +08
T_BILI = T_WB + 3 * 3600   # 同一天 22:02, 比微博那条晚

WB = {"source": "weibo", "id": "5369222189809983", "bvid": "wb5369222189809983",
      "title": "书中内容解读", "pubdate": T_WB, "duration": 151.0, "dur_str": "02:31",
      "weibo_url": "https://m.weibo.cn/status/5369222189809983"}
BILI = {"bvid": "BV1eWy2zEE5U", "title": "美股反弹的原因", "pubdate": T_BILI,
        "dur_str": "06:21"}

print("== 章节头与清单行 ==")
head_wb = R.build_section({}, WB, "", None)[0]
chk("章节头括号是 wb<id>", "## 视频1:《书中内容解读》 (wb5369222189809983)" in head_wb, head_wb)
chk("章节里有 source=weibo", "source=weibo" in head_wb, head_wb)
chk("章节里有微博 id", "微博 id=5369222189809983" in head_wb, head_wb)
chk("章节里有微博原链接", "https://m.weibo.cn/status/5369222189809983" in head_wb, head_wb)
line_wb = R.list_line(WB)
chk("清单行括号是 wb<id>", "(wb5369222189809983)" in line_wb, line_wb)
chk("清单行标来源", line_wb.endswith("｜ source=weibo"), line_wb)

head_b = R.build_section({}, BILI, "", None)[0]
chk("B 站章节头一字不变",
    head_b.startswith("## 视频1:《美股反弹的原因》 (BV1eWy2zEE5U)"), head_b)
chk("B 站章节不出现 source 标记", "source=" not in head_b, head_b)
import datetime as _dt  # noqa: E402
from lidaxiao.common import CN_TZ as _CN  # noqa: E402
_hhmm = _dt.datetime.fromtimestamp(T_BILI, _CN).strftime("%H:%M")
chk("B 站清单行一字不变 (无 source 标记)",
    R.list_line(BILI) == f"- [{_hhmm}] 美股反弹的原因 (BV1eWy2zEE5U) 06:21",
    R.list_line(BILI))

print("== 章节键能被 parse_report 认出来 ==")
text = ("# 📺 李大霄视频日报 2026-11-25\n\n## 今日摘要\n\n## 视频清单\n\n"
        + head_wb + "\n")
secs = R.parse_report(text)["sections"]
chk("按 wb<id> 找到章节", "wb5369222189809983" in secs, secs)

print("== 重跑不重复 ==")
tmp = tempfile.mkdtemp()
_orig = R.report_path
R.report_path = lambda date_str: os.path.join(tmp, f"{date_str}.md")
try:
    rp = R.upsert_report(WB, head_wb, "- 视频《书中内容解读》: 讲读书的方法", line_wb)
    first = R.read_text(rp)
    # 同一条微博重跑一次 (内容换一句), 章节数必须还是 1、编号不变
    again = R.upsert_report(WB, head_wb.replace("(wb5369222189809983)",
                                                "(wb5369222189809983)"),
                            "- 视频《书中内容解读》: 换了摘要", line_wb)
    second = R.read_text(again)
    import re as _re
    _secs = lambda t: len([l for l in t.splitlines() if _re.match(r"^## 视频\d+:", l)])
    chk("重跑后仍只有一个视频章节", _secs(second) == 1, _secs(second))
    chk("重跑后章节数不变", _secs(first) == _secs(second) == 1, (_secs(first), _secs(second)))
    chk("重跑后清单里只有一行微博", second.count("(wb5369222189809983)") == 2,
        second.count("(wb5369222189809983)"))
    chk("摘要被替换而不是叠加", second.count("讲读书的方法") == 0
        and second.count("换了摘要") == 1, second)

    # 同一天再来一条 B 站视频: 两者都在, 且按时间升序 (微博 19:02 在前)
    R.upsert_report(BILI, R.build_section({}, BILI, "", None)[0],
                    "- 视频《美股反弹的原因》: B站那条", R.list_line(BILI))
    mixed = R.read_text(R.report_path("2026-11-25"))
    import re
    order = [ln for ln in mixed.splitlines() if re.match(r"^## 视频\d+:", ln)]
    chk("两条视频章节都在", len(order) == 2, order)
    chk("按发布时间升序 (微博 19:02 在前)",
        "书中内容解读" in order[0] and "美股反弹的原因" in order[1], order)
    chk("重编号连续", order[0].startswith("## 视频1:") and order[1].startswith("## 视频2:"),
        order)
    chk("微博那条仍带 source=weibo", "source=weibo" in mixed, mixed[:400])
    chk("清单里两条都在",
        "(wb5369222189809983)" in mixed and "(BV1eWy2zEE5U)" in mixed, mixed)
finally:
    R.report_path = _orig

print("== 跨源替换: 删掉往轮那条微博节 (2026-11-27 真实报告) ==")
# 这份 fixture 就是线上出事那天的原样: 同一内容并列两条, 前者是微博的降级空节
_dup = os.path.join(ROOT, "tests", "fixtures", "report", "dup_2026-11-27.md")
_real_text = open(_dup, encoding="utf-8").read()
chk("fixture 里确实有两条(否则下面全是空转)",
    _real_text.count("## 视频1:《") == 1 and _real_text.count("## 视频2:《") == 1
    and "wb5369798433178131" in _real_text and "BV1hv1pz2EbP" in _real_text,
    _real_text.count("## 视频"))

_new, _info = R.drop_section(_real_text, "wb5369798433178131")
chk("删掉的是第 1 节", _info["idx"] == 1 and "wb5369798433178131" in _info["head"], _info)
chk("节数 2 -> 1", (_info["sections_before"], _info["sections_after"]) == (2, 1), _info)
chk("微博键一处不剩", "wb5369798433178131" not in _new, _new[:300])
chk("source=weibo 也不剩", "source=weibo" not in _new)
chk("留的是 B 站那节", "BV1hv1pz2EbP" in _new and "## 视频1:《六大举措" in _new, _new[:400])
chk("节序号连续(视频1, 无视频2)", _new.count("## 视频1:") == 1
    and "## 视频2:" not in _new, _new.count("## 视频1:"))
chk("摘要行只删一条(不是把留下的那条也抹掉)",
    sum(1 for l in _new.splitlines() if l.startswith("- 视频《")) == 1,
    [l for l in _new.splitlines() if l.startswith("- 视频《")])
chk("清单行只剩 B 站那行",
    sum(1 for l in _new.splitlines() if l.startswith("- [")) == 1,
    [l for l in _new.splitlines() if l.startswith("- [")])
chk("整文件变小但不为空", _info["bytes_after"] < _info["bytes_before"] > 1000,
    (_info["bytes_before"], _info["bytes_after"]))
chk("没有该节时原样返回且不标记删除",
    R.drop_section(_new, "wb5369798433178131")[1]["removed"] is False)

print("== 跨源替换: 只有 B 站这次真出了章节才删(判定层) ==")
# 判定与落盘是两层: 这里只验「决定删哪些」; 真正删节走 publish_reports,
# 见 tests/test_crossreplace_publish.py —— 第一版就是只在函数层测过, 线上没生效。
from lidaxiao.report import crossreplace as CR  # noqa: E402
_pairs = [{"bvid": "BV1hv1pz2EbP", "weibo_key": "wb5369798433178131",
           "weibo_id": "5369798433178131",
           "weibo_title": "六大举措在发力 稳市曙光或已出现", "weibo_date": "2026-11-27"}]
_d, _skip = CR.apply_replacements(_pairs, {"BV1hv1pz2EbP": "error"})   # 下载就失败
chk("B 站没出章节 -> 不列入待删", _d == [] and bool(_skip), (_d, _skip))
_d2, _skip2 = CR.apply_replacements(_pairs, {"BV1hv1pz2EbP": "ok"})
chk("B 站出了章节 -> 列入待删", len(_d2) == 1 and not _skip2, (_d2, _skip2))
chk("待删项带着微博键与报告日期(发布段靠它找文件)",
    _d2[0].get("weibo_key") == "wb5369798433178131"
    and _d2[0].get("weibo_date") == "2026-11-27", _d2)
chk("partial(分析失败但章节在)也删",
    len(CR.apply_replacements(_pairs, {"BV1hv1pz2EbP": "partial"})[0]) == 1)
chk("没有状态记录时按未出章节处理",
    CR.apply_replacements(_pairs, {})[0] == [])
chk("配对里没有微博键时不删(C 站那类无键条目)",
    CR.apply_replacements([{"bvid": "BV1hv1pz2EbP", "weibo_key": ""}], {})[0] == [])

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
