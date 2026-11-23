"""frame_dedup 单测: 合成画面验证游程压缩不会误合并/漏帧"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import frame_dedup as F
from PIL import Image, ImageDraw

ok = fail = 0


def chk(name, cond, info=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}\n     {info}")


def make_frame(path, text, bg=200):
    """模拟字幕带: 白底黑字, 字放大到接近真实字幕高度"""
    im = Image.new("L", (240, 60), 255)
    d = ImageDraw.Draw(im)
    d.text((10, 20), text, fill=0)
    im = im.resize((480, 380), Image.LANCZOS)   # 放大, 让字够大
    bgim = Image.new("L", (480, 380), bg)
    bgim.paste(im.crop((0, 100, 480, 260)), (0, 120))
    bgim.save(path)


tmp = tempfile.mkdtemp()
seq = ["AAA", "AAA", "AAA", "BBB", "BBB", "CCC", "CCC", "CCC", "CCC", "DDD"]
paths = []
for i, t in enumerate(seq):
    p = os.path.join(tmp, f"{i:02d}.jpg")
    make_frame(p, t)
    paths.append(p)

print("游程压缩")
hs = [F.band_sig(p) for p in paths]
pairs = F.pick_indices(hs)
chk("游程数量=4", len(pairs) == 4, pairs)
chk("代表帧取中间", [i for i, _ in pairs] == [1, 3, 6, 9], pairs)
chk("时间戳取段首", [s for _, s in pairs] == [0, 3, 5, 9], pairs)

print("不误合并")
# 只差几个像素的两句真字幕必须分开
p1 = os.path.join(tmp, "x1.jpg")
p2 = os.path.join(tmp, "x2.jpg")
make_frame(p1, "涨了 3%")
make_frame(p2, "美股泡沫破灭之后资金开始撤离")
r = F.changed_ratio(F.band_sig(p1), F.band_sig(p2))
chk("完全不同句要分开", r > F.MAX_CHANGED_RATIO, f"变化比例 {r:.4f}")
r_same = F.changed_ratio(F.band_sig(paths[0]), F.band_sig(paths[1]))
chk("同句像素一致", r_same <= F.MAX_CHANGED_RATIO, f"变化比例 {r_same:.4f}")

print("边界")
chk("空输入", F.split_runs([]) == [] and F.pick_indices([]) == [])
chk("单帧", F.pick_indices([F.band_sig(paths[0])]) == [(0, 0)])

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
