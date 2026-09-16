# VS Code / Desktop 的 session 管理狀態存在哪裡

**調查日期**：2026-09-04（本機實測，非推測）
**觸發原因**：VS Code 的 SESSION MANAGER 面板有 Pin / Archive / Rename / Fork /
Move to group / Mark as unread / Delete，需要確認這些狀態的來源，
才知道本工具該「讀它」還是「自己發明一套」。

## 結論：不在 `~/.claude/`，在 VS Code 擴充的 globalState

```
%APPDATA%\Code\User\globalStorage\state.vscdb
  → 資料表 ItemTable，key = 'Anthropic.claude-code'（value 是 JSON 字串）
```

實測到的相關欄位：

```json
{
  "hiddenSessionIds": ["69b656e2-…", "c35bcf33-…"],   // 共 9 個
  "sessionGroups:d:\\path\\to\\project": [
     {"id": "326c2a0f-…", "name": "New group",
      "collapsed": false, "sessionIds": []}
  ],
  "defaultPermissionMode": "auto",
  "extensionUpdateCheck": {"version": "2.1.259"}
}
```

群組的 key 是 `sessionGroups:<專案絕對路徑>`，**per-project**。

## 各功能的機制對照

| 選單項目 | 機制 | 查證狀態 |
|---|---|---|
| Rename | append `custom-title` 到 jsonl | ✅ 已查證，本工具已實作 |
| Archive | 把 id 加進 `hiddenSessionIds`，**不刪檔** | ✅ 實測 9 個 id 中 8 個原始檔仍存在 |
| Move to group | `sessionGroups:<路徑>` 陣列 | ✅ 結構已知 |
| Pin | 推測 `pinnedSessionIds` | ⚠️ **未證實** —— 使用者沒點過，沒有 key |
| Mark as unread | 推測類似清單 | ⚠️ **未證實** |
| Delete | 真刪 jsonl | 未直接驗證（不拿真實資料試） |
| Fork / Open in | 動作，不是狀態 | — |

**Pin 與 unread 的 key 名稱是推測，不要照著寫。** 在 VS Code 實際點一次
再回來看 `state.vscdb` 就知道真名 —— 上次 `custom-title` 的教訓就是猜錯的代價。

## 其他相關檔案（查過，沒有這些狀態）

- `~/.claude.json` —— 只有 feature flag（`tengu_bridge_unarchive_on_resume`、
  `teardown_archive_timeout_ms`，正是這些名字透露 archive 屬於「bridge」也就是
  編輯器擴充的概念）與 project 層級設定，**沒有** session 的 pin/archive/group
- `%APPDATA%\Code\User\globalStorage\agent-host.db` —— VS Code 統一的 agent
  session 登錄簿。`sessions` 表 90 筆，欄位只有
  `session_uri`（`claude:/<id>`）、`provider`、`start_time`、`external`、
  `registration_source`、`modified_time`。**只記「有哪些 session」，沒有狀態**
- `%APPDATA%\Code\User\globalStorage\agent-host-config.json` —— VS Code 的
  agent host 設定（含 `claudeMultiRootEnabled`、`showExternalSessions`），
  對應面板的 Local / Web 分頁
- `workspaceStorage/*/state.vscdb` —— 只有 webview 的 UI 狀態
  （目前開著哪個 sessionID、是不是全螢幕），沒有管理狀態

## 目前存在的落差

使用者在 VS Code archive 掉 9 個 session（多為同一個專案的
「專案檔案數統計」「目錄檔案計數」等測試對話），**本工具仍會列出它們** ——
因為兩邊的隱藏清單各存各的。

## 風險：這是內部實作，不是公開介面

- **只能唯讀。** VS Code 執行中 `state.vscdb` 有鎖；而且寫壞會影響整個編輯器的
  狀態，不只是這個擴充。
- **格式隨時會變。** 讀取端必須容錯：DB 開不了、key 不存在、JSON 變形、
  欄位改名，全部安靜退回「沒有這個資訊」，絕不能讓主功能掛掉。
- 讀取方式（實測可行）：
  ```python
  sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True)
  ```

## 後續可做的調整（依價值排序，均未實作）

1. **同步 Archive 狀態**（唯讀）—— 讀 `hiddenSessionIds` 併進本工具的隱藏判斷。
   零風險、工作量小、現在就有實際效果（9 個會立刻消失）。
   UI 要區分來源：「本工具隱藏」vs「VS Code 已封存」，兩者獨立可各自切換。
2. **讀取 sessionGroups** —— 按群組摺疊顯示。目前只有一個空群組，
   做了看不到效果，等實際分組後再說。
3. **Pin** —— 先在 VS Code 點一次確認 key 名稱，再實作。
4. **不做**：寫入 `state.vscdb`（風險遠大於效益）、Fork（屬於 Claude Code 的
   執行職責）、Mark as unread（對事後檢視的工具沒有意義）。
