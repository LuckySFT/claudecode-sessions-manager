# 待辦

> 目前狀態：**測試觀察中**（2026-09-04 起）。
> 下面「待決定」的項目都先不動手，等老大用一陣子有實際手感再調整。

---

# 待決定

## 同步 VS Code 的 session 管理狀態

已查明 VS Code 擴充的 Pin / Archive / Group 狀態全部存在
`%APPDATA%\Code\User\globalStorage\state.vscdb`（key = `Anthropic.claude-code`），
**不在 `~/.claude/`**。完整結構與風險見
[known-issues/vscode-extension-session-state.md](known-issues/vscode-extension-session-state.md)。

**現在就存在的落差**：老大在 VS Code archive 掉 9 個 session，本工具仍然列出它們
（兩邊的隱藏清單各存各的）。

| 優先 | 項目 | 說明 |
|---|---|---|
| 1 | 同步 Archive 狀態 | 讀 `hiddenSessionIds` 併進本工具的隱藏判斷。唯讀、零風險、工作量小。UI 要區分「本工具隱藏」與「VS Code 已封存」 |
| 2 | 讀取 sessionGroups | 按群組摺疊顯示。目前只有一個空群組，做了看不到效果，等實際分組後再說 |
| 3 | Pin | 推測是 `pinnedSessionIds`，但**沒有證據** —— 請先在 VS Code 點一次 Pin，確認 key 真名再實作 |

**不做**：寫入 `state.vscdb`（VS Code 執行中有鎖，寫壞影響整個編輯器）、
Fork（屬於 Claude Code 的執行職責）、Mark as unread（對事後檢視的工具沒意義）。

## session 真刪除（刪原始檔）

老大已決定**不做**，交給 Claude Code 自己的保留期機制。保留評估結論備查：

| 語意 | 可逆 | 狀態 |
|---|---|---|
| 隱藏（清畫面雜訊） | 完全可逆 | ✅ 已完成 |
| 移除已消失的記錄 | 視深度而定 | ✅ 已完成 |
| 刪還存在的原始檔 | 靠歸檔可還原 | **決定不做** |

若哪天要做，必須包含：先確認已歸檔且驗證通過才允許刪；
**擋進行中的 session**（實測進行中的 jsonl 沒有被獨占鎖住，可以正常刪除，
會無聲砸掉正在進行的對話而 Claude Code 不會報錯，必須用 mtime 判斷）；
徹底清除只給 CLI，不放 UI 按鈕。

## 移動專案（評估後建議不做）

slug 取自 Claude Code **啟動時的目錄**，不是 `cwd` 的函數 —— 實測某個 slug
一個 slug 底下掛著四個不同的 cwd（專案根、其大小寫變體、
以及其下兩層不同的子目錄），
反向也成立（同一個 cwd 出現在兩個 slug 下）。沒有乾淨的對應可以改寫。

真要搬還得動：每筆記錄的 `cwd`（改寫 170MB 原始資料，且歸檔雜湊全失效、
備份體積翻倍）、`~/.claude.json` 的絕對路徑 key、`file-history/`。
而且做完也沒用 —— 下次從新路徑啟動，Claude Code 會直接建一個新 slug。

**已用專案別名合併取代**，拿到九成價值、零風險。

## 歸檔的版本累積

每次歸檔都保留舊版本的 blob（session 是 append-only，內容一變就是新版本）。
好處是完整的版本歷史，代價是**活躍的大 session 每天會多存一份**。
實測 2026-09-03 一天內跑了 4 次，blob 從 287 長到 291、儲存量 47.3MB → 53.5MB。

若體積失控，需要 `archive --prune-versions`：只刪「同一路徑的舊版本」，
且永不刪除當前版本、也永不刪除原始檔已消失者的任何版本。
**尚未實作** —— 先觀察一兩週的成長曲線再決定。

## 圖片內容沒有索引

session jsonl 裡有 `message.content[].source.data`，是貼進對話的截圖以 base64
儲存。實測最大的那個 session 有 88% 的位元組是這種資料。

parser 目前只處理 `text` / `thinking` / `tool_use` / `tool_result` 四種區塊，
**圖片區塊被整個忽略** —— 一則「只有一張截圖」的訊息在 UI 上看起來是空的。
歸檔有完整保存（原始位元組），只是索引與 UI 看不到。

要不要在 transcript 顯示「[圖片 N KB]」佔位，或讓 UI 顯示縮圖，待決定。

## 索引 attribution / effort 欄位

外部審查（`handoff-sessions-manager-improvements.md`）建議的唯一實質新增。
數據已複查（見 [known-issues/session-jsonl-record-types.md](known-issues/session-jsonl-record-types.md)
的「欄位層面的盤點」），可以拿來做「哪個 skill 最貴」「哪些 MCP 沒在用」。

**先不做** —— 現在加了沒有消費端，等做成本報表時一起。屆時注意：
`attributionSkill` 只涵蓋 21/78 個 session（v2.1.187 才開始寫），
UI 要能區分「此版本未記錄」與「沒用 skill」；既有記錄不會自動回填。

那份文件把這些欄位說成在 `user` 記錄上，**實測全部在 `assistant` 上** ——
所以不需要跨記錄 join，同一列就有，實作比它想的簡單。

## 原訂剩餘階段

3. **成本換算報表** —— `cost-state` 記錄裡有官方算好的 `totalCostUSD` 與每模型
   `costUSD`，但全機只有 2 筆（涵蓋 1 個 session），顯然很新才加的。
   仍需按 token 自己算，但可以拿它驗算。
