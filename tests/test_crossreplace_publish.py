"""跨源替换的**生产入口**测试 (离线): 走 publish_reports, 不直接调 drop_section。

为什么必须走这个入口 —— 第一版删的是 monitor 里的本地 `reports/<date>.md`, 函数级
测试全绿, 线上却一次都没生效: `.gitignore` 第 5 行是 `/reports/`, 云端 checkout 后
该目录根本不存在(run 45939046960 的日志是 `↩️ 跨源替换未删 …: 报告不存在`); 而真正
发布的是 `docs/reports/<date>.md`, 就算删了本地那份, `_merge_report` 的规则「旧文件
里新文件没有的节原样补回」也会把微博节搬回来(fixture 实测 7399→6679 再合并回 7400)。

所以这里把**云端形状**原样摆出来: `reports/` 不存在, `docs/reports/` 里两节, 跑完
只剩 B 站那节。git 提交推送段整体打桩(本机 git 不完整), 只验文件与日志。
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.modules.setdefault("requests", types.ModuleType("requests"))

from lidaxiao.report import crossreplace as CR  # noqa: E402
from lidaxiao.report import render as R  # noqa: E402
import local.local_daemon as LD  # noqa: E402

OK = FAIL = 0


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


FIX = os.path.join(ROOT, "tests", "fixtures", "report", "dup_2026-11-27.md")
with open(FIX, encoding="utf-8") as _fh:
    REAL = _fh.read()                      # 2026-11-27 线上原样, 7399 B
KEY, BVID = "wb5369798433178131", "BV1hv1pz2EbP"
PAIR = {"bvid": BVID, "weibo_key": KEY, "weibo_id": KEY[2:],
        "weibo_title": "六大举措在发力 稳市曙光或已出现", "weibo_date": "2026-11-27"}
ARGS = types.SimpleNamespace(config=None, force=False)


class _FakeProc:
    returncode = 1


def _stub_git():
    """git add/commit/push 全部打桩: 本机 git 缺 revert/diff, 不拿真仓库冒险。"""
    LD.subprocess = types.SimpleNamespace(run=lambda *a, **k: _FakeProc())
    LD._ensure_git_user = lambda: None
    LD._push = lambda: True


_stub_git()


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def env(docs_files=None, reports_files=None, drops=None, added=0):
    """摆一个临时 WORKDIR; reports_files=None 表示**不建** reports/ (云端形状)。"""
    tmp = tempfile.mkdtemp()
    LD.WORKDIR, CR.WORKDIR = tmp, tmp
    docs = os.path.join(tmp, "docs", "reports")
    os.makedirs(docs)
    for name, text in (docs_files or {}).items():
        write(os.path.join(docs, name), text)
    if reports_files is not None:
        rep = os.path.join(tmp, "reports")
        os.makedirs(rep)
        for name, text in reports_files.items():
            write(os.path.join(rep, name), text)
    if drops is not None:
        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
        write(os.path.join(tmp, "data", CR.HANDOFF_NAME),
              json.dumps({"added": added, "drops": drops}, ensure_ascii=False))
    return tmp, docs


def run_publish():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        LD.publish_reports(ARGS, tag="cloud")
    return buf.getvalue()


def sections(text):
    """只数真正的视频章节标题; `## 视频清单` 也以 `## 视频` 开头, 不能算进去。"""
    return sum(1 for ln in text.splitlines() if re.match(r"^## 视频\d+[:：]", ln))


# ------------------------------------------------ ① 云端形状: reports/ 不存在
print("== 云端形状: 只有 docs/reports, 没有 reports/ 目录 ==")
tmp, docs = env({"2026-11-27.md": REAL}, reports_files=None, drops=[PAIR], added=0)
before = os.path.join(docs, "2026-11-27.md")
_orig_len = os.path.getsize(before)
out = run_publish()
after = read(before)
chk("fixture 真的是两条(否则下面空转)",
    sections(REAL) == 2 and KEY in REAL and BVID in REAL, sections(REAL))
chk("线上那份从 2 节变 1 节", sections(after) == 1, sections(after))
chk("微博键一处不剩", KEY not in after, after[:300])
chk("source=weibo 也不剩", "source=weibo" not in after)
chk("留的是 B 站那节且编号连续",
    "## 视频1:《六大举措在发力 稳市曙光或已出现》 (BV1hv1pz2EbP)" in after
    and "## 视频2:" not in after, after[:400])
chk("字节数 = 同一份原文跑 drop_section 的数",
    len(after.encode("utf-8")) == R.drop_section(REAL, KEY)[1]["bytes_after"],
    (len(after.encode("utf-8")), R.drop_section(REAL, KEY)[1]["bytes_after"]))
chk("整文件变小而不是变空", 1000 < len(after.encode("utf-8")) < _orig_len,
    (len(after.encode("utf-8")), _orig_len))
chk("日志有真正删掉的那一行", "🧹 跨源替换: 删微博节 " + KEY in out, out[-600:])
chk("日志有 删除=1 的节数账",
    "报告节: 追加=0 删除=1 净增=-1" in out, out[-600:])
chk("没有再造一个云端本不存在的 reports/ 目录",
    not os.path.isdir(os.path.join(tmp, "reports")))
chk("latest.md 也跟着更新(不是删节前那份)",
    KEY not in read(os.path.join(docs, "latest.md")))
chk("交接文件用完即删", not os.path.exists(CR.handoff_path()))

# ------------------------------------------------ ② 幂等
print("== 幂等: 同一份报告再跑一次不写盘 ==")
_n = os.path.getsize(before)
run_publish()
chk("再跑一次: 无待删也不会重写", os.path.getsize(before) == _n
    and read(before) == after, os.path.getsize(before))
chk("再跑一次: 微博节没回来", KEY not in read(before))
tmp3, docs3 = env({"2026-11-27.md": after}, reports_files=None, drops=[PAIR], added=0)
out3 = run_publish()
chk("重复决定落在已删过的报告上: removed=0、字节不变",
    read(os.path.join(docs3, "2026-11-27.md")) == after
    and "跨源替换(微博节已删)=0" in out3, out3[-600:])
chk("并打出未删原因(不是静默)",
    ("↩️ 跨源替换未删 " + KEY) in out3, out3[-600:])

# ------------------------------------------------ ③ B 站没出章节 -> 不删
print("== B 站那条没出章节时一节都不删 ==")
_drops_err, _skip_err = CR.apply_replacements([PAIR], {BVID: "error"})
chk("status=error 不进待删", _drops_err == [] and _skip_err, (_drops_err, _skip_err))
chk("无状态记录也不进待删", CR.apply_replacements([PAIR], {})[0] == [])
chk("ok/partial/no-subtitle 才进待删",
    [len(CR.apply_replacements([PAIR], {BVID: s})[0])
     for s in ("ok", "partial", "no-subtitle")] == [1, 1, 1])
tmp4, docs4 = env({"2026-11-27.md": REAL}, reports_files={"2026-11-27.md": REAL},
                  drops=[], added=0)
out4 = run_publish()
_k4 = read(os.path.join(docs4, "2026-11-27.md"))
chk("待删为空: 微博节一条不少(合并本身会重排字节, 所以只数节与键)",
    KEY in _k4 and sections(_k4) == 2, sections(_k4))
chk("待删为空: 账上 删除=0", "报告节: 追加=0 删除=0 净增=0" in out4, out4[-400:])

# ------------------------------------------------ ④ 删节失败 -> 写未删节原文
print("== 删节抛异常时必须写没删过的原文 ==")
tmp5, docs5 = env({"2026-11-27.md": REAL}, reports_files=None, drops=[PAIR], added=0)
_orig_drop = CR.drop_section


def _boom(text, key):
    raise RuntimeError("模拟解析崩溃")


CR.drop_section = _boom
try:
    out5 = run_publish()
finally:
    CR.drop_section = _orig_drop
_kept = read(os.path.join(docs5, "2026-11-27.md"))
chk("报告没被写坏写空", _kept == REAL and len(_kept.encode("utf-8")) == _orig_len,
    len(_kept.encode("utf-8")))
chk("宁可留重复也不丢内容: 两节都还在", sections(_kept) == 2 and BVID in _kept)
chk("打了一行错误(不含静默)", "⚠️ 跨源替换失败" in out5, out5[-400:])

# ------------------------------------------------ ⑤ 本地形状: 合并后删
print("== 本地形状: reports/ 与 docs/ 都有, 合并之后再删 ==")
tmp6, docs6 = env({"2026-11-27.md": REAL}, reports_files={"2026-11-27.md": REAL},
                  drops=[PAIR], added=0)
out6 = run_publish()
_m6 = read(os.path.join(docs6, "2026-11-27.md"))
chk("走的是合并路径", "报告已合并: 2026-11-27.md" in out6, out6[-600:])
chk("合并后仍是 1 节, 微博节没被补回", sections(_m6) == 1 and KEY not in _m6,
    sections(_m6))

# ------------------------------------------------ ⑥ 跨午夜: 待删节在更早那份报告里
print("== 待删节在上一份日报里(跨午夜)也要删掉 ==")


def wb_report(date, key="wb5369222189809983", title="书中内容解读"):
    return (f"# 📺 李大霄视频日报 {date}\n\n## 今日摘要\n"
            f"- 视频《{title}》: 读书\n\n## 视频清单\n"
            f"- [19:02] {title} ({key}) 02:31 ｜ source=weibo\n\n"
            f"## 视频1:《{title}》 ({key})\n\n"
            f"> source=weibo ｜ 微博 id={key[2:]}\n\n【一句话摘要】读书\n")


OLD_PAIR = {"bvid": "BV1eWy2zEE5U", "weibo_key": "wb5369222189809983",
            "weibo_id": "5369222189809983", "weibo_title": "书中内容解读",
            "weibo_date": "2026-11-26"}
NEW_DAY = ("# 📺 李大霄视频日报 2026-11-27\n\n## 今日摘要\n- 视频《新片》: 新\n\n"
           "## 视频清单\n- [09:00] 新片 (BV1hv1pz2EbP)\n\n"
           "## 视频1:《新片》 (BV1hv1pz2EbP)\n\n【一句话摘要】新\n")
tmp7, docs7 = env({"2026-11-26.md": wb_report("2026-11-26"),
                   "2026-11-27.md": REAL},
                  reports_files={"2026-11-27.md": NEW_DAY},
                  drops=[OLD_PAIR, PAIR], added=1)
out7 = run_publish()
_o26 = read(os.path.join(docs7, "2026-11-26.md"))
_o27 = read(os.path.join(docs7, "2026-11-27.md"))
chk("上一份日报里的微博节被删", "wb5369222189809983" not in _o26, _o26)
chk("上一份日报没被删成空文件(表头还在)",
    _o26.strip().startswith("# 📺 李大霄视频日报"), _o26[:120])
chk("当天那份照常写入(新 B 站节在)", "BV1hv1pz2EbP" in _o27 and "新片" in _o27, _o27[:300])
chk("合并会补回 old 侧的微博节 —— 所以它必须被删掉",
    KEY not in _o27 and sections(_o27) == 1, sections(_o27))
chk("账同时记追加与删除", "报告节: 追加=1 删除=2 净增=-1" in out7, out7[-600:])

# ------------------------------------------------ ⑦ 交接文件
print("== monitor -> publish 的交接文件 ==")
tmp8 = tempfile.mkdtemp()
LD.WORKDIR, CR.WORKDIR = tmp8, tmp8
_p = CR.save_handoff([PAIR], 3)
chk("写在 data/ 下(不在 state/, 也不在 reports/)",
    _p and os.path.dirname(_p) == os.path.join(tmp8, "data"), _p)
_back = CR.load_handoff()
chk("读回来内容一致", _back["added"] == 3 and _back["drops"] == [PAIR], _back)
chk("读一次就消费掉", not os.path.exists(_p))
chk("空列表也写(清掉上一跑残留)",
    CR.save_handoff([], 0) and CR.load_handoff()["drops"] == [])
chk("没有交接文件时是空决定", CR.load_handoff() == {"added": 0, "drops": []})

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
