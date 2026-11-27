"""跨源替换: B 站补发同一条内容时, 把往轮留下的微博节从报告里删掉。

为什么要有这一层 (2026-11-27 线上实况):
    09:32 微博先发《六大举措在发力 稳市曙光或已出现》(wb5369798433178131), 当轮处理,
    进了报告也进了 state; 09:35 B 站发同一条 (BV1hv1pz2EbP), 两分钟后那轮拉列表时
    B 站接口还没给出它, 于是下一轮它作为「待处理」被独立下载分析, 报告里同一内容
    并列两条。配对其实是**成功**的(那一跑 匹配跳过=2 里就有这对), 但配对只从微博侧
    扣人 —— 已发布的微博节没有任何人去删。

规则 (督查 2026-11-27 拍板, 选 (b)): 同一内容优先 B 站 —— 删掉旧的微博节, 保留这次
B 站重出的那一节。理由不是洁癖: 微博那条往往正是降级产生的空节(AI失败/仅字幕),
而它已经被 state 记住、永远不会重做, 留着就是一条永久的空节。

**删在哪**: 第一版在 monitor 里直接删本地 `reports/<date>.md`, 线上完全没生效 ——
`.gitignore` 第 5 行是 `/reports/`, 云端 checkout 后该目录根本不存在, 而真正发布的
是 `docs/reports/<date>.md`(旧侧)。就算删了本地那份, 发布时 `_merge_report` 的规则
是「旧文件里新文件没有的节原样补回」, 微博节会被从 docs 那份搬回来(run 45939046960
实测)。所以删节只落在**一个写盘点**: `publish_reports` 里 `_merge_report` 之后的
`merged` 上、写盘之前。monitor → publish 是两个进程, 配对决定经 data/ 下的交接文件
传递(不进仓库、不进报告目录)。
"""
import json
import os

from lidaxiao.common import WORKDIR
from lidaxiao.report.render import drop_section
from lidaxiao.textutil import log, read_text, write_text

#: 这些状态下 B 站那条的报告章节已经落盘(ok=正常, partial=分析失败但章节在,
#: no-subtitle=无字幕也照样出一节), 删微博节才不会造成内容空洞。
SECTION_WRITTEN = ("ok", "partial", "no-subtitle")

#: monitor -> publish 的交接文件。放 data/ 是因为它能写且被 `/data/` 忽略:
#: 放 state/ 会被 `git add state` 提交进仓库, 放 reports/ 会造出云端本不该存在的目录。
HANDOFF_NAME = "cross_replace.json"


def handoff_path():
    return os.path.join(WORKDIR, "data", HANDOFF_NAME)


def apply_replacements(pairs, statuses):
    """按配对结果决定要删哪些微博节 -> (待删列表, 未删说明列表)。

    只判定、不动文件: 真正删节在 publish_reports 里对合并后的报告做。
    pairs   : sources.merge.build_queue 给的 replaced 列表
              [{bvid, weibo_key, weibo_id, weibo_title, weibo_date}, ...]
    statuses: processed_status_map(), 用来确认 B 站那条这次真的出了章节
    """
    drops, skipped = [], []
    for pair in pairs or []:
        bvid = pair.get("bvid") or ""
        key = pair.get("weibo_key") or ""
        if not key:
            skipped.append((bvid, "配对里没有微博键"))
            continue
        status = (statuses or {}).get(bvid)
        if status not in SECTION_WRITTEN:
            # B 站这条没出章节(下载失败/异常): 微博节必须留着, 否则这条内容就没了
            skipped.append((key, f"B 站 {bvid} 未出章节(status={status or '无'})"))
            continue
        drops.append({"bvid": bvid, "weibo_key": key,
                      "weibo_title": pair.get("weibo_title", ""),
                      "weibo_date": pair.get("weibo_date") or ""})
    for key, why in skipped:
        log(f"↩️ 跨源替换未删 {key}: {why}")
    return drops, skipped


def save_handoff(drops, added):
    """把待删节交给发布段; 每次运行都写(空列表也要写), 免得上一跑的残留被当成本轮的。"""
    try:
        path = handoff_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_text(path, json.dumps({"added": int(added or 0), "drops": drops or []},
                                    ensure_ascii=False))
        return path
    except Exception as e:
        log(f"⚠️ 跨源替换交接文件写入失败 (本次不删节): {type(e).__name__}: {e}")
        return ""


def load_handoff():
    """读出交接文件并立刻删掉: 一次运行只消费一次, 出错也不留到下一跑。"""
    path = handoff_path()
    data = {}
    try:
        raw = read_text(path, "")
        if raw:
            data = json.loads(raw)
    except Exception as e:
        log(f"⚠️ 跨源替换交接文件解析失败 (本次不删节): {type(e).__name__}: {e}")
    try:
        os.remove(path)
    except OSError:
        pass
    drops = [d for d in (data.get("drops") or [])
             if isinstance(d, dict) and d.get("weibo_key")]
    return {"added": int(data.get("added") or 0), "drops": drops}


def drop_from_text(text, drops):
    """在**合并后**的报告文本上删掉配对里的微博节 -> (文本, 删除节数)。

    删节失败(异常/解析不出)时保留该次改动前的文本: 宁可留一条重复, 也不许把报告
    写坏或写空 —— 报告是这条链路唯一的产出。
    """
    out, removed = text, 0
    for pair in drops or []:
        key = pair.get("weibo_key") or ""
        try:
            new, info = drop_section(out, key)
        except Exception as e:
            log(f"⚠️ 跨源替换失败 {key}: {type(e).__name__}: {e} (写入未删节原文)")
            continue
        if not info.get("removed"):
            log(f"↩️ 跨源替换未删 {key}: {info.get('reason') or '报告里没有该节'}")
            continue
        out = new
        removed += 1
        log(f"🧹 跨源替换: 删微博节 {key}《{pair.get('weibo_title', '')}》"
            f"(原第 {info.get('idx')} 节, 节数 {info.get('sections_before')}"
            f"→{info.get('sections_after')}, {info.get('bytes_before')}B→"
            f"{info.get('bytes_after')}B), 保留 B 站 {pair.get('bvid', '')}")
    return out, removed
