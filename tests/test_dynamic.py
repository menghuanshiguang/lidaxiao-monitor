"""动态解析层单测 (离线, 用探针真实响应裁剪的 fixture)

fixture 来源见 tests/fixtures/dynamic/README.md —— 都是 2026-11-24 探针的原始响应,
裁剪只保留信封 + 需要的条目, 没有手编假结构。

两份"一页"fixture 形状不同, 分别覆盖两条分支:
  feed_space_page.json  不带 features 抓到的形状 -> AV(ARCHIVE) + 纯图(DRAW, 图在 draw.items)
  feed_space_opus.json  带 features=itemOpusStyle 抓到的形状 -> AV(ARCHIVE) + 长文(OPUS, 814 字在 opus.summary.text)

重点钉三件事:
1. 空页(items=0, code=0)必须判成「本次没拿到」, 不能判成「没有新动态」——
   判错就会静默漏采, 这是阶段 1 探针挖出的坑。
2. desc 恒为 null 的条目不能崩。
3. 同一个 id_str 第二次不得重复产出。
"""
import contextlib
import glob
import hashlib
import io
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lidaxiao.sources import dynamic as D

OK = FAIL = 0
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "dynamic")


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


page = fixture("feed_space_page.json")
opus_page = fixture("feed_space_opus.json")
empty = fixture("feed_space_empty.json")
risk = fixture("feed_space_risk.json")

# ---------------------------------------------------------------- 归类
ok_res = D.classify_payload(page)
chk("一页正常响应判 ok", ok_res["status"] == "ok", ok_res["status"])
chk("ok 页条目数", len(ok_res["items"]) == 2, len(ok_res["items"]))
chk("ok 可推进 state", D.is_usable(ok_res) is True)

empty_res = D.classify_payload(empty)
chk("空页判 empty 而不是 ok", empty_res["status"] == "empty", empty_res["status"])
chk("空页不得推进 state", D.is_usable(empty_res) is False)
chk("空页条目为空", empty_res["items"] == [])
chk("空页 code 仍是 0(所以不能看 code 判空)", empty_res["code"] == 0, empty_res["code"])

risk_res = D.classify_payload(risk)
chk("-352 判 risk", risk_res["status"] == "risk", risk_res["status"])
chk("risk 不得推进 state", D.is_usable(risk_res) is False)
chk("risk 保留原始 code", risk_res["code"] == -352, risk_res["code"])

chk("HTML 风控页判 error", D.classify_payload("<!DOCTYPE html><html>412</html>")["status"] == "error")
chk("非 dict 判 error", D.classify_payload(None)["status"] == "error")
chk("items 缺失判 empty", D.classify_payload({"code": 0, "data": {}})["status"] == "empty")

# ---------------------------------------------------------------- 解析
raw_items = page["data"]["items"]
raw_av = next(i for i in raw_items if i["type"] == "DYNAMIC_TYPE_AV")
raw_draw = next(i for i in raw_items if i["type"] == "DYNAMIC_TYPE_DRAW")

av = D.parse_dynamic(raw_av)
chk("AV id_str 取自 items[].id_str", av["id_str"] == raw_av["id_str"], av["id_str"])
chk("AV 类型", av["type"] == "DYNAMIC_TYPE_AV", av["type"])
chk("AV pub_ts 等于原响应(字符串转 int)", av["pub_ts"] == int(raw_av["modules"]["module_author"]["pub_ts"]), av["pub_ts"])
chk("AV pub_ts 是 int 而不是字符串", isinstance(av["pub_ts"], int), type(av["pub_ts"]).__name__)
chk("AV 标题取 major.archive.title",
    av["title"] == raw_av["modules"]["module_dynamic"]["major"]["archive"]["title"], av["title"])
chk("AV 标题与探针原文一致", av["title"] == "提醒11月3日对美股的风险", av["title"])
chk("AV bvid 与探针原文一致", av["bvid"] == "BV1uEtH6mEY6", av["bvid"])
chk("AV 无文案时 text 为空串", av["text"] == "", repr(av["text"]))
chk("AV 无图 images 为空", av["images"] == [])
chk("AV 用 archive.jump_url 补成完整 https 链接",
    av["url"].startswith("https://") and av["bvid"] in av["url"], av["url"])

draw = D.parse_dynamic(raw_draw)
chk("DRAW desc 为 null 时不崩", draw is not None)
chk("DRAW text 为空串", draw["text"] == "", repr(draw["text"]))
chk("DRAW 图片数等于原响应",
    len(draw["images"]) == len(raw_draw["modules"]["module_dynamic"]["major"]["draw"]["items"]),
    len(draw["images"]))
chk("DRAW 图片 src 取自 major.draw.items[].src",
    draw["images"] == [i["src"] for i in raw_draw["modules"]["module_dynamic"]["major"]["draw"]["items"]])

# ---------------------------------------------------------------- opus 分支
# 同一 id 两种形状: 不带 features 时是 DRAW(正文为空), 带 features 时是 OPUS(814 字)
raw_opus_items = opus_page["data"]["items"]
raw_opus = next(i for i in raw_opus_items
                if (i["modules"]["module_dynamic"].get("major") or {}).get("type") == "MAJOR_TYPE_OPUS")
raw_opus_av = next(i for i in raw_opus_items
                   if (i["modules"]["module_dynamic"].get("major") or {}).get("type") == "MAJOR_TYPE_ARCHIVE")
opus = D.parse_dynamic(raw_opus)
opus_major = raw_opus["modules"]["module_dynamic"]["major"]["opus"]
chk("OPUS 分支 major_type 记下来", opus["major_type"] == "MAJOR_TYPE_OPUS", opus["major_type"])
chk("OPUS 正文取自 major.opus.summary.text",
    opus["text"] == (opus_major["summary"]["text"] or "").strip(), len(opus["text"]))
chk("OPUS 原始正文 814 字", len(opus_major["summary"]["text"]) == 814, len(opus_major["summary"]["text"]))
chk("OPUS 解析后去掉首尾换行 813 字", len(opus["text"]) == 813, len(opus["text"]))
chk("OPUS 正文不是空串(不带 features 时才是空)", opus["text"] != "")
chk("OPUS 正文开头与原文一致", opus["text"].startswith("李大霄：救市组合拳有助于加速市场稳定"), repr(opus["text"][:30]))
chk("OPUS 图取自 major.opus.pics[].url",
    opus["images"] == [p["url"] for p in (opus_major.get("pics") or [])], opus["images"])
chk("OPUS 有图", len(opus["images"]) == 1, len(opus["images"]))
chk("OPUS jump_url 转成 https 完整链接",
    opus["url"] == "https:" + opus_major["jump_url"], opus["url"])
chk("OPUS 的 id_str 与 DRAW fixture 里那条同 id 对得上",
    opus["id_str"] in [i["id_str"] for i in page["data"]["items"]], opus["id_str"])

