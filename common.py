"""共享路径与时区常量 (从 monitor.py 抽出)"""
import datetime
import os

WORKDIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(WORKDIR, "state")
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
DISCLAIMER = "以上为李大霄个人观点提炼,不构成投资建议"
