#!/usr/bin/env bash
# 准备 Xiaomi-OCR-0 运行环境 (llama.cpp + 权重), 幂等: 已存在则跳过
set -e

TAG="${LLAMA_TAG:-b12503}"
DEST="${OCR_DIR:-llama}"
mkdir -p "$DEST"

MODEL_URL="https://huggingface.co/prithivMLmods/Xiaomi-OCR-0-GGUF/resolve/main"
Q4="$DEST/xiaomi-ocr-0-q4km.gguf"
MM="$DEST/xiaomi-ocr-0-mmproj.gguf"

if [ ! -f "$Q4" ]; then
  echo "下载 Q4_K_M 权重..."
  curl -L --retry 3 --retry-delay 3 -o "$Q4" "$MODEL_URL/Xiaomi-OCR-0.Q4_K_M.gguf"
fi
if [ ! -f "$MM" ]; then
  echo "下载 mmproj..."
  curl -L --retry 3 --retry-delay 3 -o "$MM" "$MODEL_URL/Xiaomi-OCR-0.mmproj-bf16.gguf"
fi

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    ASSET="llama-${TAG}-bin-win-cpu-x64.zip"
    [ -f "$DEST/llama-server.exe" ] && { echo "已有 llama-server.exe"; ls -lh "$DEST"; exit 0; }
    echo "下载 $ASSET ..."
    curl -sL --retry 3 -o /tmp/lc.zip "https://github.com/ggml-org/llama.cpp/releases/download/${TAG}/${ASSET}"
    rm -rf /tmp/lc && mkdir -p /tmp/lc
    unzip -q -o /tmp/lc.zip -d /tmp/lc
    find /tmp/lc -name 'llama-server.exe' -exec cp {} "$DEST/" \;
    find /tmp/lc -name '*.dll' -exec cp {} "$DEST/" \; 2>/dev/null || true
    ;;
  *)
    ASSET="llama-${TAG}-bin-ubuntu-x64.tar.gz"
    [ -f "$DEST/llama-server" ] && { echo "已有 llama-server"; ls -lh "$DEST"; exit 0; }
    echo "下载 $ASSET ..."
    curl -sL --retry 3 -o /tmp/lc.tgz "https://github.com/ggml-org/llama.cpp/releases/download/${TAG}/${ASSET}"
    rm -rf /tmp/lc && mkdir -p /tmp/lc
    tar xzf /tmp/lc.tgz -C /tmp/lc
    find /tmp/lc -name 'llama-server' -exec cp {} "$DEST/" \;
    ;;
esac

chmod +x "$DEST/llama-server" 2>/dev/null || true
echo "=== $DEST ==="
ls -lh "$DEST"
