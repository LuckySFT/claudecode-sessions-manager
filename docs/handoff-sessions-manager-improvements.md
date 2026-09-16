# 給 claudecode-sessions-manager 的建議（源自 claude-settings 審查）

日期：2026-09-08
來源：審查 `kevintsai1202/claude-settings` v3.4.0（記錄見
[research/2026-09-08-claude-settings-manager.md](research/2026-09-08-claude-settings-manager.md)）
對象專案：本 repo（Python + FastAPI + SQLite，3,448 行）

## 先講結論

**claude-settings 的實作對這個專案幾乎沒有可借鑑之處**——凡是兩邊重疊的面向，
sessions-manager 都做得更深。原本以為可搬的四項，逐一比對後全部落空（見下表）。

這次真正的產出不是「抄它的做法」，而是驗證過程中**盤點出四個尚未索引的 jsonl 欄位**，
其中 `attributionSkill` 與 `effort` 能補上 `otel-local/` 標的拿不到的資訊。

### 原以為可搬、實際已被覆蓋（不要重複研究）

| 原本的想法 | 實際狀況 | 結論 |
| --- | --- | --- |
| 用 jsonl 的 `cwd` 當專案路徑的可靠來源，因為目錄名編碼有損 | `docs/TODO.md`「移動專案」已證明更複雜的事實：slug 取自**啟動時的目錄**，不是 `cwd` 的函數，實測一個 slug 對四個 cwd、反向也成立 | 已知且更深，`cwd` 不是可靠解 |
| 過濾非對話類 record type，避免污染 FTS | `docs/known-issues/session-jsonl-record-types.md`（144 檔實測）已完整盤點 14 種 type，`file-history-*`、`queue-operation` 已跳過 | 已完成，且樣本比本次大 |
| 借鑑 sidechain 分組與 `subagent_type` 抽取 | 做法更好：subagent 逐字稿當**獨立 session** 入同一張表（`kind`、`parent_session_id`、`is_sidechain`），搜尋自然涵蓋 subagent 內做的事。claude-settings 只是 UI 摺疊 | 已完成，且優於對方 |
| 借鑑 CJK 加權的 token 估算 | 本專案直接讀 `usage` 真實 token，另有 `cost-state` 可驗算 | 不需要估算 |
| 借鑑並行 I/O 池上限 8 + 搜尋版本號 ref | 那是前端 Tauri 讀檔的解法；本專案搜尋在 SQLite FTS，伺服器端完成 | 不適用 |
| 借鑑刪除前的路徑前綴檢查 | 本專案防護更嚴（只處理 `source_missing`、API 要求明列 session_ids、rename 有尾端換行 + mtime 閒置 10 分鐘兩層防護） | 已完成，且優於對方 |

---

## 建議一：索引 attribution / effort 欄位（唯一實質新增）

`known-issues/session-jsonl-record-types.md` 盤點的是 **record type**，欄位層面沒盤點過。
本機今日全機實測（78 個 jsonl、41,036 筆記錄、74 個 session）：

| 欄位 | 出現在 | 筆數 | 涉及 session | 最早 `version` |
| --- | --- | ---: | ---: | --- |
| `effort` | assistant | 14,122 | 58 / 74 | 2.1.215 |
| `requestId` | assistant | 14,804 | 74 / 74 | 2.1.149 |
| `attributionSkill` | user | 1,069 | 21 / 74 | 2.1.187 |
| `attributionMcpServer` + `attributionMcpTool` | user | 602 | 8 / 74 | 2.1.149 |
| `cost-state`（type，非欄位） | — | 4 | 2 / 74 | — |

> 掃到 78 檔而非盤點時的 144 檔，是 Claude Code 保留期已清掉一批
> （見 `known-issues/claude-code-session-retention.md`），不是掃描漏檔。
> `cost-state` 從 2 筆長到 4 筆、涵蓋 1 → 2 個 session，**成長極慢**，
> 印證 TODO 裡「不能只靠它、要自己按 token 算」的判斷仍然正確。

實測分佈（可直接當 UI 設計依據）：

```
attributionSkill  top 5 : superpowers:subagent-driven-development 467
                          github-recon 105 / superpowers:brainstorming 93
                          frontend-design 83 / claude-api 71
attributionMcp    top 3 : playwright/browser_take_screenshot 160
                          playwright/browser_evaluate 54
                          chrome-devtools/evaluate_script 50
effort                  : medium 8918 / high 5085 / max 119
```

