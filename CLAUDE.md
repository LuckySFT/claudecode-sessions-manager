# claudecode-sessions-manager

Claude Code 每開一次對話就寫一個 jsonl 到 `~/.claude/projects/<專案 slug>/`。
本工具把那些 jsonl 掃成 SQLite 全文索引、用本機 Web UI 瀏覽與搜尋、可改名，
並把原始檔壓縮備份 —— **因為 Claude Code 會按保留期自動刪掉它們**。

環境：Windows 11 + Python venv，FastAPI + 單一自包含 HTML（原生 JS，無建置流程）。

```powershell
Session管理.cmd                                          # 日常入口：雙擊即可（起服務 + 開瀏覽器）
.venv\Scripts\python.exe -m src.cli index                # 增量建索引
.venv\Scripts\python.exe -m unittest discover -s tests   # 測試（目前 111 項）
```

## 資料怎麼流

```
【別人的資料，唯讀】
~/.claude/projects/<slug>/
    <sessionId>.jsonl                    主對話，Claude Code 持續 append
    <sessionId>/subagents/agent-*.jsonl  subagent 的工作記錄（獨立檔案，容易漏掃）
                 │
                 │  indexer 掃描 → parser 切區塊 → redact 遮罩機密
                 ▼
【我們的產出】
data/index.db          session / message / block + FTS5(trigram) 全文
data/labels.db         使用者手打的標題、隱藏狀態、專案別名 ← 唯一來源，非衍生物
archive/blobs/         原始 jsonl 的 lzma 備份（內容定址、只新增不刪）
archive/manifest.db    哪個路徑的哪個版本對應哪顆 blob
                 │
                 ▼
api (FastAPI) + src/static/  →  http://127.0.0.1:8787
```

模組職責（只列容易找錯地方的）：

| 想改什麼 | 去哪 |
|---|---|
| 掃檔、決定哪些要重新索引 | `indexer.py` + `retention.py`（判斷原始檔是否已消失） |
| jsonl 怎麼切成可索引區塊 | `parser.py`（索引與還原**共用**同一條，見下） |
| 查詢、清單、標題優先序 | `search.py` / `browse.py` |
| **會寫入 `~/.claude/` 的只有兩支** | `rename.py`、`archive.py` 的 `restore()` |
| 「是不是進行中」的判斷 | `liveness.py`（上面兩支共用，不要自己重寫） |

## 三個前提，決定了後面所有規則

1. **原始檔是別的程式的**：Claude Code 隨時在 append，並依 `cleanupPeriodDays`
   （預設 30 天）自動刪除。我們只是旁觀者，不是擁有者。
2. **原始檔被刪掉後，索引裡那份就是唯一留存** —— 所以索引不是可拋棄的衍生物。
3. **進行中的 jsonl 並沒有被獨占鎖住**：可以正常覆寫與刪除，而且 Claude Code
   那端不會報錯，會無聲砸掉正在進行的對話。任何寫入都得自己判斷。

---

## 三條紅線

### 1. 絕對不要砍 `data/index.db` 重建

索引看起來是可拋棄的衍生物，但它有一種內容是**唯一留存**的：Claude Code 的
保留期（`cleanupPeriodDays`，預設 30 天）刪掉原始 jsonl 之後，索引裡那份記錄
就沒有別的來源了 —— 歸檔只涵蓋「歸檔功能建立之後還存在」的檔案。

2026-09-03 為了加一個欄位而砍檔重建，**銷毀了 140 個這種 session 的記錄，
無法還原**。加欄位一律走 `db.migrate()` 的 `ALTER TABLE ADD COLUMN`。

### 2. 原始資料唯讀，只有兩個例外，都必須過 `liveness` 檢查

`~/.claude/` 底下不改寫、不刪除。兩個例外：
- **rename**：往主 session 的 jsonl 檔尾 append 一筆 `custom-title`（Claude Code
  官方自己的機制）→ `liveness.check_appendable()`
- **restore 覆寫既有檔案** → `liveness.check_overwritable()`

任何新的寫入路徑都必須先過 `liveness`（理由見前提 3），**不要自己重寫判斷**
—— 這套原本只有 rename 有、restore 漏掉，已經漏過一次。

兩個檢查刻意不同：append 必須查尾端是否為換行（沒寫完就 append 會把兩行黏成
一行），overwrite 不必查（反正整檔換掉）但危險度更高，所以 `--overwrite`
不含「即使進行中也寫」的授權，要再加 `--force`。

### 3. `data/labels.db` 與 `index.db` 是兩個 DB，不要一起砍

`labels.db` 存使用者手打的標題、隱藏狀態、專案別名 —— 那些是**唯一來源**，
不是衍生物。砍索引時只砍 `index.db*`。

---

## 驗證破壞性功能：用隔離實例，不要拿真實資料試

三個路徑都可用環境變數覆寫：

```bash
CCSM_CLAUDE_HOME=<假的 .claude>  CCSM_DATA_DIR=<暫存>  CCSM_ARCHIVE_DIR=<暫存> \
  .venv/Scripts/python.exe -m src.cli serve --port 8799
```

