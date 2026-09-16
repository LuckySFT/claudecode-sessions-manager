#Requires -Version 5.1
<#
.SYNOPSIS
    把每日歸檔註冊到 Windows 工作排程器（或移除）。

.EXAMPLE
    # 註冊，每天 12:30 執行
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-task.ps1

.EXAMPLE
    # 改時間
    ... -File scripts\register-task.ps1 -At 03:00

.EXAMPLE
    # 改成註冊「登入時自動啟動 Web 服務」（另一個獨立的工作，兩者可並存）
    ... -File scripts\register-task.ps1 -AtLogon

.EXAMPLE
    # 移除（-AtLogon 移除的是登入那個工作）
    ... -File scripts\register-task.ps1 -Remove
    ... -File scripts\register-task.ps1 -AtLogon -Remove

.NOTES
    刻意不用 -RunLevel Highest：這個工作只讀 ~/.claude 與寫自己的 archive/，
    不需要系統管理員權限。要求提權只會讓註冊變麻煩又擴大風險面。
    此檔含中文，必須存成帶 BOM 的 UTF-8。
#>
[CmdletBinding()]
param(
    [string]$TaskName,
    [string]$At = '12:30',
    [switch]$Remove,
    [switch]$RunNow,
    # 改成註冊「登入時啟動 Web 服務」而不是每日歸檔
    [switch]$AtLogon
)

$ErrorActionPreference = 'Stop'

if (-not $PSScriptRoot) { throw '請以檔案方式執行（powershell -File ...）' }
$projectRoot = Split-Path -Parent $PSScriptRoot

if ($AtLogon) {
    if (-not $TaskName) { $TaskName = 'ClaudeCode-SessionManager-Serve' }
    $script = Join-Path $PSScriptRoot 'open-ui.ps1'
    $scriptArgs = '-NoBrowser'
    $description = '登入時啟動 Claude Code Session 管理的本機 Web 服務'
} else {
    if (-not $TaskName) { $TaskName = 'ClaudeCode-SessionArchive' }
    $script = Join-Path $PSScriptRoot 'daily-archive.ps1'
    $scriptArgs = ''
    $description = '每日備份 Claude Code session 原始檔並更新搜尋索引'
}

if (-not (Test-Path -LiteralPath $script)) { throw "找不到 $script" }

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Remove) {
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已移除排程工作：$TaskName"
    } else {
        Write-Host "排程工作不存在，不需要移除：$TaskName"
    }
    return
}

# -File 後面的路徑用引號包住，路徑含空白時才不會被拆開
$argument = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $script
if ($scriptArgs) { $argument = "$argument $scriptArgs" }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument $argument -WorkingDirectory $projectRoot

if ($AtLogon) {
    $trigger = New-ScheduledTaskTrigger -AtLogOn
} else {
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
}

# StartWhenAvailable：筆電那個時間沒開機的話，開機後補跑
# DontStopIfGoingOnBatteries + AllowStartIfOnBatteries：不插電也要跑
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

$when = if ($AtLogon) { '登入時' } else { "每天 $At" }

if ($existing) {
    Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings | Out-Null
    Write-Host "已更新排程工作：$TaskName（$when）"
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description $description | Out-Null
    Write-Host "已註冊排程工作：$TaskName（$when）"
}

Write-Host "  執行內容：$script"
Write-Host "  工作目錄：$projectRoot"
Write-Host "  日誌：$(Join-Path $projectRoot 'logs')"

if ($RunNow) {
    Write-Host '立即執行一次…'
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "  上次執行結果代碼：$($info.LastTaskResult)"
}

Write-Host ''
Write-Host '查看狀態： Get-ScheduledTaskInfo -TaskName ' -NoNewline
Write-Host $TaskName
Write-Host '手動執行： Start-ScheduledTask -TaskName ' -NoNewline
Write-Host $TaskName
Write-Host "移除：     ... -File scripts\register-task.ps1 -Remove"
