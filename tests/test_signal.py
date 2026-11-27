"""每日操作建议 (docs/signal/) 单测 — 离线, 不联网, 不调模型。

钉住的四件事, 都是 督查 定的硬约束:
1. 每条 citations 必须能在当天报告里按 video 键定位到真实那一节, text 是那节原话片段;
   人为改错一个字就必须被丢掉, 不许写进 JSON。
2. 当天没有内容 -> status=no_content, 原样延续上一份 stance/action/as_of, 不许编。
3. LLM 挂了 -> status=degraded, 只放原话引用, key_points 为空, 不做归纳。
4. 写建议时 docs/reports/ 一个字节都不许变; 同一份报告跑两次输出逐字节相同。
"""
import contextlib
import hashlib
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

from lidaxiao.report import signal as S  # noqa: E402

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
    REPORT = _fh.read()          # 2026-11-27 线上那一版(两节: wb + BV)
DATE = "2026-11-27"
BV, WB = "BV1hv1pz2EbP", "wb5369798433178131"


def norm(t):
    """与实现同一把尺子: 去空白+标点+符号, 留中文与数字。"""
    from lidaxiao.textutil import norm_text
    return norm_text(t or "")


def read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def env(report_text=REPORT, keep_reports=True):
    """临时 WORKDIR: docs/reports 有当天日报, docs/signal 还没有。

    `local_daemon` 也必须跟着改 —— 它的 publish_reports 用的是自己模块里的
    WORKDIR, 漏掉这一步就会拿真仓库当工作区(本机真跑过一次, 靠日期不新近才没写盘)。
    """
    tmp = tempfile.mkdtemp()
    S.WORKDIR = tmp
    try:
        import local.local_daemon as _ld
        _ld.WORKDIR = tmp
    except Exception:
        pass
    rdir = os.path.join(tmp, "docs", "reports")
    os.makedirs(rdir)
    if keep_reports:
        with open(os.path.join(rdir, f"{DATE}.md"), "w", encoding="utf-8") as fh:
            fh.write(report_text)
    return tmp


def snapshot(dirpath):
    out = {}
    for name in sorted(os.listdir(dirpath)):
        p = os.path.join(dirpath, name)
        if os.path.isfile(p):
            out[name] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


print("== 报告解析: 章节与时间只认报告里真实的内容 ==")
secs = S.report_sections(REPORT)
chk("解析出两节", len(secs) == 2, [s["key"] for s in secs])
chk("键是 wb 与 BV", {s["key"] for s in secs} == {WB, BV}, [s["key"] for s in secs])
chk("时间取自报告清单行(不是挂钟)",
    [s["time"] for s in secs] == ["09:32", "09:35"], [s["time"] for s in secs])
chk("标题取自章节标题",
    any(s["title"].startswith("六大举措在发力") for s in secs), [s["title"] for s in secs])

print("\n== 引用可定位: 报告里找得到的原话才留下 ==")
_body = {s["key"]: s["body"] for s in secs}
_quote = S._pick_quote(_body[BV])
chk("从 B 站那节取到一句原话", len(_quote) > 10 and norm(_quote) in norm(_body[BV]), _quote)
PAYLOAD = {
    "stance": "看多", "action": "持有", "strength": 2, "horizon": "中线",
    "assets": ["A股", "恒生科技", "A股"], "levels": [{"asset": "上证", "level": 3455}],
    "key_points": ["李大霄认为企稳概率在提升"],
    "citations": [{"text": _quote, "video": BV, "time": "23:59"},
                  {"text": "恒生科技探到367点、黄金探到3426美元/盎司、上证探到3455点",
                   "video": BV, "time": "23:59"}],
}
tmp = env()
sig, dropped = S.build_signal(DATE, REPORT, payload=PAYLOAD), None
chk("两条引用都能定位 -> 全留", len(sig["citations"]) == 2, sig["citations"])
chk("time 以报告为准, 不信 LLM 给的 23:59",
    sig["citations"][0]["time"] == "09:35", sig["citations"][0])
chk("字段与顺序固定为 schema",
    list(sig.keys()) == list(S.FIELDS), list(sig.keys()))
chk("assets 按**被引那一节的正文**核, 重复项去掉",
    sig["assets"] == ["A股", "恒生科技"], sig["assets"])
chk("留下的标的都能在被引那一节的正文里找到",
    "A股相对抗跌" in _body[BV], sig["assets"])
