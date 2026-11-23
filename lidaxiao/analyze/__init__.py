"""分析层: LLM 调用与提示词构造"""
# check_vision 已随视觉通道搬去 vision/channel.py: analyze 不得 import vision,
# 所以这里不再导出它 (唯一消费者 monitor.py 改为从 lidaxiao.vision import)。
from lidaxiao.analyze.llm import (  # noqa: F401
    analyze_subtitles, build_dsc_prompt, call_llm, load_history,
    tighten_analysis, validate_report)
