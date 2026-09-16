# Claude Code Session 管理工具

把本機 `~/.claude/` 底下的 Claude Code 對話記錄建成可搜尋的索引，用本機 Web UI 瀏覽。

## 使用

日常用這個就好 —— **雙擊 `Session管理.cmd`**。它會判斷服務有沒有在跑，沒跑就啟動
（無主控台視窗），然後開瀏覽器。重複雙擊不會重複啟動。

```
Session管理.cmd              需要就啟動，然後開瀏覽器
Session管理.cmd -Stop        停掉服務
Session管理.cmd -Status      顯示行程樹與真正在聽 port 的 PID
Session管理.cmd -Reindex     啟動前先重建索引
Session管理.cmd -NoBrowser   只啟動不開瀏覽器
```

除錯用的兩個開關：

```
-UseConsole               改用 python.exe（預設是 pythonw.exe）
-UseConsole -ShowWindow    顯示主控台視窗、日誌直接印在裡面（此模式不寫日誌檔）
```

`-ShowWindow` 與寫日誌檔互斥 —— 導向了視窗裡就什麼都看不到，所以只能選一個。

想要開機就常駐（不用每次雙擊）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-task.ps1 -AtLogon
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-task.ps1 -AtLogon -Remove
```

### 直接用 CLI

```powershell
.venv\Scripts\python.exe -m src.cli index      # 建立/更新索引（可反覆執行，增量）
.venv\Scripts\python.exe -m src.cli status     # 看索引概況
.venv\Scripts\python.exe -m src.cli serve      # 起 Web UI，http://127.0.0.1:8787

.venv\Scripts\python.exe -m src.cli archive            # 備份原始 jsonl（只新增）
.venv\Scripts\python.exe -m src.cli archive --status   # 看歸檔概況
.venv\Scripts\python.exe -m src.cli archive --verify   # 驗證所有備份可還原
.venv\Scripts\python.exe -m src.cli restore --session <id>   # 還原單一 session
#   --overwrite 目標存在也寫；--force 連進行中的 session 也覆寫（危險）

# 改名（寫入原始 jsonl，Claude Code 的 /resume 選單也會顯示）
.venv\Scripts\python.exe -m src.cli rename --session <id> --title "新名稱"
.venv\Scripts\python.exe -m src.cli rename --session <id> --title "x" --dry-run

# 移除「原始檔已被 Claude Code 清掉」的記錄（預設只預覽，要加 --yes 才執行）
.venv\Scripts\python.exe -m src.cli purge
.venv\Scripts\python.exe -m src.cli purge --older-than 90 --only-backed-up --yes
.venv\Scripts\python.exe -m src.cli vacuum        # 讓刪除後的空間真的釋出
```

每日自動執行（歸檔 + 索引，預設每天 12:30）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-task.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Remove  # 移除
```

測試：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 目前完成的範圍

- 增量索引（主 session + subagent 逐字稿）
- 跨專案全文搜尋、按 session 聚合、命中位置跳轉
- session 瀏覽（對話還原、token 統計、resume 指令）
- 原始檔歸檔與還原，含完整性驗證與每日排程
- 保留期可見化（哪些已被清掉、哪些快到期、哪些還沒備份）
- 版面可調（字體大小、淺色/深色、欄寬、區塊高度，全部記住）
- **session 改名**（寫入原始 jsonl，Claude Code 也看得到）與本地覆寫兩種模式
- session 隱藏、專案別名合併、專案五種排序
- 移除「原始檔已被清掉」的記錄（兩種深度，預設只預覽）
  —— 可從 session 詳細頁單筆移除，或用清理面板批次勾選

尚未實作：成本換算報表、Markdown/HTML 匯出。詳見 [docs/TODO.md](docs/TODO.md)。

## 資料來源結構（實測 Claude Code 2.1.x）

```
~/.claude/projects/<專案slug>/
├─ <sessionId>.jsonl                    主 session
└─ <sessionId>/
   ├─ subagents/agent-<agentId>.jsonl   subagent 逐字稿（獨立檔案，不在主檔內）
   ├─ tool-results/*.txt                外置的大型工具輸出（主檔內嵌前 2KB 預覽）
   └─ memory/*.md                       專案記憶
~/.claude/history.jsonl                 全域 prompt 歷史
~/.claude/file-history/<sessionId>/     rewind 用的檔案備份
```

