# 交接文件

**建立日期：2026-09-22**　·　**最後更新：2026-09-22 — Claude Opus 5**

> 任何 agent 開工先讀 `AGENTS.md` ＋ 本檔；收工必回來更新本檔。

---

## 目前狀態

專案功能已完整可用（索引、搜尋、瀏覽、改名、歸檔／還原、scratchpad 排除），
測試 117 項。本次做的是**基礎建設補建**，沒有動到功能程式碼的邏輯。

## 本次做了什麼（2026-09-22 — Claude Opus 5）

1. **修掉「資料停在 9/17」** — 不是索引壞掉，是索引自 2026-09-18 12:30 後沒再跑過。
   `serve` 只讀不索引，而日常入口 `Session管理.cmd` 當時預設不帶 `-Reindex`。
   已透過運行中服務的 `POST /api/reindex` 補掃：
   `scanned 183 / rebuilt 4 / appended 3 / skipped 176 / +399 messages / +320 blocks / errors 0`，
   sessions 179 → 183，最新 `last_ts` 到 2026-09-22。
2. **根治**：`scripts/open-ui.ps1` 改成**預設就跑增量索引**，另加 `-NoReindex` 開關。
3. **補建 `AGENTS.md`** — 原 `CLAUDE.md` 的 160 行（資料流、三條紅線、「刻意如此」
   清單、專案特有陷阱）整批移入，再補上目標／路線圖／驗收方式／禁止事項。
   目的是換 Codex／Gemini 接手也讀得到紅線。
4. **`CLAUDE.md` 瘦身**成 `@AGENTS.md` 導入 ＋ Claude 專屬的 skill 提示。
5. **Obsidian workbench 註冊** — 見下節。

## 下一步

- `AGENTS.md` 的「這個專案要解決什麼」目前只有老大給的一句話，要補細節請老大自己寫
  （AI 不得臆測擴寫）。「關鍵時程」留白。
- 功能面的待決定事項在 `docs/TODO.md`（圖片區塊怎麼處理仍未決；移動專案／刪除
  還存在的原始檔已評估後決定**不做**，不要順手實作）。

## 踩過不要再踩

- **服務運行中要更新索引，走 `POST /api/reindex` 讓服務自己寫**，不要另起 CLI 行程
  同時寫同一個 DB。
- **`index.db` 的 mtime 會騙人**：WAL 模式下服務只讀也會動到 `-shm`，
  要判斷索引新不新請查 `sessions.indexed_at`，不要看檔案時間。
