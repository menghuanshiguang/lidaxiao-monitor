"""字幕带重复帧压缩 (2026-11-21)

1fps 抽帧下同一句字幕通常停留 2-3 帧, 逐帧 OCR 有 50%+ 的调用是在重复识别一模一样的一行。
这里只比较**字幕带**的降采样灰度图, 把连续相同画面合并成游程, 每个游程只 OCR 中间帧:

  - 省调用: 240 帧通常只剩 80-120 次
  - 提质: 段首段尾常是淡入/半透明/两行滚动的过渡态, 取中间帧天然避开
  - 滚动双行字幕会退化成逐帧(无害), 不会误合并

为什么不用 dHash: 字幕带大片是纯色背景, 64bit 结构哈希对"换了一个字"几乎不敏感
(实测"涨了3%"与"涨了8%"汉明距离=0)。改成降采样 + 逐像素差分比例, 更好控阈值。

【实测结论 2026-11-21, 不要贸然启用】
真实素材 BV1x3y8zpEGD(160 帧/约 2.2s 间隔, 基线 116 行字幕) 上标定:
  紧凑字幕区(0.84-0.97) + 亮像素掩码, 最保守档 thr=0.05 也只省 4%(漏切 4 条);
  放宽到 thr=0.12 省 23% 但漏切 23 条(约两成字幕丢失)。
根因: 这是"人物口播"素材, 西装/肩膀/手部在字幕带里持续运动, 同一句字幕的两帧
像素差已经和换句时同量级 —— 像素级去重在这类画面上不可行。
结论: 默认关闭(config.ocr_dedup=false)。真要省调用, 应改用音频句子切分对齐,
而不是画面比对。
"""
from PIL import Image

SIG_SIZE = (160, 48)      # 降采样尺寸: 足够保住汉字笔画, 又能滤掉压缩噪声
PIX_DELTA = 28            # 单像素算"变了"的灰度差阈值
MAX_CHANGED_RATIO = 0.004  # 变化像素占比 < 0.4% 视为同一画面


def band_box(width, height, band):
    """band=(top,bottom) 比例 -> 像素裁剪框"""
    top, bot = band
    return (0, int(height * top), width, int(height * bot))


def band_sig(path, band=None):
    """字幕带指纹: 灰度 + 裁带 + 降采样, 返回 bytes"""
    with Image.open(path) as im:
        g = im.convert("L")
        if band:
            g = g.crop(band_box(g.width, g.height, band))
        g = g.resize(SIG_SIZE, Image.BILINEAR)
        return g.tobytes()


def changed_ratio(a, b):
    """两指纹之间"明显变化"的像素占比"""
    if not a or not b or len(a) != len(b):
        return 1.0
    hit = sum(1 for x, y in zip(a, b) if abs(x - y) > PIX_DELTA)
    return hit / len(a)


def same_frame(a, b, max_ratio=MAX_CHANGED_RATIO):
    return changed_ratio(a, b) <= max_ratio


def split_runs(sigs, max_ratio=MAX_CHANGED_RATIO):
    """连续相同画面分段 -> [(start, end_inclusive)]"""
    if not sigs:
        return []
    runs, start = [], 0
    for i in range(1, len(sigs)):
        if not same_frame(sigs[i - 1], sigs[i], max_ratio):
            runs.append((start, i - 1))
            start = i
    runs.append((start, len(sigs) - 1))
    return runs


def pick_indices(sigs, max_ratio=MAX_CHANGED_RATIO):
    """每个游程 -> (代表帧下标=中间帧, 游程起始帧); 时间戳用起始帧更贴合字幕出现时刻"""
    return [((s + e) // 2, s) for s, e in split_runs(sigs, max_ratio)]
