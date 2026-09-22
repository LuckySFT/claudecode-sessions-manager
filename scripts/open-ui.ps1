#Requires -Version 5.1
<#
.SYNOPSIS
    開啟 Session 管理介面。服務沒在跑就先啟動它。

.DESCRIPTION
    冪等：先探測 port，已經有服務在聽就直接開瀏覽器，不會重複啟動。
    預設用 pythonw.exe，完全不會有主控台視窗殘留在工作列。

    要對照 python.exe 的行為（例如比較行程樹、看即時輸出）用 -UseConsole。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\open-ui.ps1

.EXAMPLE
    # 只啟動服務不開瀏覽器
    ... -File scripts\open-ui.ps1 -NoBrowser

.EXAMPLE
    # 用 python.exe（有主控台）啟動，輸出仍寫進 logs\
    ... -File scripts\open-ui.ps1 -UseConsole

.EXAMPLE
    # 用 python.exe 且顯示視窗，日誌直接印在那個視窗裡（不重導向）
    ... -File scripts\open-ui.ps1 -UseConsole -ShowWindow

.EXAMPLE
    # 看目前的行程樹與 listener，用來對照兩種啟動方式的差別
    ... -File scripts\open-ui.ps1 -Status

.EXAMPLE
    # 停掉服務
    ... -File scripts\open-ui.ps1 -Stop

.NOTES
    此檔含中文，必須存成帶 BOM 的 UTF-8。
#>
[CmdletBinding()]
param(
    [int]$Port = 8787,
    [switch]$NoBrowser,
    [switch]$Stop,
    # 強制重新索引。預設本來就會跑增量索引，這個開關只有在想明確表達意圖時才需要。
    [switch]$Reindex,
    # 跳過索引，只開服務。趕時間或確定索引已是最新時用。
    [switch]$NoReindex,
    # 用 python.exe 取代 pythonw.exe
    [switch]$UseConsole,
    # 顯示主控台視窗。此時不重導向輸出，日誌會直接印在那個視窗
    # （重導向與「看得到輸出」互斥，只能選一個）
    [switch]$ShowWindow,
    # 只印出目前狀態，不啟動也不停止
    [switch]$Status
)

$ErrorActionPreference = 'Stop'

if (-not $PSScriptRoot) { throw '請以檔案方式執行（powershell -File ...）' }
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$python  = Join-Path $projectRoot '.venv\Scripts\python.exe'
$logDir  = Join-Path $projectRoot 'logs'
$pidFile = Join-Path $logDir 'serve.pid'
$url     = "http://127.0.0.1:$Port"

function Test-Port {
    param([int]$P)
    # Test-NetConnection 太慢（有 DNS/ICMP 步驟），直接開 socket 最快
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync('127.0.0.1', $P)
        if ($task.Wait(400)) { return $client.Connected }
        return $false
    } catch { return $false }
    finally { $client.Dispose() }
}

function Get-RecordedPid {
    if (-not (Test-Path -LiteralPath $pidFile)) { return 0 }
    $raw = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $raw) { return 0 }
    $procId = 0
    if ([int]::TryParse($raw.Trim(), [ref]$procId)) { return $procId }
    return 0
}

<#
找出真正在聽這個 port 的行程。

不能只信 pid 檔：venv 的 pythonw.exe 是啟動器 stub，它會再開一個系統 Python
的子行程，**實際在聽 port 的是子行程**，pid 檔記到的是 stub。
（目前殺 stub 剛好也會帶走子行程，但那是 job object 的副作用，不該依賴。）

回傳前一定要驗身分：port 8787 也可能被完全無關的程式佔用，不能盲殺。
#>
function Get-ListenerProcesses {
    param([int]$P)
    $out = @()
    $conns = Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $wmi = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
        if (-not $wmi) { continue }
        # 必須同時是 python 且跑的是我們的 serve 指令，才承認是自己的服務
        if ($wmi.Name -notmatch '^pythonw?\.exe$') { continue }
        if ($wmi.CommandLine -notmatch 'src\.cli.+serve') { continue }
        $out += $wmi
    }
    return $out
}

