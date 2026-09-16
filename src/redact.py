"""機密遮罩。

【為什麼在索引時就遮，而不是查詢時才遮】
index.db 本身就是產出物。全域規範明訂憑證不得寫入任何產出物，
所以明文不能進 DB —— 查詢時才遮等於 DB 裡已經躺著一份明文副本。
代價是遮掉的內容無法還原；要看原文只能自己去讀原始 jsonl。這是刻意的取捨。
"""
from __future__ import annotations

import re

MASK = "[已遮罩]"

# 整檔遮罩：這些檔名一出現，附件內容整段不留。
# 逐行 regex 對 .dfm / .env 這種「整份都是設定」的檔案沒用，只能整檔丟掉。
SECRET_FILE_RE = re.compile(
    r"(^|[/\\])("
    r"\.env(\.[A-Za-z0-9_-]+)?"
    r"|\.npmrc|\.netrc|\.pgpass"
    r"|id_rsa|id_ed25519|id_ecdsa"
    r"|credentials(\.json)?|service[-_]account.*\.json"
    r")$"
    r"|\.(pem|pfx|p12|key|keystore|jks)$"
    # 目錄型規則必須容許出現在字串開頭（相對路徑如 secrets/db.json 沒有前導分隔符）
    r"|(^|[/\\])\.(ssh|aws|gnupg)[/\\]"
    r"|(^|[/\\])secrets?[/\\]",
    re.IGNORECASE,
)

# 舊 Delphi 的 .dfm / .pas 常內嵌明文連線帳密，一律當敏感檔處理。
LEGACY_DELPHI_RE = re.compile(r"\.(dfm|dpr)$", re.IGNORECASE)

# 行內遮罩：抓 key=value 形式的機密，以及有固定前綴的 token。
# 每條都保留欄位名，只吃掉值，這樣搜尋「哪個 session 提到 DB_PASSWORD」還找得到。
INLINE_RULES: list[tuple[re.Pattern[str], str]] = [
    # 連線字串片段：Password=xxx; / pwd=xxx; / User Id=xxx;
    (re.compile(r"\b(password|pwd|passwd)\s*=\s*[^;\s\"'&]{1,200}", re.IGNORECASE),
     r"\1=" + MASK),
    # 一般設定檔／環境變數：API_KEY: xxx / SECRET_TOKEN = "xxx"
    (re.compile(
        r"\b([A-Za-z0-9_.\-]*(?:PASSWORD|PASSWD|SECRET|API[_-]?KEY|ACCESS[_-]?KEY"
        r"|PRIVATE[_-]?KEY|AUTH[_-]?TOKEN|CLIENT[_-]?SECRET|CONN(?:ECTION)?[_-]?STRING))"
        r"\s*[:=]\s*[\"']?[^\s\"'&,}]{1,200}",
        re.IGNORECASE),
     r"\1=" + MASK),
    # 有固定前綴的 token，就算沒有欄位名也要抓
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), MASK),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"), MASK),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), MASK),
    # HTTP Authorization header
    (re.compile(r"\b(Authorization\s*:\s*)(Bearer|Basic)\s+\S+", re.IGNORECASE),
     r"\1\2 " + MASK),
    # URL 內嵌帳密：scheme://user:pass@host
    (re.compile(r"(://[^/\s:@]+):[^/\s:@]{1,200}@"), r"\1:" + MASK + "@"),
    # PEM 私鑰整塊
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL), MASK),
]


def is_secret_path(path: str | None) -> bool:
    """判斷這個檔案路徑是否該整檔遮罩。"""
    if not path:
        return False
    p = path.replace("\\", "/")
    return bool(SECRET_FILE_RE.search(p) or LEGACY_DELPHI_RE.search(p))


def redact(text: str) -> tuple[str, bool]:
    """行內遮罩。回傳 (處理後文字, 是否有東西被遮掉)。"""
    if not text:
        return text, False
    out = text
    for pattern, repl in INLINE_RULES:
        out = pattern.sub(repl, out)
    return out, out != text