## 設計上的硬規則

**原始資料唯讀，只有兩個例外。** `~/.claude/` 底下的檔案不改寫、不刪除。
兩個例外都經過 `src/liveness.py` 的同一組防護：
**session 改名**（往主 session 的 jsonl 檔尾追加一行 `custom-title` 記錄，
這是 Claude Code 官方自己的機制）與 **restore 覆寫既有檔案**。
改名的兩層防護缺一不可 ——
尾端位元組必須是 `
`（否則 append 會把兩行黏在一起，這是唯一會造成損壞的情境），
且 mtime 必須閒置 10 分鐘以上（進行中的 session 不給改）。

**restore 是整檔覆寫，比改名危險。** append 最多多一行垃圾，覆寫是整個對話被
舊版本蓋掉，而且 Claude Code 那端不會報錯。所以 `--overwrite` 不含
「即使進行中也寫」的授權，要再加 `--force` 才行；被擋下來會歸到 `blocked`
（跟 `failed` 分開 —— 前者是刻意保護，後者是出錯，處理方式不同）。

**絕對不要砍掉 index.db 重建。** 索引通常被當成可拋棄的衍生物，但它有一種內容是
**唯一留存**的：Claude Code 的保留期刪掉原始 jsonl 之後，索引裡那份記錄就沒有
別的來源了（歸檔只涵蓋歸檔功能建立之後還存在的檔案）。
2026-09-03 為了加一個欄位而砍檔重建，銷毀了 140 個這種 session 的記錄，
無法還原。加欄位一律走 `db.migrate()`（`ALTER TABLE ADD COLUMN`），既有列不動。

**機密在索引時就遮罩，不是查詢時。** `index.db` 本身就是產出物，明文不能進去。
代價是遮掉的內容無法從索引還原，要看原文只能自己去讀原始 jsonl。
遮罩規則見 [src/redact.py](src/redact.py)。

**索引不是備份，`archive/` 才是。** 索引是有損的衍生物：文字截斷、遮罩不可逆、
空區塊丟棄、非對話記錄跳過，連 JSON 結構都沒留。實測已被清掉的 140 個 session，
36.8MB 原始資料在索引裡只剩 7.7MB 文字（21%）。歸檔存的是**原始位元組**，
兩者職責完全分開。

**`archive/` 只新增，永不刪除。** 原始檔被 Claude Code 清掉之後 blob 仍然留著，
這正是它存在的理由。`index --prune` 與 `prune_missing()` 會把「原始檔已消失」的
session 從索引移除，在沒有備份的情況下等於銷毀唯一副本 —— 不要隨手用。
搬移專案目錄後會整批出現**假的**「原始檔已消失」（session id 沒變，只是換了 slug
目錄），此時先跑一次 `index` 就會自己接回去；**先 prune 會把真正消失的那些一起砍掉**。
搬家流程見全域 skill `project-relocate`。

**`data/labels.db` 不可與索引一起砍。** 使用者手打的標題、隱藏狀態、專案別名是
**唯一來源**，不是任何東西的衍生物，所以刻意跟索引分成兩個 DB。
（索引本身也不可砍掉重建，理由見上一節；加欄位一律走 `db.migrate()` 的
`ALTER TABLE ADD COLUMN`。）
砍索引時只砍 `index.db*`，不要整個 `data/` 刪掉。
（測試若只覆寫 `DATA_DIR` 而忘了 `LABELS_DB_PATH`，會跑去讀寫真正的標籤庫。）

**只綁 127.0.0.1。** 這支服務會把本機所有開發歷程透過 HTTP 吐出來。

**驗證破壞性功能要用隔離實例，不要拿真實資料試。** 三個路徑都可用環境變數覆寫：

```bash
CCSM_CLAUDE_HOME=<假的 .claude>  CCSM_DATA_DIR=<暫存>  CCSM_ARCHIVE_DIR=<暫存>   .venv/Scripts/python.exe -m src.cli serve --port 8799
```

