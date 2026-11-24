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
import json
import os
import sys

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

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
