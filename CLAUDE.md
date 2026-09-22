@AGENTS.md

## Claude Code 專屬

- **寫或改 `.ps1` 前載入 `ps1-authoring` skill**。本專案的 `scripts\open-ui.ps1`
  必須是 UTF-8 **帶 BOM**（有中文輸出），並由 `tests/test_script_encoding.py` 釘住。
- **改 `Session管理.cmd` 前先看 AGENTS.md 的「專案特有的陷阱」** —— 純 ASCII + CRLF，
  違反會出現看不出原因的 `'pen' 不是內部或外部命令`。
- **用 Edit 工具改 `.ps1` / `.cmd`，不要用 `sed` / `awk`** —— Git Bash 的 sed 會
  無警告把 CRLF 吃成 LF，也會砸掉 BOM。
- 本專案沒有 Big5／CP950 舊檔，不需要 `big5-edit`。
- 收工用 `shutdown` skill 更新 `handoff.md` 與 Obsidian workbench 的工作紀錄。
