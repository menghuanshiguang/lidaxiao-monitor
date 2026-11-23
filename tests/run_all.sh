#!/usr/bin/env bash
# 跑全部纯函数单测 (不联网、不用模型)
set -u
cd "$(dirname "$0")/.." || exit 1
fail=0
for t in tests/test_*.py; do
  printf '%-28s ' "$(basename "$t")"
  if out=$(python3 "$t" 2>&1); then
    echo "$(echo "$out" | tail -1)"
  else
    echo "FAILED"
    echo "$out" | tail -12
    fail=1
  fi
done
exit $fail
