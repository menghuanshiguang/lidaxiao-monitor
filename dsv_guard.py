#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsv_guard.py —— DeepSeek 网页版账号池守卫（预算 / 冷却熔断 / 出口一致性 / 指纹稳定）。

用途
----
lidaxiao-monitor 用 chat.deepseek.com 的登录 token 驱动无头 Edge 跑对话。账号已被禁言，
所以调用侧必须在「同一账号不能高频、不能多 IP 复用、被风控立刻停」三个约束下排队取额度。
本模块只做守卫，不碰浏览器、不碰 token 值：

    from dsv_guard import Guard, NoSlot, EgressMismatch

    g = Guard()                                   # $DSV_ACCOUNTS 或 <模块目录>/accounts.json
    slot = g.acquire("text", caller="monitor")     # 拿不到就阻塞轮询，最多 max_wait_s
    try:
        rc = run_dsc(profile=slot["profile_dir"], token=read_token(slot),
                     proxy=proxy_for(slot["egress"]))
        g.report(slot, ok=(rc == 0), duration_s=elapsed)
    except Exception as exc:
        g.report(slot, ok=False, risk="", note=repr(exc)[:300])

配置（accounts.json）
--------------------
    {
      "defaults": {
        "daily_budget": 6, "hourly_budget": 2, "min_interval_s": 600,
        "max_wait_s": 180, "poll_interval_s": 2.0,
        "locale": "zh-CN", "accept_lang": "zh-CN,zh;q=0.9,en;q=0.8",
        "timezone": "Asia/Shanghai", "window_size": "1280,900",
        "profiles_dir": "profiles", "user_agent": null, "extra_flags": []
      },
      "accounts": [
        {"id": "acc-a", "egress": "home-cn", "enabled": true,
         "token_env": "DSV_TOKEN_ACC_A", "notes": "家里宽带"},
        {"id": "acc-b", "egress": "vps-jp-1", "enabled": true,
         "token_file": "C:/secrets/acc-b.token",
         "daily_budget": 3, "hourly_budget": 1, "min_interval_s": 900}
      ]
    }

也接受裸数组 `[{...}, {...}]`，或 `{"acc-a": {...}, "acc-b": {...}}` 这种 id→账号的映射。
每个账号必须且只能靠 `token_env`（环境变量名）或 `token_file`（文件路径）指向 token；
本模块**从不读取、从不写入、从不返回** token 值，只把「该去哪拿」告诉调用方。

落地的约束
----------
* 双闸预算：`账号 × 天` + `账号 × 小时`，文本与视觉共用同一池子（同账号同风险源）。
* 最小间隔：`min_interval_s` 跨 caller / 跨进程生效 —— 状态文件 + `O_CREAT|O_EXCL` 自旋文件锁，
  读改写全程持锁，落盘走「临时文件 + `os.replace`」。
* 风控熔断：`risk` 非空即进冷却，时长按连续次数 1h → 6h → 24h → 72h（封顶）升级。
  成功一次**不降级**；连续计数只在 `reset()` 或冷却自然到期后清零。
* 出口一致：`egress` 是账号的登记属性。`acquire(egress_hint=...)` 与登记不符直接
  `EgressMismatch`；状态里记过 egress 而配置被改成别的，同样 `EgressMismatch`，不静默降级。
  有意改线时先 `reset()`（reset 同时把该账号重新登记到当前配置的 egress）。
* 审计：每次 acquire / report 追加一行 JSONL 到 `$DSV_AUDIT_LOG` 或 `<模块目录>/dsv_audit.jsonl`。

预算在 acquire 时就扣（预留），report 只记账不回滚。崩掉的调用也算用掉额度 ——
风控方向上保守才是对的。

纯标准库，Python 3.12，Windows 为主，无第三方依赖。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from typing import Any

__all__ = [
    "Guard",
    "GuardError",
    "GuardConfigError",
    "NoSlot",
    "EgressMismatch",
    "LockTimeout",
    "KINDS",
    "DEFAULT_DAILY_BUDGET",
    "DEFAULT_HOURLY_BUDGET",
    "DEFAULT_MIN_INTERVAL_S",
    "DEFAULT_MAX_WAIT_S",
    "DEFAULT_COOLDOWN_LADDER_S",
]

__version__ = "1.0.0"

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- 默认值（自测里逐条断言，防止有人把这些数字悄悄放松）----------------------
DEFAULT_DAILY_BUDGET = 6            # 账号 × 天
DEFAULT_HOURLY_BUDGET = 2           # 账号 × 小时
DEFAULT_MIN_INTERVAL_S = 600.0      # 同账号两次 acquire 的最小间隔
DEFAULT_MAX_WAIT_S = 180.0          # wait=True 时最长轮询
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_COOLDOWN_LADDER_S = (3600.0, 21600.0, 86400.0, 259200.0)  # 1h / 6h / 24h / 72h
DEFAULT_RESERVATION_TTL_S = 900.0   # 预留多久没回报就视为调用方崩了
DEFAULT_LOCALE = "zh-CN"
DEFAULT_ACCEPT_LANG = "zh-CN,zh;q=0.9,en;q=0.8"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_WINDOW_SIZE = "1280,900"
DEFAULT_WINDOW_POSITION = "60,60"
DEFAULT_HEADLESS_FLAG = "--headless=new"

KINDS = ("text", "vision")

LOCK_TIMEOUT_S = 30.0
LOCK_STALE_S = 120.0
STATE_VERSION = 1

_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$")
_EGRESS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

# os.replace 的唯一调用点：既是原子写的落点，也是自测注入「替换失败」的挂点。
_REPLACE = os.replace


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class GuardError(Exception):
    """守卫模块的所有错误基类。"""


class GuardConfigError(GuardError):
    """配置/状态不可用。加载期就该炸的错误，绝不静默跳过。"""


class LockTimeout(GuardError):
    """抢状态文件锁超时。"""


