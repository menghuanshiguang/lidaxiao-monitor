# 启动本地 Qwen3.5-9B (llama.cpp), 供同一 step 内的 python 调用
# 注意: GitHub Actions 在 step 结束时会杀掉子进程, 所以必须和调用方在同一个 step
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

New-Item -ItemType Directory -Force models | Out-Null

if (-not (Test-Path "models\qwen35-9b-q4.gguf")) {
  Write-Host "下载 Qwen3.5-9B Q4_K_M (~5.5GB)..."
  Invoke-WebRequest -Uri "https://huggingface.co/mradermacher/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-GGUF/resolve/main/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q4_K_M.gguf" -OutFile "models\qwen35-9b-q4.gguf" -UseBasicParsing
}

if (-not (Test-Path "llama\llama-server.exe")) {
  Write-Host "下载 llama.cpp (win-cpu)..."
  Invoke-WebRequest -Uri "https://github.com/ggml-org/llama.cpp/releases/download/b12503/llama-b12503-bin-win-cpu-x64.zip" -OutFile "$env:RUNNER_TEMP\llama.zip" -UseBasicParsing
  Expand-Archive -Path "$env:RUNNER_TEMP\llama.zip" -DestinationPath "llama" -Force
}

$exe = Get-ChildItem -Path llama -Recurse -Filter llama-server.exe | Select-Object -First 1
if (-not $exe) { throw "llama-server.exe 未找到" }

$modelPath = (Resolve-Path "models\qwen35-9b-q4.gguf").Path
$logOut = Join-Path (Get-Location) "llama-server.log"
$logErr = Join-Path (Get-Location) "llama-server.err.log"

Write-Host "启动 llama-server: $($exe.FullName)"
Start-Process -FilePath $exe.FullName -WorkingDirectory $exe.DirectoryName `
  -ArgumentList @("-m", $modelPath, "-c", "16384", "-t", "4", "--jinja",
                  "--host", "127.0.0.1", "--port", "8080", "--no-webui") `
  -RedirectStandardOutput $logOut -RedirectStandardError $logErr -WindowStyle Hidden

$ok = $false
for ($i = 0; $i -lt 180; $i++) {
  try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -TimeoutSec 3
    if ($r.status -eq "ok" -or $r) { $ok = $true; break }
  } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) {
  if (Test-Path $logErr) { Get-Content $logErr -Tail 40 }
  if (Test-Path $logOut) { Get-Content $logOut -Tail 40 }
  throw "本地模型启动失败"
}
Write-Host "本地模型就绪 (http://127.0.0.1:8080)"
