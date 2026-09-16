"""SQLite 索引的 schema 與連線。

刻意不在 sessions 表存 token 加總 —— 增量匯入時反覆更新加總欄位很容易算錯，
而 messages 只有數萬列，需要時 GROUP BY 就好，沒有效能問題。

【schema 變更絕對不能用「砍掉重建」】
索引通常被當成可拋棄的衍生物，但它有一種內容是**唯一留存**的：
Claude Code 的保留期刪掉原始 jsonl 之後，索引裡那份記錄就沒有別的來源了
（歸檔只涵蓋歸檔功能建立之後還存在的檔案）。

2026-09-03 為了加一個 custom_title 欄位而砍掉 index.db 重建，
直接銷毀了 140 個這種 session 的記錄（36.8MB 原始資料對應的 7.7MB 索引文字），
無法還原。所以加欄位一律走 migrate()，用 ALTER TABLE ADD COLUMN，
既有列原封不動。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    -- 主 session 的 id 是 sessionId；subagent 逐字稿的 id 是 agent-<agentId>，
    -- 兩者同表，用 kind 區分，這樣搜尋自然涵蓋 subagent 裡做的事。
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'main',   -- main | subagent
    parent_session_id TEXT,                       -- subagent 才有
    agent_id      TEXT,
    project_slug  TEXT NOT NULL,
    cwd           TEXT,
    ai_title      TEXT,
    custom_title  TEXT,          -- 使用者在 Claude Code UI 改的名稱，優先於 ai_title
    last_prompt   TEXT,
    git_branch    TEXT,
    cc_version    TEXT,
    entrypoint    TEXT,
    first_ts      TEXT,
    last_ts       TEXT,
    jsonl_path    TEXT NOT NULL,
    jsonl_size    INTEGER NOT NULL,
    jsonl_mtime   REAL    NOT NULL,
    scan_offset   INTEGER NOT NULL DEFAULT 0,
    bad_lines     INTEGER NOT NULL DEFAULT 0,
    -- <sessionId>/ 目錄（subagents、tool-results、memory）的總位元組數，
    -- 只有主 session 會填，供磁碟盤點用
    sidecar_bytes INTEGER NOT NULL DEFAULT 0,
    indexed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_slug);
CREATE INDEX IF NOT EXISTS idx_sessions_last_ts ON sessions(last_ts DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);

CREATE TABLE IF NOT EXISTS messages (
    uuid             TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    parent_uuid      TEXT,
    seq              INTEGER NOT NULL,
    kind             TEXT,
    is_sidechain     INTEGER NOT NULL DEFAULT 0,
    ts               TEXT,
    model            TEXT,
    in_tok           INTEGER NOT NULL DEFAULT 0,
    out_tok          INTEGER NOT NULL DEFAULT 0,
    cache_create_tok INTEGER NOT NULL DEFAULT 0,
    cache_read_tok   INTEGER NOT NULL DEFAULT 0,
    file_offset      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model);

CREATE TABLE IF NOT EXISTS blocks (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT NOT NULL,
    message_uuid TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    role         TEXT NOT NULL,
    tool_name    TEXT,
    redacted     INTEGER NOT NULL DEFAULT 0,
    truncated    INTEGER NOT NULL DEFAULT 0,
    text         TEXT
);
CREATE INDEX IF NOT EXISTS idx_blocks_session ON blocks(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_blocks_message ON blocks(message_uuid);
CREATE INDEX IF NOT EXISTS idx_blocks_tool ON blocks(tool_name);

-- trigram tokenizer：子字串搜尋，對中文可用，但查詢字串必須 >= 3 字元。
-- 1-2 字元的查詢要由呼叫端回退成 LIKE 掃描（見 search 模組）。
CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
    text,
    content='blocks',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS blocks_ai AFTER INSERT ON blocks BEGIN
    INSERT INTO blocks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS blocks_ad AFTER DELETE ON blocks BEGIN
    INSERT INTO blocks_fts(blocks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS blocks_au AFTER UPDATE ON blocks BEGIN
    INSERT INTO blocks_fts(blocks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO blocks_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


# 每個版本新增的欄位。key 是欄位名，value 是完整的 ALTER TABLE 型別宣告。
# 新增欄位就往這裡加一條，不要去改 SCHEMA 之後砍檔重建。
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "sessions": [
        ("kind", "TEXT NOT NULL DEFAULT 'main'"),
        ("parent_session_id", "TEXT"),
        ("agent_id", "TEXT"),
        ("custom_title", "TEXT"),
        ("entrypoint", "TEXT"),
        ("sidecar_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """把缺少的欄位補上。既有列完全不動，回傳實際新增了哪些。

    用 PRAGMA table_info 比對而不是靠版本號推：版本號可能因為手動操作而失準，
    但「欄位在不在」是事實。ALTER TABLE ADD COLUMN 在 SQLite 是 O(1) 的
    metadata 變更，不會重寫資料。
    """
    applied: list[str] = []
    for table, cols in _ADDED_COLUMNS.items():
        exists = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not exists:
            continue          # 表還不存在，SCHEMA 會建好完整版本
        for name, decl in cols:
            if name in exists:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            applied.append(f"{table}.{name}")
    return applied


def connect(path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    target = Path(path or config.DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if not read_only:
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS 對既有的表不會補欄位，要靠 migrate
        migrate(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    return conn