**測試若只覆寫 `DATA_DIR` 而忘了 `LABELS_DB_PATH`，會跑去讀寫真正的標籤庫** ——
那是 import 時就從 `DATA_DIR` 算好的常數，不會連動。

---

## 刻意如此，不要順手「修掉」

- **`thinking` 區塊多數文字為空、只存簽章**（抽樣 300 筆全部如此）。這些 message
  沒有可索引內容，但仍帶 `usage`，**必須保留 message 列**否則 token 統計會少算。
  看到「有 message 沒有 block」不是 bug。
- **空區塊在索引時被丟掉**，所以還原完整內容時**必須**走跟索引時同一條
  `parser.blocks_from_content()`（只差不截斷）。自己按原始 content 的索引去取
  會抓到隔壁那段。
- **啟動器在「輸出被管線捕捉」時會掛住**（`$out = & cmd.exe /c "..."`、bash 的
  `| tail`）。`CreateProcess` 在 `bInheritHandles=TRUE` 時會繼承所有可繼承
  handle，而 venv 的 python 是 stub 又傳給子行程；實測導掉三個標準流仍無效。
  **不修** —— 要解決得改用 WMI 建行程，會失去 `-RedirectStandardOutput`
  與 `HasExited`。雙擊與工作排程器都不受影響；腳本裡呼叫請重導向到檔案再讀。
- **Claude Code 自己的 scratchpad 不納管**（`config.EXCLUDE_SLUG_RE`）。
  `%TEMP%\claude\<專案>\<session-id>\scratchpad\` 底下啟動的 session，會被
  Claude Code 記成一個獨立「專案」，slug 長到佔滿整個清單，內容實測都是 skill eval
  的測試探針。**這是全專案唯一「主動放棄留存」的地方**，跟紅線 1 的精神相反，
  所以刻意做成可用環境變數 `CCSM_EXCLUDE_SLUG_RE` 關掉（設空字串即全部納管），
  並由 `tests/test_core.py::TestScratchpadExclusion` 正反兩面釘住 —— 該排的要排掉，
  真的叫 scratchpad 的專案一個都不能誤殺。看到這類 session 沒被索引不是 bug。
- **移動專案 / 刪除還存在的原始檔**：評估後決定不做，理由見 `docs/TODO.md`。
- **VS Code 的 Pin 與 Mark-as-unread 的 key 名稱是推測，沒有證據** ——
  不要憑推測寫。要做先實際點一次再去 `state.vscdb` 看真名。

---

## 專案特有的陷阱

- **`.cmd` 必須純 ASCII + CRLF**。任一違反，`cmd.exe` 都會把 `rem` 註解當指令
  執行，跳出「'pen' 不是內部或外部命令」這種看不出原因的訊息（LF 讓它解析錯亂；
  UTF-8 中文則因 cp950 的 lead byte 吃掉後續位元組而把 `rem` 推離行首）。
  **檔名可以是中文**，內容不行 —— 引用自己的檔名用 `%~nx0`。
  已用 `tests/test_script_encoding.py` 釘住，連 `.ps1` 的 BOM 一起檢查。
- **FTS5 trigram 查詢詞必須 >= 3 字元**。中文兩字詞會回 **0 筆而不是報錯**，
  看起來像索引壞了。所以按長度分流：>= 3 字走 FTS，1-2 字走 LIKE 全表掃描。
- **SQLite 的 `ORDER BY` 預設大小寫敏感**，而專案 slug 大小寫混雜（Windows
  檔案系統不分大小寫，目錄名由誰先建誰決定），直接排會變成兩坨。
  要 `COLLATE NOCASE` 或 `casefold()`。
- **subagent 的工作內容在獨立檔案**（`<sessionId>/subagents/agent-*.jsonl`），
  只掃 `<slug>/*.jsonl` 會漏掉大量實際工作記錄。
- **jsonl 有很大一部分是 base64 圖片**（貼進對話的截圖，最大的 session 佔 88%），
  所以歸檔用 lzma 不用 gzip（27% vs 47%）。parser 目前**不處理圖片區塊**，
  只有截圖的訊息在 UI 上會是空的（待決定，見 TODO）。
- **機密在索引時就遮罩，不是查詢時** —— `index.db` 本身就是產出物。
  代價是遮掉的內容無法從索引還原。

---

## 詳細文件

| 檔案 | 內容 |
|---|---|
| [README.md](README.md) | 使用方式、完整的硬規則與踩雷清單、模組職責 |
| [docs/TODO.md](docs/TODO.md) | 待決定事項、已發生的資料損失記錄 |
| [docs/known-issues/claude-code-session-retention.md](docs/known-issues/claude-code-session-retention.md) | Claude Code 自動刪 session 的機制與實測 |
| [docs/known-issues/session-jsonl-record-types.md](docs/known-issues/session-jsonl-record-types.md) | jsonl 的 17 種 record type 與欄位盤點 |
| [docs/known-issues/vscode-extension-session-state.md](docs/known-issues/vscode-extension-session-state.md) | VS Code 擴充的 Pin/Archive/Group 存在哪裡 |

外部來源的建議（`docs/handoff-*.md`）**數據要自己複查再採用** ——
上一份的欄位位置就寫錯了（說在 user 記錄上，實測全在 assistant 上）。