class NoSlot(GuardError):
    """没有可用账号。`reason` 是给人看的摘要，`per_account` 是逐账号拒绝原因。"""

    def __init__(self, reason: str, per_account: list[dict[str, Any]] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.per_account = per_account or []


class EgressMismatch(GuardError):
    """出口 IP 与账号登记不一致。不许静默降级。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _now() -> float:
    return time.time()


def _day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _hour_key(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H", time.localtime(ts))


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _atomic_write_json(path: str, obj: Any) -> None:
    """临时文件 → fsync → os.replace。半截写永远落不到目标文件上。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dsvguard-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _REPLACE(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)


def _read_json(path: str, what: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise GuardError(f"{what} 不是合法 JSON: {path}: {exc}") from exc
    except OSError as exc:
        raise GuardError(f"{what} 读取失败: {path}: {exc}") from exc


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise GuardConfigError(f"配置 {name} 必须是 true/false，得到 {value!r}")
    return value


def _require_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GuardConfigError(f"配置 {name} 必须是数字，得到 {value!r}")
    out = float(value)
    if out != out or out in (float("inf"), float("-inf")):
        raise GuardConfigError(f"配置 {name} 必须是有限数字，得到 {value!r}")
    if minimum is not None and out < minimum:
        raise GuardConfigError(f"配置 {name} 不能小于 {minimum}，得到 {value!r}")
    return out


def _require_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    raw = _require_number(value, name, minimum=minimum)
    if raw != int(raw):
        raise GuardConfigError(f"配置 {name} 必须是整数，得到 {value!r}")
    return int(raw)


def _require_str(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise GuardConfigError(f"配置 {name} 必须是字符串，得到 {value!r}")
    out = value.strip()
    if not out and not allow_empty:
        raise GuardConfigError(f"配置 {name} 不能为空")
    return out


# ---------------------------------------------------------------------------
# 文件锁：Windows 上没有 fcntl，用 O_CREAT|O_EXCL 自旋 + 超时 + 陈旧锁回收
# ---------------------------------------------------------------------------
class _FileLock:
    """跨进程互斥。临界区只有几十微秒，冲突出清即可，不排队等条件变量。"""

    def __init__(
        self,
        path: str,
        timeout: float = LOCK_TIMEOUT_S,
        stale_after: float = LOCK_STALE_S,
        poll: float = 0.005,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self.poll = poll
        self._held = False

    def _slept(self) -> None:
        # 进程号取模做抖动，避免两个进程同相撞在同一毫秒。
        time.sleep(self.poll + (os.getpid() % 7) * 0.001)

    def _break_stale(self) -> None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return
        except OSError:
            return
        age = _now() - st.st_mtime
        if age <= self.stale_after:
            return
        with contextlib.suppress(OSError):
            os.unlink(self.path)
            print(
                f"[dsv_guard] 回收陈旧锁文件（{age:.0f}s 未释放）: {self.path}",
                file=sys.stderr,
            )

    def acquire(self) -> "_FileLock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                self._break_stale()
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"抢锁超时 {self.timeout:.1f}s: {self.path}"
                        "（若确认没有其它 monitor 在跑，可删除该 .lock 文件）"
                    ) from None
                self._slept()
                continue
            except FileNotFoundError:
                directory = os.path.dirname(os.path.abspath(self.path))
                os.makedirs(directory, exist_ok=True)
                continue
            except OSError as exc:
                raise LockTimeout(f"创建锁文件失败: {self.path}: {exc}") from exc
            try:
                os.write(fd, f"{os.getpid()} {_now():.3f}\n".encode("ascii"))
            finally:
                os.close(fd)
            self._held = True
            return self

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

    def __enter__(self) -> "_FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


# ---------------------------------------------------------------------------
# 账号配置
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class _AccountConfig:
    id: str
    egress: str
    enabled: bool
    token_env: str | None
    token_file: str | None
    profile_dir: str
    daily_budget: int
    hourly_budget: int
    min_interval_s: float
    user_agent: str | None
    window_size: str | None
    window_position: str | None
    notes: str

    @property
    def token_source(self) -> str:
        if self.token_file:
            return f"file:{self.token_file}"
        return f"env:{self.token_env}"

    @property
    def token_path(self) -> str:
        """给调用方的路径提示。只用 token_env 时为空串（token 由调用方从环境变量注入）。"""
        return self.token_file or ""


class Guard:
    """账号池守卫。构造即读配置；每次 acquire/report 都重新读状态，不缓存。"""

    def __init__(self, path: str | None = None) -> None:
        self.config_path = _resolve_config_path(path)
        self._base_dir = os.path.dirname(self.config_path)

        self.defaults, accounts = _load_config(self.config_path)
        self._accounts: tuple[_AccountConfig, ...] = tuple(accounts)
        self._by_id: dict[str, _AccountConfig] = {a.id: a for a in self._accounts} if accounts else {}
        self._order: dict[str, int] = {a.id: i for i, a in enumerate(self._accounts)}

        state_override = os.environ.get("DSV_GUARD_STATE", "").strip()
        self.state_path = (
            os.path.abspath(os.path.expanduser(state_override))
            if state_override
            else os.path.join(self._base_dir, "dsv_guard_state.json")
        )
        self.lock_path = self.state_path + ".lock"

        audit_override = os.environ.get("DSV_AUDIT_LOG", "").strip()
        self.audit_path = (
            os.path.abspath(os.path.expanduser(audit_override))
            if audit_override
            else os.path.join(MODULE_DIR, "dsv_audit.jsonl")
        )

        self._lock = _FileLock(self.lock_path)

    # -- 配置查询 ----------------------------------------------------------
    def _account(self, account_id: str) -> _AccountConfig:
        try:
            return self._by_id[account_id]
        except KeyError:
            known = ", ".join(a.id for a in self._accounts) or "<空>"
            raise GuardConfigError(
                f"未知账号 {account_id!r}；配置里的账号: {known}"
            ) from None

    # -- 对外：账号表（脱敏） ----------------------------------------------
    def accounts(self) -> list[dict]:
        """当前账号表。只给 token 的来源名，绝不给值。"""
        out: list[dict] = []
        for cfg in self._accounts:
            out.append(
                {
                    "account": cfg.id,
                    "egress": cfg.egress,
                    "enabled": cfg.enabled,
                    "token_source": cfg.token_source,
                    "profile_dir": cfg.profile_dir,
                    "daily_budget": cfg.daily_budget,
                    "hourly_budget": cfg.hourly_budget,
                    "min_interval_s": cfg.min_interval_s,
                    "notes": cfg.notes,
                }
            )
        return out

    # -- 对外：稳定指纹 -----------------------------------------------------
    def profile_for(self, account_id: str) -> str:
        """每账号一个永久 profile 目录，按需创建，永不删除。"""
        cfg = self._account(account_id)
        os.makedirs(cfg.profile_dir, exist_ok=True)
        return cfg.profile_dir

    def launch_flags(self, account_id: str, headful: bool = False) -> list[str]:
        """Edge/Chromium 启动参数：固定 profile、语言、窗口、关自动化特征、可选有头。"""
        cfg = self._account(account_id)
        d = self.defaults
        profile = self.profile_for(cfg.id)
        window_size = cfg.window_size or d["window_size"]
        window_pos = cfg.window_position or d["window_position"]
        user_agent = cfg.user_agent or d["user_agent"]

        flags = [
            f"--user-data-dir={profile}",
            "--profile-directory=Default",
            f"--lang={d['locale']}",
            f"--accept-lang={d['accept_lang']}",
            f"--window-size={window_size}",
            f"--window-position={window_pos}",
            f"--force-device-scale-factor={d['device_scale_factor']}",
            "--force-color-profile=srgb",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-notifications",
            "--disable-popup-blocking",
            "--disable-dev-shm-usage",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-client-side-phishing-detection",
            "--disable-domain-reliability",
            "--disable-breakpad",
            "--disable-default-apps",
            "--disable-hang-monitor",
            "--disable-prompt-on-repost",
            "--disable-search-engine-choice-screen",
            "--metrics-recording-only",
            "--noerrdialogs",
            "--no-pings",
            "--no-service-autorun",
            "--password-store=basic",
            "--use-mock-keychain",
            "--disable-sync",
            "--mute-audio",
            "--hide-crash-restore-bubble",
        ]
        if user_agent:
            flags.append(f"--user-agent={user_agent}")
        if not headful:
            flags.append(d["headless_flag"])
        flags.extend(d["extra_flags"])
        return flags

    def env_for(self, account_id: str) -> dict:
        """该账号调用时要注入的环境变量。token 只给名字/路径，不给值。"""
        cfg = self._account(account_id)
        profile = self.profile_for(cfg.id)
        env = {
            "TZ": self.defaults["timezone"],
            "LANG": "zh_CN.UTF-8",
            "LC_ALL": "zh_CN.UTF-8",
            "LANGUAGE": "zh_CN:zh",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "DSV_ACCOUNT": cfg.id,
            "DSV_EGRESS": cfg.egress,
            "DSV_EDGE_PROFILE": profile,
            "DSV_GUARD_CONFIG": self.config_path,
            "DSV_GUARD_AUDIT": self.audit_path,
        }
        if cfg.token_env:
            env["DSV_TOKEN_ENV"] = cfg.token_env
        if cfg.token_file:
            env["DSV_TOKEN_FILE"] = cfg.token_file
        return env

    # -- 状态：读改写全程持锁 ----------------------------------------------
    @staticmethod
    def _empty_state() -> dict:
        return {"version": STATE_VERSION, "accounts": {}}

    def _load_state(self) -> dict:
        if not os.path.exists(self.state_path):
            return self._empty_state()
        raw = _read_json(self.state_path, "守卫状态文件")
        if not isinstance(raw, dict):
            raise GuardError(f"守卫状态文件结构不对（应为 JSON 对象）: {self.state_path}")
        accounts = raw.get("accounts")
        if accounts is None:
            raw["accounts"] = {}
        elif not isinstance(accounts, dict):
            raise GuardError(f"守卫状态文件 accounts 字段应为对象: {self.state_path}")
        raw.setdefault("version", STATE_VERSION)
        return raw

    def _save_state(self, state: dict) -> None:
        state["version"] = STATE_VERSION
        state["updated_at"] = _now()
        state["updated_at_iso"] = _iso(_now())
        _atomic_write_json(self.state_path, state)

    @staticmethod
    def _empty_account_state(cfg: _AccountConfig) -> dict:
        return {
            "egress": cfg.egress,
            "day_key": "",
            "day_used": 0,
            "hour_key": "",
            "hour_used": 0,
            "last_acquire_ts": 0.0,
            "last_ts": 0.0,
            "last_ok": None,
            "last_kind": "",
            "last_caller": "",
            "last_note": "",
            "last_duration_s": None,
            "risk_streak": 0,
            "risk_hits_total": 0,
            "risk_hits_day": 0,
            "risk_day_key": "",
            "cooldown_until": 0.0,
            "last_cooldown_s": 0.0,
            "reservations": {},
            "total_acquired": 0,
            "total_ok": 0,
            "total_fail": 0,
        }

    def _normalize_account(self, st: dict, now: float) -> bool:
        """滚动窗口归零 + 冷却自然到期清计数 + 过期预留回收。返回是否改动过。"""
        dirty = False
        day = _day_key(now)
        hour = _hour_key(now)

        if st.get("day_key") != day:
            st["day_key"] = day
            st["day_used"] = 0
            dirty = True
        if st.get("hour_key") != hour:
            st["hour_key"] = hour
            st["hour_used"] = 0
            dirty = True
        if st.get("risk_day_key") != day:
            st["risk_day_key"] = day
            st["risk_hits_day"] = 0
            dirty = True

        cooldown_until = float(st.get("cooldown_until") or 0.0)
        if cooldown_until and cooldown_until <= now and int(st.get("risk_streak") or 0) > 0:
            # 冷却自然到期 -> 连续计数清零（成功调用本身不清零）
            st["risk_streak"] = 0
            st["cooldown_until"] = 0.0
            dirty = True

        ttl = self.defaults["reservation_ttl_s"]
        reservations = st.get("reservations")
        if not isinstance(reservations, dict):
            st["reservations"] = {}
            reservations = st["reservations"]
            dirty = True
        for slot_id, info in list(reservations.items()):
            try:
                ts = float((info or {}).get("ts") or 0.0)
            except (AttributeError, TypeError, ValueError):
                ts = 0.0
            if ttl > 0 and (now - ts) > ttl:
                del reservations[slot_id]
                dirty = True

        if int(st.get("inflight") or 0):
            del st["inflight"]  # 旧字段，预留数以 reservations 为唯一事实
            dirty = True
        return dirty

    def _account_status(self, cfg: _AccountConfig, st: dict, now: float) -> dict:
        cd_until = float(st.get("cooldown_until") or 0.0)
        cd_rem = max(0.0, cd_until - now)
        if not cfg.enabled:
            status = "disabled"
        elif cd_rem > 0:
            status = "cooldown"
        else:
            status = "ready"
        gap = now - float(st.get("last_acquire_ts") or 0.0)
        reservations = st.get("reservations") or {}
        return {
            "account": cfg.id,
            "status": status,
            "egress": cfg.egress,
            "enabled": cfg.enabled,
            "cooldown_until": cd_until,
            "cooldown_remaining": round(cd_rem, 3),
            "last_cooldown_s": float(st.get("last_cooldown_s") or 0.0),
            "hits": int(st.get("risk_hits_total") or 0),
            "risk_streak": int(st.get("risk_streak") or 0),
            "risk_hits_today": int(st.get("risk_hits_day") or 0),
            "day_used": int(st.get("day_used") or 0),
            "daily_budget": cfg.daily_budget,
            "day_key": st.get("day_key") or "",
            "hour_used": int(st.get("hour_used") or 0),
            "hourly_budget": cfg.hourly_budget,
            "hour_key": st.get("hour_key") or "",
            "min_interval_s": cfg.min_interval_s,
            "min_interval_remaining": round(
                max(0.0, cfg.min_interval_s - gap), 3
            ) if st.get("last_acquire_ts") else 0.0,
            "last_ts": float(st.get("last_ts") or 0.0),
            "last_ts_iso": _iso(float(st.get("last_ts") or 0.0) or None),
            "last_ok": st.get("last_ok"),
            "last_kind": st.get("last_kind") or "",
            "last_caller": st.get("last_caller") or "",
            "last_note": st.get("last_note") or "",
            "last_duration_s": st.get("last_duration_s"),
            "inflight": len(reservations),
            "total_acquired": int(st.get("total_acquired") or 0),
            "total_ok": int(st.get("total_ok") or 0),
            "total_fail": int(st.get("total_fail") or 0),
            "profile_dir": cfg.profile_dir,
            "token_source": cfg.token_source,
        }

    # -- 审计 --------------------------------------------------------------
    def _audit(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        try:
            directory = os.path.dirname(os.path.abspath(self.audit_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            # 状态已经提交，审计失败不能反过来毁掉一次合法调用；但必须可见。
            print(
                f"[dsv_guard] 审计写入失败（状态已提交，调用继续）: "
                f"{self.audit_path}: {exc}",
                file=sys.stderr,
            )

    def _audit_record(
        self,
        event: str,
        cfg: _AccountConfig | None,
        *,
        kind: str = "",
        caller: str = "",
        ok: bool | None = None,
        risk: str = "",
        note: str = "",
        duration_s: float | None = None,
        day_used: int | None = None,
        hour_used: int | None = None,
        reason: str = "",
        extra: dict | None = None,
    ) -> None:
        ts = _now()
        record = {
            "ts": ts,
            "ts_iso": _iso(ts),
            "event": event,
            "account": cfg.id if cfg else "",
            "kind": kind,
            "caller": caller,
            "ok": ok,
            "risk": risk,
            "note": note,
            "duration_s": duration_s,
            "egress": cfg.egress if cfg else "",
            "day_used": day_used,
            "hour_used": hour_used,
        }
        if reason:
            record["reason"] = reason
        if extra:
            record.update(extra)
        self._audit(record)

    # -- 取额度 ------------------------------------------------------------
    def acquire(
        self,
        kind: str,
        caller: str = "monitor",
        wait: bool = True,
        egress_hint: str | None = None,
    ) -> dict:
        """按预算/冷却/最小间隔/egress 一致性挑一个可用账号并预留额度。

        `egress_hint` 是附加的可选约束（规格第 5 条要求）：传入时必须与账号登记值一致，
        没有任何账号登记这个 egress 直接抛 `EgressMismatch`（配置错误，等 180s 也没用）。
        拿不到账号：`wait=True` 轮询到 `max_wait_s` 后抛 `NoSlot`，否则立刻抛 `NoSlot`。
        """
        if kind not in KINDS:
            raise ValueError(f"kind 必须是 {KINDS} 之一，得到 {kind!r}")
        if not isinstance(caller, str) or not caller.strip():
            caller = "monitor"
        caller = caller.strip()[:64]

        hint: str | None = None
        if egress_hint is not None:
            hint = str(egress_hint).strip()
            if not hint:
                raise EgressMismatch("egress_hint 为空串；要么不传，要么传登记过的 egress")
            if not any(a.egress == hint for a in self._accounts):
                known = sorted({a.egress for a in self._accounts}) or ["<无账号>"]
                raise EgressMismatch(
                    f"egress_hint={hint!r} 无账号登记；已登记的出口: {', '.join(known)}"
                )

        max_wait = float(self.defaults["max_wait_s"]) if wait else 0.0
        poll = float(self.defaults["poll_interval_s"])
        started = _now()
        deadline = started + max_wait

        last_rejections: list[dict[str, Any]] = []
        while True:
            slot, rejections = self._try_acquire(kind, caller, hint)
            if slot is not None:
                return slot
            last_rejections = rejections
            now = _now()
            if not wait or now >= deadline:
                waited = now - started
                raise NoSlot(self._no_slot_reason(rejections, waited, wait), rejections)
            time.sleep(min(poll, max(0.01, deadline - now)))

    @staticmethod
    def _no_slot_reason(
        rejections: list[dict[str, Any]], waited: float, wait: bool
    ) -> str:
        if not rejections:
            return "没有已启用的账号（检查 accounts.json 的 enabled 字段）"
        parts = []
        for item in rejections:
            label = item.get("account", "?")
            reason = item.get("reason", "?")
            detail = item.get("detail")
            parts.append(f"{label}: {reason}" + (f"（{detail}）" if detail else ""))
        tail = f"，已等待 {waited:.1f}s" if wait else ""
        return "无可用账号：" + "；".join(parts) + tail

    def _try_acquire(
        self, kind: str, caller: str, egress_hint: str | None
    ) -> tuple[dict | None, list[dict[str, Any]]]:
        """一次尝试。整个读改写都在锁内，失败也把滚动归零落盘。"""
        with self._lock:
            state = self._load_state()
            now = _now()
            accounts = state["accounts"]
            rejections: list[dict[str, Any]] = []
            dirty = False
            candidates: list[tuple[tuple, _AccountConfig, dict]] = []

            for cfg in self._accounts:
                if egress_hint is not None and cfg.egress != egress_hint:
                    continue
                if not cfg.enabled:
                    rejections.append(
                        {"account": cfg.id, "reason": "已禁用", "detail": "enabled=false"}
                    )
                    continue
                st = accounts.get(cfg.id)
                if not isinstance(st, dict):
                    st = self._empty_account_state(cfg)
                    accounts[cfg.id] = st
                    dirty = True
                if self._normalize_account(st, now):
                    dirty = True

                recorded = st.get("egress")
                if recorded and recorded != cfg.egress:
                    # 账号登记过的出口被改了：不静默降级，就地炸。
                    raise EgressMismatch(
                        f"账号 {cfg.id} 上次登记 egress={recorded!r}，配置已改为 "
                        f"{cfg.egress!r}；同一账号不允许换出口。若是有意改线，先 "
                        f"`python dsv_guard.py reset {cfg.id}` 重新登记。"
                    )
                if not recorded:
                    st["egress"] = cfg.egress
                    dirty = True

                cd_rem = float(st.get("cooldown_until") or 0.0) - now
                if cd_rem > 0:
                    rejections.append(
                        {
                            "account": cfg.id,
                            "reason": "冷却中",
                            "detail": f"剩余 {cd_rem:.0f}s / 连续风控 {st.get('risk_streak', 0)} 次",
                        }
                    )
                    continue
                day_used = int(st.get("day_used") or 0)
                if day_used >= cfg.daily_budget:
                    rejections.append(
                        {
                            "account": cfg.id,
                            "reason": "日预算用完",
                            "detail": f"{day_used}/{cfg.daily_budget}",
                        }
                    )
                    continue
                hour_used = int(st.get("hour_used") or 0)
                if hour_used >= cfg.hourly_budget:
                    rejections.append(
                        {
                            "account": cfg.id,
                            "reason": "小时预算用完",
                            "detail": f"{hour_used}/{cfg.hourly_budget}",
                        }
                    )
                    continue
                gap_need = cfg.min_interval_s - (now - float(st.get("last_acquire_ts") or 0.0))
                if st.get("last_acquire_ts") and gap_need > 0:
                    rejections.append(
                        {
                            "account": cfg.id,
                            "reason": "最小间隔未到",
                            "detail": f"还要等 {gap_need:.0f}s",
                        }
                    )
                    continue

                sort_key = (
                    day_used,
                    hour_used,
                    float(st.get("last_acquire_ts") or 0.0),
                    self._order.get(cfg.id, 0),
                )
                candidates.append((sort_key, cfg, st))

            if not candidates:
                if dirty:
                    self._save_state(state)
                return None, rejections

            # 挑最闲/最久没用的那个：池子越小越要摊开，别把单个账号连着打。
            candidates.sort(key=lambda item: item[0])
            _, cfg, st = candidates[0]

            slot_id = uuid.uuid4().hex[:12]
            st["day_used"] = int(st.get("day_used") or 0) + 1
            st["hour_used"] = int(st.get("hour_used") or 0) + 1
            st["last_acquire_ts"] = now
            st["last_ts"] = now
            st["last_kind"] = kind
            st["last_caller"] = caller
            st["last_ok"] = None
            st["last_note"] = ""
            st["total_acquired"] = int(st.get("total_acquired") or 0) + 1
            reservations = st.setdefault("reservations", {})
            reservations[slot_id] = {"ts": now, "kind": kind, "caller": caller}
            self._save_state(state)

            slot = {
                "account": cfg.id,
                "profile_dir": self.profile_for(cfg.id),
                "token_path": cfg.token_path,
                "egress": cfg.egress,
                "reserved_at": now,
                "slot_id": slot_id,
                "kind": kind,
                "caller": caller,
                "day_used": st["day_used"],
                "daily_budget": cfg.daily_budget,
                "hour_used": st["hour_used"],
                "hourly_budget": cfg.hourly_budget,
            }
            if cfg.token_env:
                slot["token_env"] = cfg.token_env
            self._audit_record(
                "acquire",
                cfg,
                kind=kind,
                caller=caller,
                ok=None,
                day_used=st["day_used"],
                hour_used=st["hour_used"],
                extra={"slot_id": slot_id},
            )
            return slot, rejections

    # -- 回报 --------------------------------------------------------------
    def report(
        self,
        slot: dict,
        ok: bool,
        risk: str = "",
        note: str = "",
        duration_s: float | None = None,
    ) -> dict:
        """记账。`risk` 非空 -> 进冷却，时长按连续风控次数 1h/6h/24h/72h 升级。

        成功一次不清连续计数（规格要求）；冷却期内命中继续累加。
        同一 slot 重复 report 是幂等的（预留已出队则不再动预留）。
        """
        if not isinstance(slot, dict) or not slot.get("account"):
            raise ValueError("slot 必须是 acquire() 返回的 dict（至少含 account 字段）")
        account_id = str(slot["account"])
        cfg = self._account(account_id)

        slot_egress = slot.get("egress")
        if slot_egress and str(slot_egress) != cfg.egress:
            raise EgressMismatch(
                f"report 的 slot.egress={slot_egress!r} 与账号 {cfg.id} 登记的 "
                f"{cfg.egress!r} 不符；同一账号不允许换出口。"
            )

        risk = (risk or "").strip()
        note = (note or "").strip()
        if duration_s is not None:
            duration_s = _require_number(duration_s, "duration_s", minimum=0.0)
        kind = str(slot.get("kind") or "")

        with self._lock:
            state = self._load_state()
            now = _now()
            st = state["accounts"].get(account_id)
            if not isinstance(st, dict):
                st = self._empty_account_state(cfg)
                state["accounts"][account_id] = st
            self._normalize_account(st, now)

            slot_id = slot.get("slot_id")
            reservations = st.setdefault("reservations", {})
            if slot_id and slot_id in reservations:
                del reservations[slot_id]

            st["last_ts"] = now
            st["last_ok"] = bool(ok)
            st["last_kind"] = kind or st.get("last_kind") or ""
            st["last_caller"] = str(slot.get("caller") or st.get("last_caller") or "")
            if duration_s is not None:
                st["last_duration_s"] = round(float(duration_s), 3)

            applied_cooldown = 0.0
            if risk:
                st["risk_streak"] = int(st.get("risk_streak") or 0) + 1
                st["risk_hits_total"] = int(st.get("risk_hits_total") or 0) + 1
                st["risk_hits_day"] = int(st.get("risk_hits_day") or 0) + 1
                ladder = self.defaults["cooldown_ladder_s"]
                idx = min(int(st["risk_streak"]), len(ladder)) - 1
                applied_cooldown = float(ladder[idx])
                st["cooldown_until"] = now + applied_cooldown
                st["last_cooldown_s"] = applied_cooldown
                st["last_note"] = note or risk
            else:
                if ok:
                    st["total_ok"] = int(st.get("total_ok") or 0) + 1
                else:
                    st["total_fail"] = int(st.get("total_fail") or 0) + 1
                st["last_note"] = note

            self._save_state(state)
            result = self._account_status(cfg, st, now)
            result["ok"] = bool(ok)
            result["risk"] = risk
            result["applied_cooldown_s"] = applied_cooldown
            self._audit_record(
                "report",
                cfg,
                kind=kind,
                caller=str(slot.get("caller") or ""),
                ok=bool(ok),
                risk=risk,
                note=note,
                duration_s=duration_s,
                day_used=int(st.get("day_used") or 0),
                hour_used=int(st.get("hour_used") or 0),
                extra={
                    "slot_id": slot_id or "",
                    "applied_cooldown_s": applied_cooldown,
                    "risk_streak": int(st.get("risk_streak") or 0),
                },
            )
            return result

    # -- 汇总 --------------------------------------------------------------
    def status(self) -> dict:
        """每账号：今日已用/预算、小时已用/预算、最近一次、冷却剩余、连续风控次数。"""
        with self._lock:
            state = self._load_state()
            now = _now()
            accounts = state["accounts"]
            dirty = False
            rows: list[dict] = []
            for cfg in self._accounts:
                st = accounts.get(cfg.id)
                if not isinstance(st, dict):
                    st = self._empty_account_state(cfg)
                    accounts[cfg.id] = st
                    dirty = True
                if self._normalize_account(st, now):
                    dirty = True
                if not st.get("egress"):
                    st["egress"] = cfg.egress
                    dirty = True
                rows.append(self._account_status(cfg, st, now))
            if dirty:
                self._save_state(state)

        return {
            "version": __version__,
            "now": now,
            "now_iso": _iso(now),
            "day_key": _day_key(now),
            "hour_key": _hour_key(now),
            "config_path": self.config_path,
            "state_path": self.state_path,
            "audit_path": self.audit_path,
            "defaults": {
                "daily_budget": self.defaults["daily_budget"],
                "hourly_budget": self.defaults["hourly_budget"],
                "min_interval_s": self.defaults["min_interval_s"],
                "max_wait_s": self.defaults["max_wait_s"],
                "poll_interval_s": self.defaults["poll_interval_s"],
                "cooldown_ladder_s": list(self.defaults["cooldown_ladder_s"]),
            },
            "accounts": rows,
        }

    # -- 手动解冷却 --------------------------------------------------------
    def reset(self, account_id: str) -> None:
        """解冷却 + 清零连续风控计数，并把该账号重新登记到当前配置的 egress。"""
        cfg = self._account(account_id)
        with self._lock:
            state = self._load_state()
            now = _now()
            st = state["accounts"].get(account_id)
            if not isinstance(st, dict):
                st = self._empty_account_state(cfg)
                state["accounts"][account_id] = st
            self._normalize_account(st, now)
            st["risk_streak"] = 0
            st["cooldown_until"] = 0.0
            st["last_cooldown_s"] = 0.0
            st["egress"] = cfg.egress
            st["last_note"] = st.get("last_note") or ""
            self._save_state(state)
            self._audit_record(
                "reset",
                cfg,
                caller="reset",
                note="手动解冷却并清零连续风控计数",
                day_used=int(st.get("day_used") or 0),
                hour_used=int(st.get("hour_used") or 0),
            )


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------
_RESERVED_TOP = {"defaults", "accounts", "version", "$schema", "profiles_dir", "profiles_root"}

_DEFAULT_SCALARS = (
    "daily_budget",
    "hourly_budget",
    "min_interval_s",
    "max_wait_s",
    "poll_interval_s",
    "cooldown_ladder_s",
    "reservation_ttl_s",
    "locale",
    "accept_lang",
    "timezone",
    "window_size",
    "window_position",
    "device_scale_factor",
    "user_agent",
    "extra_flags",
    "profiles_dir",
    "headless_flag",
)


def _resolve_config_path(path: str | None) -> str:
    if path:
        return os.path.abspath(os.path.expanduser(path))
    env = os.environ.get("DSV_ACCOUNTS", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    # 本项目的账号表就叫 web_accounts.json(与 config.json 的 web_lane.accounts_file 一致);
    # accounts.json 是通用名, 保留兼容。都不存在时返回前者, 让报错指向项目实际的路径。
    for name in ("web_accounts.json", "accounts.json"):
        cand = os.path.join(MODULE_DIR, name)
        if os.path.isfile(cand):
            return cand
    return os.path.join(MODULE_DIR, "web_accounts.json")


def _resolve_under(base_dir: str, value: str) -> str:
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(base_dir, expanded))


def _load_config(config_path: str) -> tuple[dict, list[_AccountConfig]]:
    if not os.path.isfile(config_path):
        raise GuardConfigError(
            f"账号配置不存在: {config_path}"
            "（用 -c/--config 或 $DSV_ACCOUNTS 指定路径；项目默认文件名是 web_accounts.json）"
        )
    try:
        with open(config_path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise GuardConfigError(
            f"账号配置不是合法 JSON: {config_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise GuardConfigError(f"账号配置读取失败: {config_path}: {exc}") from exc

    base_dir = os.path.dirname(os.path.abspath(config_path))
    raw_defaults: dict[str, Any] = {}
    entries: list[dict] = []

    if isinstance(raw, dict):
        if isinstance(raw.get("defaults"), dict):
            raw_defaults.update(raw["defaults"])
        elif raw.get("defaults") is not None:
            raise GuardConfigError("defaults 必须是对象")
        accounts_field = raw.get("accounts")
        if isinstance(accounts_field, list):
            entries = list(accounts_field)
        elif isinstance(accounts_field, dict):
            entries = [
                {**v, "id": k} if isinstance(v, dict) else v
                for k, v in accounts_field.items()
            ]
        elif accounts_field is None:
            entries = [
                {**(v or {}), "id": k}
                for k, v in raw.items()
                if k not in _RESERVED_TOP and k not in _DEFAULT_SCALARS and isinstance(v, dict)
            ]
        else:
            raise GuardConfigError("accounts 必须是数组或对象")
        for key in _DEFAULT_SCALARS:
            if key in raw:
                raw_defaults.setdefault(key, raw[key])
    elif isinstance(raw, list):
        entries = list(raw)
    else:
        raise GuardConfigError(f"账号配置顶层必须是对象或数组: {config_path}")

    if not entries:
        raise GuardConfigError(f"账号配置里没有账号: {config_path}")

    profiles_dir = _resolve_under(
        base_dir, _require_str(raw_defaults.get("profiles_dir", "profiles"), "profiles_dir")
    )

    ladder_raw = raw_defaults.get("cooldown_ladder_s", list(DEFAULT_COOLDOWN_LADDER_S))
    if not isinstance(ladder_raw, (list, tuple)) or not ladder_raw:
        raise GuardConfigError("cooldown_ladder_s 必须是非空数组")
    ladder = tuple(
        _require_number(v, f"cooldown_ladder_s[{i}]", minimum=1.0)
        for i, v in enumerate(ladder_raw)
    )

    extra_flags_raw = raw_defaults.get("extra_flags", [])
    if extra_flags_raw is None:
        extra_flags_raw = []
    if not isinstance(extra_flags_raw, (list, tuple)):
        raise GuardConfigError("extra_flags 必须是字符串数组")
    extra_flags = [_require_str(v, "extra_flags[]") for v in extra_flags_raw]

    user_agent_raw = raw_defaults.get("user_agent")
    if user_agent_raw is not None and not isinstance(user_agent_raw, str):
        raise GuardConfigError("user_agent 必须是字符串或 null")

    defaults = {
        "daily_budget": _require_int(
            raw_defaults.get("daily_budget", DEFAULT_DAILY_BUDGET),
            "daily_budget",
            minimum=0,
        ),
        "hourly_budget": _require_int(
            raw_defaults.get("hourly_budget", DEFAULT_HOURLY_BUDGET),
            "hourly_budget",
            minimum=0,
        ),
        "min_interval_s": _require_number(
            raw_defaults.get("min_interval_s", DEFAULT_MIN_INTERVAL_S),
            "min_interval_s",
            minimum=0.0,
        ),
        "max_wait_s": _require_number(
            raw_defaults.get("max_wait_s", DEFAULT_MAX_WAIT_S),
            "max_wait_s",
            minimum=0.0,
        ),
        "poll_interval_s": _require_number(
            raw_defaults.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S),
            "poll_interval_s",
            minimum=0.01,
        ),
        "cooldown_ladder_s": ladder,
        "reservation_ttl_s": _require_number(
            raw_defaults.get("reservation_ttl_s", DEFAULT_RESERVATION_TTL_S),
            "reservation_ttl_s",
            minimum=0.0,
        ),
        "locale": _require_str(
            raw_defaults.get("locale", DEFAULT_LOCALE), "locale"
        ),
        "accept_lang": _require_str(
            raw_defaults.get("accept_lang", DEFAULT_ACCEPT_LANG), "accept_lang"
        ),
        "timezone": _require_str(
            raw_defaults.get("timezone", DEFAULT_TIMEZONE), "timezone"
        ),
        "window_size": _require_str(
            raw_defaults.get("window_size", DEFAULT_WINDOW_SIZE), "window_size"
        ),
        "window_position": _require_str(
            raw_defaults.get("window_position", DEFAULT_WINDOW_POSITION), "window_position"
        ),
        "device_scale_factor": _require_number(
            raw_defaults.get("device_scale_factor", 1.0),
            "device_scale_factor",
            minimum=0.1,
        ),
        "user_agent": (user_agent_raw.strip() or None) if isinstance(user_agent_raw, str) else None,
        "extra_flags": extra_flags,
        "profiles_dir": profiles_dir,
        "headless_flag": _require_str(
            raw_defaults.get("headless_flag", DEFAULT_HEADLESS_FLAG), "headless_flag"
        ),
    }

    accounts: list[_AccountConfig] = []
    seen: dict[str, int] = {}
    errors: list[str] = []
    for index, entry in enumerate(entries):
        try:
            accounts.append(
                _parse_account(entry, index, base_dir, defaults, seen)
            )
        except GuardConfigError as exc:
            errors.append(f"[accounts[{index}] {getattr(entry, 'get', lambda *_: '')('id') or '?'}] {exc}")
    if errors:
        raise GuardConfigError("账号配置有误：" + "；".join(errors))
    return defaults, accounts


def _parse_account(
    entry: Any,
    index: int,
    base_dir: str,
    defaults: dict,
    seen: dict[str, int],
) -> _AccountConfig:
    if not isinstance(entry, dict):
        raise GuardConfigError(f"账号条目必须是对象，得到 {type(entry).__name__}")

    acc_id = entry.get("id") or entry.get("account")
    if not isinstance(acc_id, str) or not acc_id.strip():
        raise GuardConfigError("缺少 id 字段")
    acc_id = acc_id.strip()
    if not _ACCOUNT_ID_RE.match(acc_id):
        raise GuardConfigError(
            "id 只允许字母数字与 _ . @ -（首字符必须是字母数字，最长 64）："
            f"得到 {acc_id!r}"
        )
    if acc_id in seen:
        raise GuardConfigError(
            f"id 重复（与 accounts[{seen[acc_id]}] 撞了）：{acc_id!r}"
        )
    seen[acc_id] = index

    egress_raw = entry.get("egress")
    if not isinstance(egress_raw, str) or not egress_raw.strip():
        raise GuardConfigError(
            "缺少 egress 字段（必须登记出口，例如 home-cn / vps-jp-1 / ci-us）"
        )
    egress = egress_raw.strip()
    if not _EGRESS_RE.match(egress):
        raise GuardConfigError(
            f"egress 只允许字母数字与 _ . : -（最长 64）：得到 {egress!r}"
        )

    enabled = _require_bool(entry.get("enabled", True), f"{acc_id}.enabled")

    token_env = entry.get("token_env")
    token_file = entry.get("token_file")
    if token_env is not None and not isinstance(token_env, str):
        raise GuardConfigError(f"{acc_id}.token_env 必须是字符串")
    if token_file is not None and not isinstance(token_file, str):
        raise GuardConfigError(f"{acc_id}.token_file 必须是字符串")
    token_env = token_env.strip() if isinstance(token_env, str) else None
    token_file_resolved = (
        _resolve_under(base_dir, token_file.strip()) if isinstance(token_file, str) and token_file.strip() else None
    )
    if not token_env and not token_file_resolved:
        raise GuardConfigError(
            f"{acc_id} 必须提供 token_env（环境变量名）或 token_file（文件路径）之一，"
            "否则 token 无从注入"
        )

    profile_raw = entry.get("profile_dir")
    if profile_raw is not None and not isinstance(profile_raw, str):
        raise GuardConfigError(f"{acc_id}.profile_dir 必须是字符串")
    if isinstance(profile_raw, str) and profile_raw.strip():
        profile_dir = _resolve_under(base_dir, profile_raw.strip())
    else:
        profile_dir = os.path.normpath(os.path.join(defaults["profiles_dir"], acc_id))

    daily_budget = _require_int(
        entry.get("daily_budget", defaults["daily_budget"]),
        f"{acc_id}.daily_budget",
        minimum=0,
    )
    hourly_budget = _require_int(
        entry.get("hourly_budget", defaults["hourly_budget"]),
        f"{acc_id}.hourly_budget",
        minimum=0,
    )
    min_interval_s = _require_number(
        entry.get("min_interval_s", defaults["min_interval_s"]),
        f"{acc_id}.min_interval_s",
        minimum=0.0,
    )

    window_size = entry.get("window_size")
    if window_size is not None:
        window_size = _require_str(window_size, f"{acc_id}.window_size")
    window_position = entry.get("window_position")
    if window_position is not None:
        window_position = _require_str(window_position, f"{acc_id}.window_position")
    user_agent = entry.get("user_agent")
    if user_agent is not None:
        user_agent = _require_str(user_agent, f"{acc_id}.user_agent")
    notes = entry.get("notes") or ""
    if not isinstance(notes, str):
        raise GuardConfigError(f"{acc_id}.notes 必须是字符串")

    return _AccountConfig(
        id=acc_id,
        egress=egress,
        enabled=enabled,
        token_env=token_env or None,
        token_file=token_file_resolved,
        profile_dir=profile_dir,
        daily_budget=daily_budget,
        hourly_budget=hourly_budget,
        min_interval_s=min_interval_s,
        user_agent=user_agent,
        window_size=window_size,
        window_position=window_position,
        notes=notes.strip(),
    )


# ---------------------------------------------------------------------------
# 自测（离线、临时目录、不碰真实状态）
# ---------------------------------------------------------------------------
class _Suite:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []
        self.tmp_roots: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        ok = bool(ok)
        self.results.append((label, ok, detail))
        line = f"{'PASS' if ok else 'FAIL'}  {label}"
        if detail:
            line += f"   [{detail}]"
        print(line, flush=True)

    def fail(self, label: str, detail: str) -> None:
        self.check(label, False, detail)

    def run(self, label: str, fn) -> None:
        """一条断言崩了只记 FAIL，不带走后面的用例。"""
        try:
            fn()
        except Exception:
            tb = traceback.format_exc(limit=4).strip().splitlines()
            self.fail(label, f"用例抛异常: {tb[-1] if tb else 'unknown'}")

    def mkdir(self) -> str:
        path = tempfile.mkdtemp(prefix="dsvguard-selftest-")
        self.tmp_roots.append(path)
        return path

    def cleanup(self) -> None:
        for root in self.tmp_roots:
            shutil.rmtree(root, ignore_errors=True)
        self.tmp_roots.clear()

    @property
    def failures(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.results if not r[1]]


@contextlib.contextmanager
def _env(**kwargs: str | None):
    saved = {k: os.environ.get(k) for k in kwargs}
    try:
        for key, value in kwargs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_cfg(
    root: str,
    accounts: list[dict],
    defaults: dict | None = None,
) -> str:
    path = os.path.join(root, "accounts.json")
    payload: dict[str, Any] = {"accounts": accounts}
    if defaults is not None:
        payload["defaults"] = defaults
    _atomic_write_json(path, payload)
    return path


def _isolated_env(root: str) -> dict:
    """给自测开的环境：状态与审计都落在临时目录里。"""
    return {
        "DSV_ACCOUNTS": os.path.join(root, "accounts.json"),
        "DSV_GUARD_STATE": os.path.join(root, "state.json"),
        "DSV_AUDIT_LOG": os.path.join(root, "audit.jsonl"),
    }


def _selftest_defaults(**overrides: Any) -> dict:
    base = {
        "daily_budget": 6,
        "hourly_budget": 2,
        "min_interval_s": 0.0,
        "max_wait_s": 180.0,
        "poll_interval_s": 0.2,
    }
    base.update(overrides)
    return base


def _acct(acc_id: str = "solo", egress: str = "home-cn", **overrides: Any) -> dict:
    entry = {"id": acc_id, "egress": egress, "enabled": True, "token_env": f"DSV_TOKEN_{acc_id.upper()}"}
    entry.update(overrides)
    return entry


def run_selftest() -> int:
    _enable_utf8_streams()  # 直接调用本函数（不经 CLI）时同样不因编码崩掉
    suite = _Suite()
    try:
        _st_defaults(suite)
        _st_hourly_gate(suite)
        _st_min_interval(suite)
        _st_concurrency(suite)
        _st_risk_cooldown(suite)
        _st_cooldown_expiry(suite)
        _st_atomic_write(suite)
        _st_day_rollover(suite)
        _st_audit(suite)
        _st_masking(suite)
        _st_interfaces(suite)
    finally:
        suite.cleanup()

    passed = sum(1 for r in suite.results if r[1])
    failed = len(suite.results) - passed
    print("", flush=True)
    print(f"=== selftest: {passed} PASS / {failed} FAIL -> exit {0 if failed == 0 else 1} ===", flush=True)
    if failed:
        print("失败项：", flush=True)
        for label, _, detail in suite.failures:
            print(f"  - {label}   [{detail}]", flush=True)
    return 0 if failed == 0 else 1


def _st_defaults(suite: _Suite) -> None:
    print("--- ⓪ 默认值（防止有人把闸门放松成恒真）", flush=True)
    suite.check(
        "⓪-1 默认预算/间隔等于规格值 6 / 2 / 600",
        (DEFAULT_DAILY_BUDGET, DEFAULT_HOURLY_BUDGET, DEFAULT_MIN_INTERVAL_S) == (6, 2, 600.0),
        f"daily={DEFAULT_DAILY_BUDGET} hourly={DEFAULT_HOURLY_BUDGET} interval={DEFAULT_MIN_INTERVAL_S}",
    )
    suite.check(
        "⓪-2 默认等待上限 180s",
        DEFAULT_MAX_WAIT_S == 180.0,
        f"max_wait={DEFAULT_MAX_WAIT_S}",
    )
    suite.check(
        "⓪-3 冷却阶梯默认 1h/6h/24h/72h",
        DEFAULT_COOLDOWN_LADDER_S == (3600.0, 21600.0, 86400.0, 259200.0),
        f"{DEFAULT_COOLDOWN_LADDER_S}",
    )
    suite.check(
        "⓪-4 默认 kind 集合为 text/vision",
        KINDS == ("text", "vision"),
        f"{KINDS}",
    )


def _st_hourly_gate(suite: _Suite) -> None:
    print("--- ① 小时预算：同账号 1 小时内第 3 次 acquire 被拒（等待模式超时抛 NoSlot）", flush=True)
    root = suite.mkdir()
    cfg = _write_cfg(
        root,
        [_acct("solo", daily_budget=6, hourly_budget=2, min_interval_s=0)],
        _selftest_defaults(daily_budget=6, hourly_budget=2, min_interval_s=0.0, max_wait_s=1.5, poll_interval_s=0.2),
    )
    with _env(**_isolated_env(root)):
        g = Guard(cfg)

        def body() -> None:
            first = g.acquire("text", caller="t1", wait=False)
            second = g.acquire("vision", caller="t2", wait=False)   # 文本/视觉共用同一预算池
            suite.check(
                "①-1 前两次 acquire 成功且共用同一预算池",
                first["account"] == "solo" and second["account"] == "solo",
                f"day_used={second['day_used']} hour_used={second['hour_used']}",
            )
            started = time.time()
            raised: Exception | None = None
            try:
                g.acquire("text", caller="t3", wait=True)
            except NoSlot as exc:
                raised = exc
            elapsed = time.time() - started
            suite.check(
                "①-2 第 3 次在等待模式超时后抛 NoSlot",
                isinstance(raised, NoSlot),
                f"elapsed={elapsed:.2f}s type={type(raised).__name__}",
            )
            suite.check(
                "①-3 等待确实发生了（>=1.2s，而不是立刻返回）",
                isinstance(raised, NoSlot) and elapsed >= 1.2,
                f"elapsed={elapsed:.2f}s max_wait=1.5s",
            )
            reason = getattr(raised, "reason", "") if raised else ""
            suite.check(
                "①-4 NoSlot 原因点名小时预算",
                "小时预算" in reason,
                reason[:160],
            )
            suite.check(
                "①-5 拒绝后没有多扣预算（day_used 仍为 2）",
                g.status()["accounts"][0]["day_used"] == 2,
                f"day_used={g.status()['accounts'][0]['day_used']}",
            )

        suite.run("① 小时预算", body)


def _st_min_interval(suite: _Suite) -> None:
    print("--- ①b 最小间隔：同账号连发第二次被拒，剩余时间点名", flush=True)
    root = suite.mkdir()
    cfg = _write_cfg(
        root,
        [_acct("solo", daily_budget=6, hourly_budget=2, min_interval_s=600)],
        _selftest_defaults(min_interval_s=600.0, poll_interval_s=0.2),
    )
    with _env(**_isolated_env(root)):
        g = Guard(cfg)

        def body() -> None:
            g.acquire("text", caller="a", wait=False)
            raised: Exception | None = None
            try:
                g.acquire("text", caller="b", wait=False)
            except NoSlot as exc:
                raised = exc
            reason = getattr(raised, "reason", "") if raised else ""
            suite.check(
                "①b-1 第二次 acquire 抛 NoSlot 且原因点名最小间隔",
                isinstance(raised, NoSlot) and "最小间隔" in reason,
                reason[:160],
            )
            row = g.status()["accounts"][0]
            suite.check(
                "①b-2 最小间隔剩余量在 (0, 600] 内向 600 靠拢",
                0 < row["min_interval_remaining"] <= 600,
                f"remaining={row['min_interval_remaining']}",
            )

        suite.run("①b 最小间隔", body)


def _st_concurrency(suite: _Suite) -> None:
    print("--- ② 并发：两个子进程抢同账号的最后 1 个额度，不能都拿到", flush=True)
    root = suite.mkdir()
    cfg = _write_cfg(
        root,
        [_acct("solo", daily_budget=1, hourly_budget=5, min_interval_s=0)],
        _selftest_defaults(daily_budget=1, hourly_budget=5, min_interval_s=0.0, max_wait_s=0.0),
    )
    state_path = os.path.join(root, "state.json")
    audit_path = os.path.join(root, "audit.jsonl")

    def body() -> None:
        script = os.path.abspath(__file__)
        outs = [os.path.join(root, f"child-{i}.json") for i in (0, 1)]
        start_at = time.time() + 3.0
        child_env = dict(os.environ)
        child_env.update(
            {
                "DSV_GUARD_STATE": state_path,
                "DSV_AUDIT_LOG": audit_path,
                "PYTHONIOENCODING": "utf-8",
            }
        )
        # 子进程的 stdout/stderr 落到文件而不是管道：某些受限环境（含本机深沙箱）禁止
        # 创建匿名管道，subprocess.PIPE 会直接 PermissionError；文件重定向到处都能跑。
        procs: list[subprocess.Popen] = []
        handles: list[Any] = []
        log_paths: list[str] = []
        try:
            for i, out in enumerate(outs):
                log_path = os.path.join(root, f"child-{i}.log")
                log_paths.append(log_path)
                handle = open(log_path, "wb")
                handles.append(handle)
                procs.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            script,
                            "_child-acquire",
                            "--config",
                            cfg,
                            "--start-at",
                            f"{start_at:.6f}",
                            "--out",
                            out,
                        ],
                        env=child_env,
                        stdin=subprocess.DEVNULL,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                    )
                )
            codes: list[int] = []
            for proc in procs:
                codes.append(proc.wait(timeout=120))
            errs: list[str] = []
            for code, log_path in zip(codes, log_paths):
                if code != 0:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                        errs.append(fh.read()[-400:])
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
            for handle in handles:
                handle.close()

        suite.check(
            "②-1 两个子进程都正常退出（无锁超时/异常）",
            codes == [0, 0],
            f"codes={codes} errs={errs}",
        )

        results: list[dict] = []
        for out in outs:
            if os.path.exists(out):
                with open(out, "r", encoding="utf-8") as fh:
                    results.append(json.load(fh))
        suite.check(
            "②-2 两个子进程都留下了结果",
            len(results) == 2,
            f"results={json.dumps(results, ensure_ascii=False)[:300]}",
        )
        if len(results) != 2:
            return

        wins = [r for r in results if r.get("ok")]
        suite.check(
            "②-3 只有一个子进程拿到最后 1 个额度",
            len(wins) == 1,
            f"ok_count={len(wins)} " + ", ".join(
                f"pid={r.get('pid')} ok={r.get('ok')} reason={(r.get('reason') or r.get('error') or '')[:60]}"
                for r in results
            ),
        )
        suite.check(
            "②-4 失败方是被 NoSlot 拒绝而非异常",
            all(r.get("ok") or r.get("reason") for r in results),
            f"{[r.get('reason') or r.get('error', '')[:80] for r in results]}",
        )
        spread = abs(results[0].get("begin", 0) - results[1].get("begin", 0))
        suite.check(
            "②-5 两个进程确实同时在抢（begin 相差 <2s）",
            spread < 2.0,
            f"spread={spread:.3f}s",
        )

        with _env(**_isolated_env(root)):
            g = Guard(cfg)
            st = json.loads(_read_state_text(state_path))
            day_used = int(st["accounts"]["solo"]["day_used"])
            suite.check(
                "②-6 状态文件只记了 1 次扣减",
                day_used == 1,
                f"day_used={day_used}",
            )
            suite.check(
                "②-7 落盘的预留数与拿到的额度一致",
                len(st["accounts"]["solo"].get("reservations") or {}) == 1,
                f"reservations={len(st['accounts']['solo'].get('reservations') or {})}",
            )

    suite.run("② 并发", body)


def _read_state_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _st_risk_cooldown(suite: _Suite) -> None:
    print("--- ③ 风控冷却：立刻拒、剩余 >0、连续命中时长递增", flush=True)
    root = suite.mkdir()
    cfg = _write_cfg(
        root,
        [_acct("solo", daily_budget=6, hourly_budget=2, min_interval_s=0)],
        _selftest_defaults(min_interval_s=0.0, poll_interval_s=0.2),
    )
    with _env(**_isolated_env(root)):
        g = Guard(cfg)

        def body() -> None:
            slot = g.acquire("text", caller="risk-run", wait=False)
            r1 = g.report(slot, ok=False, risk="频率限制", note="被限流")
            rem1 = max(0.0, r1["cooldown_until"] - time.time())
            suite.check(
                "③-1 report(risk) 后账号立刻进入冷却，剩余 >0",
                r1["status"] == "cooldown" and rem1 > 0,
                f"status={r1['status']} remaining={rem1:.0f}s",
            )
            raised: Exception | None = None
            try:
                g.acquire("text", caller="after-risk", wait=False)
            except NoSlot as exc:
                raised = exc
            suite.check(
                "③-2 冷却中的账号 acquire 立刻被拒（NoSlot）",
                isinstance(raised, NoSlot) and "冷却" in getattr(raised, "reason", ""),
                getattr(raised, "reason", "未抛 NoSlot")[:160],
            )

            # 成功一次不降级：连续计数与冷却都不动。
            r_ok = g.report(slot, ok=True, note="同账号补一次成功回报")
            suite.check(
                "③-3 成功调用不降级、不清冷却",
                r_ok["risk_streak"] == 1 and r_ok["cooldown_remaining"] > 0,
                f"streak={r_ok['risk_streak']} remaining={r_ok['cooldown_remaining']:.0f}s",
            )

            r2 = g.report(slot, ok=False, risk="频率限制")
            r3 = g.report(slot, ok=False, risk="频率限制")
            d1 = r1["applied_cooldown_s"]
            d2 = r2["applied_cooldown_s"]
            d3 = r3["applied_cooldown_s"]
            suite.check(
                "③-4 连续 3 次风控冷却时长严格递增",
                d1 < d2 < d3,
                f"{d1:.0f}s < {d2:.0f}s < {d3:.0f}s",
            )
            suite.check(
                "③-5 冷却阶梯命中 1h -> 6h -> 24h",
                (d1 == 3600.0 and d2 == 21600.0 and d3 == 86400.0),
                f"[{d1:.0f}, {d2:.0f}, {d3:.0f}]",
            )
            r4 = g.report(slot, ok=False, risk="频率限制")
            r5 = g.report(slot, ok=False, risk="频率限制")
            suite.check(
                "③-6 第 4/5 次风控封顶 72h",
                r4["applied_cooldown_s"] == 259200.0 and r5["applied_cooldown_s"] == 259200.0,
                f"[{r4['applied_cooldown_s']:.0f}, {r5['applied_cooldown_s']:.0f}] streak={r5['risk_streak']}",
            )
            suite.check(
                "③-7 hits 累计风控总次数（成功回报不增）",
                r5["hits"] == 5 and r_ok["hits"] == 1,
                f"hits={r5['hits']} after_ok_hits={r_ok['hits']}",
            )

            g.reset("solo")
            after = g.status()["accounts"][0]
            suite.check(
                "③-8 reset 解冷却并清零连续风控计数",
                after["cooldown_remaining"] == 0
                and after["risk_streak"] == 0
                and after["hits"] == 5,
                f"streak={after['risk_streak']} remaining={after['cooldown_remaining']} hits={after['hits']}",
            )
            slot2 = g.acquire("text", caller="after-reset", wait=False)
            suite.check(
                "③-9 reset 之后可以重新取额度",
                slot2["account"] == "solo",
                f"account={slot2['account']}",
            )

        suite.run("③ 风控冷却", body)


def _st_cooldown_expiry(suite: _Suite) -> None:
    print("--- ③b 冷却自然到期：连续计数清零、阶梯从 1h 重新开始", flush=True)
    root = suite.mkdir()
    cfg = _write_cfg(
        root,
        [_acct("solo", daily_budget=6, hourly_budget=5, min_interval_s=0)],
        _selftest_defaults(min_interval_s=0.0),
    )
    state_path = os.path.join(root, "state.json")
    with _env(**_isolated_env(root)):
        g = Guard(cfg)

        def body() -> None:
            slot = g.acquire("text", caller="expiry", wait=False)
            g.report(slot, ok=False, risk="频率限制")
            second = g.report(slot, ok=False, risk="频率限制")
            suite.check(
                "③b-1 连续两次风控后阶梯已到 6h、连续计数为 2",
                second["applied_cooldown_s"] == 21600.0 and second["risk_streak"] == 2,
                f"applied={second['applied_cooldown_s']:.0f}s streak={second['risk_streak']}",
            )

            # 把冷却终点拨到过去，模拟“冷却自然到期”（不经过 reset）。
            state = json.loads(_read_state_text(state_path))
            state["accounts"]["solo"]["cooldown_until"] = time.time() - 5.0
            _atomic_write_json(state_path, state)

            slot2 = g.acquire("text", caller="expiry-after", wait=False)
            suite.check(
                "③b-2 冷却自然到期后可以重新取额度",
                slot2["account"] == "solo",
                f"account={slot2['account']}",
            )
            row = g.status()["accounts"][0]
            suite.check(
                "③b-3 冷却自然到期后连续风控计数归零",
                row["risk_streak"] == 0 and row["cooldown_remaining"] == 0,
                f"streak={row['risk_streak']} remaining={row['cooldown_remaining']}",
            )
            again = g.report(slot2, ok=False, risk="频率限制")
            suite.check(
                "③b-4 到期后再次风控，阶梯从 1h 重新开始（不是继续 24h/72h）",
                again["applied_cooldown_s"] == 3600.0 and again["risk_streak"] == 1,
                f"applied={again['applied_cooldown_s']:.0f}s streak={again['risk_streak']}",
            )

        suite.run("③b 冷却自然到期", body)


def _st_atomic_write(suite: _Suite) -> None:
    print("--- ④ 原子写：os.replace 失败/写一半都不会留下半截 JSON", flush=True)
    root = suite.mkdir()
    cfg = _write_cfg(
        root,
        [_acct("solo", daily_budget=6, hourly_budget=5, min_interval_s=0)],
        _selftest_defaults(min_interval_s=0.0),
    )
    with _env(**_isolated_env(root)):
        g = Guard(cfg)

        def body() -> None:
            slot = g.acquire("text", caller="atomic-1", wait=False)
            g.report(slot, ok=True, note="baseline")
            with open(g.state_path, "rb") as fh:
                before_bytes = fh.read()
            before_obj = json.loads(before_bytes.decode("utf-8"))

            original_replace = globals()["_REPLACE"]

            def boom(src: str, dst: str) -> None:
                # 模拟「临时文件写到一半就崩，替换没发生」
                with open(src, "r+b") as fh:
                    fh.truncate(7)
                    fh.flush()
                    os.fsync(fh.fileno())
                raise OSError("模拟磁盘/进程故障：os.replace 失败")

            raised: Exception | None = None
            globals()["_REPLACE"] = boom
            try:
                slot2 = g.acquire("vision", caller="atomic-2", wait=False)
                g.report(slot2, ok=True, note="这次写入必须失败")
            except OSError as exc:
                raised = exc
            finally:
                globals()["_REPLACE"] = original_replace

            suite.check(
                "④-1 替换失败被如实抛出（不是静默成功）",
                isinstance(raised, OSError),
                f"{type(raised).__name__}: {raised}"[:160],
            )

            loadable = True
            detail = ""
            try:
                with open(g.state_path, "r", encoding="utf-8") as fh:
                    after_obj = json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                loadable = False
                after_obj = None
                detail = f"{type(exc).__name__}: {exc}"
            suite.check(
                "④-2 写坏一半之后状态文件仍能 json.load",
                loadable,
                detail or f"accounts={len((after_obj or {}).get('accounts', {}))}",
            )
            suite.check(
                "④-3 状态文件内容与失败前完全一致（没被半截写污染）",
                loadable and after_obj == before_obj,
                "byte-equal=" + str(loadable and before_bytes == b""),
            )
            leftovers = [
                name
                for name in os.listdir(root)
                if name.startswith(".dsvguard-") and name.endswith(".tmp")
            ]
            suite.check(
                "④-4 失败路径没有残留临时文件",
                not leftovers,
                f"leftovers={leftovers}",
            )

            slot3 = g.acquire("text", caller="atomic-3", wait=False)
            g.report(slot3, ok=True, note="恢复后继续写")
            with open(g.state_path, "r", encoding="utf-8") as fh:
                recovered = json.load(fh)
            suite.check(
                "④-5 替换恢复后写入照常（状态前进到新值）",
                recovered["accounts"]["solo"]["total_ok"] > before_obj["accounts"]["solo"]["total_ok"],
                f"total_ok {before_obj['accounts']['solo']['total_ok']} -> "
                f"{recovered['accounts']['solo']['total_ok']}",
            )

        suite.run("④ 原子写", body)


def _st_day_rollover(suite: _Suite) -> None:
    print("--- ⑤ 跨天：伪造 day_key 后预算重置", flush=True)
    root = suite.mkdir()
    cfg = _write_cfg(
        root,
        [_acct("solo", daily_budget=2, hourly_budget=5, min_interval_s=0)],
        _selftest_defaults(daily_budget=2, hourly_budget=5, min_interval_s=0.0, max_wait_s=0.0),
    )
    with _env(**_isolated_env(root)):
        g = Guard(cfg)

        def body() -> None:
            g.acquire("text", caller="d1", wait=False)
            g.acquire("vision", caller="d2", wait=False)
            raised: Exception | None = None
            try:
                g.acquire("text", caller="d3", wait=False)
            except NoSlot as exc:
                raised = exc
            suite.check(
                "⑤-1 当天第 3 次被日预算拒绝",
                isinstance(raised, NoSlot) and "日预算" in getattr(raised, "reason", ""),
                getattr(raised, "reason", "未抛 NoSlot")[:120],
            )

            state = json.loads(_read_state_text(g.state_path))
            acc = state["accounts"]["solo"]
            acc["day_key"] = "2000-01-01"
            acc["day_used"] = 2
            acc["hour_key"] = "2000-01-01T00"
            acc["hour_used"] = 5
            acc["last_acquire_ts"] = 0.0
            _atomic_write_json(g.state_path, state)

            slot = g.acquire("text", caller="next-day", wait=False)
            row = g.status()["accounts"][0]
            suite.check(
                "⑤-2 伪造跨天后 acquire 成功且预算从 1 重新计",
                row["day_used"] == 1 and row["hour_used"] == 1,
                f"day_used={row['day_used']} hour_used={row['hour_used']} account={slot['account']}",
            )
            suite.check(
                "⑤-3 day_key 已被推进到今天",
                row["day_key"] == _day_key(time.time()),
                f"day_key={row['day_key']}",
            )

        suite.run("⑤ 跨天重置", body)


def _st_audit(suite: _Suite) -> None:
    print("--- ⑥ 审计：acquire/report 各一行 JSONL，字段齐全", flush=True)
    root = suite.mkdir()
    cfg = _write_cfg(
        root,
        [_acct("solo", daily_budget=6, hourly_budget=5, min_interval_s=0)],
        _selftest_defaults(min_interval_s=0.0),
    )
    with _env(**_isolated_env(root)):
        g = Guard(cfg)

        def body() -> None:
            slot = g.acquire("vision", caller="audit-caller", wait=False)
            g.report(slot, ok=False, risk="验证码", note="拖拽验证", duration_s=3.5)
            path = g.audit_path
            lines = [
                json.loads(line)
                for line in _read_state_text(path).splitlines()
                if line.strip()
            ]
            suite.check(
                "⑥-1 审计写入 acquire + report 两行",
                len(lines) == 2,
                f"lines={len(lines)} path={path}",
            )
            required = {
                "ts",
                "account",
                "kind",
                "caller",
                "ok",
                "risk",
                "note",
                "duration_s",
                "egress",
                "day_used",
            }
            missing = [sorted(required - set(line)) for line in lines]
            suite.check(
                "⑥-2 每行都含约定字段",
                all(not m for m in missing),
                f"missing={missing}",
            )
            rep = lines[-1]
            suite.check(
                "⑥-3 report 行的取值正确",
                rep["event"] == "report"
                and rep["ok"] is False
                and rep["risk"] == "验证码"
                and rep["duration_s"] == 3.5
                and rep["egress"] == "home-cn"
                and rep["kind"] == "vision",
                json.dumps(
                    {k: rep[k] for k in ("event", "ok", "risk", "duration_s", "egress", "kind")},
                    ensure_ascii=False,
                ),
            )

        suite.run("⑥ 审计", body)


def _st_masking(suite: _Suite) -> None:
    print("--- ⑦ 脱敏：status()/accounts() 里不许出现 token 值", flush=True)
    root = suite.mkdir()
    secret_env_value = "tok-live-ENV-9f8e7d6c5b4a"
    secret_file_value = "tok-live-FILE-1234567890ab"
    token_file = os.path.join(root, "acc-b.token")
    with open(token_file, "w", encoding="utf-8") as fh:
        fh.write(secret_file_value + "\n")
    cfg = _write_cfg(
        root,
        [
            _acct("solo", token_env="DSV_TOKEN_SOLO"),
            _acct("alt", egress="vps-jp-1", token_env=None, token_file=token_file),
        ],
        _selftest_defaults(min_interval_s=0.0),
    )
    with _env(DSV_TOKEN_SOLO=secret_env_value, **_isolated_env(root)):
        g = Guard(cfg)

        def body() -> None:
            blob = json.dumps(g.status(), ensure_ascii=False) + json.dumps(
                g.accounts(), ensure_ascii=False
            )
            suite.check(
                "⑦-1 status()/accounts() 不含环境变量里的 token 值",
                secret_env_value not in blob,
                f"len={len(blob)}",
            )
            suite.check(
                "⑦-2 status()/accounts() 不含 token 文件内容",
                secret_file_value not in blob,
                f"len={len(blob)}",
            )
            names = json.dumps(g.accounts(), ensure_ascii=False)
            suite.check(
                "⑦-3 只暴露 token 来源名（env:XXX / file:路径）",
                "env:DSV_TOKEN_SOLO" in names and "file:" in names,
                f"accounts={names[:200]}",
            )

        suite.run("⑦ 脱敏", body)


def _st_interfaces(suite: _Suite) -> None:
    print("--- ⑧ 接口契约：返回形状 / egress 一致性 / 启动参数 / 环境变量", flush=True)
    root = suite.mkdir()
    cfg = _write_cfg(
        root,
        [
            _acct("solo", egress="home-cn", token_env="DSV_TOKEN_SOLO"),
            _acct("alt", egress="vps-jp-1", token_file=os.path.join(root, "alt.token")),
        ],
        _selftest_defaults(min_interval_s=0.0),
    )
    secret = "sk-ENV-SECRET-a1b2c3-NEVER-LEAK"
    alt_secret = "sk-FILE-SECRET-d4e5f6-NEVER-LEAK"
    with open(os.path.join(root, "alt.token"), "w", encoding="utf-8") as fh:
        fh.write(alt_secret + "\n")
    with _env(**_isolated_env(root), DSV_TOKEN_SOLO=secret):
        g = Guard(cfg)

        def body() -> None:
            slot = g.acquire("text", caller="iface", wait=False)
            expected = {"account", "profile_dir", "token_path", "egress", "reserved_at"}
            suite.check(
                "⑧-1 acquire 返回契约字段齐全",
                expected <= set(slot),
                f"missing={sorted(expected - set(slot))}",
            )
            suite.check(
                "⑧-2 acquire 回报的 egress 与账号登记一致",
                slot["egress"] == "home-cn",
                f"egress={slot['egress']}",
            )
            suite.check(
                "⑧-3 profile_dir 已创建且稳定（两次调用同一路径）",
                os.path.isdir(slot["profile_dir"])
                and g.profile_for("solo") == g.profile_for("solo"),
                f"{slot['profile_dir']}",
            )
            suite.check(
                "⑧-4 每账号 profile 目录互不相同",
                g.profile_for("solo") != g.profile_for("alt"),
                f"{g.profile_for('solo')} vs {g.profile_for('alt')}",
            )

            g.report(slot, ok=True, duration_s=1.25)
            rep_keys = {"account", "status", "cooldown_until", "hits", "day_used", "hour_used"}
            status_keys = {"account", "day_used", "daily_budget", "cooldown_remaining", "risk_streak"}
            st = g.status()
            suite.check(
                "⑧-5 status() 每账号含今日已用/预算/冷却剩余/连续风控次数",
                all(status_keys <= set(row) for row in st["accounts"]),
                f"rows={len(st['accounts'])}",
            )
            suite.check(
                "⑧-6 status() 返回的账号数与配置一致",
                len(st["accounts"]) == 2,
                f"rows={len(st['accounts'])}",
            )

            got: Exception | None = None
            try:
                g.acquire("text", wait=False, egress_hint="ci-us")
            except EgressMismatch as exc:
                got = exc
            suite.check(
                "⑧-7 egress_hint 与登记不符 -> EgressMismatch（不静默降级）",
                isinstance(got, EgressMismatch),
                f"{type(got).__name__}: {str(got)[:120]}",
            )
            slot2 = g.acquire("text", caller="hint-ok", wait=False, egress_hint="vps-jp-1")
            suite.check(
                "⑧-8 egress_hint 匹配时只从该出口挑账号",
                slot2["account"] == "alt" and slot2["egress"] == "vps-jp-1",
                f"account={slot2['account']} egress={slot2['egress']}",
            )

            flags = g.launch_flags("solo")
            joined = " ".join(flags)
            suite.check(
                "⑧-9 launch_flags 固定 profile + 语言 + 窗口 + 关自动化特征",
                any(f.startswith("--user-data-dir=") for f in flags)
                and "--lang=zh-CN" in flags
                and any(f.startswith("--window-size=") for f in flags)
                and "--disable-blink-features=AutomationControlled" in flags,
                joined[:200],
            )
            suite.check(
                "⑧-10 默认无头、headful=True 时去掉无头标记",
                DEFAULT_HEADLESS_FLAG in flags
                and DEFAULT_HEADLESS_FLAG not in g.launch_flags("solo", headful=True),
                f"headless_flag={DEFAULT_HEADLESS_FLAG}",
            )
            env = g.env_for("solo")
            alt_env = g.env_for("alt")
            leaked = sorted(k for k, v in env.items() if secret in str(v))
            alt_leaked = sorted(k for k, v in alt_env.items() if alt_secret in str(v))
            suite.check(
                "⑧-11 env_for 给 TZ/LANG/DSV_EDGE_PROFILE，且只暴露 token 来源不暴露 token 值",
                env.get("TZ") == DEFAULT_TIMEZONE
                and str(env.get("LANG", "")).lower().startswith("zh_cn")
                and env.get("DSV_EDGE_PROFILE") == g.profile_for("solo")
                and env.get("DSV_TOKEN_ENV") == "DSV_TOKEN_SOLO"
                and alt_env.get("DSV_TOKEN_FILE") == os.path.join(root, "alt.token")
                and not leaked
                and not alt_leaked,
                f"keys={sorted(env)} leaked={leaked} alt_leaked={alt_leaked}",
            )

            bad = False
            try:
                g.acquire("audio", wait=False)
            except ValueError:
                bad = True
            suite.check(
                "⑧-12 非法 kind 直接 ValueError",
                bad,
                "kind=audio",
            )

        suite.run("⑧ 接口契约", body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli_child_acquire(args: argparse.Namespace) -> int:
    """自测用的子进程：到点后抢一次额度，把结果写成 JSON。"""
    guard = Guard(args.config)
    start_at = float(args.start_at)
    delta = start_at - time.time()
    if delta > 0:
        time.sleep(delta)
    result: dict[str, Any] = {"pid": os.getpid(), "begin": time.time(), "start_at": start_at}
    try:
        slot = guard.acquire("text", caller="child", wait=False)
        result.update(
            ok=True,
            account=slot["account"],
            slot_id=slot["slot_id"],
            reserved_at=slot["reserved_at"],
        )
    except NoSlot as exc:
        result.update(ok=False, reason=str(exc), per_account=getattr(exc, "per_account", []))
    except Exception as exc:  # 子进程要把异常带回去，否则父进程只能看到空结果
        result.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    result["end"] = time.time()
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsv_guard.py",
        description="DeepSeek 网页版账号池守卫：预算 / 冷却熔断 / 出口一致性 / 稳定指纹",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="账号配置路径（默认 $DSV_ACCOUNTS 或 <模块目录>/accounts.json）",
    )
    parser.add_argument("--version", action="version", version=f"dsv_guard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="打印每账号预算/冷却/连续风控汇总（JSON）")

    reset_parser = sub.add_parser("reset", help="解冷却 + 清零连续风控计数")
    reset_parser.add_argument("account_id", help="账号 id")

    sub.add_parser("selftest", help="离线自测（临时目录，不碰真实状态）")

    child = sub.add_parser("_child-acquire", help=argparse.SUPPRESS)
    child.add_argument("--config", dest="child_config", default=None, help=argparse.SUPPRESS)
    child.add_argument("--start-at", required=True, help=argparse.SUPPRESS)
    child.add_argument("--out", required=True, help=argparse.SUPPRESS)
    return parser


def _enable_utf8_streams() -> None:
    """让输出在任何重定向下都不因编码崩掉。

    两步走：先把错误处理降级为 replace（GBK 控制台遇到 ⓪/① 之类字符也不会抛
    UnicodeEncodeError），再在“不是真终端”时把编码切到 UTF-8 —— 日志文件里就能
    完整保留中文与符号。注意 git-bash 下 /dev/null 的 isatty() 会返回 True，
    所以这里不把 isatty 当作“是否写文件”的可靠信号，errors=replace 才是兜底。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass


def _main(argv: list[str] | None = None) -> int:
    _enable_utf8_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "selftest":
        return run_selftest()

    if args.command == "_child-acquire":
        if args.child_config:
            args.config = args.child_config
        try:
            return _cli_child_acquire(args)
        except GuardError as exc:
            print(f"[dsv_guard] 子进程失败: {exc}", file=sys.stderr, flush=True)
            return 2

    try:
        guard = Guard(args.config)
    except GuardError as exc:
        print(f"[dsv_guard] {exc}", file=sys.stderr, flush=True)
        return 2

    if args.command == "status":
        print(json.dumps(guard.status(), ensure_ascii=False, indent=2), flush=True)
        return 0

    if args.command == "reset":
        try:
            guard.reset(args.account_id)
        except GuardError as exc:
            print(f"[dsv_guard] {exc}", file=sys.stderr, flush=True)
            return 2
        row = next(
            (r for r in guard.status()["accounts"] if r["account"] == args.account_id), {}
        )
        print(json.dumps(row, ensure_ascii=False, indent=2), flush=True)
        return 0

    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
