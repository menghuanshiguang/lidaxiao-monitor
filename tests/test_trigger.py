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
    [x.strftime("%H:%M") for x in T.slot_datetimes(datetime.date(2026, 11, 24))] == ["14:40", "20:00"])
chk("slot_key 零填充", T.slot_key(dt(2026, 11, 24, 14, 40)) == "2026-11-24 14:40")
chk("14:40 的有效期到次日 20:00",
    T.slot_deadline(dt(2026, 11, 24, 14, 40)) == dt(2026, 11, 25, 20, 0))
chk("20:00 的有效期到次日 14:40",
    T.slot_deadline(dt(2026, 11, 24, 20, 0)) == dt(2026, 11, 25, 14, 40))

chk("14:39 补昨天 20:00(它还差 1 分钟到期), 不是「未到点」",
    T.slot_key(T.due_slot(dt(2026, 11, 24, 14, 39))) == "2026-11-23 20:00")
chk("14:40 正好到点 -> 14:40",
    T.slot_key(T.due_slot(dt(2026, 11, 24, 14, 40))) == "2026-11-24 14:40")
chk("15:30 仍补 14:40", T.slot_key(T.due_slot(dt(2026, 11, 24, 15, 30))) == "2026-11-24 14:40")
chk("19:00 仍补 14:40(不是已过 60 分钟就丢)",
    T.slot_key(T.due_slot(dt(2026, 11, 24, 19, 0))) == "2026-11-24 14:40")
chk("20:00 -> 20:00", T.slot_key(T.due_slot(dt(2026, 11, 24, 20, 0))) == "2026-11-24 20:00")
chk("21:30 仍补 20:00(挂起 1.5 小时)",
    T.slot_key(T.due_slot(dt(2026, 11, 24, 21, 30))) == "2026-11-24 20:00")
chk("次日 03:10 仍补昨天 20:00(挂起 7h 那种)",
    T.slot_key(T.due_slot(dt(2026, 11, 25, 3, 10))) == "2026-11-24 20:00")
chk("次日 13:00 仍补昨天 20:00", T.slot_key(T.due_slot(dt(2026, 11, 25, 13, 0))) == "2026-11-24 20:00")
chk("次日 14:39 仍是昨天 20:00(快到 14:40 还没到)",
    T.slot_key(T.due_slot(dt(2026, 11, 25, 14, 39))) == "2026-11-24 20:00")
chk("次日 14:40 切到今天的 14:40",
    T.slot_key(T.due_slot(dt(2026, 11, 25, 14, 40))) == "2026-11-25 14:40")
chk("次日 15:00 只补今天的 14:40, 不补昨天的槽",
    T.slot_key(T.due_slot(dt(2026, 11, 25, 15, 0))) == "2026-11-25 14:40")
chk("上午 10:00 补昨天 20:00(整天没开机也能补)",
    T.slot_key(T.due_slot(dt(2026, 11, 25, 10, 0))) == "2026-11-24 20:00")
chk("两个 slot 都在窗内时只返回最近那个",
    T.slot_key(T.due_slot(dt(2026, 11, 25, 13, 0))) == "2026-11-24 20:00")
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
def _just_now_z(after=1):
    """刚刚创建的 run(N 秒前), 不写死日期 —— 写死会让测试随时间腐坏。"""
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=after)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


new_run = {"id": 4242, "path": T.WORKFLOW_PATH, "status": "queued",
           "created_at": _just_now_z(1)}
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
T.ledger_add(led_e, "2026-11-23 20:00", "1")     # 昨天那格已派过 -> 此刻无事可做
f5 = Fake()
rc, out = run_main(f5, dt(2026, 11, 24, 14, 39), led_e)
chk("该补的槽已派过 -> 0 且不联网", rc == 0 and f5.calls == [], (rc, f5.calls))

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

print("== 冷启动: 没有 data/ 也要能自建 ==")
_cold_root = tempfile.mkdtemp()               # 里面没有 data/
_cold_lock = os.path.join(_cold_root, "data", "trigger.lock")
_cold_led = os.path.join(_cold_root, "data", "triggered_slots.txt")
f12 = Fake(run_after=[new_run])
try:
    rc, out = run_main(f12, dt(2026, 11, 24, 14, 40), _cold_led, use_lock=_cold_lock)
except Exception as exc:          # 冷启动崩了要报成 ❌ 而不是把整轮测试炸掉
    rc, out = f"EXC {type(exc).__name__}: {exc}", ""
chk("data/ 不存在时能自建并走到派发确认", rc == 0 and f12.dispatched == 1, (rc, out[-160:]))
chk("自建后锁文件已释放", not os.path.exists(_cold_lock), _cold_lock)
chk("记账落在 data/ 下", os.path.exists(_cold_led), _cold_led)
chk("data/ 目录被建出来", os.path.isdir(os.path.join(_cold_root, "data")))

print("== 记账位置: 不进 state/ ==")
chk("默认记账路径在 data/ 下, 不在 state/",
    os.sep + "data" + os.sep in T.LEDGER_PATH
    and (os.sep + "state" + os.sep) not in T.LEDGER_PATH, T.LEDGER_PATH)
chk("默认锁路径也在 data/ 下",
    os.sep + "data" + os.sep in T.LOCK_PATH, T.LOCK_PATH)

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
