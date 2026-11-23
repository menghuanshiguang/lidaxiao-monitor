"""共享路径与时区常量 (从 monitor.py 抽出)"""
import datetime
import os
import shutil

# 包在 lidaxiao/ 下, 仓库根是它的父目录
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(WORKDIR, "state")
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
DISCLAIMER = "以上为李大霄个人观点提炼,不构成投资建议"

FFMPEG_DIR = os.path.join(WORKDIR, "tools", "ffmpeg", "bin")

def _find_tool(name):
    """查找 ffmpeg/ffprobe: 环境变量 LIDAXIAO_<NAME> > tools/ > PATH"""
    p = os.environ.get(f"LIDAXIAO_{name.upper()}", "").strip()
    if p and os.path.exists(p):
        return p
    cand = os.path.join(FFMPEG_DIR, f"{name}.exe")
    if os.path.exists(cand):
        return cand
    w = shutil.which(name)
    return w or name

FFMPEG = _find_tool("ffmpeg")

FFPROBE = _find_tool("ffprobe")
