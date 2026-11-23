# CreditCheckinDaily - 注册每日签到计划任务（个人使用，普通权限）
# 用法: powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1

$ErrorActionPreference = "Stop"

$TaskName = "CreditCheckinDaily"
$Root = Split-Path -Parent $PSScriptRoot
$Python = $null

$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd) {
    $Python = $cmd.Source
} else {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $Python = $py.Source }
}

if (-not $Python) {
    Write-Error "未找到 Python。请先安装 Python 3.9+ 并确保 python 或 py 在 PATH 中。"
    exit 1
}

$Arguments = "-m checkin run"
if ($Python -match "py\.exe$") { $Arguments = "-3 -m checkin run" }

$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At "10:20"
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已移除旧任务: $TaskName"
}

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Credit-checkin personal daily check-in (WorkBuddy/Trae/Qoder, cn/global)" | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "已注册任务: $TaskName"
Write-Host "状态: $($task.State)"
Write-Host "下次运行: $($info.NextRunTime)"
Write-Host "程序: $Python $Arguments"
Write-Host "工作目录: $Root"
