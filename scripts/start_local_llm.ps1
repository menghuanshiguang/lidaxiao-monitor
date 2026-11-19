# 在 Actions 里用 Ollama 拉起本地模型（同一 step 内调用，否则进程会被杀）
# 模型: rinibr20/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-GGUF
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$OLLAMA_MODEL = "rinibr20/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-GGUF"
$OllamaDir = Join-Path (Get-Location) "ollama"

if (-not (Test-Path (Join-Path $OllamaDir "ollama.exe"))) {
  Write-Host "下载 Ollama (windows-amd64)..."
  Invoke-WebRequest -Uri "https://github.com/ollama/ollama/releases/download/v0.40.16/ollama-windows-amd64.zip" `
    -OutFile "$env:RUNNER_TEMP\ollama.zip" -UseBasicParsing
  Expand-Archive -Path "$env:RUNNER_TEMP\ollama.zip" -DestinationPath $OllamaDir -Force
}
$exe = Get-ChildItem -Path $OllamaDir -Recurse -Filter ollama.exe | Select-Object -First 1
if (-not $exe) { throw "ollama.exe 未找到" }

# 已在跑就别重复起
$alive = $false
try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null; $alive = $true } catch {}

if (-not $alive) {
  Write-Host "启动 ollama serve"
  Start-Process -FilePath $exe.FullName -ArgumentList "serve" -WorkingDirectory $exe.DirectoryName `
    -RedirectStandardOutput "ollama.log" -RedirectStandardError "ollama.err.log" -WindowStyle Hidden
  for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null; $alive = $true; break }
    catch { Start-Sleep -Seconds 2 }
  }
}
if (-not $alive) {
  if (Test-Path ollama.err.log) { Get-Content ollama.err.log -Tail 30 }
  throw "ollama serve 启动失败"
}

# 拉模型（已缓存则秒过）
Write-Host "ollama pull $OLLAMA_MODEL"
& $exe.FullName pull $OLLAMA_MODEL
if ($LASTEXITCODE -ne 0) { throw "ollama pull 失败 exit=$LASTEXITCODE" }

Write-Host "本地模型就绪 (Ollama @ http://127.0.0.1:11434/v1)"
