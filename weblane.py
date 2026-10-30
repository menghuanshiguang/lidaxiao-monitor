#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""weblane.py — DeepSeek 网页版通道的唯一入口( dsc.py / dsv.py 的封装层 )

为什么需要这一层
================
dsc.py / dsv.py 用的是 chat.deepseek.com 的**登录态**, 它不是免费 API:

- 一个登录态能撑多少次调用、会不会被风控标记, 完全由 DeepSeek 侧决定;
- 被标记之后没有第二次机会 —— 直接禁言/封停, 没有配额响应头, 没有 429 可退避。

所以这一层把网页版登录态当**有限、可熔断、可审计的资源**来管:

    策略层(账号池/预算/冷却/最小间隔/出口一致性)  -> dsv_guard.Guard
    传输层(稳定 profile/启动环境/拟人化/退出码分类) -> 本模块

对外只暴露: enabled() / chat() / vision() / doctor()
monitor.py 里的 _call_dsc / _call_dsv 各自变成一行委托。

设计红线(改代码前请先读)
========================
1. **不复用临时 profile**。每次调用建一个 uuid profile 再 rmtree, 等于每次都当新访客:
   没有 cookie、没有本地存储、没有缓存指纹积累, 这是最典型的脚本特征。
   本模块给每个账号一个**永久** profile 目录。
2. **CI 里不许跑**。GitHub Actions runner 是数据中心 IP(Azure 美国), 和家宽共用同一个
   登录态 = 异地登录 + 账号共享, 这条信号的权重比调用频率高得多。
   默认在 GITHUB_ACTIONS=true 时直接拒绝, 要强行跑必须显式 WEB_LANE_ALLOW_CI=1。
3. **默认有头**。headless 的 UA/fonts/WebGL 指纹是硬伤, 宁可不开也不要开着装人。
4. **失败要响**。没有可用账号就抛 WebLaneDisabled / NoSlot, 让上层兜底到 API 通道,
   绝不静默返回空串假装成功。
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))

# 真实 Edge 141 / Windows 的 UA。headless 模式下 Chromium 会自带 "HeadlessChrome", 必须覆盖。
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0")

DEFAULT_BLOCK = {
    "enabled": False,              # 默认关闭: 网页版通道是兜底, 不是主力
    "allow_headless": False,       # 默认要求有头浏览器
    "allow_ci": False,             # 默认禁止在 CI 里跑
    "keep_session": True,          # 传 --keep: 会话不再建完即删
    "profile_root": "data/profiles",
    "accounts_file": "web_accounts.json",
    "ua": DEFAULT_UA,
    "viewport": [1366, 900],
    "locale": "zh-CN",
    "timezone": "Asia/Shanghai",
    "jitter_s": [3, 11],           # 调用前随机等待, 打掉"机器时钟"指纹
    # 单次运行的调用上限不在这里: 由 dsv_guard 的 daily_budget / hourly_budget /
    # min_interval_s 统一守住 —— 死循环会先把当天额度烧完, 然后被 NoSlot 拒掉。
}


class WebLaneError(RuntimeError):
    """网页版通道不可用/调用失败。上层应据此降级到 API 通道。"""


class WebLaneDisabled(WebLaneError):
    """配置或环境不允许使用网页版通道。"""


# ---------------------------------------------------------------- 配置

def block(cfg: dict | None) -> dict:
    """合并 config.json 的 web_lane 段与默认值(缺字段用默认, 不抛异常)。"""
    b = dict(DEFAULT_BLOCK)
    raw = (cfg or {}).get("web_lane") or {}
    if isinstance(raw, dict):
        b.update(raw)
    return b


def enabled(cfg: dict | None) -> bool:
    """网页版通道是否允许使用。CI 环境一律拒绝(除非显式放行)。"""
    b = block(cfg)
    if not b.get("enabled"):
        return False
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true" and not (
            b.get("allow_ci") or os.environ.get("WEB_LANE_ALLOW_CI") == "1"):
        return False
    if not b.get("allow_headless") and os.environ.get("WEB_LANE_HEADFUL", "1") == "0":
        return False
    return True