chk("levels 补默认 kind 且点位能在引用里找到",
    sig["levels"] == [{"asset": "上证", "level": 3455, "kind": "支撑"}], sig["levels"])
chk("每条引用在整份报告里 grep 得到",
    all(norm(c["text"]) in norm(REPORT) for c in sig["citations"]), sig["citations"])
chk("每条引用的 video 都是报告里真实存在的节",
    all(c["video"] in {s["key"] for s in secs} for c in sig["citations"]))
chk("ok 状态", sig["status"] == "ok" and sig["change"] == "无前值", sig["status"])
chk("as_of 是 报告日期+收盘时点, 不带挂钟",
    sig["as_of"] == "2026-11-27 15:00", sig["as_of"])

print("\n== 标的/点位只认被引那一节的正文, 不是整份报告任意处 ==")
TWO = ("# 📺 李大霄视频日报 2026-11-28\n\n## 今日摘要\n"
       "- 视频《甲》: 甲摘要\n- 视频《乙》: 乙摘要\n\n## 视频清单\n"
       "- [09:00] 甲 (BVaaaaaaa01)\n- [10:00] 乙 (BVaaaaaaa02)\n\n"
       "## 视频1:《甲》 (BVaaaaaaa01)\n\n【一句话摘要】\n"
       "甲这一段说恒生科技在3500点附近见底\n\n"
       "## 视频2:《乙》 (BVaaaaaaa02)\n\n【一句话摘要】\n"
       "乙这一段说比特币和黄金9999要小心\n")
_only_first = {"stance": "看多", "action": "持有", "strength": 1, "horizon": "短线",
               "assets": ["恒生科技", "比特币"],
               "levels": [{"asset": "恒生科技", "level": 3500},
                          {"asset": "比特币", "level": 9999}],
               "key_points": ["只引甲那节"],
               "citations": [{"text": "甲这一段说恒生科技在3500点附近见底",
                              "video": "BVaaaaaaa01"}]}
_sig_two = S.build_signal("2026-11-28", TWO, payload=_only_first)
chk("只会引了甲 -> 乙里的比特币被丢", _sig_two["assets"] == ["恒生科技"], _sig_two["assets"])
chk("点位同理: 3500 在甲里留下, 9999 只在乙里被丢",
    _sig_two["levels"] == [{"asset": "恒生科技", "level": 3500, "kind": "支撑"}],
    _sig_two["levels"])
chk("比特币仍在整份报告里(证明不是『报告里没有』而是『不在被引那节』)",
    "比特币" in TWO and "比特币" not in json.dumps(_sig_two, ensure_ascii=False))
_both = dict(_only_first)
_both["citations"] = _only_first["citations"] + [
    {"text": "乙这一段说比特币和黄金9999要小心", "video": "BVaaaaaaa02"}]
_sig_two2 = S.build_signal("2026-11-28", TWO, payload=_both)
chk("把乙那节也引上 -> 比特币就能留下", _sig_two2["assets"] == ["恒生科技", "比特币"],
    _sig_two2["assets"])
chk("两节都引时点位也进", len(_sig_two2["levels"]) == 2, _sig_two2["levels"])

print("\n== 引用长度门槛: 少于 10 个字即使真在报告里也不算 ==")
chk("『会师』在报告里, 但只有 2 字 -> 不合格",
    "会师" in REPORT and S.quote_ok(secs, BV, "会师") is False)
chk("『3500点』命中也没用(6 字)", S.quote_ok(secs, BV, "3500点") is False)
chk("够 10 字的原话才合格",
    S.quote_ok(secs, BV, "恒生科技探到367点、黄金探到3426美元/盎司") is True)
_sig_short = S.build_signal(DATE, REPORT, payload={**PAYLOAD, "citations": [
    {"text": "3500点", "video": BV}]})
chk("短引用整条丢掉, 不留坏数据",
    all("3500点" != c["text"] for c in _sig_short["citations"]), _sig_short["citations"])
chk("引用全被丢光 -> 整份降级, 不许 ok 配零引用", _sig_short["status"] == "degraded",
    _sig_short["status"])

print("\n== 引用全丢光时不许留一个 ok ==")
_sig_allbad = S.build_signal(DATE, REPORT, payload={
    **PAYLOAD, "citations": [{"text": "李大霄说满仓加杠杆必赚", "video": BV},
                             {"text": "以太坊年底翻三倍", "video": WB}]})