if ($Status) {
    Write-Host "port $Port ： " -NoNewline
    if (Test-Port -P $Port) { Write-Host '有服務在聽' -ForegroundColor Green }
    else { Write-Host '沒有服務' -ForegroundColor Yellow }

    $recorded = Get-RecordedPid
    Write-Host "pid 檔記錄：$(if ($recorded) { $recorded } else { '（無）' })"

    # 一定要用 @() 包住 —— PowerShell 會把單元素陣列拆成純物件，
    # 而 CimInstance 沒有 Count 屬性，$null -gt 0 是 false，整段就被跳過了。
    $listeners = @(Get-ListenerProcesses -P $Port)
    $listenerIds = @($listeners | ForEach-Object { $_.ProcessId })
    if ($listeners.Count -gt 0) {
        Write-Host '真正在聽的行程：'
        foreach ($l in $listeners) {
            Write-Host "  PID $($l.ProcessId)  parent=$($l.ParentProcessId)  $($l.Name)"
            Write-Host "     $($l.CommandLine)"
        }
    }

    $all = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" `
        -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'src\.cli.+serve' })
    if ($all.Count -gt 0) {
        Write-Host '所有相關的 python 行程（含啟動器 stub）：'
        foreach ($p in $all) {
            $mark = if ($listenerIds -contains $p.ProcessId) { ' <== listener' } else { ' （stub）' }
            Write-Host "  PID $($p.ProcessId)  parent=$($p.ParentProcessId)  $($p.ExecutablePath)$mark"
        }
    }
    return
}

if ($Stop) {
    $killed = @()
    foreach ($wmi in Get-ListenerProcesses -P $Port) {
        Stop-Process -Id $wmi.ProcessId -Force -ErrorAction SilentlyContinue
        $killed += $wmi.ProcessId
    }
    # stub 可能還活著（子行程已被殺），一併收掉
    $recorded = Get-RecordedPid
    if ($recorded -and $killed -notcontains $recorded) {
        $p = Get-Process -Id $recorded -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -match '^pythonw?$') {
            Stop-Process -Id $recorded -Force -ErrorAction SilentlyContinue
            $killed += $recorded
        }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue

    if ($killed.Count -gt 0) {
        Write-Host "已停止服務（PID $($killed -join ', ')）"
    } elseif (Test-Port -P $Port) {
        Write-Host "port $Port 被別的程式佔用，不是這個工具的服務，沒有動它" -ForegroundColor Yellow
    } else {
        Write-Host '服務本來就沒在跑'
    }
    return
}

foreach ($exe in @($pythonw, $python)) {
    if (-not (Test-Path -LiteralPath $exe)) { throw "找不到 $exe（venv 是不是還沒建？）" }
}
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$dbPath = Join-Path $projectRoot 'data\index.db'