4. **Markdown / HTML 匯出**

---

# 已發生的資料損失（2026-09-03）

**為了加一個 `custom_title` 欄位而砍掉 `index.db` 重建，
銷毀了 140 個「原始檔已被 Claude Code 清掉」的 session 記錄。**

那些記錄是唯一留存 —— 歸檔功能是在 Claude Code 已經清掉那批檔案之後才建的，
所以歸檔沒有它們。損失約 36.8MB 原始資料對應的 7.7MB 索引文字，
包含那個 Delphi `Invalid BLOB handle` 的完整調查。

嘗試過的救援：`~/.claude/history.jsonl` 只留 135 筆、涵蓋 50 個 session，
且都是 2026-05 的早期記錄，與 7 月那批對不上。**無法還原。**

根因：把索引當成純衍生物，忽略了「原始檔消失後索引即唯一來源」這個狀態。
已在 README 的硬規則與 `db.py` 的模組註解記下，並實作 `migrate()` 讓
未來的 schema 變更不需要砍檔。

---

# 已完成

## 2026-09-03 第三批

### session 真改名
- [x] append `custom-title` 到原始 jsonl，Claude Code 的 `/resume` 選單也生效
- [x] 兩層防護：尾端位元組必須是換行、mtime 閒置 >= 10 分鐘
- [x] UI 兩個選項：「寫入 Claude Code」與「僅本工具」，進行中時前者變灰並說明
- [x] `can-rename` 端點讓 UI 先問過再顯示

### 移除已刪除 session 的記錄
- [x] 只處理 `source_missing` 的，原始檔還在的一律不動
- [x] 兩種深度：`index`（歸檔留著可還原）／`full`（連 blob 刪，不可逆）
- [x] 分別統計「有備份」與「無備份」，無備份的移除後就徹底消失
- [x] 預設只預覽；API 要求明確列出 session_ids，不接受條件式全刪
- [x] `vacuum` 指令讓刪除後的空間真的釋出
- [x] 單筆移除：session 詳細頁直接處理一筆，依有無備份給不同選項

### 事故修正
- [x] `db.migrate()`：加欄位改用 `ALTER TABLE ADD COLUMN`，不再砍檔重建

### 啟動器編碼修正（2026-09-04）
- [x] `.cmd` 轉 CRLF + 全 ASCII（用 `%~nx0` 代替寫死的中文檔名）——
      原本 LF 行尾讓 cmd.exe 把 `rem` 註解當指令執行
- [x] `tests/test_script_encoding.py` 釘住 `.ps1` 的 BOM 與 `.cmd` 的
      ASCII/CRLF，這個坑已經踩過兩次

## 2026-09-08

### restore 的進行中防護（外部審查抓到的缺口）
- [x] 抽出 `src/liveness.py`：rename 與 restore 共用同一組「是不是進行中」判斷
      —— 原本這套只有 rename 有，restore 漏掉了
- [x] restore 覆寫既有檔案前檢查閒置時間；`--overwrite` 不含
      「即使進行中也寫」的授權，要再加 `--force`
- [x] 被擋下歸到 `blocked`（跟 `failed` 分開：前者刻意保護、後者出錯）
- [x] 兩種寫入的檢查刻意分開：append 必須查尾端換行，overwrite 不必
      （反正整檔換掉）但危險度更高
- [x] 真實驗證：拿進行中的 session 當目標，`overwrite=True` 被擋、
      12MB 檔案一個位元組未動

### 文件更新
- [x] record type 從 15 種增為 17 種（新增 `artifact-autoreact-ledger`、
      `artifact-comment-monitor`）
- [x] 補上欄位層面的盤點（`effort` / `requestId` / `attributionSkill` /
      `attributionMcp*`），並更正外部文件說它們在 user 記錄上的錯誤 ——
      **實測全部在 assistant 上**

## 2026-09-03 第二批

### 專案排序
- [x] 五種排序：日期／名稱／體積／數量／未備份，選擇存 localStorage
- [x] 名稱排序用 casefold（SQLite 預設大小寫敏感會把 `D--` 全排在 `d--` 前面）

### session 更名 / 隱藏
- [x] 更名（本工具內覆寫，原始 jsonl 不動；清空回到原本的 ai-title）
- [x] 隱藏（三態切換：正常／含隱藏／只看隱藏），同時作用於清單**與搜尋**
- [x] 標籤存在獨立的 `data/labels.db`，砍索引重建不會消失

### 專案別名合併
- [x] 多個 slug 指到同一個顯示名稱就合併成一列（專案搬過目錄時用）
- [x] 篩選與搜尋都會把顯示名稱展開成所有成員 slug

## 2026-09-03 第一批

### UI 調整
- [x] 字體大小可調（10–20px，A− / A+，存 localStorage）
- [x] 淺色模式（淺色／深色／跟隨系統三態，尊重 `prefers-color-scheme`）
- [x] 區塊大小可調（欄寬可拖曳；對話區塊高度四段：矮／中／高／不限）
- [x] 「重設版面」還原所有版面設定

### 資料保全
- [x] 原始 jsonl 歸檔（內容定址 blob store + lzma，只新增不刪除）
- [x] 還原（單一 session／指定路徑／全部；預設不覆蓋；mtime 設為當下）
- [x] 完整性驗證（解壓並重算 SHA-256）
- [x] 消失狀態可見化（總覽統計、專案計數、清單標籤、存活/已消失篩選、
      到期倒數、已備份/未備份標示）
- [x] Windows 工作排程器（每天 12:30 跑歸檔 + 索引）
