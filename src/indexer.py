"""掃描 + 增量匯入。

增量判斷靠 (size, mtime, scan_offset)：
- size 與 mtime 都沒變      -> 跳過
- size 變大且 offset 合理    -> 從 scan_offset 續讀，只追加新訊息
- 其他（檔案變小、被改寫）  -> 整檔重建

進行中的 session 每次掃描都會從上次停住的不完整行重讀，所以反覆執行是安全的。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import config, db, parser


@dataclass
class SessionFile:
    session_id: str
    project_slug: str
    path: Path
    size: int
    mtime: float
    kind: str = "main"                    # main | subagent
    parent_session_id: str | None = None
    sidecar_bytes: int = 0


@dataclass
class IndexStats:
    scanned: int = 0
    skipped: int = 0
    appended: int = 0
    rebuilt: int = 0
    messages: int = 0
    blocks: int = 0
    bad_lines: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def _dir_bytes(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def is_excluded(slug: str, pattern: str | None = None) -> bool:
    """這個 slug 是否為刻意不納管的暫存工作區（見 config.EXCLUDE_SLUG_RE）。

    pattern 給 None 代表讀 config 的預設值；給空字串代表不排除任何東西。
    """
    pat = config.EXCLUDE_SLUG_RE if pattern is None else pattern
    if not pat:
        return False
    return re.search(pat, slug) is not None


def discover(projects_dir: Path | None = None) -> list[SessionFile]:
    """掃出主 session 與 subagent 逐字稿。

    目錄結構（實測 Claude Code 2.1.x）：
        <slug>/<sessionId>.jsonl                    主 session
        <slug>/<sessionId>/subagents/agent-*.jsonl  subagent 逐字稿
        <slug>/<sessionId>/tool-results/*.txt       外置的大型工具輸出
        <slug>/memory/*.md                          專案記憶（非 session，不掃）
    """
    root = Path(projects_dir or config.PROJECTS_DIR)
    if not root.is_dir():
        return []
    found: list[SessionFile] = []

    for path in sorted(root.glob("*/*.jsonl")):
        if is_excluded(path.parent.name):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        sidecar = path.parent / path.stem
        found.append(SessionFile(
            session_id=path.stem,
            project_slug=path.parent.name,
            path=path,
            size=st.st_size,
            mtime=st.st_mtime,
            kind="main",
            sidecar_bytes=_dir_bytes(sidecar),
        ))

    for path in sorted(root.glob("*/*/subagents/*.jsonl")):
        if is_excluded(path.parent.parent.parent.name):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        parent_id = path.parent.parent.name
        found.append(SessionFile(
            # agentId 有沒有跨 session 唯一無法保證，用父 session 加檔名組出主鍵
            session_id=f"{parent_id}/{path.stem}",
            project_slug=path.parent.parent.parent.name,
            path=path,
            size=st.st_size,
            mtime=st.st_mtime,
            kind="subagent",
            parent_session_id=parent_id,
        ))

    return found


def _decide(conn: sqlite3.Connection, sf: SessionFile) -> tuple[str, int]:
    """回傳 (mode, start_offset)，mode 為 skip / append / rebuild。"""
    row = conn.execute(
        "SELECT jsonl_size, jsonl_mtime, scan_offset FROM sessions WHERE id = ?",
        (sf.session_id,),
    ).fetchone()
    if row is None:
        return "rebuild", 0
    # mtime 用 float 直接比對；檔案沒動過時兩者會完全相等
    if int(row["jsonl_size"]) == sf.size and float(row["jsonl_mtime"]) == sf.mtime:
        return "skip", 0
    offset = int(row["scan_offset"])
    if sf.size >= int(row["jsonl_size"]) and 0 <= offset <= sf.size:
        return "append", offset
    return "rebuild", 0


