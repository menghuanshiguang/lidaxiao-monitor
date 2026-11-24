"""local/trigger.py 的离线单测: HTTP 全部 mock, 不联网。

重点钉住「互斥三层」的每一层, 以及失败必须响亮:
  1) 本地锁: 锁被占时 0 派发
  2) 云端查询: queued/in_progress 时 0 派发; 查不到时**不发**并返回 2
  3) 本地记账: 同一 slot 绝不二次派发; 时间被往回拨也不派
失败路径: dispatch 非 204 -> 4; 204 但 run 不出现 -> 3 且不记账
"""
import contextlib
import datetime
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = os.path.join(ROOT, "local")
for _p in (ROOT, LOCAL):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import trigger as T  # noqa: E402

OK = FAIL = 0
CN = T.CN_TZ


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


def dt(y, mo, d, h, mi, tz=CN):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=tz)


print("== slot 计算 ==")
chk("slot_datetimes 一天两个点",
    [s.strftime("%H:%M") for s in T.slot_datetimes(datetime.date(2026, 11, 24))] == ["14:40", "20:00"])
chk("slot_key 零填充", T.slot_key(dt(2026, 11, 24, 14, 40)) == "2026-11-24 14:40")
chk("14:39 未到点 -> None", T.due_slot(dt(2026, 11, 24, 14, 39)) is None)
chk("14:40 正好到点 -> 14:40",
    T.slot_key(T.due_slot(dt(2026, 11, 24, 14, 40))) == "2026-11-24 14:40")
chk("15:30 在 60 分钟窗口内 -> 补 14:40",
    T.slot_key(T.due_slot(dt(2026, 11, 24, 15, 30))) == "2026-11-24 14:40")
chk("15:41 超出窗口 -> None", T.due_slot(dt(2026, 11, 24, 15, 41)) is None)
chk("20:00 -> 20:00", T.slot_key(T.due_slot(dt(2026, 11, 24, 20, 0))) == "2026-11-24 20:00")
chk("21:01 超出窗口 -> None", T.due_slot(dt(2026, 11, 24, 21, 1)) is None)
chk("上午 10:00 -> None(昨天 20:00 早已超窗)", T.due_slot(dt(2026, 11, 24, 10, 0)) is None)
chk("20:59 -> 仍补 20:00", T.slot_key(T.due_slot(dt(2026, 11, 24, 20, 59)))
    == "2026-11-24 20:00")
chk("跨夜 00:10 -> None(离 20:00 已 4 小时, 出了 60 分钟窗口)",
    T.due_slot(dt(2026, 11, 25, 0, 10)) is None)
chk("睡到 21:00 醒: 只补 20:00 这一发, 不连发两发",
    T.slot_key(T.due_slot(dt(2026, 11, 24, 21, 0))) == "2026-11-24 20:00")
chk("睡到 22:10 醒: 两发都超窗 -> None", T.due_slot(dt(2026, 11, 24, 22, 10)) is None)
chk("UTC 时刻会先换算到 CN_TZ",
    T.slot_key(T.due_slot(datetime.datetime(2026, 11, 24, 6, 40,
                                            tzinfo=datetime.timezone.utc))) == "2026-11-24 14:40")

print("== 记账 ==")
tmp = tempfile.mkdtemp()
led = os.path.join(tmp, "state", "triggered_slots.txt")
chk("记账文件不存在 -> 空", T.ledger_keys(led) == set() and T.ledger_latest_key(led) == "")
T.ledger_add(led, "2026-11-24 14:40", "111")
chk("写入后可读", T.ledger_keys(led) == {"2026-11-24 14:40"})
chk("追加不覆盖", (T.ledger_add(led, "2026-11-24 20:00", "222"),
                 T.ledger_keys(led) == {"2026-11-24 14:40", "2026-11-24 20:00"})[1])
chk("latest 取最大", T.ledger_latest_key(led) == "2026-11-24 20:00")
chk("注释与空行被跳过",
    (open(led, "a", encoding="utf-8").write("# 注释\n\n"),
     len(T.ledger_entries(led))) [1] == 2)

print("== 跳过判定 ==")
led2 = os.path.join(tmp, "l2.txt")
chk("没记账 -> 不跳过", T.should_skip(dt(2026, 11, 24, 14, 40), led2)[0] is False)
T.ledger_add(led2, "2026-11-24 14:40", "1")
skip, why = T.should_skip(dt(2026, 11, 24, 14, 40), led2)
chk("同 slot 已派过 -> 跳过", skip and "已派过" in why, why)
chk("另一个 slot 不受影响", T.should_skip(dt(2026, 11, 24, 20, 0), led2)[0] is False)
led3 = os.path.join(tmp, "l3.txt")
T.ledger_add(led3, "2026-11-25 20:00", "9")
skip, why = T.should_skip(dt(2026, 11, 24, 14, 40), led3)
chk("时间被往回拨 -> 跳过并说明", skip and "往回拨" in why, why)