chk("status 降级", _sig_allbad["status"] == "degraded", _sig_allbad["status"])
chk("不进任何模型给的结论", _sig_allbad["key_points"] == []
    and _sig_allbad["assets"] == [] and _sig_allbad["levels"] == [], _sig_allbad)
chk("留下的只有能从正文直接取到的原话",
    all(norm(c["text"]) in norm(REPORT) for c in _sig_allbad["citations"]),
    _sig_allbad["citations"])

print("\n== 人为改错的引用必须被丢掉, 不许写进 JSON ==")
BAD = "李大霄说满仓加杠杆必赚"
PAYLOAD_BAD = dict(PAYLOAD)
PAYLOAD_BAD["citations"] = [{"text": _quote, "video": BV},
                            {"text": BAD, "video": BV},
                            {"text": "稳定的曙光或已出现", "video": "BV1不存在0000"}]
env()
(_sig_bad, _secs_b, _drop_b), _out = quiet(S.generate, DATE, REPORT,
                                            payload=PAYLOAD_BAD, use_llm=False)
chk("三条里只剩一条能定位", len(_sig_bad["citations"]) == 1
    and _sig_bad["citations"][0]["text"] == _quote, _sig_bad["citations"])
chk("编造的原话没进结果", BAD not in json.dumps(_sig_bad, ensure_ascii=False))
chk("不存在的 video 也没进结果", "BV1不存在0000" not in json.dumps(_sig_bad, ensure_ascii=False))
chk("丢弃有日志(不是静默)", "↩️ 建议: 丢弃引用" in _out, _out[-400:])
chk("只有结论没有可定位原话时, 结论一并丢弃",
    S.build_signal(DATE, REPORT, payload={**PAYLOAD, "citations": [
        {"text": BAD, "video": BV}]})["key_points"] == [])

print("\n== LLM 挂了 -> degraded: 只放原话, 不写归纳 ==")
env()
(_sig_deg, _secs_d, _drop_d), _out_d = quiet(S.generate, DATE, REPORT, prev=None,
                                                 payload=None, use_llm=False)
chk("状态 degraded", _sig_deg["status"] == "degraded", _sig_deg["status"])
chk("不放归纳", _sig_deg["key_points"] == [] and _sig_deg["assets"] == []
    and _sig_deg["levels"] == [], _sig_deg)
chk("只放原话且每条都能定位",
    bool(_sig_deg["citations"]) and all(
        norm(c["text"]) in norm(REPORT) for c in _sig_deg["citations"]),
    _sig_deg["citations"])
chk("降级原话也受 10 字门槛: 一节正文只有 1 个字就不写",
    S.build_signal("2026-11-28", "# 📺 李大霄视频日报 2026-11-28\n\n## 今日摘要\n\n"
                   "## 视频清单\n\n## 视频1:《短》(BV1hv1pz2EbP 不存在)\n\n新\n")["citations"]
    == [])
chk("白名单外的 stance/action 也算 degraded",
    S.build_signal(DATE, REPORT, payload={**PAYLOAD, "stance": "梭哈"})["status"] == "degraded")
chk("LLM 返回不是 JSON 也算 degraded", S._parse_json("我看多，建议满仓。") is None)

print("\n== 原话截断不落在半句上 ==")
_long = "。".join(["一二三四五六七八九十"] * 12) + "。"
_clipped = S._clip(_long, 80)
chk("截断后是原文前缀(所以仍能过子串判定)", _long.startswith(_clipped))
chk("截在句号上, 不是半句", _clipped.endswith("。"), _clipped[-12:])
chk("长度不超过上限", len(_clipped) <= 80, len(_clipped))
chk("够不到句子边界时硬截, 不返回空",
    len(S._clip("一" * 200, 80)) == 80 and len(S._clip("甲乙丙丁戊", 80)) == 5)

print("\n== 当天没内容 -> no_content, 原样延续上一份, 不许编 ==")
EMPTY_REPORT = ("# 📺 李大霄视频日报 2026-11-28\n\n## 今日摘要\n\n## 视频清单\n\n"
                "## 李大霄动态\n\n### 2026-11-28 09:00 · 视频投稿\n<!-- dynamic:1 -->\n原文(3 字): 没有\n")
