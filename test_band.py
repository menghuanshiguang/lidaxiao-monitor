"""band_from_points 单测: 字幕带折算要稳 (不切字、不被离群点拉爆)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from monitor import band_from_points as B

ok = fail = 0


def chk(name, cond, info=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}\n     {info}")


H = 360
# 单行字幕: 中心约 0.88
one = [int(H * 0.88)] * 8
b1 = B(one, H)
chk("单行只包住底部", 0.78 <= b1[0] <= 0.86 and 0.90 <= b1[1] <= 1.0, b1)

# 两行字幕: 0.84 / 0.93
two = [int(H * 0.84)] * 4 + [int(H * 0.93)] * 4
b2 = B(two, H)
chk("两行覆盖两行", b2[0] <= 0.84 and b2[1] >= 0.93, b2)

# 离群点(片尾大字/水印)不能把带子拉到中间
out = one + [int(H * 0.30)] * 2
b3 = B(out, H)
chk("离群点不进主体", b3[0] > 0.7, b3)

# 点太少 -> 回退默认
chk("样本不足回退", B([int(H * 0.88)] * 3, H) == (0.55, 1.0))

# 极端: 全部挤在一点 -> 补到最小高度
b4 = B([180] * 10, H)
chk("最小高度兜底", b4[1] - b4[0] >= 0.099, b4)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
