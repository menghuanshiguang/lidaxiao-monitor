"""退出码: monitor 失败必须变成 job 失败 (离线, 不跑 monitor)。

2026-11-26 那次事故里, `monitor.py` 退出码 1、一份报告都没产出, job 却显示 success ——
因为 `run_monitor` 只把退出码 log 出来不返回, `run_once` 又无条件 `return 0`。
于是 workflow 里那个 `if ($LASTEXITCODE -ne 0) { exit }` 永远触发不到,
"run success" 根本不等于 "报告产出"。这里钉住整条链: 子进程 1 → run_monitor 1 →
run_once 1。
"""
import importlib.util
import io
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# local_daemon 顶部 import requests (本机没有), 给个占位模块再导入
sys.modules.setdefault("requests", types.ModuleType("requests"))


def _load_entry():
    """按**文件路径**加载根目录的 lidaxiao.py。

    不能用 `import lidaxiao`: 同名的 `lidaxiao/` 包会先被找到, 拿到的是包不是入口脚本。
    """
    spec = importlib.util.spec_from_file_location(
        "lidaxiao_entry", os.path.join(ROOT, "lidaxiao.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = _load_entry()
LD = L.ld
ARGS = types.SimpleNamespace(config=None, force=False)

OK = FAIL = 0


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


_orig = (LD.run_monitor, LD.publish_reports, LD.claim_is_fresh,
         LD.reset_local_state, LD.log, LD.run_slot, LD.subprocess.run)
try:
    print("== run_monitor 交出子进程退出码 ==")
    LD.log = lambda *a, **k: None

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    for rc in (0, 1, 2):
        LD.subprocess.run = lambda *a, **k: _R(rc)
        chk(f"子进程 {rc} -> run_monitor 返回 {rc}", LD.run_monitor(ARGS) == rc,
            LD.run_monitor(ARGS))

    def _boom(*a, **k):
        raise OSError("python 起不来")
    LD.subprocess.run = _boom
    chk("起不了子进程 -> 返回 1 (不是 None)", LD.run_monitor(ARGS) == 1, LD.run_monitor(ARGS))

    print("== 云端: monitor 失败要传成非 0 ==")
    os.environ["GITHUB_ACTIONS"] = "true"
    published = []
    LD.claim_is_fresh = lambda: False
    LD.reset_local_state = lambda: None
    LD.publish_reports = lambda args, tag="local": published.append(tag)

    LD.run_monitor = lambda args, local=True: 1
    got = L.run_once(ARGS)
    chk("monitor 退出码 1 -> run_once 返回 1", got == 1, got)
    chk("报告照发 (崩之前那几节不丢)", published == ["cloud"], published)

    LD.run_monitor = lambda args, local=True: 0
    chk("monitor 成功 -> run_once 返回 0", L.run_once(ARGS) == 0)
    chk("成功也照发报告", published == ["cloud", "cloud"], published)

    LD.run_monitor = lambda args, local=True: 2
    chk("退出码 2 原样传出去 (不被压成 1)", L.run_once(ARGS) == 2, L.run_once(ARGS))

    LD.run_monitor = lambda args, local=True: 1
    LD.claim_is_fresh = lambda: True
    published.clear()
    chk("本地已认领时跳过且返回 0",
        L.run_once(ARGS) == 0 and published == [], (L.run_once(ARGS), published))
    os.environ.pop("GITHUB_ACTIONS", None)

    print("== 本地槽位 ==")
    LD.run_slot = lambda args: False
    chk("本地认领失败 -> 返回 1", L.run_once(ARGS) == 1)
    LD.run_slot = lambda args: True
    chk("本地跑成 -> 返回 0", L.run_once(ARGS) == 0)
finally:
    (LD.run_monitor, LD.publish_reports, LD.claim_is_fresh,
     LD.reset_local_state, LD.log, LD.run_slot, LD.subprocess.run) = _orig
    os.environ.pop("GITHUB_ACTIONS", None)

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