def _purge(conn: sqlite3.Connection, session_id: str) -> None:
    # blocks 的 DELETE trigger 會同步清掉 FTS，不能用 DROP/重建繞過
    conn.execute("DELETE FROM blocks WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))


def _next_seq(conn: sqlite3.Connection, table: str, session_id: str) -> int:
    row = conn.execute(
        f"SELECT COALESCE(MAX(seq), -1) AS m FROM {table} WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["m"]) + 1


def index_file(conn: sqlite3.Connection, sf: SessionFile, stats: IndexStats) -> None:
    mode, start_offset = _decide(conn, sf)
    if mode == "skip":
        stats.skipped += 1
        return

    result = parser.parse_session(sf.path, start_offset)

    conn.execute("BEGIN")
    try:
        if mode == "rebuild":
            _purge(conn, sf.session_id)
            msg_seq = 0
            blk_seq = 0
        else:
            msg_seq = _next_seq(conn, "messages", sf.session_id)
            blk_seq = _next_seq(conn, "blocks", sf.session_id)

        for msg in result.messages:
            if mode == "append":
                # 續讀理論上不會重疊，但版本升級改寫過檔案時可能碰到同一個 uuid，
                # 先清掉舊 block 才不會留下孤兒列
                conn.execute("DELETE FROM blocks WHERE message_uuid = ?", (msg.uuid,))
            conn.execute(
                "INSERT OR REPLACE INTO messages"
                "(uuid, session_id, parent_uuid, seq, kind, is_sidechain, ts, model,"
                " in_tok, out_tok, cache_create_tok, cache_read_tok, file_offset)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg.uuid, sf.session_id, msg.parent_uuid, msg_seq, msg.kind,
                 int(msg.is_sidechain), msg.ts, msg.model, msg.in_tok, msg.out_tok,
                 msg.cache_create_tok, msg.cache_read_tok, msg.file_offset),
            )
            msg_seq += 1
            stats.messages += 1

            for blk in msg.blocks:
                conn.execute(
                    "INSERT INTO blocks"
                    "(session_id, message_uuid, seq, role, tool_name, redacted,"
                    " truncated, text) VALUES (?,?,?,?,?,?,?,?)",
                    (sf.session_id, msg.uuid, blk_seq, blk.role, blk.tool_name,
                     int(blk.redacted), int(blk.truncated), blk.text),
                )
                blk_seq += 1
                stats.blocks += 1

        meta = result.meta
        now = datetime.now(timezone.utc).isoformat()
        existing = conn.execute(
            "SELECT ai_title, custom_title, last_prompt, cwd, git_branch,"
            " cc_version, entrypoint, agent_id, first_ts, last_ts, bad_lines"
            " FROM sessions WHERE id = ?",
            (sf.session_id,),
        ).fetchone()

        def keep(new, old_key):
            """增量讀取時新片段可能完全沒帶某個欄位，別把舊值蓋成 NULL。"""
            if new is not None:
                return new
            return existing[old_key] if existing is not None else None

        first_ts = meta.first_ts
        if existing is not None and existing["first_ts"]:
            if first_ts is None or existing["first_ts"] < first_ts:
                first_ts = existing["first_ts"]
        last_ts = meta.last_ts
        if existing is not None and existing["last_ts"]:
            if last_ts is None or existing["last_ts"] > last_ts:
                last_ts = existing["last_ts"]

        prev_bad = int(existing["bad_lines"]) if existing is not None else 0
        bad = result.bad_lines + (prev_bad if mode == "append" else 0)

        conn.execute(
            "INSERT INTO sessions"
            "(id, kind, parent_session_id, agent_id, project_slug, cwd, ai_title,"
            " custom_title, last_prompt, git_branch, cc_version, entrypoint,"
            " first_ts, last_ts, jsonl_path, jsonl_size, jsonl_mtime,"
            " scan_offset, bad_lines, sidecar_bytes, indexed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "  kind=excluded.kind, parent_session_id=excluded.parent_session_id,"
            "  agent_id=excluded.agent_id,"
            "  project_slug=excluded.project_slug, cwd=excluded.cwd,"
            "  ai_title=excluded.ai_title, custom_title=excluded.custom_title,"
            "  last_prompt=excluded.last_prompt,"
            "  git_branch=excluded.git_branch, cc_version=excluded.cc_version,"
            "  entrypoint=excluded.entrypoint, first_ts=excluded.first_ts,"
            "  last_ts=excluded.last_ts, jsonl_path=excluded.jsonl_path,"
            "  jsonl_size=excluded.jsonl_size, jsonl_mtime=excluded.jsonl_mtime,"
            "  scan_offset=excluded.scan_offset, bad_lines=excluded.bad_lines,"
            "  sidecar_bytes=excluded.sidecar_bytes, indexed_at=excluded.indexed_at",
            (sf.session_id, sf.kind, sf.parent_session_id,
             keep(meta.agent_id, "agent_id"),
             sf.project_slug, keep(meta.cwd, "cwd"),
             keep(meta.ai_title, "ai_title"),
             keep(meta.custom_title, "custom_title"),
             keep(meta.last_prompt, "last_prompt"),
             keep(meta.git_branch, "git_branch"), keep(meta.cc_version, "cc_version"),
             keep(meta.entrypoint, "entrypoint"), first_ts, last_ts,
             str(sf.path), sf.size, sf.mtime, result.new_offset, bad,
             sf.sidecar_bytes, now),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    stats.bad_lines += result.bad_lines
    if mode == "append":
        stats.appended += 1
    else:
        stats.rebuilt += 1


def reindex(conn: sqlite3.Connection,
            files: Iterable[SessionFile] | None = None,
            *,
            progress: Callable[[int, int, SessionFile], None] | None = None,
            ) -> IndexStats:
    items = list(files) if files is not None else discover()
    stats = IndexStats()
    total = len(items)
    for i, sf in enumerate(items, start=1):
        if progress:
            progress(i, total, sf)
        try:
            index_file(conn, sf, stats)
        except Exception as exc:  # 單一檔案壞掉不該讓整批索引中止
            stats.errors.append(f"{sf.project_slug}/{sf.session_id}: {exc!r}")
        stats.scanned += 1
    return stats


def prune_missing(conn: sqlite3.Connection) -> int:
    """清掉原始 jsonl 已不存在的 session（例如被歸檔搬走）。"""
    rows = conn.execute("SELECT id, jsonl_path FROM sessions").fetchall()
    gone = [r["id"] for r in rows if not Path(r["jsonl_path"]).exists()]
    for sid in gone:
        conn.execute("BEGIN")
        try:
            _purge(conn, sid)
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(gone)
