"""报告渲染 / 章节合并 / 后处理 (从 monitor.py 抽出, 逻辑未改)"""
import datetime
import os
import re
import time

from lidaxiao.common import CN_TZ, DISCLAIMER, WORKDIR
from lidaxiao.textutil import CLEAN_RE, log, norm_text, read_text, write_text

try:
    from lidaxiao import audit as _audit
except Exception:            # 模块缺失时退化为无审计
    _audit = None


def report_path(date_str):
    return os.path.join(WORKDIR, "reports", f"{date_str}.md")

REPORT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def report_date_of(path):
    """报告文件路径 -> 日期(YYYY-MM-DD); 不是日报文件名(如 index.md/latest.md)返回 ""。"""
    name = os.path.splitext(os.path.basename(path or ""))[0]
    return name if REPORT_DATE_RE.match(name) else ""


def parse_report(text):
    """解析报告 -> {summary_lines, list_lines, sections:{bvid:(start,end)}}"""
    res = {"summary_lines": [], "list_lines": [], "sections": {}}
    lines = text.splitlines()
    in_summary = in_list = False
    cur_bvid = None
    sec_start = None
    for i, ln in enumerate(lines):
        if ln.startswith("## 今日摘要"):
            in_summary, in_list = True, False
            continue
        if ln.startswith("## 视频清单"):
            in_summary, in_list = False, True
            continue
        m = re.match(r"^## 视频\d+:《(.+?)》\s*\(?(BV\w+)\)?\s*$", ln)
        if m:
            if cur_bvid and sec_start is not None:
                res["sections"][cur_bvid] = (sec_start, i)
            cur_bvid, sec_start = m.group(2), i
            in_summary = in_list = False
            continue
        if in_summary and ln.startswith("- "):
            res["summary_lines"].append(ln)
        elif in_list and ln.startswith("- "):
            res["list_lines"].append(ln)
    if cur_bvid and sec_start is not None:
        res["sections"][cur_bvid] = (sec_start, len(lines))
    return res

def upsert_report(video, section_md, summary_line, list_line):
    """追加/更新当日报告; 返回报告路径"""
    date_str = datetime.datetime.fromtimestamp(
        video.get("pubdate", time.time()), CN_TZ).strftime("%Y-%m-%d")
    rp = report_path(date_str)
    bvid = video["bvid"]

    if os.path.exists(rp):
        text = read_text(rp)
    else:
        text = (f"# 📺 李大霄视频日报 {date_str}\n\n"
                f"## 今日摘要\n\n"
                f"## 视频清单\n\n")

    # 视频清单行: 有则替换, 无则插入
    list_lines = [l for l in text.splitlines() if l.startswith("- ")]
    # 先删掉旧清单行 (基于BV匹配), 稍后统一重建
    lines = text.splitlines()
    new_lines = []
    in_list = False
    for ln in lines:
        if ln.startswith("## 视频清单"):
            in_list = True
            new_lines.append(ln)
            continue
        if in_list and ln.startswith("## "):
            in_list = False
        if in_list and ln.startswith("- ") and bvid in ln:
            continue
        new_lines.append(ln)
    text = "\n".join(new_lines)

    # 插入清单行
    text = re.sub(r"(## 视频清单\n)", r"\1" + list_line + "\n", text, count=1)

    # 摘要行: 删除旧的该视频摘要 (按BV或标题匹配), 插入到 今日摘要 区块
    lines = text.splitlines()
    new_lines = []
    in_summary = False
    for ln in lines:
        if ln.startswith("## 今日摘要"):
            in_summary = True
            new_lines.append(ln)
            continue
        if in_summary and ln.startswith("## "):
            in_summary = False
        if in_summary and ln.startswith("- ") and (bvid in ln or video["title"] in ln):
            continue
        new_lines.append(ln)
    text = "\n".join(new_lines)
    text = re.sub(r"(## 今日摘要\n)", r"\1" + summary_line + "\n", text, count=1)

    # 视频章节: 已有同BV章节则整体替换, 否则末尾追加
    sec = parse_report(text)["sections"].get(bvid)
    if sec is not None:
        s, e = sec
        lines = text.splitlines()
        # build_section 头恒为 "## 视频1:", 替换时必须沿用原章节编号,
        # 否则重跑同一章节会产生重复的 "## 视频1:"
        m = re.match(r"^## 视频(\d+):", lines[s]) if s < len(lines) else None
        if m:
            section_md = re.sub(r"^## 视频\d+:", f"## 视频{m.group(1)}:",
                                section_md, count=1)
        text = "\n".join(lines[:s] + [section_md] + lines[e:])
        log(f"报告章节已更新(BV {bvid}): {rp}")
    else:
        num = len(re.findall(r"^## 视频\d+:", text, re.M)) + 1
        section_md = re.sub(r"^## 视频\d+:", f"## 视频{num}:", section_md, count=1)
        text = text.rstrip() + "\n\n" + section_md + "\n"
        log(f"报告已追加视频#{num}: {rp}")
    if _audit:
        text = _audit.resort_report(text)
    write_text(rp, text)
    return rp