def _require_enabled(cfg: dict | None) -> dict:
    b = block(cfg)
    if not b.get("enabled"):
        raise WebLaneDisabled("web_lane.enabled=false (网页版通道已关闭, 请用 API 通道)")
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true" and not (
            b.get("allow_ci") or os.environ.get("WEB_LANE_ALLOW_CI") == "1"):
        raise WebLaneDisabled(
            "拒绝在 CI 中调用 DeepSeek 网页版: 数据中心 IP 与家宽共用登录态是最高权重风控信号。"
            "确需放行请设 WEB_LANE_ALLOW_CI=1")
    return b


# ---------------------------------------------------------------- 账号/凭据

def _accounts_path(cfg: dict | None) -> str:
    p = block(cfg).get("accounts_file") or "web_accounts.json"
    return p if os.path.isabs(p) else os.path.join(HERE, p)


def load_accounts(cfg: dict | None) -> list[dict]:
    p = _accounts_path(cfg)
    if not os.path.exists(p):
        raise WebLaneDisabled(f"账号表不存在: {p} (先从 web_accounts.example.json 复制一份)")
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    accts = data.get("accounts") if isinstance(data, dict) else data
    if isinstance(accts, dict):
        # 允许 {"accounts": {"main-home": {...}}} 这种以 id 为键的写法(与 dsv_guard 一致)
        accts = [{**v, "id": k} if isinstance(v, dict) else v for k, v in accts.items()]
    if not isinstance(accts, list) or not accts:
        raise WebLaneError(f"账号表格式错误或为空: {p}")
    return accts


def account_record(cfg: dict | None, account_id: str) -> dict:
    for a in load_accounts(cfg):
        if a.get("id") == account_id:
            return a
    raise WebLaneError(f"账号表里没有 id={account_id!r}")


def dotenv_value(name: str, base_dir: str | None = None) -> str:
    """从 <模块目录>/.env 里取一个变量的值(与 monitor.py 的 .env 约定一致)。

    monitor.py 自己解析 .env(_load_env_var), 但那是在进程内把值传给 _call_dsc 的
    fallback; `weblane.py doctor` 单独跑时看不到, 会误报"取不到 token"。这里补一条
    同口径的读取路径, 让 doctor 的结论与真实运行一致。
    解析器故意极简: KEY=VALUE、# 注释、成对引号剥掉 —— 不做变量展开、不猜。
    """
    base = base_dir or HERE
    path = os.path.join(base, ".env")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip().lstrip("export ").strip() != name:
                    continue
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                return v.strip()
    except OSError:
        return ""
    return ""


def resolve_token(cfg: dict | None, account_id: str, fallback: str = "") -> str:
    """按账号取 token: 环境变量 > token_file > .env > fallback。

    token_file 是账号级别的显式指定, 比全局 .env 更具体, 所以排在 .env 前面。
    fallback 是单账号模式(账号表只登记一个、token 仍走 DSV_TOKEN)的兼容路径;
    多处都取不到就抛, 绝不带着空 token 去开浏览器。
    """
    rec = account_record(cfg, account_id)
    env_name = (rec.get("token_env") or "").strip()
    if env_name:
        tok = (os.environ.get(env_name) or "").strip()
        if tok:
            return tok
    tf = (rec.get("token_file") or "").strip()
    if tf:
        path = tf if os.path.isabs(tf) else os.path.join(HERE, tf)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                tok = f.read().strip()
            if tok:
                return tok
    if env_name:
        tok = dotenv_value(env_name)
        if tok:
            return tok
    fb = (fallback or "").strip().lstrip("\ufeff")
    if fb:
        return fb
    raise WebLaneError(
        f"账号 {account_id} 取不到 token: {env_name or 'token_env 未配置'} "
        f"不在环境变量/.env 里, 也没有 token_file")


# ---------------------------------------------------------------- 策略层

_GUARDS: dict[str, object] = {}


