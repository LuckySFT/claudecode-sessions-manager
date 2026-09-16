# session jsonl 的 record type 清單

**盤點日期**：2026-09-03（144 檔）／**2026-09-08 複查**（78 檔、41,092 筆記錄）
檔案數變少是 Claude Code 的保留期清掉了一批，不是掃描漏檔。
複查時 type 從 15 種增為 **17 種**（新增兩個 artifact 相關）。

每一行 jsonl 都是一筆獨立記錄，用 `type` 區分。schema 隨版本變動，
所以 parser 一律用 `.get()` 取值，不 assert 結構。

## 已處理

| type | 筆數 | 用途 |
|---|---|---|
| `assistant` | 12533 | 助理訊息（含 usage） |
| `user` | 7374 | 使用者訊息、tool_result |
| `attachment` | 5047 | 附件（只索引 `type == "file"` 的） |
| `last-prompt` | 2044 | 最後一句 prompt，當標題的最後備援 |
| `ai-title` | 1956 | AI 自動產生的標題 |
| `custom-title` | 74 | **使用者手改的標題**（見下） |
| `file-history-*` | 1160 | rewind 用的檔案快照，跳過 |
| `queue-operation` | 1092 | 佇列操作，跳過 |

## 尚未處理（目前無害，記錄備查）

| type | 筆數 | 內容 |
|---|---|---|
| `mode` | 1150 | `{"mode": "normal"}` |
| `atis-latch` | 751 | `{"atis": ""}` |
| `system` | 186 | hook 執行摘要（`subtype`、`hookErrors`、`stopReason`） |
| `permission-mode` | 62 | `{"permissionMode": "default"}` |
| `cost-state` | 4 | **官方算好的成本**（見下） |
| `artifact-autoreact-ledger` | 10 | Artifact 的自動回覆帳本（2026-09-08 新出現） |
| `artifact-comment-monitor` | 7 | Artifact 的留言監看（2026-09-08 新出現） |
| `frame-link` | 2 | Artifact 連結（`frameUrl`、`path`、`title`） |

## `custom-title`：Claude Code 自己的改名機制

```json
{"type": "custom-title", "customTitle": "Markdown to DOCX converter",
 "sessionId": "8cab7281-..."}
```

只有三個欄位，**沒有 timestamp、沒有 uuid**。

**改名是 append 一筆新記錄，最後一筆勝出** —— 不改寫既有內容。所以同一個
session 會累積很多筆：實測某個 session 有 46 筆、另一個 21 筆。筆數多不是因為
改名那麼多次，而是**編輯標題時逐步存檔**（`建立專案目錄結構&需求單: 115` →
`...11508130001` 是同一次輸入的兩個中間狀態）。

**這推翻了「不改寫原始資料就無法改名」的判斷。** append-only 就能改名，
而且那是 Claude Code 官方自己的做法。

### 漏讀它的後果（已修）

實測本機 7 個 session 帶 `custom-title`，其中 **4 個完全沒有 `ai-title`** ——
漏讀就會顯示成「(無標題)」，而使用者在 Claude Code 裡看到的是
`Top 10 emails`、`Webmail message count` 這些名字。另外 3 個則顯示過期的舊名。

顯示標題的優先序（見 `browse.decorate`）：

```
本工具的 label_title  >  custom-title  >  ai-title  >  last-prompt  >  (無標題)
```

`label_title` 是本工具存在 `labels.db` 的另一層覆寫，不碰 `~/.claude/`。
要讓改名雙向一致（Claude Code 也看得到）就得 append `custom-title` 到原始
jsonl —— 需要先鬆綁「原始資料唯讀」，見 [../TODO.md](../TODO.md)。

## 欄位層面的盤點（2026-09-08 補）

record type 之外，還有幾個**欄位**值得知道。全部出現在 `assistant` 記錄上
（不是 user —— 這點容易搞錯，弄錯就會以為要跨表 join）：

| 欄位 | 筆數 | 涉及 session | 最早 version |
|---|---:|---:|---|
| `requestId` | 14,816 | 74/78 | 2.1.149 |
| `effort` | 14,134 | 58/78 | 2.1.215 |
| `attributionSkill` | 1,069 | 21/78 | 2.1.187 |
| `attributionMcpServer` + `attributionMcpTool` | 602 | 8/78 | 2.1.149 |

`attributionSkill` 的值是 `plugin:skill` 格式（例如
`superpowers:subagent-driven-development`、`frontend-design:frontend-design`），
不是單純的 skill 名稱。`effort` 只有 `medium` / `high` / `max` 三種值。

**目前都沒有索引。** 它們的用途是成本報表（哪個 skill 最貴、哪些 MCP 沒在用），
等做第 3 階段時一起加 —— 現在加了沒有消費端。真要做時注意兩件事：
覆蓋率不完整是正常的（`attributionSkill` 只涵蓋 21/78，因為 v2.1.187 才開始寫），
UI 要能區分「此版本未記錄」與「沒用 skill」；還有既有記錄不會自動回填。

## `cost-state`：官方算好的成本

```json
{"type": "cost-state", "totalCostUSD": ...,
 "modelUsage": {"claude-haiku-4-5-20251001": {
    "inputTokens": 970, "outputTokens": 20,
    "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
    "webSearchRequests": 0, "costUSD": 0.00107}},
 "hasUnknownModelCost": false, "totalAPIDuration": ...,
 "totalLinesAdded": ..., "totalLinesRemoved": ...}
```

**對第 3 階段（成本報表）很有用，但不能只靠它** —— 全機只有 2 筆、涵蓋 1 個
session（共 67 個現存主 session），顯然是很新的版本才開始寫。

做法：仍按 token 自己乘單價算，但拿這些記錄**驗算**自己的算法對不對。
另外 `hasUnknownModelCost` 這個欄位暗示官方自己也會遇到查不到單價的模型。