flat_av = D.parse_dynamic(raw_opus_av)
chk("ARCHIVE 分支 major_type", flat_av["major_type"] == "MAJOR_TYPE_ARCHIVE", flat_av["major_type"])
chk("ARCHIVE 分支仍取 archive.bvid", flat_av["bvid"].startswith("BV"), flat_av["bvid"])
chk("ARCHIVE 分支没有正文", flat_av["text"] == "", repr(flat_av["text"]))

chk("页级 major.type 分布覆盖 ARCHIVE+OPUS",
    sorted({D.parse_dynamic(i)["major_type"] for i in raw_opus_items}) ==
    ["MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_OPUS"])

chk("缺 id_str 返回 None", D.parse_dynamic({"type": "DYNAMIC_TYPE_WORD"}) is None)
chk("非 dict 返回 None", D.parse_dynamic("x") is None)
chk("modules 缺失不崩", D.parse_dynamic({"id_str": "1"})["text"] == "")
chk("desc 是空对象也不崩", D.parse_dynamic({"id_str": "2", "modules": {"module_dynamic": {"desc": {}}}})["text"] == "")

# ---------------------------------------------------------------- 幂等
cfg = {"state_dir": "/tmp/lidaxiao-test-never-written"}
first = D.new_dynamics(cfg, page["data"]["items"], seen=set())
chk("首次拿到 2 条", len(first) == 2, len(first))
second = D.new_dynamics(cfg, page["data"]["items"], seen={p["id_str"] for p in first})
chk("同 id 第二次不重复", second == [], [p["id_str"] for p in second])

seen_partial = {raw_av["id_str"]}
partial = D.new_dynamics(cfg, page["data"]["items"], seen=seen_partial)
chk("已见的那条被跳过", [p["id_str"] for p in partial] == [raw_draw["id_str"]],
    [p["id_str"] for p in partial])

dup_batch = D.new_dynamics(cfg, [raw_av, raw_av, raw_draw], seen=set())
chk("同一批重复 id 只留一条", len(dup_batch) == 2, len(dup_batch))

already_parsed = [av, draw]
chk("传入已解析对象不重复解析",
    [p["title"] for p in D.new_dynamics(cfg, already_parsed, seen=set())] == [av["title"], draw["title"]])

chk("空列表不产出", D.new_dynamics(cfg, [], seen=set()) == [])

# 空页场景: 调用方必须先看 status, 不能拿 items 空当「没有新动态」
chk("空页经过分类后不可用于推进 state",
    D.is_usable(D.classify_payload(empty)) is False)

# ---------------------------------------------------------------- 常量守卫
chk("请求间隔不低于实测下限 15s", D.MIN_GAP_SECONDS >= 15, D.MIN_GAP_SECONDS)
chk("重试上限为 2", D.MAX_RETRIES == 2, D.MAX_RETRIES)
chk("端点只用 polymer feed/space", D.FEED_URL.endswith("/polymer/web-dynamic/v1/feed/space"), D.FEED_URL)
params = D.build_params(D.DEFAULT_UID)
chk("请求参数带 features=itemOpusStyle(不带会丢正文)",
    params.get("features") == "itemOpusStyle", params)
chk("请求参数带 host_mid", params.get("host_mid") == str(D.DEFAULT_UID), params)
chk("limit 生效", D.build_params(1, 20).get("limit") == "20")
chk("不给 limit 时不带该参数", "limit" not in D.build_params(1))

# ---------------------------------------------------------------- 签名
q1 = D.sign_params({"host_mid": "1"}, "abc", wts=1700000000)
q2 = D.sign_params({"host_mid": "1"}, "abc", wts=1700000000)
chk("签名可复现", q1 == q2, q1)
chk("签名带 wts 与 w_rid", "wts=1700000000" in q1 and "w_rid=" in q1, q1)
chk("换密钥签名变化",
    D.sign_params({"host_mid": "1"}, "abd", wts=1700000000) != q1)
chk("参数按 key 排序", q1.index("host_mid") < q1.index("wts"), q1)

# ---------------------------------------------------------------- 编排层
# 顺序硬约束: collect(只读, 不写 state) -> 渲染 -> 报告写成功 -> commit(state)
def run_collect(cfg, fetched):
    origin = D.fetch_dynamics
    D.fetch_dynamics = lambda *a, **k: fetched
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            res = D.collect_dynamics(cfg)
    finally:
        D.fetch_dynamics = origin
    return res, buf.getvalue()


tmpdir = tempfile.mkdtemp()
tcfg = {"state_dir": tmpdir, "dynamic": {"enabled": True, "limit": 10}}
seen_path = D.seen_path(tcfg)

chk("无 state 文件时不报错", D.load_seen(tcfg) == [] and D.load_last_id(tcfg) == "")

for name, payload in (("空页", empty), ("风控", risk)):
    res, logtxt = run_collect(tcfg, D.classify_payload(payload))
    chk(f"{name}: new 为空", res["new"] == [], res["new"])
    chk(f"{name}: 不写 state", not os.path.exists(seen_path))
    chk(f"{name}: 日志有标记", "没拿到" in logtxt and "不写 state" in logtxt, logtxt.strip())

res_err, _ = run_collect(tcfg, {"status": "error", "items": [], "code": None, "message": "boom"})
chk("异常: new 为空且不写 state", res_err["new"] == [] and not os.path.exists(seen_path))

res_dis, log_dis = run_collect({"state_dir": tmpdir, "dynamic": {"enabled": False}}, empty)
chk("禁用: 状态 disabled", res_dis["status"] == "disabled", res_dis["status"])
chk("禁用: 不写 state", not os.path.exists(seen_path))

ok_page = D.classify_payload(opus_page)
res_ok, _ = run_collect(tcfg, ok_page)
chk("拿到数据: 待处理 2 条", len(res_ok["new"]) == 2, len(res_ok["new"]))
chk("collect 自己不写 state(留给报告成功后)", not os.path.exists(seen_path))

last = D.commit_dynamics(tcfg, res_ok["new"])
chk("commit 后才有 state", os.path.exists(seen_path))
chk("state 记了 last_dynamic_id", last == D.load_last_id(tcfg) != "", last)
chk("state 里 ids 与新增一致",
    set(D.load_seen(tcfg)) == {d["id_str"] for d in res_ok["new"]})
chk("commit 空列表不改 state", D.commit_dynamics(tcfg, []) == last)

before = hashlib.sha256(open(seen_path, "rb").read()).hexdigest()
res_again, _ = run_collect(tcfg, ok_page)
chk("同一页第二次: 无新增", res_again["new"] == [])
after = hashlib.sha256(open(seen_path, "rb").read()).hexdigest()
chk("同一页第二次: state 字节不变", before == after)

with open(seen_path, "w", encoding="utf-8") as fh:
    json.dump(["1", "2"], fh)
chk("兼容旧的纯数组 state", D.load_seen(tcfg) == ["1", "2"])
chk("纯数组格式下 last_id 为空", D.load_last_id(tcfg) == "")

# ---------------------------------------------------------------- 报告一节
R = D and __import__("lidaxiao.report.render", fromlist=["render"])
repdir = tempfile.mkdtemp()
R.report_path = lambda date_str: os.path.join(repdir, f"{date_str}.md")
today = "2026-11-24"