def guard(cfg: dict | None):
    """懒加载 dsv_guard.Guard(策略层)。缺文件时报错而不是静默放行。"""
    path = _accounts_path(cfg)
    g = _GUARDS.get(path)
    if g is not None:
        return g
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import dsv_guard  # noqa: WPS433  同目录模块
    except ImportError as e:
        raise WebLaneError(
            f"缺少策略层 dsv_guard.py ({e}). 网页版通道必须带预算/冷却才能用, 拒绝在无保护状态下调用。")
    g = dsv_guard.Guard(path=path)
    _GUARDS[path] = g
    return g


def set_guard(cfg: dict | None, g) -> None:
    """测试注入点: 用替身替换策略层。"""
    _GUARDS[_accounts_path(cfg)] = g


# ---------------------------------------------------------------- 传输层

def token_file_path() -> str:
    """dsc.py / dsv.py 固定读 ~/.dsv_token; DSV_TOKEN_FILE 可覆盖(分账号隔离/沙箱内自测)。"""
    return os.path.expanduser(os.environ.get("DSV_TOKEN_FILE") or "~/.dsv_token")


@contextlib.contextmanager
def token_lock(timeout: float = 420.0):
    """dsc.py / dsv.py 都只认 ~/.dsv_token 这一个全局文件, 多个账号必须串行写它。

    这里用独占锁文件把「写 token + 跑子进程」整段罩住, 避免 A 账号的 token 被 B 覆盖。
    """
    lock = token_file_path() + ".lock"
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise WebLaneError(f"等待 token 锁超时: {lock} (另一个网页版调用还在跑)")
            time.sleep(0.5)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(lock)


def write_token(tok: str) -> str:
    path = token_file_path()
    with open(path, "w", encoding="utf-8") as f:
        f.write(tok)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path


