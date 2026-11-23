#!/usr/bin/env bash
# 全部纯函数单测 + 静态检查 (不联网、不用模型)
set -u
cd "$(dirname "$0")/.." || exit 1
fail=0

# 1) 静态检查: 只卡"未定义名"这一类运行时才会炸的错误
if python3 -m pyflakes --version >/dev/null 2>&1; then
  echo "== pyflakes (undefined names) =="
  bad=$(python3 -m pyflakes monitor.py lidaxiao.py aoyuon_ocr.py xiaomi_ocr.py \
        lidaxiao/ 2>&1 | rg 'undefined name' || true)
  if [ -n "$bad" ]; then
    echo "$bad"
    fail=1
  else
    echo "  ✅ 无未定义名"
  fi
else
  echo "== pyflakes 未安装, 跳过 (pip install pyflakes) =="
fi

# 2) 单测
echo "== 单测 =="
for t in tests/test_*.py; do
  printf '  %-26s ' "$(basename "$t")"
  if out=$(python3 "$t" 2>&1); then
    echo "$(echo "$out" | tail -1)"
  else
    echo "FAILED"
    echo "$out" | tail -12
    fail=1
  fi
done
exit $fail