PREV = {"date": "2026-11-27", "as_of": "2026-11-27 15:00", "stance": "看空", "action": "减仓",
        "strength": 2, "horizon": "中线", "assets": ["黄金"],
        "levels": [{"asset": "黄金", "level": 3400, "kind": "支撑"}],
        "key_points": [], "citations": [], "change": "无前值",
        "coverage": {"videos": 0, "weibo": 0}, "status": "ok",
        "disclaimer": S.DISCLAIMER}
_sig_nc = S.build_signal("2026-11-28", EMPTY_REPORT, payload=PAYLOAD, prev=PREV)
chk("状态 no_content", _sig_nc["status"] == "no_content", _sig_nc["status"])
chk("stance/action/strength 原样延续", (_sig_nc["stance"], _sig_nc["action"],
                                       _sig_nc["strength"]) == ("看空", "减仓", 2), _sig_nc)
chk("as_of 沿用上一份(带上它自己的日期)", _sig_nc["as_of"] == "2026-11-27 15:00",
    _sig_nc["as_of"])
chk("不许借上一份的标的/点位充数", _sig_nc["assets"] == [] and _sig_nc["levels"] == [],
    (_sig_nc["assets"], _sig_nc["levels"]))
chk("不许编: 没有 citation / key_point",
    _sig_nc["citations"] == [] and _sig_nc["key_points"] == [], _sig_nc)
chk("覆盖数如实记 0", _sig_nc["coverage"] == {"videos": 0, "weibo": 0}, _sig_nc["coverage"])
chk("md 里写明当日无内容且标 as_of",
    "当日无视频内容" in S.render_md(_sig_nc) and "沿用上一份" in S.render_md(_sig_nc))
chk("没有上一份时也不会编出指向内容的结论",
    S.build_signal("2026-11-28", EMPTY_REPORT)["key_points"] == [])

print("\n== change 四档 ==")
chk("无前值", S._change(None, "看多", 1) == "无前值")
chk("转向", S._change({"stance": "看空", "strength": 2}, "看多", 1) == "转向")
chk("升级", S._change({"stance": "看多", "strength": 1}, "看多", 3) == "升级")
chk("维持(强度不变)", S._change({"stance": "看多", "strength": 2}, "看多", 2) == "维持")
chk("四档外的值不会出现",
    S.build_signal(DATE, REPORT, payload=PAYLOAD,
                   prev={"stance": "看多", "strength": 1})["change"] in S.CHANGES)

print("\n== 写盘: 四份文件 + reports/ 一个字节都不许动 ==")
tmp_w = env()
rdir = os.path.join(tmp_w, "docs", "reports")
before = snapshot(rdir)
_sig_w, secs_w, _ = S.generate(DATE, REPORT, payload=PAYLOAD, use_llm=False)
outdir = S.write_signal(_sig_w, secs_w)
after = snapshot(rdir)
names = sorted(os.listdir(outdir))
chk("写了 <date>.md / latest.md / latest.json / index.md",
    names == ["2026-11-27.md", "index.md", "latest.json", "latest.md"], names)
chk("docs/reports/ 逐文件哈希不变", before == after and before, (before, after))
chk("latest.json 是固定 schema 且能解析",
    list(json.loads(read(os.path.join(outdir, "latest.json"))).keys()) == list(S.FIELDS))
chk("latest.md 与 <date>.md 同内容",
    read(os.path.join(outdir, "latest.md")) == read(os.path.join(outdir, "2026-11-27.md")))
chk("index.md 链到当天", "[2026-11-27](./2026-11-27.md)" in read(os.path.join(outdir, "index.md")))
_md = read(os.path.join(outdir, "latest.md"))
chk("md 里有原话引用与视频 id", "## 原话引用" in _md and BV in _md
    and _quote in _md, _md[-500:])
chk("md 末尾是免责声明",
    read(os.path.join(outdir, "latest.md")).strip().endswith(S.DISCLAIMER))

print("\n== 同一天跑两次, 输出逐字节相同 ==")
_first = {n: open(os.path.join(outdir, n), "rb").read() for n in names}
_sig_w2, secs_w2, _ = S.generate(DATE, REPORT, payload=PAYLOAD, use_llm=False)
S.write_signal(_sig_w2, secs_w2)
_second = {n: open(os.path.join(outdir, n), "rb").read() for n in sorted(os.listdir(outdir))}
chk("四份文件字节完全相同", _first == _second,
    [n for n in _first if _first[n] != _second.get(n)])