print("== 云端忙碌判定 ==")
busy_run = {"id": 7, "path": T.WORKFLOW_PATH, "status": "queued"}
chk("queued -> 忙", (T.remote_busy([busy_run]) or {}).get("id") == 7)
chk("in_progress -> 忙",
    (T.remote_busy([{"path": T.WORKFLOW_PATH, "status": "in_progress"}]) or {}) != {})
chk("completed -> 不忙",
    T.remote_busy([{"path": T.WORKFLOW_PATH, "status": "completed"}]) is None)
chk("别的 workflow(tests.yml) 忙不算",
    T.remote_busy([{"path": ".github/workflows/tests.yml", "status": "queued"}]) is None)
chk("空列表 -> 不忙", T.remote_busy([]) is None and T.remote_busy(None) is None)

print("== run 归属判定 ==")
since = datetime.datetime(2026, 11, 24, 6, 40, tzinfo=datetime.timezone.utc)
chk("时间相近的 monitor run 算我的",
    T.run_matches({"path": T.WORKFLOW_PATH, "created_at": "2026-11-24T06:41:00Z"}, since))
chk("很久以前的 run 不算",
    T.run_matches({"path": T.WORKFLOW_PATH, "created_at": "2026-11-24T02:00:00Z"}, since) is False)
chk("别的 workflow 不算",
    T.run_matches({"path": ".github/workflows/tests.yml",
                   "created_at": "2026-11-24T06:41:00Z"}, since) is False)
chk("没有 created_at 也算(已按 path 过滤)",
    T.run_matches({"path": T.WORKFLOW_PATH}, since))

print("== 主流程 ==")


class Fake:
    """扮演 _http: 记录调用, 按脚本返回。"""

    def __init__(self, get_runs=(), dispatch_status=204, run_after=()):
        self.get_runs = list(get_runs)
        self.dispatch_status = dispatch_status
        self.run_after = list(run_after)
        self.calls = []
        self.dispatched = 0

    def __call__(self, method, url, token="", body=None, timeout=30):
        self.calls.append((method, url, token, body))
        if method == "POST":
            self.dispatched += 1
            return self.dispatch_status, ""
        # GET: 第一次返回派发前的列表, 之后返回含新 run 的列表
        if self.dispatched:
            return 200, '{"workflow_runs": %s}' % __import__("json").dumps(self.run_after)
        return 200, '{"workflow_runs": %s}' % __import__("json").dumps(self.get_runs)


def run_main(fetch, now, ledger, use_lock=None, dry=False):
    lockp = use_lock or os.path.join(tempfile.mkdtemp(), "trigger.lock")
    argv = ["--now", now.strftime("%Y-%m-%dT%H:%M:%S%z")] + (["--dry-run"] if dry else [])
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        return ticks["n"] * 10.0        # 每次看表推进 10s, 不真等

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = T.main(argv, fetch=fetch, ledger_path=ledger, lock_path=lockp,
                    sleep=lambda *_a: None, clock=clock)
    return rc, buf.getvalue()


TOKEN = "ghp_thisMustNeverBePrinted123456"
T.load_token = lambda: (TOKEN, "测试注入")

led_a = os.path.join(tempfile.mkdtemp(), "slots.txt")
new_run = {"id": 4242, "path": T.WORKFLOW_PATH, "status": "queued",
           "created_at": "2026-11-24T06:40:30Z"}
f = Fake(dispatch_status=204, run_after=[new_run])
rc, out = run_main(f, dt(2026, 11, 24, 14, 40), led_a)
chk("正常路径返回 0", rc == 0, (rc, out[-300:]))
chk("正常路径派发 1 次", f.dispatched == 1, f.dispatched)
_posts = [c for c in f.calls if c[0] == "POST"]
chk("正常路径派发到 monitor.yml dispatches 端点",
    len(_posts) == 1
    and _posts[0][1] == f"{T.API}/repos/{T.REPO}/actions/workflows/monitor.yml/dispatches",
    _posts)
chk("正常路径派发体 = ref=main + force=true",
    len(_posts) == 1 and _posts[0][3] == {"ref": "main", "inputs": {"force": "true"}},
    _posts[0][3] if _posts else None)
chk("正常路径记账(带 run_id)", T.ledger_keys(led_a) == {"2026-11-24 14:40"}
    and T.ledger_entries(led_a)[0][1] == "4242", T.ledger_entries(led_a))
chk("token 不出现在输出里", TOKEN not in out, out[-200:])

f2 = Fake(get_runs=[busy_run])
led_b = os.path.join(tempfile.mkdtemp(), "slots.txt")
rc, out = run_main(f2, dt(2026, 11, 24, 14, 40), led_b)
chk("云端已有 queued -> 0 派发", f2.dispatched == 0 and rc == 0, (f2.dispatched, rc))
chk("云端已有 queued -> 不记账", T.ledger_keys(led_b) == set())