def build_section(cfg, video, subtitle_text, analysis, err=None, ending=None, warn=None):
    """生成单个视频的报告章节 markdown"""
    head = f"## 视频1:《{video['title']}》 ({video['bvid']})"
    if warn:
        head += f"\n\n> ⚠️ {warn}"
    emd = ending_md(ending).strip()
    if emd and analysis and _audit:
        _cn = _audit.conflict_note((ending or {}).get("text", ""), analysis)
        if _cn:
            emd = emd + "\n\n" + _cn

    def with_tail(text):
        """片尾块插在免责声明之前, 保证"以上为...不构成投资建议"仍是章节最后一行"""
        if not emd:
            return text + "\n"
        i = find_disclaimer(text)
        if i >= 0:
            return (text[:i].rstrip() + "\n\n" + emd + "\n\n"
                    + text[i:].strip() + "\n")
        return text + "\n\n" + emd + "\n"

    if err:
        return (with_tail(head + f"\n\n> ⚠️ 处理失败: {err}"), "")
    if not subtitle_text:
        note = "无字幕, 跳过分析" + ("; 已单独识别片尾画面" if emd else "")
        return (with_tail(head + f"\n\n> {note}"),
                f"- 视频《{video['title']}》: {note}")
    if analysis is None:
        return (with_tail(head + "\n\n> ⚠️ AI分析失败, 仅保留字幕"), "")
    one_line = ""
    if "【一句话摘要】" not in analysis:
        log(f"⚠️ 分析缺少【一句话摘要】标记(BV {video['bvid']}), 摘要走兜底")
    m = re.search(r"【一句话摘要】\s*(.+)", analysis)
    if m:
        one_line = m.group(1).strip()
    if _audit:
        summary_line = _audit.make_summary_line(video["title"], analysis)
    else:
        summary_line = f"- 视频《{video['title']}》: {one_line}" if one_line else ""
    return (with_tail(head + "\n\n" + analysis.strip()), summary_line)

def find_disclaimer(text):
    """免责声明在 text 中的起始下标; 找不到返回 -1。

    先按精确子串找; 找不到再按 norm_text 的去符号口径找一次 —— 模型常把常量里的
    半角逗号写成全角「，」, 精确匹配会漏, ensure_disclaimer 于是把同一句话追加两遍
    (2026-11-23 那份 baseline 的 2 处就是这么来的)。归一化只抹掉标点与空白, 改字或
    改词的变体仍然判为"没有", 不会把缺声明的报告当成有。
    """
    i = text.rfind(DISCLAIMER)
    if i >= 0:
        return i
    needle = norm_text(DISCLAIMER)
    if not needle:
        return -1
    pos = norm_text(text).rfind(needle)
    if pos < 0:
        return -1
    keep = [idx for idx, ch in enumerate(text) if not CLEAN_RE.fullmatch(ch)]
    return keep[pos] if pos < len(keep) else -1

def ensure_disclaimer(analysis):
    if find_disclaimer(analysis) < 0:
        analysis = analysis.rstrip() + "\n\n" + DISCLAIMER
    return analysis