chk("上一份能被读回来(供次日算 change)",
    S.load_prev()["date"] == DATE and S.load_prev()["stance"] == "看多")

print("\n== 生成不越过日报: 没有已发布日报就不出建议 ==")
env(keep_reports=False)
_sig_e, secs_e, _ = S.generate(DATE, "", payload=None, use_llm=False)
chk("空文本 -> no_content", _sig_e["status"] == "no_content", _sig_e["status"])
chk("空文本不会写出一份像样的建议(无引用无结论)",
    _sig_e["citations"] == [] and _sig_e["key_points"] == [])
chk("不需要 reports/ 里的文件也能判定(不读盘)", secs_e == [])


print("\n== 接线: 挂在 publish_reports 发布成功之后(生产入口) ==")
# 桩掉: LLM 网络调用、git 提交推送、配置读取 —— 只验「什么时候生成、什么时候不生成」
sys.modules.setdefault("requests", types.ModuleType("requests"))
import local.local_daemon as LD  # noqa: E402
from lidaxiao import config as CFG  # noqa: E402
import lidaxiao.analyze.llm as LLM  # noqa: E402

CALLS, PUSHED = [], []


def _fake_llm(cfg, api_key, system, user):
    CALLS.append(user[:60])
    return json.dumps(PAYLOAD, ensure_ascii=False)


LLM.call_llm = _fake_llm
CFG.load_config = lambda path=None: {}
CFG.load_llm_key = lambda provider=None: "stub-key"


class _P:
    returncode = 1


LD.subprocess = types.SimpleNamespace(run=lambda *a, **k: _P())
LD._ensure_git_user = lambda: None
LD._push = lambda: (PUSHED.append(1), _PUSH_OK[0])[1]
_PUSH_OK = [True]
ARGS = types.SimpleNamespace(config=None, force=False)


def today_str():
    import datetime
    from lidaxiao.common import CN_TZ
    return datetime.datetime.now(CN_TZ).strftime("%Y-%m-%d")


# ① 本 run 什么都没发布(reports/ 不存在) -> 不生成
env(keep_reports=False)
LD.publish_reports(ARGS, tag="cloud")
chk("日报没产出 -> docs/signal 都不建",
    not os.path.isdir(os.path.join(S.WORKDIR, "docs", "signal")))
chk("也没调 LLM", CALLS == [], CALLS)

# ② 有日报发布 -> 生成四份, 并且单独提交推送一次
tmp_p = env(keep_reports=False)
day = today_str()
os.makedirs(os.path.join(tmp_p, "reports"))
with open(os.path.join(tmp_p, "reports", f"{day}.md"), "w", encoding="utf-8") as fh:
    fh.write(REPORT)
CALLS.clear()
PUSHED.clear()
LD.publish_reports(ARGS, tag="cloud")
_sdir = os.path.join(tmp_p, "docs", "signal")
chk("日报发布成功后生成建议目录", os.path.isdir(_sdir))
chk("四份文件齐", sorted(os.listdir(_sdir)) ==
    sorted([f"{day}.md", "latest.md", "latest.json", "index.md"]), os.listdir(_sdir))
_j = json.loads(read(os.path.join(_sdir, "latest.json")))
chk("当天建议用的是已发布的日报", _j["date"] == day and _j["status"] == "ok", _j)
chk("引用都在那份日报里找得到",
    all(norm(c["text"]) in norm(REPORT) for c in _j["citations"]), _j["citations"])
chk("调了一次 LLM", len(CALLS) == 1, CALLS)
chk("日报与建议各推一次", len(PUSHED) == 2, PUSHED)

# ③ 日报推送失败 -> 一个建议都不许生成
tmp_f = env(keep_reports=False)
os.makedirs(os.path.join(tmp_f, "reports"))
with open(os.path.join(tmp_f, "reports", f"{today_str()}.md"), "w", encoding="utf-8") as fh:
    fh.write(REPORT)
_PUSH_OK[0] = False
CALLS.clear()
LD.publish_reports(ARGS, tag="cloud")
_PUSH_OK[0] = True
chk("日报没推上去 -> 不生成建议(宁可空一天)",
    not os.path.isdir(os.path.join(tmp_f, "docs", "signal")))
chk("也没调 LLM", CALLS == [], CALLS)

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
