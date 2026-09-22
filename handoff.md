# 交接檔

> 任何 agent、任何時間接手前**必讀**；收工時**必更新**。
> 只放交接必需的精簡資訊，詳細脈絡在 vault 的 `workbench/claudecode-sessions-manager.md`。
> 專案藍圖（資料流、三條紅線、陷阱清單）在 `AGENTS.md`。

## ⏯️ 目前做到哪

修掉「UI 資料停在 9/17」，並補齊專案基礎建設（`AGENTS.md`、本檔、Obsidian workbench 註冊）。
功能程式碼的邏輯沒動，只改了啟動器 `scripts/open-ui.ps1`。

## 🚦 目前狀態

可運行，沒有做一半的東西。測試 117 項全綠。索引已補掃到 2026-09-22（sessions 179 → 183）。

## ➡️ 下一步

1. **實地驗一次啟動器新行為**：服務開著時雙擊 `Session管理.cmd`，應印出
   「更新索引中…」＋ `索引已更新：掃 N、重建 N、追加 N、略過 N`。
   （本次只跑了單元測試與直接打 API，沒有實際雙擊走過 PowerShell 那條路。）
2. `AGENTS.md` 的「這個專案要解決什麼」細節與「關鍵時程」留白，等老大自己填。
3. 功能面待決定事項見 `docs/TODO.md`。**注意**：移動專案／刪除還存在的原始檔
   是評估後**決定不做**，不是待辦，不要順手實作。

## ⚠️ 注意事項

- **服務運行中要更新索引，走 `POST /api/reindex` 讓服務自己寫**，不要另起 CLI
  行程同時寫同一個 DB。
- **`index.db` 的 mtime 會騙人** —— WAL 模式下服務只讀也會動到 `-shm`。
  判斷索引新不新要查 `sessions.indexed_at`，不要看檔案時間。這次就是靠這點才定位到
  「索引自 9/18 12:30 後沒再跑過」。
- 改 `Session管理.cmd` 前記得：純 ASCII + CRLF；`.ps1` 則要 UTF-8 帶 BOM。
  兩者由 `tests/test_script_encoding.py` 釘住，用 Edit 工具改，不要用 sed。

## 🕐 最後更新

- 時間：2026-09-22 09:37
- 更新者：Claude Code（Opus 5）
- Git push：✅ 已推
