"""免责声明归一化匹配单测

钉住一个真实缺陷: 模型把常量里的半角逗号写成全角「，」时,
`DISCLAIMER not in analysis` 的精确子串匹配会漏, 同一句话被追加第二遍
(2026-11-23 的 baseline 报告里就出现两处)。修法是归一化匹配, 但只归一化标点与空白,
改字改词的变体仍须判为"没有", 否则缺声明的报告会被当成有而不再补。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lidaxiao.common import DISCLAIMER
from lidaxiao.report import render as R

ok = fail = 0


def chk(name, cond, info=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}\n     {info}")


FULL_WIDTH = DISCLAIMER.replace(",", "，")      # 模型实际写法
BODY = "【一句话摘要】测试摘要\n【核心观点】测试观点"

# 1) 全角逗号的声明: 不再追加第二遍
one = R.ensure_disclaimer(BODY + "\n\n" + FULL_WIDTH)
chk("全角逗号声明不重复追加", one.count("不构成投资建议") == 1, repr(one[-60:]))
chk("全角逗号声明原文未被改写", FULL_WIDTH in one)

# 2) 精确声明: 行为不变
two = R.ensure_disclaimer(BODY + "\n\n" + DISCLAIMER)
chk("精确声明不重复追加", two.count("不构成投资建议") == 1)
chk("精确声明原样保留", two.rstrip().endswith(DISCLAIMER))

# 3) 完全没有声明: 必须补上
three = R.ensure_disclaimer(BODY)
chk("缺声明时补一次", three.count("不构成投资建议") == 1 and three.rstrip().endswith(DISCLAIMER))

# 4) 改词的变体不算有: 仍然补
variant = "以上为个人观点，不构成投资建议"
four = R.ensure_disclaimer(BODY + "\n\n" + variant)
chk("改词变体仍判为缺声明", four.count(DISCLAIMER) == 1, repr(four[-80:]))

# 5) 定位函数: 全角版本能定位到句首, 用于把片尾块插在它前面
five = R.find_disclaimer(BODY + "\n\n" + FULL_WIDTH)
chk("全角版本可定位", five >= 0 and five >= len(BODY))
chk("定位落在句首", (BODY + "\n\n" + FULL_WIDTH)[five:].startswith("以上为李大霄个人观点提炼"))

# 6) 片尾块插在声明之前: 声明仍是章节最后一行
video = {"title": "测试视频", "bvid": "BV1test00000"}
ending = {"text": "【片尾口语】测试片尾"}
sec, _ = R.build_section(None, video, "字幕文本", BODY + "\n\n" + FULL_WIDTH, ending=ending)
lines = [ln for ln in sec.splitlines() if ln.strip()]
chk("片尾块已插入", "片尾口语" in sec, sec[-200:])
chk("声明仍是最后一行", lines[-1].startswith("以上为李大霄个人观点提炼"), repr(lines[-1]))
chk("声明只出现一次", sec.count("不构成投资建议") == 1, str(sec.count("不构成投资建议")))
chk("片尾块在声明之前",
    sec.index("片尾口语") < sec.index("以上为李大霄个人观点提炼"))

# 7) 无声明 + 有片尾: 按 monitor.py 的真实调用顺序 (先 ensure_disclaimer 再 build_section),
#    补上的声明仍须是最后一行
analysis3 = R.ensure_disclaimer(BODY)
sec2, _ = R.build_section(None, video, "字幕文本", analysis3, ending=ending)
lines2 = [ln for ln in sec2.splitlines() if ln.strip()]
chk("无声明时补在最后", lines2[-1] == DISCLAIMER, repr(lines2[-1]))
chk("无声明时只出现一次", sec2.count("不构成投资建议") == 1)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
