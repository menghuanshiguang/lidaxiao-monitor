#!/usr/bin/env bash
# 准备 Xiaomi-OCR-0 运行环境 (llama.cpp + 权重), 幂等: 已存在且大小合理则跳过
set -e

TAG="${LLAMA_TAG:-b12503}"
DEST="${OCR_DIR:-llama}"
mkdir -p "$DEST"

MODEL_URL="https://huggingface.co/prithivMLmods/Xiaomi-OCR-0-GGUF/resolve/main"
Q4="$DEST/xiaomi-ocr-0-q4km.gguf"
MM="$DEST/xiaomi-ocr-0-mmproj.gguf"

# 文件大小 (跨平台: GNU stat / BSD stat)
fsize() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || echo 0; }

# 有效 = 存在 且 >= 阈值(MB)
ok_file() { [ -f "$1" ] && [ "$(fsize "$1")" -ge $(( $2 * 1024 * 1024 )) ]; }

dl() {  # dl <url> <out> <minMB> <label>
  echo "下载 $4 ..."
  rm -f "$2"
  curl -fL --retry 3 --retry-delay 3 -o "$2" "$1"
  if ! ok_file "$2" "$3"; then
    echo "!! $4 下载异常: $(fsize "$2") 字节 < 期望 $3 MB" >&2
    rm -f "$2"
    return 1
  fi
  echo "   ok: $4 $(fsize "$2") 字节"
}

# Q4_K_M 约 529MB, mmproj-bf16 约 207MB
ok_file "$Q4" 300 || dl "$MODEL_URL/Xiaomi-OCR-0.Q4_K_M.gguf"    "$Q4" 300 "Q4_K_M 权重"
ok_file "$MM" 100 || dl "$MODEL_URL/Xiaomi-OCR-0.mmproj-bf16.gguf" "$MM" 100 "mmproj"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    ASSET="llama-${TAG}-bin-win-cpu-x64.zip"
    if [ -f "$DEST/llama-server.exe" ]; then
      echo "已有 llama-server.exe"; ls -lh "$DEST"; exit 0
    fi
    echo "下载 $ASSET ..."
    curl -fsL --retry 3 -o /tmp/lc.zip "https://github.com/ggml-org/llama.cpp/releases/download/${TAG}/${ASSET}"
    rm -rf /tmp/lc && mkdir -p /tmp/lc
    unzip -q -o /tmp/lc.zip -d /tmp/lc
    find /tmp/lc -name 'llama-server.exe' -exec cp {} "$DEST/" \;
    find /tmp/lc -name '*.dll' -exec cp {} "$DEST/" \; 2>/dev/null || true
    ;;
  *)
    ASSET="llama-${TAG}-bin-ubuntu-x64.tar.gz"
    if [ -f "$DEST/llama-server" ]; then
      echo "已有 llama-server"; ls -lh "$DEST"; exit 0
    fi
    echo "下载 $ASSET ..."
    curl -fsL --retry 3 -o /tmp/lc.tgz "https://github.com/ggml-org/llama.cpp/releases/download/${TAG}/${ASSET}"
    rm -rf /tmp/lc && mkdir -p /tmp/lc
    tar xzf /tmp/lc.tgz -C /tmp/lc
    find /tmp/lc -name 'llama-server' -exec cp {} "$DEST/" \;
    ;;
esac

chmod +x "$DEST/llama-server" 2>/dev/null || true
echo "=== $DEST ==="
ls -lh "$DEST"