def stable_profile(cfg: dict | None, account_id: str) -> str:
    """每个账号一个固定 profile 目录, **永久保留**。

    这是整套改动里最便宜、收益最高的一条: 让账号在服务端看到的是一个
    有历史、有 cookie、有缓存的老访客, 而不是每天几十个零历史新设备。
    """
    root = block(cfg).get("profile_root") or "data/profiles"
    root = root if os.path.isabs(root) else os.path.join(HERE, root)
    prof = os.path.join(root, account_id)
    os.makedirs(prof, exist_ok=True)
    marker = os.path.join(prof, ".weblane")
    if not os.path.exists(marker):
        with open(marker, "w", encoding="utf-8") as f:
            json.dump({"created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "account": account_id}, f)
    return prof


def _guard_identity(g, account_id: str) -> dict:
    """从策略层取该账号的指纹参数(UA/视口/语言/时区)。

    策略层是这些事实的所有者(它按账号登记, 也按账号隔离); web_lane 段里的同名项
    只是"策略层替身/自测"时的兜底默认值。这里只走公开接口 launch_flags/env_for,
    不读策略层内部状态 —— 换实现不影响这里。
    """
    out: dict = {}
    flags_fn = getattr(g, "launch_flags", None)
    if callable(flags_fn):
        try:
            flags = flags_fn(account_id, headful=False) or []
        except Exception:                      # noqa: BLE001  指纹取不到不该阻断调用
            flags = []
        for f in flags:
            if not isinstance(f, str):
                continue
            if f.startswith("--user-agent="):
                out["ua"] = f.split("=", 1)[1]
            elif f.startswith("--window-size="):
                out["viewport"] = f.split("=", 1)[1]
            elif f.startswith("--lang="):
                out["locale"] = f.split("=", 1)[1]
    env_fn = getattr(g, "env_for", None)
    if callable(env_fn):
        try:
            genv = env_fn(account_id) or {}
        except Exception:                      # noqa: BLE001
            genv = {}
        if genv.get("TZ"):
            out["timezone"] = genv["TZ"]
    return out


def build_env(cfg: dict | None, account_id: str, profile_dir: str,
              timeout: int, kind: str, g=None) -> dict:
    """构造子进程环境: 时区/语言/UA/视口/有头开关, 全部显式, 不吃宿主机默认值。"""
    b = block(cfg)
    ident = _guard_identity(g, account_id) if g is not None else {}
    vp = ident.get("viewport") or b.get("viewport") or [1366, 900]
    if isinstance(vp, str):
        # --window-size 只认 "宽,高"; 有人会写 "1366x900", 这里统一成逗号形式,
        # 否则 dsc.py 把整串原样拼进 --window-size= 时会被 Chromium 忽略(退回 800x600)。
        vp_str = vp.replace("x", ",").replace("X", ",")
    else:
        vp_str = f"{int(vp[0])},{int(vp[1])}"
    env = dict(os.environ)
    env["DSV_EDGE_PROFILE"] = profile_dir
    env["TZ"] = ident.get("timezone") or b.get("timezone") or "Asia/Shanghai"
    env["LANG"] = "zh_CN.UTF-8"
    env["DSV_UA"] = ident.get("ua") or b.get("ua") or DEFAULT_UA
    env["DSV_VIEWPORT"] = vp_str
    env["DSV_LOCALE"] = ident.get("locale") or b.get("locale") or "zh-CN"
    env["DSV_ACCOUNT"] = account_id
    headful = not b.get("allow_headless", False) or os.environ.get("WEB_LANE_HEADFUL") == "1"
    # dsc.py 认 DSC_HEADFUL, dsv.py 认 DSV_HEADFUL(后者需配合补丁)
    if headful:
        env["DSC_HEADFUL"] = "1"
        env["DSV_HEADFUL"] = "1"
    else:
        env.pop("DSC_HEADFUL", None)
        env.pop("DSV_HEADFUL", None)
    if kind == "text":
        env["DSC_TIMEOUT"] = str(timeout)
    else:
        env["DSV_TIMEOUT"] = str(timeout)
    return env


def _classify(rc: int, out: str, err: str, kind: str) -> tuple[bool, str]:
    """把子进程结果映射成 (ok, risk)。risk 非空 -> 账号进冷却。

    退出码含义(dsc.py/dsv.py 一致): 0 成功 / 1 用法 / 2 参数 / 3 发码失败 /
    4 token 失效 / 5 并发锁 / 6 风控熔断。
    """
    tail = ((err or "") + "\n" + (out or "")).strip()
    low = tail.lower()
    risk_words = ("操作过于频繁", "请求过于频繁", "账号异常", "环境异常", "行为异常",
                  "验证失败", "too many requests", "rate limit", "unusual activity")
    if rc == 6:
        return False, "breaker"
    if rc == 4:
        return False, "token-invalid"
    if rc == 3:
        return False, "send-failed"
    if rc == 5:
        return False, ""            # 本地锁冲突, 与平台风控无关
    if rc != 0:
        for w in risk_words:
            if w in low:
                return False, f"risk-word:{w}"
        return False, ""
    if "超时未获取回答" in out or "未获取回答" in out:
        return False, "timeout"
    if "DeepSeek拒绝处理" in out:
        return False, "content-refused"
    if not out.strip():
        return False, "empty"
    return True, ""


def _run(cfg: dict | None, kind: str, argv: list[str], timeout: int,
         token: str = "", caller: str = "monitor") -> str:
    b = _require_enabled(cfg)
    g = guard(cfg)
    slot = g.acquire(kind, caller=caller)          # 预算/冷却/最小间隔都在这里卡
    account = slot.get("account") or ""
    prof = slot.get("profile_dir") or stable_profile(cfg, account)
    os.makedirs(prof, exist_ok=True)

    jit = b.get("jitter_s") or [0, 0]
    delay = random.uniform(float(jit[0]), float(jit[1]))
    if delay > 0:
        time.sleep(delay)

    tok = resolve_token(cfg, account, fallback=token)
    env = build_env(cfg, account, prof, timeout, kind, g=g)

    # 子进程输出落文件而不是走管道:
    #  1) 每次调用留一份原始日志, 事后能复盘"平台到底回了什么" —— 被风控时这是唯一证据;
    #  2) Windows 上管道受沙箱/句柄限制, 落文件更稳, 也不会被超长输出噎死。
    logdir = os.path.join(prof, "logs")
    os.makedirs(logdir, exist_ok=True)
    logpath = os.path.join(logdir, f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}.log")

    t0 = time.time()
    rc, ok, risk, note = None, False, "", ""
    try:
        with token_lock():
            write_token(tok)
            with open(logpath, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(f"# argv={argv[:2]} ... (prompt {len(argv[-1])}ch)\n")
                fh.write(f"# account={account} egress={slot.get('egress')} "
                         f"profile={prof} caller={caller} kind={kind}\n")
                fh.write("# ---- child output ----\n")
                fh.flush()
                rc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=fh,
                                    stderr=subprocess.STDOUT, timeout=timeout + 90,
                                    env=env,
                                    cwd=os.path.dirname(os.path.abspath(argv[1]))
                                    ).returncode
        raw = read_text_tail(logpath, 200_000)
        body = raw.split("# ---- child output ----\n", 1)[-1]
        ok, risk = _classify(rc, body, "", kind)
        note = f"exit={rc} log={os.path.basename(logpath)}"
    except subprocess.TimeoutExpired:
        rc, ok, risk, note = None, False, "timeout", f"timeout log={os.path.basename(logpath)}"
    except OSError as e:
        ok, risk, note = False, "", f"spawn-error:{e}"
    finally:
        g.report(slot, ok=ok, risk=risk, duration_s=time.time() - t0, note=note)

    if not ok:
        tail = (read_text_tail(logpath, 4000) if os.path.exists(logpath) else "")[-400:]
        raise WebLaneError(
            f"weblane[{kind}/{account}] 失败 risk={risk or 'unknown'} exit={rc} {note}: {tail}")
    return body.strip()


def read_text_tail(path: str, limit: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
    except OSError:
        return ""
    return data[-limit:]


# ---------------------------------------------------------------- 对外接口

def chat(cfg: dict, pcfg: dict, token: str, prompt: str, dsc_py: str | None = None,
         caller: str = "monitor") -> str:
    """文本: 走 dsc.py。cfg 是 config.json 全文(读 web_lane 段), pcfg 是 llm_cli 段。"""
    b = block(cfg)
    exe = dsc_py or pcfg.get("path") or "dsc.py"
    argv = [sys.executable, os.path.abspath(exe)]
    if b.get("keep_session", True):
        argv.append("--keep")          # 不再"建会话-问一句-立刻删", 去掉最尖的那条脚本特征
    argv.append(prompt)
    return _run(cfg, "text", argv, int(pcfg.get("timeout", 300) or 300),
                token=token, caller=caller)


def vision(cfg: dict, pcfg: dict, token: str, prompt: str, images: list[str],
           dsv_py: str | None = None, caller: str = "monitor") -> str:
    """视觉: 走 dsv.py, 多帧一次会话发送。"""
    b = block(cfg)
    exe = dsv_py or pcfg.get("path") or "dsv.py"
    argv = [sys.executable, os.path.abspath(exe)]
    if b.get("keep_session", True):
        argv.append("--keep")
    argv += [os.path.abspath(p) for p in images]
    argv.append(prompt)
    return _run(cfg, "vision", argv, int(pcfg.get("timeout", 240) or 240),
                token=token, caller=caller)


def doctor(cfg: dict) -> dict:
    """打印通道就绪度: 是否启用/为何禁用/账号表/各账号预算与冷却。"""
    b = block(cfg)
    info = {"enabled": enabled(cfg), "raw_enabled": bool(b.get("enabled")),
            "ci": os.environ.get("GITHUB_ACTIONS", "") or "-",
            "accounts_file": _accounts_path(cfg), "accounts_ok": False,
            "accounts": [], "warnings": []}
    try:
        recs = load_accounts(cfg)
        info["accounts_ok"] = True
        for r in recs:
            aid = r.get("id", "?")
            try:
                resolve_token(cfg, aid)
                tok_ok = True
            except WebLaneError:
                tok_ok = False
                info["warnings"].append(f"{aid}: 取不到 token, 该账号不可用")
            prof = stable_profile(cfg, aid)
            info["accounts"].append({
                "id": aid, "egress": r.get("egress", "-"), "token_ok": tok_ok,
                "profile": prof, "profile_kept": os.path.isdir(prof),
                "kinds": r.get("allowed_kinds", ["text", "vision"]),
            })
    except WebLaneError as e:
        info["warnings"].append(str(e))
    if not info["enabled"] and info["raw_enabled"]:
        info["warnings"].append("配置已开但被环境规则拦下(CI 或 headless 限制)")
    return info


# ---------------------------------------------------------------- 自检(离线)

def selftest(verbose: bool = True) -> int:
    """离线自检: 不需要网络、不需要 dsc.py、不需要真实 token。

    用一个回显 argv/env 的假 dsc.py 当被测对象, 断言传输层的每一条硬约束都生效。
    任何一条不成立就退出码 1 —— 这套守卫必须先红过, 才算守卫。
    """
    import tempfile
    import shutil

    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if verbose:
            print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="weblane_selftest_")
    old_cwd = os.getcwd()
    saved = {k: os.environ.get(k) for k in
             ("GITHUB_ACTIONS", "WEB_LANE_ALLOW_CI", "DSV_TOKEN_SELFTEST",
              "WEB_LANE_HEADFUL", "DSV_TOKEN_FILE")}
    try:
        # 假 dsc.py: 把收到的 argv 和关键 env 落成 JSONL, 供断言
        fake = os.path.join(tmp, "fake_dsc.py")
        calls_jsonl = os.path.join(tmp, "calls.jsonl")
        fake_src = '''import json, os, sys
rec = {"argv": sys.argv[1:],
       "profile": os.environ.get("DSV_EDGE_PROFILE"),
       "ua": os.environ.get("DSV_UA"),
       "vp": os.environ.get("DSV_VIEWPORT"),
       "tz": os.environ.get("TZ"),
       "headful": os.environ.get("DSC_HEADFUL"),
       "tok": open(os.path.expanduser(os.environ.get("DSV_TOKEN_FILE") or "~/.dsv_token")).read().strip()}
with open(__CALLS__, "a", encoding="utf-8") as fp:
    json.dump(rec, fp, ensure_ascii=False)
    fp.write("\\n")
sys.stdout.write("HELLO-FROM-FAKE")
'''.replace("__CALLS__", repr(calls_jsonl))
        with open(fake, "w", encoding="utf-8") as f:
            f.write(fake_src)

        # 假策略层: 固定发一个账号, 记录 acquire/report
        class FakeGuard:
            def __init__(self):
                self.calls = []
                self.reports = []

            def acquire(self, kind, caller="monitor", wait=True):
                self.calls.append(kind)
                prof = os.path.join(tmp, "profiles", "acc-01")
                os.makedirs(prof, exist_ok=True)
                return {"account": "acc-01", "profile_dir": prof, "egress": "home-cn"}

            def report(self, slot, ok, risk="", note="", duration_s=None):
                self.reports.append((ok, risk))
                return {}

        acct_file = os.path.join(tmp, "web_accounts.json")
        with open(acct_file, "w", encoding="utf-8") as f:
            json.dump({"accounts": [{"id": "acc-01", "token_env": "DSV_TOKEN_SELFTEST",
                                     "egress": "home-cn"}]}, f)
        os.environ["DSV_TOKEN_SELFTEST"] = "tok-selftest-123"
        os.environ["DSV_TOKEN_FILE"] = os.path.join(tmp, "dsv_token")   # 自测全程留在临时目录

        cfg = {"web_lane": {"enabled": True, "accounts_file": acct_file,
                            "profile_root": os.path.join(tmp, "profiles"),
                            "jitter_s": [0, 0], "allow_ci": True, "keep_session": True}}
        set_guard(cfg, FakeGuard())
        pcfg = {"timeout": 30, "path": fake}

        # 1) 基本调用成功 + --keep 传进去了 + 稳定 profile + UA/视口/时区齐备
        out = chat(cfg, pcfg, "", "PROMPT-ONE")
        check("chat 返回子进程输出", out == "HELLO-FROM-FAKE", out)
        with open(os.path.join(tmp, "calls.jsonl"), encoding="utf-8") as f:
            rec1 = json.loads(f.readline())
        check("传了 --keep(会话不再建完即删)", "--keep" in rec1["argv"], str(rec1["argv"]))
        check("prompt 作为最后一个参数", rec1["argv"][-1] == "PROMPT-ONE")
        check("profile 落在账号固定目录(非 uuid)",
              rec1["profile"].endswith(os.path.join("profiles", "acc-01")), rec1["profile"] or "")
        check("UA 已覆盖(不含 HeadlessChrome)",
              bool(rec1["ua"]) and "HeadlessChrome" not in (rec1["ua"] or ""), rec1["ua"] or "")
        check("视口显式指定", rec1["vp"] == "1366,900", str(rec1["vp"]))
        check("token 按账号注入 ~/.dsv_token", rec1["tok"] == "tok-selftest-123")

        # 2) 第二次调用复用同一个 profile 路径(证明"稳定 profile"真的稳定)
        chat(cfg, pcfg, "", "PROMPT-TWO")
        with open(os.path.join(tmp, "calls.jsonl"), encoding="utf-8") as f:
            lines = [json.loads(x) for x in f if x.strip()]
        check("两次调用 profile 路径一致(不再每次新建)",
              len(lines) >= 2 and lines[0]["profile"] == lines[1]["profile"])

        # 3) 成功调用要回报给策略层, 否则预算永远不会被记账
        g = _GUARDS[_accounts_path(cfg)]
        check("成功调用已 report 给策略层", g.reports and g.reports[-1][0] is True, str(g.reports[-1:]))

        # 4) 子进程非 0 退出 -> 抛 WebLaneError, 绝不返回空串假装成功
        boom = os.path.join(tmp, "boom_dsc.py")
        with open(boom, "w", encoding="utf-8") as f:
            f.write("import sys; sys.stderr.write('操作过于频繁'); sys.exit(6)\n")
        try:
            chat(cfg, {"timeout": 30, "path": boom}, "", "x")
            check("风控熔断时抛异常", False)
        except WebLaneError as e:
            check("风控熔断时抛异常", True, str(e)[:60])
        check("熔断被识别为 risk=breaker", g.reports[-1] == (False, "breaker"), str(g.reports[-1]))

        # 5) enabled=false -> WebLaneDisabled(而不是悄悄降级成空答案)
        cfg2 = json.loads(json.dumps(cfg))
        cfg2["web_lane"]["enabled"] = False
        check("enabled=false 时 enabled() 为假", enabled(cfg2) is False)
        try:
            chat(cfg2, {"timeout": 30, "path": fake}, "", "x")
            check("关闭时抛 WebLaneDisabled", False)
        except WebLaneDisabled:
            check("关闭时抛 WebLaneDisabled", True)

        # 6) CI 里默认拒绝(数据中心 IP 是最高权重风控信号)
        os.environ["GITHUB_ACTIONS"] = "true"
        cfg3 = json.loads(json.dumps(cfg))
        cfg3["web_lane"]["allow_ci"] = False
        check("CI 中 enabled() 默认返回假", enabled(cfg3) is False)
        try:
            chat(cfg3, {"timeout": 30, "path": fake}, "", "x")
            check("CI 中抛 WebLaneDisabled", False)
        except WebLaneDisabled:
            check("CI 中抛 WebLaneDisabled", True)
        os.environ.pop("GITHUB_ACTIONS", None)

        # 7) 缺账号表 -> 明确报错, 不静默
        cfg4 = json.loads(json.dumps(cfg))
        cfg4["web_lane"]["accounts_file"] = os.path.join(tmp, "nope.json")
        try:
            enabled(cfg4), load_accounts(cfg4)
            check("账号表缺失时抛错", False)
        except WebLaneDisabled:
            check("账号表缺失时抛错", True)

        # 8) doctor() 能报出账号/profile/token 状态
        d = doctor(cfg)
        check("doctor 认出 1 个账号且 token 可用",
              d["accounts_ok"] and d["accounts"] and d["accounts"][0]["token_ok"], json.dumps(d["accounts"], ensure_ascii=False)[:120])
    finally:
        os.chdir(old_cwd)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print(f"\nSELFTEST FAILED: {len(fails)} 项 -> {fails}")
        return 1
    print("\nSELFTEST OK: 传输层硬约束全部生效")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        sys.exit(selftest())
    if cmd == "doctor":
        here = HERE
        with open(os.path.join(here, "config.json"), encoding="utf-8") as f:
            print(json.dumps(doctor(json.load(f)), ensure_ascii=False, indent=2))
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
