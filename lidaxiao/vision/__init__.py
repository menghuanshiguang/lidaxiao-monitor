"""片尾视觉通道: 抽帧 / 缓存 / 片尾暗示识别"""
from lidaxiao.vision.channel import (  # noqa: F401
    check_vision, vision_providers)
from lidaxiao.vision.ending import (  # noqa: F401
    load_ending_cache, read_ending_hint, save_ending_cache, tail_frames)
