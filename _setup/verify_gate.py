"""CI 闸门自检: 断言「配置请求了网页版通道」且「CI 环境把它拒掉」。

用途: 云端 Actions 的 acceptance test。它证明三件事同时成立——
  1. config.json 里 web_lane.enabled 真的是 true(方案B 的意图被读到了);
  2. 在 GITHUB_ACTIONS=true 的环境里, 闸门把它判成不可用(云端不会碰登录态);
  3. dsv_guard 能被导入、账号表路径被解析出来(部署形状正确)。

在本地(非 CI)跑这个脚本会失败 —— 那是正确行为, 本地本来就应该允许。
用法: python _setup/verify_gate.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import weblane  # noqa: E402
import dsv_guard  # noqa: E402  策略层必须能在云端干净导入


def fail(msg):
    print(f"FAIL: {msg}")
    return 1


def main():
    cfg_path = os.path.join(ROOT, "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    info = weblane.doctor(cfg)
    print(json.dumps({k: v for k, v in info.items() if k != "accounts"},
                     ensure_ascii=False, indent=2))

    if os.environ.get("GITHUB_ACTIONS") != "true":
        return fail("这个自检只在 CI 里成立; 本地跑请设 GITHUB_ACTIONS=true")
    if info.get("raw_enabled") is not True:
        return fail("config.json 的 web_lane.enabled 不是 true —— 方案B 的开关没生效")
    if info.get("enabled") is not False:
        return fail(f"CI 里网页版通道必须被拒绝, 实际 enabled={info.get('enabled')}")
    if info.get("ci") != "true":
        return fail(f"没识别到 CI 环境, ci={info.get('ci')!r}")
    if not str(info.get("accounts_file", "")).endswith("web_accounts.json"):
        return fail(f"账号表路径不对: {info.get('accounts_file')}")
    if not hasattr(dsv_guard, "Guard"):
        return fail("策略层 dsv_guard.Guard 不可用")

    print("GATE_OK: 配置请求网页版, CI 闸门拒绝, 账号表路径正确 -> 云端不会碰登录态")
    return 0


if __name__ == "__main__":
    sys.exit(main())
