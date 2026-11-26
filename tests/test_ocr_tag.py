"""Aoyuon 加载日志必须按文件算, 不许写死 (离线)。

事故背景 (2026-11-26): 这行原文是 `log("Aoyuon-OCR 已加载 (19.7M int8)")`, 字面量。
模型升到 v0.4 之后日志仍印 19.7M, 督查按"有没有印 19.0M"判 v0.4 是否生效, 当场判错
—— 一行写死的日志让一次正确的升级看起来像没生效。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lidaxiao.ocr.pipeline import aoyuon_model_tag  # noqa: E402

OK = FAIL = 0


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


def make_model_dir(version="0.4", size=19935906, onnx_name="model.onnx", chars=2890):
    d = tempfile.mkdtemp()
    if version is not None:
        with open(os.path.join(d, "VERSION"), "w", encoding="utf-8") as fh:
            fh.write(version)
    if onnx_name and size:
        with open(os.path.join(d, onnx_name), "wb") as fh:
            fh.truncate(size)                 # 稀疏文件, 不真占 19MB
    if chars:
        with open(os.path.join(d, "charset.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(f"c{i}" for i in range(chars)) + "\n")
    return d


print("== 版本与体积都按文件算 ==")
chk("v0.4 / 19,935,906 B / 2890 字符 -> 'v0.4, 19.0M int8, 2890 字符'",
    aoyuon_model_tag(make_model_dir()) == "v0.4, 19.0M int8, 2890 字符",
    aoyuon_model_tag(make_model_dir()))
chk("v0.3 / 19,785,365 B -> 'v0.3, 18.9M int8, 2890 字符'",
    aoyuon_model_tag(make_model_dir("0.3", 19785365)) == "v0.3, 18.9M int8, 2890 字符",
    aoyuon_model_tag(make_model_dir("0.3", 19785365)))
chk("同一目录换版本号, 标签跟着变",
    aoyuon_model_tag(make_model_dir("9.9")) == "v9.9, 19.0M int8, 2890 字符",
    aoyuon_model_tag(make_model_dir("9.9")))
chk("字符表变了标签跟着变 (2695 -> 2890 是这次升级的实质)",
    aoyuon_model_tag(make_model_dir(chars=2695)) == "v0.4, 19.0M int8, 2695 字符",
    aoyuon_model_tag(make_model_dir(chars=2695)))

print("== 残缺情况不许抛 ==")
chk("没有 VERSION 也给出后面两项",
    aoyuon_model_tag(make_model_dir(None)) == "19.0M int8, 2890 字符",
    aoyuon_model_tag(make_model_dir(None)))
chk("没有字符表就只报版本与体积",
    aoyuon_model_tag(make_model_dir(chars=0)) == "v0.4, 19.0M int8",
    aoyuon_model_tag(make_model_dir(chars=0)))
chk("只有 model_fp32.onnx 时用它",
    aoyuon_model_tag(make_model_dir(onnx_name="model_fp32.onnx"))
    == "v0.4, 19.0M int8, 2890 字符",
    aoyuon_model_tag(make_model_dir(onnx_name="model_fp32.onnx")))
chk("空目录给空串", aoyuon_model_tag(tempfile.mkdtemp()) == "")
chk("目录不存在给空串", aoyuon_model_tag("/nonexistent/nope") == "")
chk("非路径参数不抛 (它在引擎选择分支里, 抛了会误回退 RapidOCR)",
    aoyuon_model_tag(123) == "" and aoyuon_model_tag(None) is not None,
    aoyuon_model_tag(123))

print("== 仓库里的真实模型 ==")
real = os.path.join(ROOT, "aoyuon", "model")
size = os.path.getsize(os.path.join(real, "model.onnx"))
want_mb = f"{size / (1024 * 1024):.1f}M"
tag = aoyuon_model_tag(real)
chk("落的是 VERSION 里的版本", tag.startswith("v"), tag)
chk("字符数按 charset.txt 实际行数",
    f"{len(open(os.path.join(real, 'charset.txt'), encoding='utf-8').read().splitlines())} 字符" in tag,
    tag)
chk("体积与实际字节一致 (不写死)", want_mb in tag, (want_mb, tag))
chk("默认参数指向仓库的 aoyuon/model", aoyuon_model_tag() == tag, (aoyuon_model_tag(), tag))

print("== 源码里不许再出现写死的体积 ==")
src = open(os.path.join(ROOT, "lidaxiao", "ocr", "pipeline.py"), encoding="utf-8").read()
# 判据是"日志调用里不许出现写死的体积"(注释里提到旧写法是为了留档案, 不算)
_bad = [ln for ln in src.splitlines()
        if "log(" in ln and "M int8" in ln and "aoyuon_model_tag" not in ln]
chk("日志调用里不再有写死的 '...M int8'", _bad == [], _bad)
chk("日志走 aoyuon_model_tag()", "aoyuon_model_tag()" in src)

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
