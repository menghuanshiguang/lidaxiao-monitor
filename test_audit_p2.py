# -*- coding: utf-8 -*-
"""本地单测: 不联网, 全部纯函数。"""
import audit_p2 as A

ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


print("BUG-10 点位语境")
s = "2689点就是924行情的起点，当时指数才2000多点。"
r = A.fix_point_context(s)
chk("补指数名+语境", "上证2689点（上证924行情起点）" in r, r)
s2 = "上证2689点，2024年9月24日启动。"
chk("已带正确日期不重复", A.fix_point_context(s2).count("924行情起点") == 0)
s3 = "上证2635点，是2024年2月5日的低点。"
chk("已有语境不重复", "（上证2024年2月5日阶段低点）" not in A.fix_point_context(s3))
s4 = "涨了3个点"
chk("非4位数不误伤", A.fix_point_context(s4) == s4)

print("BUG-13 连续性")
hist = "## 视频1:《A》\n银行股股息率有优势\n## 视频2:《B》\n银行股股息率还是核心\n"
r = A.fix_continuity("银行股股息率的逻辑连续第3期成立。", hist)
chk("按历史期数校正", "连续第3期" in r, r)
chk("无历史则降级", "连续第" not in A.fix_continuity("连续第3期强调", ""))
r2 = A.fix_continuity("完全没提过的话题连续第5期出现。", hist)
chk("算不出降级为近期", "近期" in r2, r2)

print("BUG-11 视觉置信度")
chk("无置信度提示", "OCR" in A.visual_note({"text": "x"}))
chk("带置信度", "0.42" in A.visual_note({"text": "x", "confidence": 0.42}))

print("BUG-16 图文冲突")
chk("冲突提示", "图文不一致" in A.conflict_note("大盘站上3400点", "上证在3200点附近"))
chk("一致不提示", A.conflict_note("上证3200点", "上证在3200点附近") == "")
chk("单边缺失不提示", A.conflict_note("", "上证在3200点") == "")

print("BUG-14 摘要去重")
an = """【一句话摘要】本期李大霄强调银行股股息率与余钱投资的重要性，提醒不要追高。
【核心观点】
1. 银行股息率4.6%显著高于十年期国债，是当前最有性价比的方向（原文数据：4.6%）。
2. 追高者要为情绪付代价。
【操作含义】
- **动作**: 持有
- **仓位**: 3成
"""
line = A.make_summary_line("如何理解稳定市场", an)
chk("不与一句话摘要相同", an.split("】")[1].split("\n")[0] not in line, line)
chk("含动作", "持有" in line, line)
print("   ->", line)

print("BUG-12 排序统一")
doc = """# 📺 李大霄视频日报 2026-11-19

## 今日摘要
- 视频《C》: 第三条

## 视频清单
- [18:32] C (BVccc) 01:35

## 视频1:《C》 (BVccc)

正文C
"""
doc2 = doc.replace("## 视频清单", "- [16:34] A (BVaaa) 06:34\n\n## 视频清单", 1)
doc2 += "\n## 视频2:《A》 (BVaaa)\n\n正文A\n"
out = A.resort_report(doc2)
chk("清单按时间升序", out.index("BVaaa") < out.index("BVccc"), out)
chk("章节重编号", "## 视频1:《A》" in out and "## 视频2:《C》" in out, out)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
