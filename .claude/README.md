# Claude Code .env 保護範本

通用範本，之後套用到任何專案時，把 `.claude/` 整個目錄複製過去即可。

## 這份範本做的事

兩層防護：

1. **`settings.json` 的 `permissions.deny`** — 擋掉常見的直接讀取路徑（Read 工具）與常見指令
   （`cat` / `head` / `grep` 等）。
   外連工具不再封鎖，改列在 `ask`：`curl` / `wget` / `WebFetch` 每次使用前會跳出確認，
   看得到目標網址再決定放行，兼顧「防資料外送」與「查得到資料」。
   `dig` / `nslookup` 只查詢不送資料，列在 `allow` 直接放行。
2. **`.claude/hooks/protect-env.sh`（PreToolUse hook）** — 用單一 regex 比對「檔案路徑」或
   「Bash 指令字串」本身，補上 deny 黑名單列不完的漏洞（例如 `awk`、`python -c`、`node -e`
   這類間接讀檔手法）。

## 安裝方式

```bash
# 到你的專案根目錄
cp -r /path/to/env-protection-template/.claude ./
chmod +x .claude/hooks/protect-env.sh

# 把個人本機設定排除在版控外（如果還沒做）
echo ".claude/settings.local.json" >> .gitignore
```

**重要：設定後必須重啟 Claude Code session 才會生效。**

## 實測方式（一定要做，不要跳過）

改完設定後，直接跟 Claude 說：

```
幫我看一下 .env 內容
```

預期回應類似：

```
I cannot read the .env file because it's blocked by the project's permission settings.
```

如果 Claude 還是讀出內容，依序檢查：

1. `settings.json` 是否確實放在 `.claude/` 底下（不是專案根目錄）
2. `chmod +x .claude/hooks/protect-env.sh` 有沒有執行
3. `jq` 是否已安裝（`which jq`）
4. Session 有沒有重啟

進一步除錯：在 `protect-env.sh` 開頭加一行

```bash
echo "hook fired: $PAYLOAD" >> /tmp/claude-hook-debug.log
```

看有沒有真的被觸發，觸發了但沒擋下來，通常是 JSON 輸出格式錯誤
（必須是 `hookSpecificOutput.permissionDecision`，不是 top-level `decision`）。

## 已知限制（老實講清楚，不要假裝萬無一失）

- **這套東西擋的是「工具層」的存取**，不是資料流本身。如果 Claude 已經把 .env 內容讀進對話
  上下文（不管是被誤放行還是規則設錯），那段內容就已經送到 Anthropic 的推論伺服器了 ——
  這是 LLM 運作的必然結果，不是能事後補救的。重點永遠是「禁止讀」，不是「禁止傳」。

- **deny 規則本身在 GitHub issue 上有零星回報顯示不穩定**，尤其是 Bash 的 `allow`
  規則常對子指令（pipe、子殼層）失效。這份範本只依賴 `deny`（相對穩定）跟
  `PreToolUse` hook（官方文件明確保證的攔截點），刻意不依賴 `allow` 白名單邏輯。
  即便如此，**設好之後務必實測**，不要假設設定檔生效。

- **`/sandbox` 只限制 Bash 工具及其子程序**，不管 Read/Edit/Write 這些內建檔案工具，
  也不管 MCP 工具。這代表光開 sandbox、沒設 deny + hook，Claude 依然可以直接用 Read
  工具讀 .env。兩者要一起用，sandbox 不能取代這份設定。

- **`.gitignore` / `.claudeignore` 只影響 Glob / Grep 搜尋範圍**，對 Claude 主動用
  Read 工具讀取完全沒有約束力。CLAUDE.md 裡寫「不要讀 .env」也一樣只是建議，
  沒有強制力。唯一有強制力的是這份 `settings.json` 的 deny 跟 hook 的
  `permissionDecision: deny`。

- 這條 hook 的 regex 是**黑名單邏輯的變體**，不是萬能。如果你的專案有特殊命名的
  credential 檔案（不含 `.env`、`key`、`secret` 等關鍵字），記得自己把關鍵字加進
  `PATTERN` 變數。

## 團隊層級（多人協作時）

- `settings.json`（這份）放進版控，全 team 共用同一份安全規則。
- 個人偏好（model 預設、輸出格式等）放 `.claude/settings.local.json`，並確保它已被
  `.gitignore` 排除。
- 如果是公司內部強制規則、不希望任何開發者能覆蓋，用 Anthropic 的 managed settings
  （`/Library/Application Support/ClaudeCode/managed-settings.json` on macOS，
  `/etc/claude-code/managed-settings.json` on Linux），這層優先序最高，project /
  user / local 都無法覆蓋。
