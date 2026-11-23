"""兜底下载的 206 完整性校验单测"""
import re

PAT = re.compile(r"bytes\s+0-(\d+)/(\d+)")
ok = fail = 0


def chk(name, cond, info=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}\n     {info}")


def full_coverage(header):
    m = PAT.fullmatch((header or "").strip())
    if not m:
        return None
    end, full = int(m.group(1)), int(m.group(2))
    return end + 1 == full


chk("完整覆盖", full_coverage("bytes 0-1048575/1048576") is True)
chk("半截 Range", full_coverage("bytes 0-500/1048576") is False)
chk("未知总长", full_coverage("bytes 0-100/*") is None)
chk("非法头", full_coverage("garbage") is None)
chk("空头", full_coverage("") is None)
chk("带空格", full_coverage("  bytes 0-9/10  ") is True)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