## 踩過的雷

- **FTS5 trigram 查詢詞必須 >= 3 字元。** 中文兩字詞（例如「中文」）會回 0 筆而不是報錯，
  看起來像索引壞了。所以查詢詞按長度分流：>= 3 字走 FTS，1-2 字走 LIKE 全表掃描
  （約 50-100ms，可接受）。UI 會標示走了慢速路徑。
- **thinking 區塊多數只存簽章、文字為空。** 實測抽樣 300 筆全部如此。
  這些訊息沒有可索引的內容，但仍帶 `usage`，所以 message 列必須保留，否則 token 統計會少算。
- **空區塊在索引時被丟掉，還原完整內容時位置會偏。** 例如「空 thinking + 文字」的訊息，
  索引裡只有 1 個 block，若按原始 content 的索引去取就會抓到那個空 thinking。
  解法是還原時走跟索引時同一條 `blocks_from_content()`，只差不截斷。
- **subagent 的工作內容在獨立檔案裡。** 只掃 `<slug>/*.jsonl` 會漏掉 194 個逐字稿、
  約 27MB 的實際工作記錄。
- **Claude Code 會自己刪 session。** 預設保留 30 天（`cleanupPeriodDays`），
  啟動時執行。2026-09-03 實測一次消失 140 個檔案／36.8MB。
  詳見 [docs/known-issues/claude-code-session-retention.md](docs/known-issues/claude-code-session-retention.md)。
- **jsonl 有很大一部分是 base64 圖片。** 貼進對話的截圖存在
  `message.content[].source.data`，實測最大的 session 有 88% 位元組是這種資料。
  所以歸檔用 lzma 不用 gzip（27% vs 47%）。
  另外 parser 目前不處理圖片區塊，只有截圖的訊息在 UI 上會是空的。
- **SQLite 的 `ORDER BY` 預設大小寫敏感。** 專案 slug 大小寫混雜（8 個 `D--`、
  34 個 `d--`，因為 Windows 檔案系統不分大小寫、目錄名由誰先建誰決定），
  直接排會變成兩坨。要 `COLLATE NOCASE`（SQL）或 `casefold()`（Python）。
- **VS Code / Desktop 的 Pin / Archive / Group 狀態不在 `~/.claude/`。**
  它們存在 VS Code 擴充的 globalState（`%APPDATA%\Code\User\globalStorage\state.vscdb`，
  key = `Anthropic.claude-code`）。**Archive 只是一個 id 清單，不刪檔** ——
  跟本工具的「隱藏」是同一件事，只是各存各的。目前兩邊不同步。
  完整結構、風險與後續規劃見
  [docs/known-issues/vscode-extension-session-state.md](docs/known-issues/vscode-extension-session-state.md)。
- **Claude Code 自己的改名是 append 一筆 `custom-title` 記錄。** 不是沒有機制，
  也不需要改寫既有內容 —— 同一個 session 會累積很多筆，最後一筆勝出
  （實測某個有 46 筆，因為編輯標題時逐步存檔）。**漏讀這個 type 會讓
  只有 custom-title 的 session 顯示成「(無標題)」**，本機實測有 4 個。
  顯示優先序：本工具的 `label_title` > `custom-title` > `ai-title` > `last-prompt`。
  完整的 record type 盤點見
  [docs/known-issues/session-jsonl-record-types.md](docs/known-issues/session-jsonl-record-types.md)。
- **含中文的 `.ps1` 必須帶 BOM，且要釘死子行程的編碼。** `PYTHONIOENCODING=utf-8`
  加 `[Console]::OutputEncoding` 兩端都設，否則 Python 印的中文在日誌裡是亂碼。
- **`$PSScriptRoot` 不能用在 `param()` 的預設值。** 5.1 繫結參數時它還沒填好，
  會拿到空字串，錯誤訊息是 `ParameterArgumentValidationErrorEmptyStringNotAllowed`，
  看不出真正原因。要在主體裡推導。