if (Test-Port -P $Port) {
    Write-Host "服務已在執行：$url"

    # 服務常態開著，所以「已在執行」這條路徑也必須索引 —— 否則資料會悄悄停在
    # 最後一次索引的時間點（實際發生過，停了四天才被發現）。
    # 服務正在寫同一個 DB，這裡不能另起行程去寫，走 API 讓服務自己來。
    if ($Reindex -or -not $NoReindex) {
        Write-Host '更新索引中…'
        try {
            $r = Invoke-RestMethod -Method Post -Uri "$url/api/reindex" -TimeoutSec 600
            Write-Host ("索引已更新：掃 {0}、重建 {1}、追加 {2}、略過 {3}" -f `
                $r.scanned, $r.rebuilt, $r.appended, $r.skipped)
            if ($r.errors -and $r.errors.Count -gt 0) {
                Write-Warning "索引有 $($r.errors.Count) 個錯誤：$($r.errors -join '; ')"
            }
        } catch {
            # 索引失敗不該擋住開 UI，舊索引仍可瀏覽
            Write-Warning "更新索引失敗，將以現有索引開啟：$($_.Exception.Message)"
        }
    }
} else {
    # 索引不存在就非建不可，否則服務會直接回 503
    if ($Reindex -or -not $NoReindex -or -not (Test-Path -LiteralPath $dbPath)) {
        if (Test-Path -LiteralPath $dbPath) {
            Write-Host '更新索引中…'
        } else {
            Write-Host '建立索引中（第一次會花約半分鐘）…'
        }
        $env:PYTHONIOENCODING = 'utf-8'
        & $python -m src.cli index --quiet
        if ($LASTEXITCODE -ne 0) { throw "建立索引失敗（結束碼 $LASTEXITCODE）" }
    }

    $exe = if ($UseConsole) { $python } else { $pythonw }
    Write-Host "啟動服務（$(Split-Path -Leaf $exe)$(if ($ShowWindow) { '，顯示視窗' })）…"

    $spArgs = @{
        FilePath         = $exe
        ArgumentList     = @('-m', 'src.cli', 'serve', '--port', $Port)
        WorkingDirectory = $projectRoot
        PassThru         = $true
    }
    if ($ShowWindow) {
        # 顯示視窗時不能重導向 —— 導走了視窗裡就什麼都看不到，
        # 而「看得到即時輸出」正是這個模式存在的目的
        $spArgs.WindowStyle = 'Normal'
    } else {
        # 不顯示視窗就一定要重導向。pythonw 根本沒有可用的 stdout，
        # python.exe 隱藏視窗時輸出也沒人看得到 ——
        # 兩種情況下不導向都會讓「服務起不來」的錯誤完全消失。
        $spArgs.WindowStyle = 'Hidden'
        $spArgs.RedirectStandardOutput = Join-Path $logDir 'serve.out.log'
        $spArgs.RedirectStandardError = Join-Path $logDir 'serve.err.log'
        # stdin 一併導掉，讓背景服務不吃呼叫端的輸入。
        #
        # 【已知限制，這樣做並沒有解決】
        # 呼叫端若用管線「捕捉」這支腳本的輸出（例如
        # `$out = & cmd.exe /c "Session管理.cmd -NoBrowser"`、bash 的 `| tail`），
        # 會一直等 pipe 關閉而掛住 —— 實測導了三個流之後仍然如此。
        # 原因是 Windows 的 CreateProcess 在 bInheritHandles=TRUE 時會繼承
        # **所有**可繼承的 handle，不只 stdio；而 venv 的 python 是啟動器 stub，
        # 它再開的子行程把那些 handle 又帶下去。
        #
        # 要真正解決得改用 WMI Win32_Process.Create（不繼承 handle）建立行程，
        # 代價是失去 -RedirectStandardOutput（服務起不來時錯誤會完全消失）
        # 與 HasExited 檢查 —— 拿確定的好處換不確定的好處，刻意不換。
        # 雙擊與工作排程器都不捕捉輸出，不受影響；要在腳本裡呼叫請把輸出
        # 重導向到檔案再讀檔，不要用管線捕捉。
        $nul = Join-Path $logDir '.nul'
        if (-not (Test-Path -LiteralPath $nul)) {
            New-Item -ItemType File -Path $nul -Force | Out-Null
        }
        $spArgs.RedirectStandardInput = $nul
    }
    # Python 那端也要釘死編碼，否則中文輸出寫進日誌會是亂碼
    $env:PYTHONIOENCODING = 'utf-8'
    $proc = Start-Process @spArgs

    Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding ASCII

    $ready = $false
    foreach ($i in 1..60) {
        Start-Sleep -Milliseconds 250
        if (Test-Port -P $Port) { $ready = $true; break }
        if ($proc.HasExited) { break }
    }
    if (-not $ready) {
        Write-Host '服務啟動失敗。' -ForegroundColor Red
        if ($ShowWindow) {
            Write-Host '  輸出在剛才開啟的主控台視窗裡（這個模式不寫日誌檔）'
        } else {
            $err = Join-Path $logDir 'serve.err.log'
            if (Test-Path -LiteralPath $err) {
                Write-Host '  錯誤輸出：'
                Get-Content -LiteralPath $err -Tail 25 -Encoding UTF8 |
                    ForEach-Object { Write-Host "    $_" }
            }
        }
        throw "服務未在 15 秒內就緒（日誌：$logDir）"
    }

    # 啟動器 stub 會再開子行程，真正在聽的通常不是 $proc.Id，兩個都印出來才不會誤導
    $listenerIds = @(Get-ListenerProcesses -P $Port |
        ForEach-Object { $_.ProcessId }) -join ', '
    Write-Host "服務已啟動：$url"
    Write-Host "  Start-Process 取得的 PID：$($proc.Id)（$(Split-Path -Leaf $exe)）"
    if ($listenerIds) { Write-Host "  實際在聽 port 的 PID：$listenerIds" }
}

if (-not $NoBrowser) { Start-Process $url }
