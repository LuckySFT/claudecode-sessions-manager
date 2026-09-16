#Requires -Version 5.1
<#
.SYNOPSIS
    每日歸檔 + 更新索引。由 Windows 工作排程器呼叫。

.DESCRIPTION
    只做兩件事：把 ~/.claude/projects 的原始檔備份進 archive/，然後更新搜尋索引。
    絕不刪除任何東西 —— 不加 --prune，不動原始資料。

    這個檔案含中文，必須存成「帶 BOM 的 UTF-8」，
    否則 Windows PowerShell 5.1 會用 cp950 解讀，中文被當語法符號，
    錯誤訊息會長得像「遺失 '}'」，完全看不出是編碼問題。
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [int]$KeepLogDays = 30
)

$ErrorActionPreference = 'Stop'

# 不能把 Split-Path $PSScriptRoot 寫成 param 的預設值 ——
# PowerShell 5.1 在繫結參數時 $PSScriptRoot 還沒填好，會拿到空字串然後噴
# 「ParameterArgumentValidationErrorEmptyStringNotAllowed」，訊息完全看不出真正原因。
# 而且 $PSScriptRoot 只有以檔案執行時才有值，貼進主控台跑是 $null。
if (-not $ProjectRoot) {
    if (-not $PSScriptRoot) {
        throw '請以檔案方式執行（powershell -File ...），或明確指定 -ProjectRoot'
    }
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$logDir = Join-Path $ProjectRoot 'logs'
$log = Join-Path $logDir ("archive-{0}.log" -f (Get-Date -Format 'yyyyMMdd'))

if (-not (Test-Path -LiteralPath $python)) {
    throw "找不到 venv 的 python：$python"
}
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# 兩端都要釘死編碼，否則 Python 印的中文會變亂碼：
# Python 走管線時用 cp950 或 UTF-8 取決於環境，PowerShell 則用 [Console]::OutputEncoding
# 解讀，兩邊不一致就壞。PYTHONIOENCODING 管 Python 那端，OutputEncoding 管接收端。
$env:PYTHONIOENCODING = 'utf-8'
$prevOut = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    # 明確指定編碼，預設會用 cp950 寫出亂碼
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

function Invoke-Step {
    param([string]$Name, [string[]]$Arguments)

    Write-Log "開始：$Name"
    # 用陣列 + @args 傳參數。反引號換行接參數在 5.1 下會整批遺失。
    $output = & $python @Arguments 2>&1
    # $ErrorActionPreference = 'Stop' 不會因外部 exe 的非零結束碼而中止，
    # 一定要自己檢查 $LASTEXITCODE，否則失敗也會印「完成」。
    $code = $LASTEXITCODE
    foreach ($line in $output) { Write-Log "  $line" }
    if ($code -ne 0) {
        Write-Log "失敗：$Name（結束碼 $code）"
        throw "$Name 失敗，結束碼 $code"
    }
    Write-Log "完成：$Name"
}

try {
    Push-Location -LiteralPath $ProjectRoot
    Write-Log '===== 每日歸檔開始 ====='

    Invoke-Step -Name '歸檔原始檔' -Arguments @('-m', 'src.cli', 'archive', '--quiet')
    Invoke-Step -Name '更新索引'   -Arguments @('-m', 'src.cli', 'index', '--quiet')

    Write-Log '===== 全部完成 ====='
    $exit = 0
}
catch {
    Write-Log "中止：$($_.Exception.Message)"
    $exit = 1
}
finally {
    Pop-Location -ErrorAction SilentlyContinue
    if ($prevOut) { [Console]::OutputEncoding = $prevOut }

    # 清掉舊日誌（只刪自己產生的 archive-*.log）
    if ($KeepLogDays -gt 0 -and (Test-Path -LiteralPath $logDir)) {
        $cutoff = (Get-Date).AddDays(-$KeepLogDays)
        Get-ChildItem -LiteralPath $logDir -Filter 'archive-*.log' -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

exit $exit