opus_parsed = D.parse_dynamic(raw_opus)
av_parsed = D.parse_dynamic(raw_opus_av)
chk("dynamic_md 有四级标题(时间·类型)",
    R.dynamic_md(opus_parsed).splitlines()[0].startswith("### 2026-")
    and "长文动态" in R.dynamic_md(opus_parsed).splitlines()[0],
    R.dynamic_md(opus_parsed).splitlines()[0])
chk("dynamic_md 带幂等标记",
    f"<!-- dynamic:{opus_parsed['id_str']} -->" in R.dynamic_md(opus_parsed))
chk("dynamic_md 原文按字符截断到 400",
    "原文(813 字): " in R.dynamic_md(opus_parsed)          # 814 原始 - 首尾空白 = 813
    and "截断, 原文 813 字" in R.dynamic_md(opus_parsed),
    [l for l in R.dynamic_md(opus_parsed).splitlines() if l.startswith("原文(")])
chk("截断只保留前 400 字, 不是全篇",
    "1）降低融资门槛" in R.dynamic_md(opus_parsed))
shown = [l for l in R.dynamic_md(opus_parsed).splitlines() if l.startswith("原文(")][0]
chk("截断后正文不超过 400 字",
    len(shown.split(": ", 1)[1].split("…(截断")[0]) <= R.DYNAMIC_TEXT_LIMIT,
    len(shown))
chk("短文本不截断", "截断" not in R.dynamic_md(av_parsed, "看多"))
# id / 时间 / 原文必须来自**同一条**条目: 手拼 dict(拿 A 条的 pub_ts 配 B 条的正文)
# 会让报告出现「同一 id、两个时间」, 阶段 4 验收时就被这坑绊过一次。
import datetime as _dtz
_mt = R.dynamic_md(opus_parsed).splitlines()
chk("### 行时间取本条自己的 pub_ts",
    _mt[0] == "### 2026-11-22 20:07 · 长文动态", _mt[0])
chk("原文与 id 对应(开头是这条 opus 的真实正文)",
    _mt[2].startswith("原文(813 字): 李大霄：救市组合拳有助于加速市场稳定"), _mt[2][:48])
chk("opus 与 AV 两条的 pub_ts 各自独立",
    _dtz.datetime.fromtimestamp(opus_parsed["pub_ts"], R.CN_TZ).strftime("%Y-%m-%d %H:%M") == "2026-11-22 20:07"
    and _dtz.datetime.fromtimestamp(av_parsed["pub_ts"], R.CN_TZ).strftime("%Y-%m-%d %H:%M") == "2026-08-30 21:10",
    (opus_parsed.get("pub_ts"), av_parsed.get("pub_ts")))
chk("有分析时出简版", "简版: 看多" in R.dynamic_md(av_parsed, "看多"))
chk("模型自带「简版:」前缀不重复渲染",
    R.dynamic_md(av_parsed, "简版: 看多").count("简版:") == 1,
    R.dynamic_md(av_parsed, "简版: 看多"))
chk("模型用全角冒号也不重复",
    R.dynamic_md(av_parsed, "简版：看多").count("简版") == 1,
    R.dynamic_md(av_parsed, "简版：看多"))
chk("LLM 失败时标注未分析",
    R.DYNAMIC_UNAVAILABLE in R.dynamic_md(av_parsed, ""), R.dynamic_md(av_parsed, ""))
chk("LLM 失败时原文仍在", "原文(" in R.dynamic_md(av_parsed, ""))

entries = [{"dynamic": opus_parsed, "analysis": "看多, 依据降准"},
           {"dynamic": av_parsed, "analysis": ""}]
added = R.append_dynamics(today, entries)
rp = R.report_path(today)
chk("首次追加 2 条", added == 2, added)
body = open(rp, encoding="utf-8").read()
chk("报告出现 ## 李大霄动态", "## 李大霄动态" in body)
chk("报告有两个动态块", body.count("<!-- dynamic:") == 2, body.count("<!-- dynamic:"))
chk("报告保留原头部", body.startswith("# 📺 李大霄视频日报 2026-11-24"))

# 崩溃窗口: 报告已写、state 丢失, 重跑不得重复追加
snapshot = hashlib.sha256(open(rp, "rb").read()).hexdigest()
added2 = R.append_dynamics(today, entries)
chk("同 id 第二次不重复追加", added2 == 0, added2)
chk("崩溃窗口重跑: 报告字节不变",
    snapshot == hashlib.sha256(open(rp, "rb").read()).hexdigest())

added3 = R.append_dynamics(today, [{"dynamic": opus_parsed, "analysis": "再来一次"}])
chk("同一条单独再跑也不追加", added3 == 0, added3)

# ---------------------------------------------------------------- LLM 降级
L = __import__("lidaxiao.analyze.llm", fromlist=["llm"])


def call_with_broken_llm(fn):
    origin = L.call_llm
    L.call_llm = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("所有通道都挂了"))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            out = fn()
    finally:
        L.call_llm = origin
    return out, buf.getvalue()


out, logtxt = call_with_broken_llm(lambda: L.analyze_dynamic({}, None, opus_parsed))
chk("LLM 全通道失败: analyze_dynamic 返回空串", out == "", repr(out))
chk("LLM 失败: 只记一条日志且说明仅出原文",
    logtxt.count("仅出原文") == 1, logtxt.strip())
chk("LLM 失败: 不抛异常", True)
chk("LLM 失败: 渲染仍出原文并标注未分析",
    "原文(813 字)" in R.dynamic_md(opus_parsed, out)
    and R.DYNAMIC_UNAVAILABLE in R.dynamic_md(opus_parsed, out))
chk("空正文动态不调用 LLM", L.analyze_dynamic({}, None, {"id_str": "1"}) == "")

# ---------------------------------------------------------------- 报告不被视频写盘抹掉
A = __import__("lidaxiao.audit", fromlist=["audit"])

# 改动前 resort_report 的原样副本: 只用来证「视频链路输出零变化」
def _ordered(txt, first, second, first_last=False, second_last=False):
    """first 出现在 second 之前吗; 任一方缺失返回 False, 不抛异常。"""
    a = txt.rfind(first) if first_last else txt.find(first)
    b = txt.rfind(second) if second_last else txt.find(second)
    return a >= 0 and b >= 0 and a < b


