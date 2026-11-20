# -*- coding: utf-8 -*-
"""审计整改 P2/P3: BUG-10/11/12/13/14/16 的确定性修复。

全部为纯函数, 输入输出都是文本/字典, 便于本地单测。
"""
import re

# BUG-10: 上证指数标志性点位 -> (标准日期, 语境说明)
# 仅在文本出现该数字时用于校正语境, 不臆造新点位。
LANDMARKS = {
    "2635": ("2024-02-05", "上证2024年2月5日阶段低点"),
    "2689": ("2024-09-24", "上证924行情起点"),
    "3174": ("2024-05-20", "上证2024年5月反弹高点"),
    "3674": ("2024-10-08", "上证2024年10月8日高点"),
    "3509": ("2024-11-08", "上证2024年11月反弹高点"),
}
INDEX_NAMES = ("上证", "沪指", "沪市", "大盘", "道指", "纳指", "标普",
               "创业板", "深证", "深成", "恒指", "恒生", "科创50", "北证")


def _date_norm(y, mo, d):
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def fix_point_context(text):
    """BUG-10 点位语境: 标志性点位缺指数名/日期不符时补注标准语境。"""
    if not text or "点" not in text:
        return text

    def rep(m):
        num = m.group(1)
        lm = LANDMARKS.get(num)
        if not lm:
            return m.group(0)
        std_date, ctx = lm
        s, e = max(0, m.start() - 40), min(len(text), m.end() + 40)
        win = text[s:e]
        # 已带正确语境则不重复
        if std_date.replace("-", "年", 1) in win.replace("-", "年", 1) or ctx in win:
            return m.group(0)
        head = text[max(0, m.start() - 4):m.start()]
        prefix = "" if any(n in head for n in INDEX_NAMES) else "上证"
        dm = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", win)
        if dm and _date_norm(*dm.groups()) == std_date:
            return m.group(0)
        # 日期写错, 或出现在清单项里 -> 补标准语境; 其余只补指数名, 避免正文注水
        line_head = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        want_note = bool(dm) or bool(re.match(r"\s*[-*]\s", line_head))
        return f"{prefix}{num}点" + (f"（{ctx}）" if want_note else "")

    return re.sub(r"(?<!\d)(\d{4})点(?!\d)", rep, text)


def fix_continuity(text, history):
    """BUG-13 连续性: '连续第N期' 必须由历史报告实际期数校正, 算不出就降级。"""
    if not text or "连续" not in text:
        return text
    flat = lambda s: re.sub(r"[\s，。、：:；;]", "", s or "")

    def count_periods(key):
        if not history or len(key) < 4:
            return 0
        secs = re.split(r"(?m)^##\s*视频\d+[:：]", history)
        n = sum(1 for s in secs if key in flat(s))
        return min(n, 30)

    trail = ("的逻辑", "逻辑", "这一点", "这一", "的", "了", "也", "仍",
             "还是", "再次", "又", "都", "同样")

    def rep(m):
        pre = flat(text[max(0, m.start() - 16):m.start()])
        for w in trail:
            while pre.endswith(w):
                pre = pre[: -len(w)]
        n = 0
        # 由长到短回退匹配, 优先最长可用主题词
        for ln in (8, 6, 4):
            if len(pre) >= ln:
                n = count_periods(pre[-ln:])
                if n > 0:
                    break
        if n <= 0:
            return "近期"
        return f"连续第{n + 1}期"

    return re.sub(r"连续第\s*\d+\s*期", rep, text)


def visual_note(rec):
    """BUG-11 视觉置信度: 片尾识别必须标注来源可信度。"""
    conf = None
    for k in ("conf", "confidence", "score"):
        v = rec.get(k) if isinstance(rec, dict) else None
        if isinstance(v, (int, float)) and 0 < v <= 1:
            conf = v
            break
    if conf is None:
        return "> 🖼 来源：片尾画面 OCR（无置信度评分，文字可能残缺或误识，仅供参考）"
    level = "高" if conf >= 0.85 else ("中" if conf >= 0.6 else "低")
    return f"> 🖼 来源：片尾画面 OCR，置信度 {conf:.2f}（{level}，仅供参考）"


def conflict_note(ending_text, analysis):
    """BUG-16 图文冲突收敛: 片尾点位与正文点位不一致时显式提示。"""
    if not ending_text or not analysis:
        return ""
    pe = sorted(set(re.findall(r"(?<!\d)(\d{4})点", ending_text)))
    pa = sorted(set(re.findall(r"(?<!\d)(\d{4})点", analysis)))
    if not pe or not pa or set(pe) & set(pa):
        return ""
    return (f"> ⚠️ 图文不一致：片尾画面出现 {'/'.join(pe)}点，正文为 "
            f"{'/'.join(pa)}点。正文以字幕为准，片尾仅作参考。")


def make_summary_line(title, analysis):
    """BUG-14: 今日摘要不得与正文『一句话摘要』逐字重复。

    改为『动作｜观点1(去数据, 截断)』的压缩式摘要。
    """
    one = ""
    m = re.search(r"【一句话摘要】\s*(.+)", analysis or "")
    if m:
        one = m.group(1).strip()
    act = ""
    ma = re.search(r"【操作含义】(.*?)(?=【|\Z)", analysis or "", re.S)
    if ma:
        am = re.search(r"动作\**\s*[:：]\s*\**\s*([^\n*｜|]+)", ma.group(1))
        if am:
            act = am.group(1).strip()
    pt = ""
    mp = re.search(r"【核心观点】(.*?)(?=【|\Z)", analysis or "", re.S)
    if mp:
        pm = re.search(r"(?m)^\s*1\.\s*(.+)$", mp.group(1))
        if pm:
            pt = re.sub(r"[（(]原文数据.*?[)）]", "", pm.group(1)).strip()
            pt = re.split(r"[，,；;]", pt)[0]
    pt = pt[:64].rstrip("。；;，,")
    if not pt:
        pt = one[:70].rstrip("。；;，,") + ("…" if len(one) > 70 else "")
    if not pt:
        return ""
    head = f"**{act}**｜" if act and act not in ("不适用", "无") else ""
    return f"- 视频《{title}》: {head}{pt}"


def resort_report(text):
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
