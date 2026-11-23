"""textutil 单测: 读写/日志/归一化/过滤 (覆盖 import 缺失这类错误)"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import textutil as T

ok = fail = 0


def chk(name, cond, info=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}\n     {info}")


tmp = tempfile.mkdtemp()
p = os.path.join(tmp, "sub", "a.txt")
T.write_text(p, "第一行\n第二行")
chk("write_text 建目录并写入", os.path.exists(p))
chk("read_text 读回", T.read_text(p) == "第一行\n第二行")
chk("read_text 缺失返回默认", T.read_text(os.path.join(tmp, "none.txt"), "x") == "x")

chk("norm_text 去符号", T.norm_text("A, B。C") == "ABC")
chk("过滤水印", T.filter_ocr_text("李大霄 bili 留得青山在", 0.5) == "留得青山在")
chk("过滤纯时间戳", T.filter_ocr_text("12:34", 0.5) is None)
chk("过短被丢弃", T.filter_ocr_text("好", 0.5) is None)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