- **venv 的 `python.exe` / `pythonw.exe` 是啟動器 stub。** 它會再開一個系統 Python
  的子行程，**實際在聽 port 的是子行程**，`Start-Process -PassThru` 拿到的 PID 是 stub。
  所以停止服務要靠 `Get-NetTCPConnection` 找出真正的 listener，不能只信 pid 檔。
  （目前殺 stub 剛好也會帶走子行程，但那是 job object 的副作用，不該依賴。）
  **兩者行為一致** —— 實測 `python.exe` 也一樣有 stub 層，換掉不會解決這件事。
  用 `-Status` 可以直接看到這個結構。
- **PowerShell 會把單元素陣列拆成純物件。** `CimInstance` 沒有 `Count` 屬性，
  於是 `$listeners.Count -gt 0` 變成 `$null -gt 0` = false，整段顯示被靜默跳過。
  凡是要看 `.Count` 的地方都得先用 `@()` 包住。
- **`pythonw.exe` 沒有可用的 stdout。** 不重導向到檔案的話，服務起不來時錯誤會完全
  消失，只看得到「開不起來」。`open-ui.ps1` 一律把 stdout/stderr 寫進 `logs/`。
- **`.cmd` 必須是純 ASCII + CRLF。** 兩者任一違反，`cmd.exe` 都會把 `rem` 註解
  當成指令執行，跳出「'pen' 不是內部或外部命令」這種完全看不出原因的訊息：
  LF 行尾讓它解析錯亂，UTF-8 中文則因為 cp950 的 lead byte 吃掉後續位元組而把
  `rem` 推離行首。**檔名可以是中文**（檔案系統用 UTF-16），但內容不行 ——
  要引用自己的檔名用 `%~nx0`。已用 `tests/test_script_encoding.py` 釘住。
- **啟動器在「輸出被管線捕捉」時會掛住。** `$out = & cmd.exe /c "Session管理.cmd"`
  或 bash 的 `| tail` 都會一直等 pipe 關閉。`CreateProcess` 在
  `bInheritHandles=TRUE` 時會繼承**所有**可繼承 handle（不只 stdio），
  而 venv 的 python stub 又把它們傳給子行程；實測把三個標準流都導掉仍無效。
  **這是刻意不修的已知限制** —— 要真正解決得改用 WMI 建立行程，會失去
  `-RedirectStandardOutput` 與 `HasExited` 檢查。雙擊與工作排程器都不捕捉輸出，
  不受影響；在腳本裡呼叫請把輸出重導向到檔案再讀檔。

## 模組

| 檔案 | 職責 |
|---|---|
| [src/parser.py](src/parser.py) | jsonl 容錯解析（不完整尾行、schema 漂移） |
| [src/redact.py](src/redact.py) | 機密遮罩 |
| [src/indexer.py](src/indexer.py) | 掃描與增量匯入 |
| [src/db.py](src/db.py) | SQLite schema |
| [src/search.py](src/search.py) | trigram + LIKE 分流搜尋 |
| [src/browse.py](src/browse.py) | session 清單、對話還原、resume 指令 |
| [src/retention.py](src/retention.py) | 保留期計算（還剩幾天被清掉） |
| [src/labels.py](src/labels.py) | session 更名/隱藏、專案別名（獨立 DB） |
| [src/archive.py](src/archive.py) | 原始檔歸檔、還原、驗證 |
| [src/liveness.py](src/liveness.py) | 「這個 session 是不是進行中」的共用判斷 |
| [src/rename.py](src/rename.py) | 真改名（append 到原始 jsonl） |
| [src/purge.py](src/purge.py) | 移除「原始檔已消失」的記錄 |
| [src/api.py](src/api.py) | FastAPI |
| [src/static/app.html](src/static/app.html) | 單一自包含前端 |
| [scripts/daily-archive.ps1](scripts/daily-archive.ps1) | 排程執行的歸檔 + 索引 |
| [scripts/register-task.ps1](scripts/register-task.ps1) | 註冊/移除工作排程 |
| [scripts/open-ui.ps1](scripts/open-ui.ps1) | 冪等啟動器（重用已在跑的服務） |
| [Session管理.cmd](Session管理.cmd) | 雙擊入口 |
