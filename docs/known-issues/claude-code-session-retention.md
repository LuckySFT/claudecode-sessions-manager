# Claude Code 會自動刪除舊 session

**發現日期**：2026-09-03（本機實測，非推測）

## 機制

Claude Code 把 session 逐字稿以明文存在 `~/.claude/projects/`，
預設**只保留 30 天**，超過就在啟動時自動刪除。
設定鍵是 `cleanupPeriodDays`，寫在 `settings.json`。本機目前沒設，走預設值。

## 本機實測到的行為

2026-09-03 掃描時，磁碟上的 jsonl 從先前索引到的 283 個掉到 143 個，
被刪掉 140 個、約 35 MB。

切點是 **2026-08-04**（掃描日往回推 30 天），與預設值完全吻合：

| 檔案 mtime | 結果 |
|---|---|
| 2026-08-05 之後 | 全部存活 |
| 2026-08-03 以前 | 全部刪除（兩個例外見下） |

**附屬目錄跟著主 session 一起生死。** 被刪的 116 個 subagent 逐字稿，
100% 都是父 session 也被刪的。反過來，父 session 若最近有活動，
它底下的 subagent 就算本身很舊（實測 mtime 2026-07-20，父 session mtime 2026-09-02）
也會一併存活。所以清理是以 `<sessionId>.jsonl` 的年齡為準，
`<sessionId>/`（subagents、tool-results、memory）整個目錄跟著父檔走。

**一個無法用 30 天規則解釋的例外**：
`<某專案 slug>/<session-id>.jsonl`，mtime 2026-07-31（34 天），
卻存活下來，而 mtime 2026-08-03 的三個檔案反而被刪了。
不確定原因（可能是清理為 best-effort、或當時檔案被鎖）。
不要把清理當成精準可預期的行為。

## 對本工具的影響（重要）

索引裡保存的是**當時掃描到的內容**，Claude Code 刪掉原始檔之後，
索引裡那份就是**唯一留存的記錄**。目前 283 個 session 中有 140 個處於這個狀態。

因此：

- **`prune_missing()` 是破壞性操作。** 它會把「原始檔已消失」的 session 從索引刪掉，
  在上述情況下等於銷毀唯一副本。
  已將 `POST /api/reindex` 的 prune 預設改為關閉，UI 不提供這個開關。
  CLI 的 `index --prune` 仍保留，但不要隨手用。
- 這類 session 無法 resume，被截斷的區塊也無法再載入完整內容
  （完整內容是回原始 jsonl 撈的）。UI 與 API 會以 `source_missing` 標示。
- **第 5 階段「壓縮歸檔」的定位因此翻轉**：重點不是清磁碟省空間，
  而是趕在保留期把檔案刪掉之前先完整保存下來。

## 若要保留更久

在 `~/.claude/settings.json` 加：

```json
{ "cleanupPeriodDays": 365 }
```

代價是磁碟占用會持續累積，且這些是**明文**逐字稿。

## 來源

- [Data usage - Claude Code Docs](https://docs.anthropic.com/en/docs/claude-code/data-usage)
