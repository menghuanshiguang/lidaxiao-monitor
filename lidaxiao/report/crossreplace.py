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

删除时机刻意放在**B 站这条处理成功之后**: 先删后处理的话, 一旦下载或分析失败,
内容就两头落空 —— 删掉的节回不来, 新的也没产生。这样也天然满足
「删除节数 - 追加节数 = 0」: 每删一节都对应一节真的写进去了。
"""
from lidaxiao.report.render import remove_section
from lidaxiao.textutil import log

#: 这些状态下 B 站那条的报告章节已经落盘(ok=正常, partial=分析失败但章节在,
#: no-subtitle=无字幕也照样出一节), 删微博节才不会造成内容空洞。
SECTION_WRITTEN = ("ok", "partial", "no-subtitle")


def apply_replacements(pairs, statuses):
    """按配对结果删旧的微博节 -> (删除节数, 未删说明列表)。

    pairs   : sources.merge.build_queue 给的 replaced 列表
              [{bvid, weibo_key, weibo_id, weibo_title, weibo_date}, ...]
    statuses: processed_status_map(), 用来确认 B 站那条这次真的出了章节
    """
    removed, skipped = 0, []
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
        info = remove_section(pair.get("weibo_date"), key)
        if not info.get("removed"):
            skipped.append((key, info.get("reason") or "报告里没有该节"))
            continue
        removed += 1
        log(f"🧹 跨源替换: 删微博节 {key}《{pair.get('weibo_title', '')}》"
            f"(原第 {info.get('idx')} 节, 节数 {info.get('sections_before')}"
            f"→{info.get('sections_after')}, {info.get('bytes_before')}B→"
            f"{info.get('bytes_after')}B), 保留 B 站 {bvid}")
    for key, why in skipped:
        log(f"↩️ 跨源替换未删 {key}: {why}")
    return removed, skipped