def _legacy_resort(text):
    """BUG-12 排序统一: 今日摘要 / 视频清单 / 正文章节 统一按发布时间升序。"""
    if not text:
        return text
    lines = text.splitlines()

    # 1) 从清单行抽取 时间 与 BV
    times = {}          # bvid -> "HH:MM"
    title_time = {}     # 标题 -> "HH:MM"
    for ln in lines:
        m = re.match(r"^-\s*\[(\d{2}:\d{2})\]\s*(.+?)\s*\((BV\w+)\)", ln)
        if m:
            times[m.group(3)] = m.group(1)
            title_time[m.group(2).strip()] = m.group(1)
    key_of = lambda s: times.get(s) or title_time.get(s) or "99:99"

    # 2) 切成 摘要块 / 清单块 / 章节块
    idx = {}
    for i, ln in enumerate(lines):
        if ln.startswith("## 今日摘要"):
            idx["sum"] = i
        elif ln.startswith("## 视频清单"):
            idx["list"] = i
    if "sum" not in idx or "list" not in idx:
        return text

    head = lines[:idx["sum"]]
    sum_hdr = lines[idx["sum"]]
    list_hdr = lines[idx["list"]]

    # 摘要行: 摘要头之后到清单头之间
    sum_fill = [l for l in lines[idx["sum"] + 1:idx["list"]] if l.startswith("- ")]
    pre_list = [l for l in lines[idx["sum"] + 1:idx["list"]] if not l.startswith("- ")]
    list_fill = [l for l in lines[idx["list"] + 1:] if l.startswith("- [")]
    # 清单之后第一个 "## 视频" 开始为章节区
    sec_start = None
    for i in range(idx["list"] + 1, len(lines)):
        if re.match(r"^##\s*视频\d*[:：]", lines[i]):
            sec_start = i
            break
    if sec_start is None:
        tail = lines[idx["list"] + 1:]
        sections = []
    else:
        tail = []
        sections = []
        cur = None
        for ln in lines[sec_start:]:
            if re.match(r"^##\s*视频\d*[:：]", ln):
                if cur is not None:
                    sections.append(cur)
                cur = [ln]
            elif cur is not None:
                cur.append(ln)
        if cur is not None:
            sections.append(cur)

    def sec_key(block):
        m = re.search(r"\((BV\w+)\)", block[0])
        return key_of(m.group(1)) if m else "99:99"

    def title_of(ln):
        m = re.search(r"视频《(.+?)》", ln)
        return m.group(1).strip() if m else ""

    sum_fill.sort(key=lambda l: key_of(title_of(l)))
    list_fill.sort(key=lambda l: key_of(re.search(r"\((BV\w+)\)", l).group(1))
                   if re.search(r"\((BV\w+)\)", l) else "99:99")
    sections.sort(key=sec_key)

    # 3) 重排章节编号
    out_secs = []
    for n, blk in enumerate(sections, 1):
        first = re.sub(r"^##\s*视频\d*[:：]", f"## 视频{n}:", blk[0])
        out_secs.extend([first] + blk[1:])

    out = list(head) + [sum_hdr] + sum_fill
    if pre_list:
        out += ([""] if pre_list[0].strip() else []) + [l for l in pre_list if l.strip()]
    out += ["", list_hdr] + list_fill
    while out and not out[-1].strip():
        out.pop()
    out.append("")
    out += out_secs
    return "\n".join(out).strip() + "\n"

repdir2 = tempfile.mkdtemp()
R.report_path = lambda date_str: os.path.join(repdir2, f"{date_str}.md")
day = "2026-11-24"
R.append_dynamics(day, entries)
rp2 = R.report_path(day)
chk("动态先落盘: 报告已建且含动态节",
    "## 李大霄动态" in R.read_text(rp2))