def postprocess_analysis(analysis):
    """后处理 LLM 输出:
    1) ~关键词~ -> **关键词** (dsc 的波浪号加粗标记)
    2) 清洗 DeepSeek 网页端引用编号残留, 如 -4、-4-10、- 6 - 8 等
    """
    # 1) 波浪号加粗
    analysis = re.sub(r"~([^~\n]+)~", r"**\1**", analysis)
    # 2) 清洗引用编号: ")-4-10" -> ")" 以及行尾 "-4" / "- 6 - 8"
    analysis = re.sub(r"\)\s*-\s*\d+(?:\s*-\s*\d+)*", ")", analysis)
    analysis = re.sub(r"\s*-\s*\d+(?:\s*-\s*\d+)*\s*(?=[。\n]|$)", "", analysis)
    # 3) 去掉模型自带的重复大标题, 并把 ##【…】 降回扁平样式(和早期报告一致)
    out_lines = []
    for ln in analysis.splitlines():
        s = ln.rstrip()
        if re.match(r"^#+\s*视频\d+", s):
            pass                                  # 章节标题保留
        elif re.search(r"📽", s) and s.startswith("#"):
            s = re.sub(r"^#+\s*", "", s)
            s = f"**{s}**"                        # 片尾小标题加粗
        elif s.startswith("#"):
            # 模型把标准小节写成 Markdown 标题(### 【一句话摘要】…)时,
            # 只剥 # 号保留内容 —— 整行丢掉会让该小节连同正文一起消失
            s = re.sub(r"^#+\s*", "", s).strip()
            if not s or not re.search(r"【[^】]+】", s):
                continue                          # 真正的自造标题才丢
        out_lines.append(s)
    analysis = "\n".join(out_lines)
    analysis = re.sub(r"\n{3,}", "\n\n", analysis)
    # 清理多余空白
    analysis = re.sub(r"[ \t]+", " ", analysis)
    return analysis.strip()


def ending_md(rec):
    """片尾识别结果 -> 报告 markdown 块 (无结果返回空串)

    画面图形隐喻(【片尾画面暗示】)单独提出来置顶加粗:
    它是李大霄"地球顶/钻石底/婴儿底"这类标志性比喻的唯一来源, 容易被淹没在文字卡片里。
    """
    if not rec or not (rec.get("text") or "").strip():
        return ""
    model = rec.get("model", "")
    text = rec["text"].strip()
    lines = [ln.strip() for ln in text.splitlines()]

    metaphor = ""
    rest = []
    for ln in lines:
        if not ln:
            continue
        if ln.startswith("【片尾画面暗示】"):
            metaphor = ln.replace("【片尾画面暗示】", "").strip()
            continue
        rest.append(ln)

    out = ["### 📽 片尾画面识别", ""]
    if _audit:
        out.append(_audit.visual_note(rec))
        out.append("")
    # "无" 或空 表示没有标志性图形, 不额外占据显眼位置
    if metaphor and metaphor not in ("无", "none", "None"):
        out.append(f"**🎯 画面图形暗示: {metaphor}**")
        out.append("")
    out.append("\n".join(rest))
    return "\n".join(out) + "\n"


def list_line(video):
    t = datetime.datetime.fromtimestamp(video.get("pubdate", time.time()), CN_TZ)
    return f"- [{t.strftime('%H:%M')}] {video['title']} ({video['bvid']}) {video.get('dur_str', '')}"


# ------------------------------------------------------------------ 动态一节
DYNAMIC_SECTION = "## 李大霄动态"
DYNAMIC_TEXT_LIMIT = 400          # 原文截断口径: 按**字符**数(len(str)), 不是字节
DYNAMIC_MARKER = "<!-- dynamic:{} -->"
DYNAMIC_MARKER_RE = re.compile(r"<!-- dynamic:(\d+) -->")
DYNAMIC_TYPE_LABEL = {
    "MAJOR_TYPE_OPUS": "长文动态",
    "MAJOR_TYPE_ARCHIVE": "视频投稿",
    "MAJOR_TYPE_DRAW": "图文动态",
}
DYNAMIC_UNAVAILABLE = "（未分析：LLM 通道全部失败，仅保留原文）"


def _dynamic_time(ts):
    if not isinstance(ts, int):
        return ""
    return datetime.datetime.fromtimestamp(ts, CN_TZ).strftime("%Y-%m-%d %H:%M")


def dynamic_md(dynamic, analysis=""):
    """单条动态的 markdown 块: 时间 + 类型 + 原文(截断) + 简版解读"""
    d = dynamic or {}
    body = (d.get("text") or "").strip() or (d.get("title") or "").strip()
    total = len(body)                       # 字符数, 不是字节
    if total > DYNAMIC_TEXT_LIMIT:
        shown = (body[:DYNAMIC_TEXT_LIMIT].rstrip()
                 + f"…(截断, 原文 {total} 字)")
    else:
        shown = body
    head = " · ".join(x for x in (_dynamic_time(d.get("pub_ts")),
                                  DYNAMIC_TYPE_LABEL.get(d.get("major_type"), "动态")) if x)
    lines = [f"### {head}",
             DYNAMIC_MARKER.format(d.get("id_str") or ""),
             f"原文({total} 字): {shown}"]
    brief = (analysis or "").strip()
    # 模型自己爱以「简版:」开头, 不剥掉就会渲染成「简版: 简版: …」
    brief = re.sub(r"^简\s*版\s*[:：]\s*", "", brief).strip()
    lines.append(f"简版: {brief}" if brief else f"简版: {DYNAMIC_UNAVAILABLE}")
    return "\n".join(lines)


