# 生成并安装"科学化定时检查"计划任务 (7个触发器: 09:30/11:30/14:00/14:50/17:00/20:00/22:30)
$ErrorActionPreference = 'Continue'   # 原生命令(stderr)不应中断脚本
$task = 'LiDaxiaoMonitor'
$cmd = Join-Path (Split-Path $PSScriptRoot -Parent) 'run_monitor.cmd'
$times = @('09:30','11:30','14:00','14:50','17:00','20:00','22:30')  # 14:50=收盘前10分钟
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$startDate = (Get-Date).ToString('yyyy-MM-dd')

$triggers = ''
foreach ($t in $times) {
    $boundary = "$startDate" + "T" + "$($t)" + ":00"
    $triggers += @"
    <CalendarTrigger>
      <StartBoundary>$boundary</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
"@
}

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>LiDaxiao video auto monitor - scientific schedule (6x daily)</Description>
  </RegistrationInfo>
  <Triggers>
$triggers  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$cmd</Command>
    </Exec>
  </Actions>
</Task>
"@

$xmlPath = Join-Path $PSScriptRoot 'LiDaxiaoMonitor.xml'
[System.IO.File]::WriteAllText($xmlPath, $xml, [System.Text.Encoding]::Unicode)

# 删除旧的 2 个任务 (容错: 任务不存在时忽略)
foreach ($old in @("${task}_Morning", "${task}_Evening")) {
    $null = schtasks.exe /query /tn $old 2>$null
    if ($LASTEXITCODE -eq 0) {
        $null = schtasks.exe /delete /tn $old /f 2>$null
        Write-Host "已删除旧任务: $old"
    }
}

# 创建新任务 (cmd /c 调用最稳, 避免 PowerShell native stderr 干扰)
cmd /c "schtasks /create /tn $task /xml `"$xmlPath`" /f"
if ($LASTEXITCODE -ne 0) {
    Write-Host "创建任务失败 (exit=$LASTEXITCODE)" -ForegroundColor Red
    exit 1
}
Write-Host "`n=== 已安装任务 $task (触发器) ==="
$xml = schtasks.exe /query /tn $task /xml
[regex]::Matches(($xml -join "`n"), '<StartBoundary>([^<]+)</StartBoundary>') |
    ForEach-Object { $_.Groups[1].Value }
