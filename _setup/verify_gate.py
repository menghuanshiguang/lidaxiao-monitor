"""CI 闸门自检: 断言闸门在「放行/拦截」两种状态下的行为都符合预期。

用途: 云端 Actions 的 acceptance test。用 GATE_EXPECT 指定期望态:

  GATE_EXPECT=blocked (默认)
      证明: config.json 的 web_lane.enabled=true, 但 CI 环境把它拒掉,
            账号表路径/策略层形状正确 —— 云端不会碰登录态。
  GATE_EXPECT=enabled
      证明: 显式放行(WEB_LANE_ALLOW_CI=1 且 allow_ci=true)后, 闸门真的开,
            且账号表存在、能解析出 egress=ci-us 的账号 —— 开关不是摆设。

在本地(非 CI)跑这个脚本会失败 —— 那是正确行为, 本地本来就不该被拦。
用法: GATE_EXPECT=enabled python _setup/verify_gate.py
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
    expect = (os.environ.get("GATE_EXPECT") or "blocked").strip().lower()
    if expect not in ("blocked", "enabled"):
        return fail(f"GATE_EXPECT 只能是 blocked/enabled, 实际 {expect!r}")

    cfg_path = os.path.join(ROOT, "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    info = weblane.doctor(cfg)
    print(json.dumps({k: v for k, v in info.items() if k != "accounts"},
                     ensure_ascii=False, indent=2))

    if os.environ.get("GITHUB_ACTIONS") != "true":
        return fail("这个自检只在 CI 里成立; 本地跑请设 GITHUB_ACTIONS=true")
    if info.get("raw_enabled") is not True:
        return fail("config.json 的 web_lane.enabled 不是 true —— 总开关没生效")
    if info.get("ci") != "true":
        return fail(f"没识别到 CI 环境, ci={info.get('ci')!r}")
    if not str(info.get("accounts_file", "")).endswith("web_accounts.json"):
        return fail(f"账号表路径不对: {info.get('accounts_file')}")
    if not hasattr(dsv_guard, "Guard"):
        return fail("策略层 dsv_guard.Guard 不可用")

    if expect == "blocked":
        if info.get("enabled") is not False:
            return fail(f"CI 里网页版通道必须被拒绝, 实际 enabled={info.get('enabled')}")
        print("GATE_OK[blocked]: 配置请求网页版, CI 闸门拒绝 -> 云端不会碰登录态")
        return 0

    # expect == "enabled"
    if info.get("enabled") is not True:
        return fail(f"已显式放行但闸门仍是关闭的, enabled={info.get('enabled')}, "
                    f"warnings={info.get('warnings')}")
    if not info.get("accounts_ok"):
        return fail(f"闸门开了但账号表不可用: {info.get('warnings')}")
    accts = info.get("accounts") or []
    ci_accts = [a for a in accts if a.get("egress") == "ci-us"]
    if not ci_accts:
        return fail(f"账号表里没有 egress=ci-us 的账号, 实际: "
                    f"{[(a.get('id'), a.get('egress')) for a in accts]}")
    if not any(a.get("token_ok") for a in ci_accts):
        return fail("ci-us 账号取不到 token(检查 DSV_TOKEN 是否注入)")
    print("GATE_OK[enabled]: 显式放行后闸门打开, ci-us 账号与 token 均就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
