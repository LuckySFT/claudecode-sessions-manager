"""使用者自訂的標籤：session 更名、隱藏、專案別名。

【為什麼要獨立一個 DB，不放進 index.db】
index.db 是**可拋棄的衍生物** —— 改遮罩規則、改 schema 都要砍掉重建
（開發期間已經重建過四次）。使用者手動打的標題與別名是**唯一來源**，
一旦跟索引放在一起就會跟著被砍掉。所以另存 data/labels.db。

【本工具的覆寫 vs Claude Code 自己的改名】
Claude Code 的 UI 也能改名，機制是往 jsonl **append 一筆 `custom-title` 記錄**
（`{"type":"custom-title","customTitle":...,"sessionId":...}`，最後一筆勝出）。
那是 append-only 的，不改寫既有內容。

這裡的 `label_title` 是**本工具自己的另一層覆寫**，存在 labels.db，
完全不碰 `~/.claude/`。兩者刻意分開命名，否則會互相蓋掉。
顯示優先序：label_title > custom-title > ai-title > last-prompt。

代價是本工具改的名字 Claude Code 看不到。要雙向一致就得 append custom-title
到原始 jsonl —— 那需要鬆綁「原始資料唯讀」，見 docs/TODO.md。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_labels (
    session_id TEXT PRIMARY KEY,
    title      TEXT,                              -- NULL 表示沒有覆寫標題
    hidden     INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    updated_at TEXT NOT NULL
);

-- 多個 slug 可以指到同一個 alias，清單就會合併成一列。
-- 用途：專案搬過目錄之後，歷史被拆成兩個 slug，用別名接回同一個專案。
CREATE TABLE IF NOT EXISTS project_aliases (
    project_slug TEXT PRIMARY KEY,
    alias        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alias ON project_aliases(alias);
"""


def db_path() -> Path:
    return config.LABELS_DB_PATH


def connect() -> sqlite3.Connection:
    target = db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── session 標籤 ─────────────────────────────────

def get_session_labels() -> dict[str, dict]:
    """一次撈全部，呼叫端自己查表。數量是數百量級，不需要分批。"""
    conn = connect()
    try:
        return {
            r["session_id"]: {
                "label_title": r["title"],
                "hidden": bool(r["hidden"]),
                "note": r["note"],
            }
            for r in conn.execute(
                "SELECT session_id, title, hidden, note FROM session_labels")
        }
    finally:
        conn.close()


def hidden_session_ids() -> set[str]:
    conn = connect()
    try:
        return {r["session_id"] for r in conn.execute(
            "SELECT session_id FROM session_labels WHERE hidden = 1")}
    finally:
        conn.close()


def set_session_label(session_id: str, *,
                      title: str | None = None,
                      hidden: bool | None = None,
                      note: str | None = None,
                      clear_title: bool = False) -> dict:
    """更新單一 session 的標籤。只寫傳進來的欄位，其餘保持原值。

    clear_title=True 才會把標題清成 NULL（回到原本的 ai-title）；
    單純傳 title=None 代表「這次不動標題」，兩者語意不同。
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT title, hidden, note FROM session_labels WHERE session_id = ?",
            (session_id,)).fetchone()
        cur_title = row["title"] if row else None
        cur_hidden = bool(row["hidden"]) if row else False
        cur_note = row["note"] if row else None

        new_title = None if clear_title else (title if title is not None else cur_title)
        if new_title is not None:
            new_title = new_title.strip() or None
        new_hidden = cur_hidden if hidden is None else bool(hidden)
        new_note = cur_note if note is None else (note.strip() or None)

        # 三個欄位都空了就不留列，避免累積無意義的資料
        if new_title is None and not new_hidden and new_note is None:
            conn.execute("DELETE FROM session_labels WHERE session_id = ?",
                         (session_id,))
            return {"label_title": None, "hidden": False, "note": None}

        conn.execute(
            "INSERT INTO session_labels(session_id, title, hidden, note, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            "  title=excluded.title, hidden=excluded.hidden,"
            "  note=excluded.note, updated_at=excluded.updated_at",
            (session_id, new_title, int(new_hidden), new_note, _now()))
        return {"label_title": new_title, "hidden": new_hidden, "note": new_note}
    finally:
        conn.close()


# ── 專案別名 ─────────────────────────────────────

def get_aliases() -> dict[str, str]:
    """slug -> alias。沒設過的 slug 不在這裡面。"""
    conn = connect()
    try:
        return {r["project_slug"]: r["alias"] for r in conn.execute(
            "SELECT project_slug, alias FROM project_aliases")}
    finally:
        conn.close()


def set_alias(project_slug: str, alias: str | None) -> None:
    """alias 傳 None 或空字串就取消別名。"""
    conn = connect()
    try:
        alias = (alias or "").strip()
        if not alias or alias == project_slug:
            conn.execute("DELETE FROM project_aliases WHERE project_slug = ?",
                         (project_slug,))
        else:
            conn.execute(
                "INSERT INTO project_aliases(project_slug, alias, updated_at)"
                " VALUES (?,?,?)"
                " ON CONFLICT(project_slug) DO UPDATE SET"
                "  alias=excluded.alias, updated_at=excluded.updated_at",
                (project_slug, alias, _now()))
    finally:
        conn.close()


def display_name(project_slug: str, aliases: dict[str, str] | None = None) -> str:
    a = aliases if aliases is not None else get_aliases()
    return a.get(project_slug, project_slug)


def resolve_slugs(name: str, aliases: dict[str, str] | None = None) -> list[str]:
    """把「顯示名稱」還原成一或多個實際的 project_slug。

    使用者在 UI 上點的是合併後的顯示名稱，但資料庫裡存的是 slug，
    篩選條件必須展開成所有對應的 slug，否則合併後的專案只會查到其中一個。
    查不到別名就當它本身是 slug。
    """
    a = aliases if aliases is not None else get_aliases()
    slugs = [slug for slug, al in a.items() if al == name]
    return slugs or [name]
