#!/bin/bash
# protect-env.sh
# PreToolUse hook：在 Read / Edit / Write / Bash 執行前，
# 檢查目標路徑或指令字串是否碰到敏感檔案，中了就 deny。
#
# 這一層的目的是補 settings.json deny 規則的漏洞——
# deny 是黑名單列舉（cat / head / grep...），
# 遇到 awk、python -c、node -e、ruby -e 這類「間接讀檔」的工具就會漏。
# 這裡用單一 regex 從指令字串或檔案路徑層面比對，繞過空間小很多。
#
# 【為什麼不用 jq】
# 舊版靠 jq 抽欄位，但開發機沒裝 jq 時 jq 會 command not found，
# TARGET 變成空字串 → grep 比不到 → exit 0 → 靜默放行。
# 安全性 hook fail open 且不噴警告是最糟的模式，所以改成純 bash + sed，
# 零外部依賴，並在任何解析不確定的情況下一律往「擋下來」倒（fail closed）。

set -uo pipefail

# 輸出 deny 決策。reason 是本腳本自己控制的靜態字串，不含使用者輸入，
# 所以直接內插進 JSON 是安全的（不需要 jq 幫忙跳脫）。
deny() {
  printf '{\n'
  printf '  "hookSpecificOutput": {\n'
  printf '    "hookEventName": "PreToolUse",\n'
  printf '    "permissionDecision": "deny",\n'
  printf '    "permissionDecisionReason": "Blocked by protect-env hook: %s"\n' "$1"
  printf '  }\n'
  printf '}\n'
  exit 0
}

PAYLOAD=$(cat)

# 收不到 payload 代表 hook 被以非預期方式呼叫，當異常處理直接擋
if [[ -z "$PAYLOAD" ]]; then
  deny "empty hook payload (fail closed)"
fi

# 壓成單行，方便下面用單行 regex 處理
FLAT=$(printf '%s' "$PAYLOAD" | tr '\n' ' ')

# 敏感檔案關鍵字，可依專案自行增補。
# 注意兩件事：
# 1. 這條 regex 只比對「路徑字串」或「bash 指令字串」，不理解語意，
#    所以像是把 .env 內容 echo 進另一個檔名的操作要另外考慮。
# 2. 副檔名一律用 ([^a-zA-Z0-9_]|$) 收尾而不是用 $ 錨定字串結尾——
#    指令字串裡 .pem/.key/.env 後面通常還接著空白或管線
#    （例如 cat foo.pem | head），用 $ 錨定會整個比不到。
#    用 \.env([^a-zA-Z0-9_]|$) 也同時確保 .venv、environment.ts 不會誤中。
PATTERN='\.env([^a-zA-Z0-9_]|$)|id_rsa|id_ed25519|\.pem([^a-zA-Z0-9_]|$)|\.key([^a-zA-Z0-9_]|$)|credentials\.json|secrets/|\.aws/|\.ssh/|\.netrc|_authToken'

if printf '%s' "$FLAT" | grep -qE '"tool_name"[[:space:]]*:[[:space:]]*"Bash"'; then
  # Bash 的 tool_input 只有 command / description / timeout，
  # 不會夾帶大段檔案內容，所以直接拿整包 payload 比對最保險：
  # 完全不解析 JSON，就沒有靠跳脫字元繞過解析的空間。
  TARGET="$FLAT"
else
  # Read / Edit / Write：只取 file_path / path。
  # 不能對整包比對——Write 的 content、Edit 的 new_string 可能合法地
  # 提到 .env（例如寫一份說明文件），那樣會誤擋。
  TARGET=$(printf '%s' "$FLAT" | sed -nE 's/.*"(file_path|path)"[[:space:]]*:[[:space:]]*"([^"]*)".*/\2/p')
  # 抽不到欄位代表 payload 格式和預期不符，退回整包比對，寧可誤擋也不要漏
  if [[ -z "$TARGET" ]]; then
    TARGET="$FLAT"
  fi
fi

# .env.example / .env.sample / .env.template 是範本檔，照慣例不含真實機密，放行。
#
# 【注意：這段目前是休眠的，不是壞掉】
# hook 放行只是「不表態、交回一般權限流程」，接著 settings.json 的
# Bash(cat *.env*) / Read(./.env.*) 會 glob 命中 .env.example 直接擋掉，
# deny 優先序最高且 glob 沒有否定語法，在 settings.json 裡挖不出例外。
# 所以實際行為仍是「範本檔讀不到」——這是刻意接受的取捨，不要「順手修掉」。
# 哪天把 settings.json 的 .env 規則收窄了，這段才會真正生效。
# （不要改成回 permissionDecision: "allow" 來硬繞 deny：那會連 ask 規則一起繞，
#   例如 `cat .env.example && rm -rf /` 會跳過 Bash(rm:*) 的確認。）
# 注意這裡是「從比對目標中剔除」而不是「命中白名單就整包放行」——
# 後者會被 `cat .env && cat .env.example` 這種一條指令同時碰兩個檔的寫法繞過
# （Bash 分支的 TARGET 是整包 payload，白名單一命中就全放了）。
# 剔除法只把範本檔名本身從字串裡拿掉，真正的 .env 仍然會留在字串中被抓到。
SCRUBBED=$(printf '%s' "$TARGET" | sed -E 's/\.env\.(example|sample|template)//g')

if printf '%s' "$SCRUBBED" | grep -qE "$PATTERN"; then
  deny "target matches sensitive file pattern"
fi

# 沒中規則，放行（不輸出等同 allow，交回一般權限流程判斷）
exit 0