### 為什麼值得做

`otel-local/` 標的只拿到 token 與 `cost_usd`，**給不出「哪個 skill 觸發了這次花費」**。
`attributionSkill` 在 user 事件上、`effort` 在 assistant 事件上，兩者同屬一個 session，
可以在 SQLite 內直接 join 出：

- 每個 skill 的累計 token / 成本（skill 值不值得留、哪個 skill 最貴）
- effort 分佈與成本的關係（`max` 只有 119 筆，但單筆成本最高）
- 哪些 MCP server / tool 真的在用（`playwright` 214 筆 vs `ccd_session` 20 筆，
  可據此決定要不要停用沒在用的 MCP，直接省 context）

### 實作方式（照本專案既有慣例）

1. **加欄位一律走 `db.migrate()` 的 `ALTER TABLE ADD COLUMN`，絕不砍 `index.db` 重建**
   ——2026-09-03 那次資料損失就是為了加 `custom_title` 砍檔重建造成的，
   原始檔已被清掉的記錄無法還原。
2. `messages` 表加 `effort TEXT`、`request_id TEXT`；
   `attributionSkill` / `attributionMcpServer` / `attributionMcpTool` 建議也放 `messages`
   （它們出現在單筆 user 記錄上，不是 session 級屬性），欄位名 `attr_skill`、
   `attr_mcp_server`、`attr_mcp_tool`。
3. parser 取值放在 `src/parser.py:250` 附近——那裡已在處理 `gitBranch`，同一個
   `rec.get()` 區塊往下加即可，維持「一律 `.get()`、不 assert 結構」的既有原則。
4. 覆蓋率不完整是正常的（`attributionSkill` 只涵蓋 21/74 個 session，因為 v2.1.187
   才開始寫）。**UI 要能區分「這個 session 沒有此欄位」與「這個 session 沒用 skill」**，
   否則舊 session 會看起來像從沒用過 skill。建議用 session 的 `version` 判斷，
   低於起始版本就顯示「此版本未記錄」而非 0。
5. 重新索引只需增量（`cli index` 可反覆執行），但既有記錄不會回填新欄位——
   要回填得對已索引的 session 重跑 parser。這是加欄位時要一併決定的事。

---

## 建議二：從 claude-settings 的缺陷反推，值得順手確認的兩點

這兩點不是它的優點，是它踩的坑；本專案多數已避開，但值得對照確認：

1. **整檔覆寫無 mtime 檢查** — claude-settings 的 `commitLayer` 用啟動時讀到的內容
   覆寫整個檔案，在 Claude Code 執行中儲存會蓋掉這期間的變更。
   本專案的 `rename` 已有兩層防護（尾端位元組必須換行 + mtime 閒置 ≥ 10 分鐘），
   正好是對方缺的那一層。**值得確認 `restore` 是否也有同級防護**——
   還原會寫回 `~/.claude/` 的原始位置，若目標 session 正在進行中，
   後果與 TODO 裡「擋進行中的 session」那條相同。
2. **批次寫入用 `Promise.all`，任一失敗即整體 reject，但先完成的已寫入磁碟**
   → 使用者看到「儲存失敗」，實際是部分成功。
   本專案的 `archive` / `restore --all` 是多檔操作，**值得確認失敗時的回報是否
   會把「部分成功」講清楚**，而不是只報一個總體失敗。

---

## 不建議做的

- **不要為了對齊 claude-settings 而加設定檔管理功能**。那是它的主場（四層
  `settings.json` 合併 + AJV schema 驗證），與 session 管理無關，
  而且它的合併預覽只涵蓋 9 個欄位（型別有 215 個），品質並不值得對齊。
- **不要抄它的 `encodeProjectPath`**。該函式只替換 `: \ /`，遇到含 `.`、空格、
  中文的路徑會算錯（本機實測 `D:\DOC\07.文件\09.AI` 這類路徑，實際 slug 是
  `d--DOC-07----09-AI`，它卻算成 `D--DOC-07.文件-09.AI`）。
  本專案是直接掃 `~/.claude/projects/` 的實際目錄，不做編碼推算，方向本來就對。