f3 = Fake()
led_c = os.path.join(tempfile.mkdtemp(), "slots.txt")
T.ledger_add(led_c, "2026-11-24 14:40", "1")
rc, out = run_main(f3, dt(2026, 11, 24, 14, 40), led_c)
chk("已记账 -> 一次网络请求都不发", rc == 0 and f3.calls == [], (rc, len(f3.calls)))

led_d = os.path.join(tempfile.mkdtemp(), "slots.txt")
f4 = Fake(get_runs=[busy_run])
T.ledger_add(led_d, "2026-11-25 20:00", "1")
rc, out = run_main(f4, dt(2026, 11, 24, 14, 40), led_d)
chk("时间往回拨 -> 不派发", rc == 0 and f4.calls == [] and "往回拨" in out, out[-200:])

f5 = Fake()
led_e = os.path.join(tempfile.mkdtemp(), "slots.txt")
rc, out = run_main(f5, dt(2026, 11, 24, 14, 39), led_e)
chk("没到 slot -> 0 且不联网", rc == 0 and f5.calls == [], (rc, f5.calls))

print("== 失败必须响亮 ==")


class Down(Fake):
    def __call__(self, method, url, token="", body=None, timeout=30):
        self.calls.append((method, url, token, body))
        return 0, "URLError: 断网"


led_f = os.path.join(tempfile.mkdtemp(), "slots.txt")
fd = Down()
rc, out = run_main(fd, dt(2026, 11, 24, 14, 40), led_f)
chk("查不到云端 -> 返回 2 且 0 派发", rc == 2 and fd.dispatched == 0, (rc, fd.dispatched))
chk("查不到云端 -> 不记账", T.ledger_keys(led_f) == set())

led_g = os.path.join(tempfile.mkdtemp(), "slots.txt")
f6 = Fake(dispatch_status=403, run_after=[new_run])
rc, out = run_main(f6, dt(2026, 11, 24, 14, 40), led_g)
chk("dispatch 非 204 -> 返回 4", rc == 4, rc)
chk("dispatch 非 204 -> 不记账", T.ledger_keys(led_g) == set())

led_h = os.path.join(tempfile.mkdtemp(), "slots.txt")
f7 = Fake(dispatch_status=204, run_after=[])      # 204 了但 run 永不出现
rc, out = run_main(f7, dt(2026, 11, 24, 14, 40), led_h)
chk("204 但 run 不出现 -> 返回 3", rc == 3, rc)
chk("204 但 run 不出现 -> 不记账(下次还会补)", T.ledger_keys(led_h) == set())

led_i = os.path.join(tempfile.mkdtemp(), "slots.txt")
lock_i = os.path.join(tempfile.mkdtemp(), "trigger.lock")
with open(lock_i, "w", encoding="utf-8") as fh:
    fh.write(f"pid={os.getpid()} start=0")
f8 = Fake(run_after=[new_run])
rc, out = run_main(f8, dt(2026, 11, 24, 14, 40), led_i, use_lock=lock_i)
chk("锁被占(活进程) -> 0 派发", rc == 0 and f8.calls == [] and "另一个 trigger" in out, out[-200:])
chk("锁被占 -> 锁文件没被删", os.path.exists(lock_i))

led_j = os.path.join(tempfile.mkdtemp(), "slots.txt")
f9 = Fake(run_after=[new_run])
rc, out = run_main(f9, dt(2026, 11, 24, 14, 40), led_j, dry=True)
chk("dry-run -> 不联网不记账", rc == 0 and f9.calls == [] and T.ledger_keys(led_j) == set(),
    (rc, f9.calls))

T.load_token = lambda: ("", "")
led_k = os.path.join(tempfile.mkdtemp(), "slots.txt")
f10 = Fake(run_after=[new_run])
rc, out = run_main(f10, dt(2026, 11, 24, 14, 40), led_k)
chk("没有 token -> 返回 2 且 0 派发", rc == 2 and f10.calls == [], (rc, f10.calls))
T.load_token = lambda: (TOKEN, "测试注入")      # 复原, 别污染后面的用例

print("== 并发: 同 slot 第二次派发被记账挡住 ==")
led_c2 = os.path.join(tempfile.mkdtemp(), "slots.txt")
f11 = Fake(run_after=[new_run])
rc1, _ = run_main(f11, dt(2026, 11, 24, 14, 40), led_c2)
n_after_first = f11.dispatched
rc2, out2 = run_main(f11, dt(2026, 11, 24, 14, 40), led_c2)
chk("第一次派发 1 次", rc1 == 0 and n_after_first == 1, (rc1, n_after_first))
chk("第二次同 slot 0 派发", rc2 == 0 and f11.dispatched == 1, (rc2, f11.dispatched))

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