def marked_dynamic_ids(text):
    """报告里已经发布过的动态 id 集合 —— 幂等与「不重分析」判据都靠它。"""
    return set(DYNAMIC_MARKER_RE.findall(text or ""))


def _disclaimer_line(text):
    """免责声明所在行号(0 基, 取**最后一次**出现); 找不到返回 -1。

    定位一律复用 find_disclaimer, 不另写一套匹配 —— 那里已经处理了模型把半角逗号
    写成全角「，」的变体, 另写一套必然会和它不一致。
    """
    at = find_disclaimer(text)
    if at < 0:
        return -1
    return text[:at].count("\n")


def _insert_dynamic_blocks(text, blocks):
    """追加「## 李大霄动态」小节: 整体排在所有视频块之后, 节末自带一条收尾声明。

    版面硬约束(阶段 4, 督查裁决): 视频块不许被切开 —— 每支视频的正文必须和它
    自己的声明相邻; 动态节独立成节, 排在所有视频块之后; 全文末行是声明。
    所以插入点是 **EOF**, 不是「最后一条声明之前」: 后者会把上一支视频拦腰截断,
    它的声明被推到动态节后面, 读者看到的动态就像那支视频的一部分(0cf14e6 的错)。

    收尾声明属于动态节自己, 与文件里原来有几条无关 —— 每追加一次就多一条收尾
    声明(已知代价: 2026-10-27 这类 6 条声明的日报变 7 条, 属正确)。
    动态节已存在时新块插在**它自己的收尾声明之前**, 不再补第二条声明, 所以
    二次追加不会让声明条数继续涨。
    """
    block = "\n\n".join(blocks)
    lines = text.splitlines()
    idx = next((i for i, ln in enumerate(lines)
                if ln.strip() == DYNAMIC_SECTION), None)
    if idx is None:
        return (text.rstrip() + "\n\n" + DYNAMIC_SECTION + "\n\n" + block
                + "\n\n" + DISCLAIMER + "\n")
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    seg = lines[idx + 1:end]
    rest = lines[end:]
    d = _disclaimer_line("\n".join(seg))
    if d >= 0:
        body, close = seg[:d], seg[d].strip()
    else:
        body, close = seg, DISCLAIMER
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    chunk = DYNAMIC_SECTION + "\n"
    if body:
        chunk += "\n" + "\n".join(body) + "\n"
    chunk += "\n" + block + "\n"
    if close:
        chunk += "\n" + close + "\n"
    out = "\n".join(lines[:idx]).rstrip() + "\n\n" + chunk
    if rest:
        out += "\n" + "\n".join(rest).lstrip("\n") + "\n"
    return out.rstrip() + "\n" if not rest else out


def append_dynamics(date_str, entries, allow_create=True):
    """把动态追加到当日报告的「## 李大霄动态」一节, 返回实际追加条数。

    幂等**靠报告自身的 `<!-- dynamic:id -->` 标记**, 不靠 state:
    "报告已写、state 还没写"的崩溃窗口重跑时不会重复追加。
    这是硬约束(阶段 4): state 不许先于报告落盘, 判重也不许只靠 state。

    allow_create=False 时, 当日报告不存在就**不新建**、直接返回 0:
    视频列表拉取失败那条路径也在 finally 里会走到这里, 若它新建一份空日报,
    等于告诉读者"当天真的没有视频", 而真实情况是根本没拉到。
    """
    entries = [e for e in (entries or [])
               if (e.get("dynamic") or {}).get("id_str")]
    if not entries:
        return 0
    rp = report_path(date_str)
    if os.path.exists(rp):
        text = read_text(rp)
    elif not allow_create:
        log(f"⚠️ 当日报告不存在且本次不允许新建(视频列表未成功), 动态暂不落盘: {date_str}")
        return 0
    else:
        text = (f"# 📺 李大霄视频日报 {date_str}\n\n"
                f"## 今日摘要\n\n"
                f"## 视频清单\n")
    seen, blocks = set(), []
    for e in entries:
        did = e["dynamic"]["id_str"]
        if did in seen or DYNAMIC_MARKER.format(did) in text:
            continue
        seen.add(did)
        blocks.append(dynamic_md(e["dynamic"], e.get("analysis", "")))
    if not blocks:
        return 0
    write_text(rp, _insert_dynamic_blocks(text, blocks))
    return len(blocks)