# 同一天再来一个视频写盘(走真实 upsert_report, 内部会调 resort_report)
import datetime as _dt
_ts = int(_dt.datetime(2026, 11, 24, 12, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp())
video = {"bvid": "BV1test00001", "title": "测试视频", "pubdate": _ts}
section = "## 视频1: 《测试视频》 (BV1test00001)\n\n【一句话摘要】测试\n"
R.upsert_report(video, section,
                "- 视频《测试视频》: 测试摘要",
                "- [12:00] 测试视频 (BV1test00001)")
after = R.read_text(rp2)
chk("视频写盘后动态节仍在", "## 李大霄动态" in after)
chk("视频写盘后动态标记仍在",
    after.count("<!-- dynamic:") == 2, after.count("<!-- dynamic:"))
chk("视频写盘后视频节也在", "## 视频1: 《测试视频》" in after, after[:200])
chk("动态节排在视频章节之后",
    _ordered(after, "## 视频1: 《测试视频》", "## 李大霄动态"),
    [l for l in after.splitlines() if l.startswith("## ")])

# 两支视频 + 动态夹在中间(A 场景: 先写动态, 再陆续来两支视频)
# 旧实现按任意 "## " 切块时, 动态会停在视频1 与视频2 之间; 必须是最后一节。
repdir_a = tempfile.mkdtemp()
R.report_path = lambda date_str: os.path.join(repdir_a, f"{date_str}.md")
day_a = "2026-11-26"
R.append_dynamics(day_a, entries)
_ts_a1 = int(_dt.datetime(2026, 11, 26, 9, 0,
                          tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp())
_ts_a2 = int(_dt.datetime(2026, 11, 26, 15, 0,
                          tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp())
R.upsert_report({"bvid": "BV1aa000001", "title": "第一支", "pubdate": _ts_a1},
                "## 视频1: 《第一支》 (BV1aa000001)\n\n【一句话摘要】一\n",
                "- 视频《第一支》: 摘要一", "- [09:00] 第一支 (BV1aa000001)")
R.upsert_report({"bvid": "BV1bb000002", "title": "第二支", "pubdate": _ts_a2},
                "## 视频1: 《第二支》 (BV1bb000002)\n\n【一句话摘要】二\n",
                "- 视频《第二支》: 摘要二", "- [15:00] 第二支 (BV1bb000002)")
a2 = R.read_text(R.report_path(day_a))
_heads = [l for l in a2.splitlines() if l.startswith("## ")]
_tail = [l for l in a2.splitlines() if l.strip()][-1]
chk("两支视频: 动态节仍在", "## 李大霄动态" in a2, _heads)
chk("两支视频: 动态节在视频章节之后、免责声明之前",
    "## 李大霄动态" in a2 and "## 视频1: 《第一支》" in a2 and "## 视频2: 《第二支》" in a2
    and _ordered(a2, "## 视频2: 《第二支》", "## 李大霄动态")
    and _ordered(a2, "## 李大霄动态", R.DISCLAIMER, second_last=True),
    _heads)
chk("两支视频: 末行仍是免责声明", _tail == R.DISCLAIMER, _tail)
chk("两支视频: 声明只有一条", a2.count(R.DISCLAIMER) == 1, a2.count(R.DISCLAIMER))
chk("两支视频: 动态标记仍在", a2.count("<!-- dynamic:") == 2, a2.count("<!-- dynamic:"))
chk("两支视频: 两个视频节都在",
    a2.count("## 视频") == 3 and "《第二支》" in a2, _heads)
chk("两支视频: 动态没被夹在视频之间",
    _ordered(a2, "《第二支》", "## 李大霄动态"), _heads)
# 幂等: 位置变了, 标记判重仍须生效(第二次不写, 字节不变)
_snap_a = hashlib.sha256(open(R.report_path(day_a), "rb").read()).hexdigest()
_added_a = R.append_dynamics(day_a, entries)
chk("两支视频: 同 id 再追加不写入", _added_a == 0, _added_a)
chk("两支视频: 再追加后报告字节不变",
    _snap_a == hashlib.sha256(open(R.report_path(day_a), "rb").read()).hexdigest())
chk("两支视频: 再追加后末行仍声明",
    [l for l in R.read_text(R.report_path(day_a)).splitlines() if l.strip()][-1]
    == R.DISCLAIMER)

# 新建日报(当天没新视频, 由动态建文件): 必须含且仅含一条声明, 且声明是末行
_nd = "2026-11-27"
_nd_dir = tempfile.mkdtemp()
R.report_path = lambda date_str: os.path.join(_nd_dir, f"{date_str}.md")
R.append_dynamics(_nd, entries)
_ndtxt = R.read_text(R.report_path(_nd))
chk("新建日报: 含且仅含一条免责声明", _ndtxt.count(R.DISCLAIMER) == 1,
    _ndtxt.count(R.DISCLAIMER))
chk("新建日报: 末行是免责声明",
    [l for l in _ndtxt.splitlines() if l.strip()][-1] == R.DISCLAIMER,
    [l for l in _ndtxt.splitlines() if l.strip()][-1])
chk("新建日报: 动态标记齐全", _ndtxt.count("<!-- dynamic:") == 2,
    _ndtxt.count("<!-- dynamic:"))

# 已有日报(每支视频各自以声明收尾, 像真实视频日报那样)追加动态:
# 版面 = 视频正文 → 视频自己的声明 → ## 李大霄动态 → 动态节自带收尾声明。
# 插入点是 EOF, 不许再「插在最后一条声明之前」——那会把上一支视频拦腰截断。
_ex_dir = tempfile.mkdtemp()
R.report_path = lambda date_str: os.path.join(_ex_dir, f"{date_str}.md")
_ex_day = "2026-11-29"
_ex_text = ("# 📺 李大霄视频日报 2026-11-29\n\n"
            "## 今日摘要\n- 视频《一支》: 摘要\n\n"
            "## 视频清单\n- [09:00] 一支 (BV1ex00001)\n\n"
            "## 视频1: 《一支》 (BV1ex00001)\n\n"
            "【片尾暗示】白色小熊玩偶 → 熊市信号\n\n"
            + R.DISCLAIMER + "\n")
with open(R.report_path(_ex_day), "w", encoding="utf-8") as fh:
    fh.write(_ex_text)
_ex_added = R.append_dynamics(_ex_day, entries)
_ex_txt = R.read_text(R.report_path(_ex_day))
_ex_nb = [l for l in _ex_txt.splitlines() if l.strip()]
chk("已有日报追加: 写入条数", _ex_added == 2, _ex_added)
chk("已有日报追加: 视频正文与它自己的声明仍相邻(视频块没被切开)",
    _ordered(_ex_txt, "【片尾暗示】白色小熊玩偶", R.DISCLAIMER)
    and not any(l.startswith("## 李大霄动态")
                for l in _ex_txt[_ex_txt.index("【片尾暗示】白色小熊玩偶"):
                                 _ex_txt.index(R.DISCLAIMER)].splitlines()),
    _ex_nb[-6:])
chk("已有日报追加: 动态节排在视频块声明之后",
    _ordered(_ex_txt, R.DISCLAIMER, "## 李大霄动态"), _ex_nb[-6:])
chk("已有日报追加: 末行仍是免责声明", _ex_nb[-1] == R.DISCLAIMER, _ex_nb[-1])
chk("已有日报追加: 声明 = 原 1 条 + 动态节收尾 1 条", _ex_txt.count(R.DISCLAIMER) == 2,
    _ex_txt.count(R.DISCLAIMER))
chk("已有日报追加: 收尾声明在动态内容之后",
    _ordered(_ex_txt, "<!-- dynamic:", R.DISCLAIMER,
             first_last=True, second_last=True), _ex_nb[-3:])
# 二次追加(有新 id): 不许再补一条声明, 新块插在动态节自己的收尾声明之前
_ex_opus2 = dict(opus_parsed, id_str="1273504300584140999")
_ex_added2 = R.append_dynamics(_ex_day, [{"dynamic": _ex_opus2, "analysis": "再看"}])
_ex_txt2 = R.read_text(R.report_path(_ex_day))
chk("已有日报二次追加: 新块写入", _ex_added2 == 1, _ex_added2)
chk("已有日报二次追加: 声明条数不涨", _ex_txt2.count(R.DISCLAIMER) == 2,
    _ex_txt2.count(R.DISCLAIMER))
chk("已有日报二次追加: 末行仍是声明",
    [l for l in _ex_txt2.splitlines() if l.strip()][-1] == R.DISCLAIMER)
chk("已有日报二次追加: 新块在收尾声明之前",
    _ordered(_ex_txt2, "1273504300584140999", R.DISCLAIMER, second_last=True),
    [l for l in _ex_txt2.splitlines() if l.strip()][-3:])

# 「分析失败、末行是 ⚠️ AI分析失败」的日报(真实 2026-11-03.md 就是这种形态):
# 追加后末行会变成声明 —— 有意的版面收敛, 不是 bug(见陷阱表)。
_fail_day = "2026-12-01"
_fail_dir = tempfile.mkdtemp()
R.report_path = lambda date_str: os.path.join(_fail_dir, f"{date_str}.md")
with open(R.report_path(_fail_day), "w", encoding="utf-8") as fh:
    fh.write("# 📺 李大霄视频日报 2026-12-01\n\n## 今日摘要\n\n## 视频清单\n\n"
             "## 视频1: 《一支》 (BV1ex00002)\n\n【一句话摘要】一\n\n"
             + R.DISCLAIMER + "\n\n> ⚠️ AI分析失败, 仅保留字幕\n")
R.append_dynamics(_fail_day, entries)
_fail_txt = R.read_text(R.report_path(_fail_day))
chk("AI失败形态: 追加后末行收敛为声明",
    [l for l in _fail_txt.splitlines() if l.strip()][-1] == R.DISCLAIMER,
    [l for l in _fail_txt.splitlines() if l.strip()][-2:])
chk("AI失败形态: 原来那行失败提示没被删掉", "AI分析失败" in _fail_txt)

# 未知顶层节: 反向枚举的代价是位置可能靠后, 但绝不许丢内容
# 未知顶层节: 反向枚举的代价是位置可能靠后, 但绝不许丢内容
_unknown = """# 📺 李大霄视频日报 2026-11-28

## 今日摘要
- 视频《一支》: 摘要

## 视频清单
- [09:00] 一支 (BV1unk0001)

## 视频1: 《一支》 (BV1unk0001)

【一句话摘要】一支

## 本周总结

本周主线: 稳市组合拳。
"""
_u_out = A.resort_report(_unknown)
chk("未知节不丢弃", "## 本周总结" in _u_out and "本周主线: 稳市组合拳。" in _u_out,
    _u_out[-200:])
chk("未知节排在视频章节之后",
    _ordered(_u_out, "## 视频1: 《一支》", "## 本周总结"), _u_out[-200:])

# 只有视频的报告: 新实现必须与改动前逐字节一致(视频链路零变化)
VIDEO_ONLY_A = """# 📺 李大霄视频日报 2026-11-24

## 今日摘要
- 视频《B视频》: 摘要B
- 视频《A视频》: 摘要A

## 视频清单
- [11:00] B视频 (BV1bbb)
- [09:00] A视频 (BV1aaa)

## 视频1: 《A视频》 (BV1aaa)

【一句话摘要】A

## 视频2: 《B视频》 (BV1bbb)

【一句话摘要】B
"""
VIDEO_ONLY_B = """# 📺 李大霄视频日报 2026-11-24

## 今日摘要
- 视频《只有一条》: 摘要

## 视频清单
- [10:00] 只有一条 (BV1only)

## 视频1: 《只有一条》 (BV1only)

【一句话摘要】只有一条
"""
for name, sample in (("两视频乱序", VIDEO_ONLY_A), ("单视频", VIDEO_ONLY_B)):
    chk(f"视频链路零变化: {name}", A.resort_report(sample) == _legacy_resort(sample))
chk("resort 幂等", A.resort_report(A.resort_report(VIDEO_ONLY_A)) == A.resort_report(VIDEO_ONLY_A))

# 真实日报全量对拍: 视频链路零变化不能只靠上面两份构造样本。
# 真实日报在清单之后用的是 `## 【一句话摘要】` 这类**视频正文小节**, 它们必须并回
# 所属视频块; 一旦块边界放宽(把任意 "## " 当边界), 标题会与视频脱钩、挤到所有视频之后。
_rep_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "reports")
_real = sorted(glob.glob(os.path.join(_rep_dir, "*.md")))
_bad = []
for _p in _real:
    _t = open(_p, encoding="utf-8").read()
    if A.resort_report(_t) != _legacy_resort(_t):
        _bad.append(os.path.basename(_p))
chk(f"真实日报 {len(_real)} 份与旧实现逐字节相同", _real and not _bad, _bad[:5])
chk("真实日报含视频正文小节(否则上一条是空转)",
    any("## 【一句话摘要】" in open(_p, encoding="utf-8").read() for _p in _real))

# 视频列表没拉到 -> 不许新建当日日报
repdir3 = tempfile.mkdtemp()
R.report_path = lambda date_str: os.path.join(repdir3, f"{date_str}.md")
added_no = R.append_dynamics("2026-11-25", entries, allow_create=False)
chk("列表失败路径不新建日报", added_no == 0 and not os.path.exists(R.report_path("2026-11-25")))
added_yes = R.append_dynamics("2026-11-25", entries, allow_create=True)
chk("正常路径仍可新建", added_yes == 2 and os.path.exists(R.report_path("2026-11-25")))

# ---------------------------------------------------------------- state 门禁
# 「state 不许先于报告落盘」的编排侧钉桩: append_dynamics 没落盘就一条都不推进。
import monitor as M

_orig = (M.collect_dynamics, M.analyze_dynamic, M.append_dynamics, M.commit_dynamics)
_saved = []
try:
    # (a) 视频列表失败那条路径: allow_create=False -> 落盘 0 条 -> state 一个字节都不许写
    gate_dir = tempfile.mkdtemp()
    gcfg = {"state_dir": gate_dir, "dynamic": {"enabled": True, "limit": 10}}
    gseen = D.seen_path(gcfg)
    M.collect_dynamics = lambda cfg: {"status": "ok", "new": [opus_parsed, av_parsed]}
    M.analyze_dynamic = lambda cfg, key, d: ""
    M.append_dynamics = lambda date_str, ents, allow_create=True: 0
    M.commit_dynamics = lambda cfg, ds: _saved.append(list(ds))
    got = M.process_dynamics(gcfg, "KEY", False)
    chk("列表失败路径: 报告没落盘就一条都不推进", got == 0 and _saved == [], (got, _saved))
    chk("列表失败路径: 不创建 state 文件", not os.path.exists(gseen))

    # (b) 正常路径: added == len(new) 时 state 照常推进
    ok_dir = tempfile.mkdtemp()
    ocfg = {"state_dir": ok_dir, "dynamic": {"enabled": True, "limit": 10}}
    M.append_dynamics = lambda date_str, ents, allow_create=True: len(ents)
    got2 = M.process_dynamics(ocfg, "KEY", True)
    chk("正常路径: 写全了就推进 state", got2 == 2 and len(_saved) == 1 and len(_saved[0]) == 2,
        (got2, len(_saved)))
finally:
    (M.collect_dynamics, M.analyze_dynamic,
     M.append_dynamics, M.commit_dynamics) = _orig

# 上面 (b) 用的是真 commit_dynamics 吗? 不是 —— 再跑一遍不替换 commit, 证真 state 会落盘
_real_commit = M.commit_dynamics
try:
    real_dir = tempfile.mkdtemp()
    rcfg = {"state_dir": real_dir, "dynamic": {"enabled": True, "limit": 10}}
    M.collect_dynamics = lambda cfg: {"status": "ok", "new": [opus_parsed, av_parsed]}
    M.analyze_dynamic = lambda cfg, key, d: ""
    M.append_dynamics = lambda date_str, ents, allow_create=True: len(ents)
    M.commit_dynamics = D.commit_dynamics
    got3 = M.process_dynamics(rcfg, "KEY", True)
    chk("正常路径: 真实 commit 后 state 文件确实落盘",
        got3 == 2 and os.path.exists(D.seen_path(rcfg))
        and set(D.load_seen(rcfg)) == {opus_parsed["id_str"], av_parsed["id_str"]},
        (got3, D.load_seen(rcfg)))
finally:
    M.collect_dynamics, M.analyze_dynamic, M.append_dynamics, M.commit_dynamics = _orig

# ---------------------------------------------------------------- 日期口径
# 线上 2026-11-24 02:37 那次处理的是 11-23 发布的视频, 报告 = 2026-11-23.md。
# 动态若按 now() 取日期, 会另建一份 2026-11-24.md 平行骨架(只有摘要/清单空壳)。
_wd = tempfile.mkdtemp()
os.makedirs(os.path.join(_wd, "reports"))
for _name in ("2026-11-20.md", "2026-11-22.md", "index.md", "latest.md"):
    open(os.path.join(_wd, "reports", _name), "w").close()
_saved_wd = R.WORKDIR
R.WORKDIR = _wd
try:
    chk("动态目标日期: 本次处理的视频报告优先(线上那次是 11-23 不是 11-24)",
        M.dynamics_target_date([os.path.join(_wd, "reports", "2026-11-23.md")],
                               today="2026-11-24") == "2026-11-23")
    chk("动态目标日期: 多份报告取日期最新那份",
        M.dynamics_target_date([os.path.join(_wd, "reports", "2026-11-19.md"),
                                os.path.join(_wd, "reports", "2026-11-21.md")]) == "2026-11-21")
    chk("动态目标日期: 没处理视频时用今天(不往旧日报里塞)",
        M.dynamics_target_date([], today="2026-11-24") == "2026-11-24")
    chk("动态目标日期: index.md / latest.md 不算日报",
        R.report_date_of(os.path.join(_wd, "reports", "latest.md")) == ""
        and R.report_date_of(os.path.join(_wd, "reports", "index.md")) == "")
finally:
    R.WORKDIR = _saved_wd
_empty_wd = tempfile.mkdtemp()
R.WORKDIR = _empty_wd
try:
    chk("动态目标日期: 没有 touched 时一律当天, 与有无旧日报无关",
        M.dynamics_target_date([], today="2026-12-31") == "2026-12-31")
finally:
    R.WORKDIR = _saved_wd

# 端到端复现线上形态: reports 里只有 2026-11-23.md, 动态必须落进去, 不许造 2026-11-24.md
_online_dir = tempfile.mkdtemp()
R.report_path = lambda date_str: os.path.join(_online_dir, f"{date_str}.md")
with open(R.report_path("2026-11-23"), "w", encoding="utf-8") as fh:
    fh.write("# 📺 李大霄视频日报 2026-11-23\n\n"
             "## 今日摘要\n- 视频《一支》: 摘要\n\n"
             "## 视频清单\n- [09:00] 一支 (BV1on00001)\n\n"
             "## 视频1: 《一支》 (BV1on00001)\n\n【一句话摘要】一\n\n"
             + R.DISCLAIMER + "\n")
_orig2 = (M.collect_dynamics, M.analyze_dynamic, M.commit_dynamics)
try:
    M.collect_dynamics = lambda cfg: {"status": "ok", "new": [opus_parsed]}
    M.analyze_dynamic = lambda cfg, key, d: "看多"
    M.commit_dynamics = lambda cfg, ds: None
    _on_added = M.process_dynamics({"state_dir": tempfile.mkdtemp(),
                                    "dynamic": {"enabled": True}},
                                   "KEY", True,
                                   [R.report_path("2026-11-23")])
finally:
    M.collect_dynamics, M.analyze_dynamic, M.commit_dynamics = _orig2
_on_files = sorted(os.listdir(_online_dir))
_on_txt = R.read_text(R.report_path("2026-11-23"))
chk("线上形态: 动态写进本次处理的 2026-11-23.md", _on_added == 1
    and "## 李大霄动态" in _on_txt and "<!-- dynamic:" in _on_txt, (_on_added, _on_files))
chk("线上形态: 不许出现 2026-11-24.md 平行骨架",
    "2026-11-24.md" not in _on_files, _on_files)
chk("线上形态: 报告目录只多出 0 个文件", _on_files == ["2026-11-23.md"], _on_files)
chk("线上形态: 视频节与动态节同在一份文件, 末行是声明",
    "## 视频1: 《一支》" in _on_txt
    and [l for l in _on_txt.splitlines() if l.strip()][-1] == R.DISCLAIMER)

# ---------------------------------------------------------------- 落盘硬约束
# ③ 视频列表失败: 由「列表失败」这个条件本身拦, 不靠 allow_create / 文件在不在。
# 以前 allow_create=False 只挡新建, 挡不住往已存在的旧报告里追加(验收员实测:
# 旧报告被写 + state 推进 2 条)。这里用真实 append_dynamics, 只桩掉抓取/分析/commit。
def _run_pd(report_dir, video_ok, touched, items, commit_records):
    R.report_path = lambda ds: os.path.join(report_dir, f"{ds}.md")
    saved = (M.collect_dynamics, M.analyze_dynamic, M.commit_dynamics)
    M.collect_dynamics = lambda cfg: {"status": "ok", "new": items}
    M.analyze_dynamic = lambda cfg, k, d: "看多"
    M.commit_dynamics = lambda cfg, ds: commit_records.append([d["id_str"] for d in ds])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            added = M.process_dynamics({"state_dir": tempfile.mkdtemp(),
                                        "dynamic": {"enabled": True}},
                                       "KEY", video_ok, touched)
    finally:
        M.collect_dynamics, M.analyze_dynamic, M.commit_dynamics = saved
    return added, buf.getvalue()


print("== 落盘硬约束 ==")
_old_dir = tempfile.mkdtemp()
_old_rp = os.path.join(_old_dir, "2026-11-03.md")
_old_text = ("# 📺 李大霄视频日报 2026-11-03\n\n## 今日摘要\n- 视频《旧》: 摘要\n\n"
             "## 视频清单\n- [09:00] 旧 (BV1old00001)\n\n"
             "## 视频1: 《旧》 (BV1old00001)\n\n【一句话摘要】旧\n\n"
             + R.DISCLAIMER + "\n")
with open(_old_rp, "w", encoding="utf-8") as fh:
    fh.write(_old_text)
_old_hash = hashlib.sha256(open(_old_rp, "rb").read()).hexdigest()
_seen_before = sorted(os.listdir(_old_dir))
_rec = []
_added_fail, _log_fail = _run_pd(_old_dir, False, [], [opus_parsed, av_parsed], _rec)
chk("列表失败+旧报告存在: 返回 0", _added_fail == 0, _added_fail)
chk("列表失败+旧报告存在: 旧报告字节不变",
    _old_hash == hashlib.sha256(open(_old_rp, "rb").read()).hexdigest())
chk("列表失败+旧报告存在: 报告目录零新增", sorted(os.listdir(_old_dir)) == _seen_before,
    sorted(os.listdir(_old_dir)))
chk("列表失败+旧报告存在: 不推进 state", _rec == [], _rec)
chk("列表失败+旧报告存在: 日志一行「不落盘」",
    _log_fail.count("不落盘") == 1 and "不推进 state" in _log_fail, _log_fail.strip()[-160:])
chk("列表失败: 不写 dynamics_seen.json",
    not any("dynamics_seen" in x for x in os.listdir(_old_dir)))

# ② 无新视频(列表拿到了) + 有动态 -> 新建当天文件, 不含「0 条」, 末行是声明
_today_dir = tempfile.mkdtemp()
with open(os.path.join(_today_dir, "2026-11-03.md"), "w", encoding="utf-8") as fh:
    fh.write(_old_text)
_rec2 = []
_added_new, _log_new = _run_pd(_today_dir, True, [], [opus_parsed], _rec2)
_today = M.dynamics_target_date([], None)
chk("无新视频+有动态: 新建当天那份",
    _added_new == 1 and os.path.exists(os.path.join(_today_dir, f"{_today}.md")),
    (_added_new, sorted(os.listdir(_today_dir))))
_today_txt = open(os.path.join(_today_dir, f"{_today}.md"), encoding="utf-8").read()
chk("无新视频+有动态: 不含「0 条」类表述",
    "0 条" not in _today_txt and "今日视频 0" not in _today_txt, _today_txt[:120])
chk("无新视频+有动态: 末行是声明",
    [l for l in _today_txt.splitlines() if l.strip()][-1] == R.DISCLAIMER)
chk("无新视频+有动态: 没往旧报告里塞",
    "## 李大霄动态" in _today_txt
    and "## 李大霄动态" not in open(os.path.join(_today_dir, "2026-11-03.md"),
                                    encoding="utf-8").read())
chk("无新视频+有动态: state 推进 1 条", _rec2 == [[opus_parsed["id_str"]]], _rec2)

# ---------------------------------------------------------------- 原子写 / 残留
print("== 原子写 ==")
from lidaxiao.textutil import sweep_temp_files, write_text as T_write
_at = tempfile.mkdtemp()
_tp = os.path.join(_at, "2026-11-24.md")
T_write(_tp, "v1")
T_write(_tp, "v2")
chk("原子写后内容是最后的版本", open(_tp, encoding="utf-8").read() == "v2")
chk("原子写不留 tmp", [x for x in os.listdir(_at) if ".tmp." in x] == [],
    os.listdir(_at))
_orig_replace = os.replace


def _boom(a, b):
    raise OSError("模拟写到一半被杀")


os.replace = _boom
try:
    try:
        T_write(_tp, "v3-should-not-land")
    except OSError:
        pass
finally:
    os.replace = _orig_replace
chk("写失败时主文件仍是旧版(没有半份)", open(_tp, encoding="utf-8").read() == "v2")
chk("写失败后 tmp 被清理", [x for x in os.listdir(_at) if ".tmp." in x] == [],
    os.listdir(_at))
# 残留识别: 够旧的 tmp 被清, 新鲜的留着(别抢正在写的)
open(os.path.join(_at, "old.md.tmp.123"), "w").close()
open(os.path.join(_at, "new.md.tmp.456"), "w").close()
_old_ts = __import__("time").time() - 3600
os.utime(os.path.join(_at, "old.md.tmp.123"), (_old_ts, _old_ts))
_removed = sweep_temp_files(_at)
chk("残留清扫只清够旧的", [os.path.basename(x) for x in _removed] == ["old.md.tmp.123"],
    _removed)
chk("新鲜 tmp 不被动", os.path.exists(os.path.join(_at, "new.md.tmp.456")))
chk("普通文件不受影响", os.path.exists(_tp))

# state 落盘也是原子写
_sd = tempfile.mkdtemp()
_scfg = {"state_dir": _sd, "dynamic": {"enabled": True}}
D.save_seen({"a", "b"}, _scfg, last_id="b")
_sp = D.seen_path(_scfg)
chk("state 写出后无 tmp 残留", [x for x in os.listdir(_sd) if ".tmp." in x] == [],
    os.listdir(_sd))
chk("state 内容可读回", set(D.load_seen(_scfg)) == {"a", "b"}
    and D.load_last_id(_scfg) == "b")

# ---------------------------------------------------------------- 已发布的不许重写
# 第二次 run 把 13 条全重新分析了一遍, 节 sha 从 19da19eb 变 76a3bdaa:
# 没重复, 但已发布的分析被模型随机性改写, 还白花一次额度。标准是两 run 节 sha 相同。
print("== 已发布动态原样复用 ==")
_re_dir = tempfile.mkdtemp()
_re_path = os.path.join(_re_dir, "2026-11-30.md")
_pub1 = dict(opus_parsed)
_pub2 = dict(av_parsed)
with open(_re_path, "w", encoding="utf-8") as fh:
    fh.write("# 📺 李大霄视频日报 2026-11-30\n\n## 今日摘要\n- 视频《一》: 摘要\n\n"
             "## 视频清单\n- [09:00] 一 (BV1re00001)\n\n"
             "## 视频1: 《一》 (BV1re00001)\n\n【一句话摘要】一\n\n"
             + R.DISCLAIMER + "\n\n## 李大霄动态\n\n"
             + "\n\n".join([R.dynamic_md(_pub1, "第一次的分析 A"),
                             R.dynamic_md(_pub2, "第一次的分析 B")])
             + "\n\n" + R.DISCLAIMER + "\n")
_before_txt = open(_re_path, encoding="utf-8").read()
_before_sec = _before_txt[_before_txt.index("## 李大霄动态"):]
_new_item = dict(opus_parsed, id_str="1273504300584140777")
_calls = []

saved_pd = (M.collect_dynamics, M.analyze_dynamic, M.commit_dynamics)
_committed = []
M.collect_dynamics = lambda cfg: {"status": "ok", "new": [_pub1, _pub2, _new_item]}
M.analyze_dynamic = lambda cfg, k, d: (_calls.append(d["id_str"]), "第二次的分析")[1]
M.commit_dynamics = lambda cfg, ds: _committed.append([d["id_str"] for d in ds])
R.report_path = lambda ds: os.path.join(_re_dir, f"{ds}.md")
_M_rp_saved = M.report_path
M.report_path = R.report_path          # monitor 按名 import 了 report_path, 两边都要打
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        _added_re = M.process_dynamics({"state_dir": tempfile.mkdtemp(),
                                        "dynamic": {"enabled": True}},
                                       "KEY", True, [_re_path])
finally:
    M.collect_dynamics, M.analyze_dynamic, M.commit_dynamics = saved_pd
_after_txt = open(_re_path, encoding="utf-8").read()
chk("只分析新 id, 已发布的 2 条不调 LLM",
    _calls == ["1273504300584140777"], _calls)
chk("只追加 1 条", _added_re == 1, _added_re)
chk("已发布的两条原样逐字节仍在(整块比对)",
    R.dynamic_md(_pub1, "第一次的分析 A") in _after_txt
    and R.dynamic_md(_pub2, "第一次的分析 B") in _after_txt, "旧块被改写")
chk("已发布的没有被第二次的分析覆盖", "第二次的分析" in _after_txt
    and _after_txt.count("第二次的分析") == 1, _after_txt.count("第二次的分析"))
chk("新条目追加在节内", "1273504300584140777" in _after_txt)
chk("末行仍是声明", [l for l in _after_txt.splitlines() if l.strip()][-1] == R.DISCLAIMER)
chk("state 推进 3 条(含已发布的)", _committed == [[str(_pub1["id_str"]),
                                                   str(_pub2["id_str"]),
                                                   "1273504300584140777"]], _committed)

# 第二次 run: 三条都已在报告里 -> 0 次 LLM, 报告字节不变(节 sha 相同)
_sha_before = hashlib.sha256(open(_re_path, "rb").read()).hexdigest()
_calls2 = []
M.collect_dynamics = lambda cfg: {"status": "ok", "new": [_pub1, _pub2, _new_item]}
M.analyze_dynamic = lambda cfg, k, d: (_calls2.append(d["id_str"]), "不该被调用")[1]
M.commit_dynamics = lambda cfg, ds: None
buf2 = io.StringIO()
try:
    with contextlib.redirect_stdout(buf2):
        _added_re2 = M.process_dynamics({"state_dir": tempfile.mkdtemp(),
                                         "dynamic": {"enabled": True}},
                                        "KEY", True, [_re_path])
finally:
    M.collect_dynamics, M.analyze_dynamic, M.commit_dynamics = saved_pd
    M.report_path = _M_rp_saved
chk("第二次 run: 一条都不重新分析", _calls2 == [], _calls2)
chk("第二次 run: 0 追加", _added_re2 == 0, _added_re2)
chk("第二次 run: 报告字节不变(节 sha 相同)",
    _sha_before == hashlib.sha256(open(_re_path, "rb").read()).hexdigest())

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
