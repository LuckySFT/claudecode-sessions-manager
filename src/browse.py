"""session 清單與對話還原。

blocks.text 在索引時已截斷（MAX_BLOCK_CHARS），檢視完整內容時直接靠
messages.file_offset 回原始 jsonl 讀那一行重解析 —— 不需要把全文塞進 DB。
"""
from __future__ import annotations

import json
import shlex
import sqlite3
from pathlib import Path

from . import config, labels, parser, redact, retention

SESSION_COLUMNS = (
    "s.id, s.kind, s.parent_session_id, s.agent_id, s.project_slug, s.cwd,"
    " s.ai_title, s.custom_title, s.last_prompt, s.git_branch, s.cc_version,"
    " s.entrypoint,"
    " s.first_ts, s.last_ts, s.jsonl_path, s.jsonl_size, s.sidecar_bytes,"
    " s.bad_lines, s.indexed_at"
)

_STATS_JOIN = """
LEFT JOIN (
    SELECT session_id,
           COUNT(*) AS msg_count,
           SUM(in_tok) AS in_tok,
           SUM(out_tok) AS out_tok,
           SUM(cache_create_tok) AS cache_create_tok,
           SUM(cache_read_tok) AS cache_read_tok,
           GROUP_CONCAT(DISTINCT model) AS models
    FROM messages GROUP BY session_id
) t ON t.session_id = s.id
"""


PROJECT_ORDERS = ("last_ts", "name", "size", "sessions", "missing", "unarchived")


def list_projects(conn: sqlite3.Connection, *,
                  order: str = "last_ts",
                  archived_ids: set[str] | None = None) -> list[dict]:
    """列出專案。設過別名的多個 slug 會合併成一列。

    排序在 Python 端做而不是丟給 SQL：別名合併之後的聚合值（體積、session 數）
    只有這裡算得出來，兩邊各排一次一定會不一致。專案數是數十量級，成本可忽略。
    """
    aliases = labels.get_aliases()
    period = retention.cleanup_period_days()

    # 先按實際 slug 聚合，再按顯示名稱做第二層合併
    groups: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT project_slug,"
        " SUM(kind = 'main') AS sessions,"
        " SUM(kind = 'subagent') AS subagents,"
        " SUM(jsonl_size) + SUM(sidecar_bytes) AS bytes,"
        " MAX(last_ts) AS last_ts, MAX(cwd) AS cwd"
        " FROM sessions GROUP BY project_slug"
    ):
        name = labels.display_name(r["project_slug"], aliases)
        g = groups.setdefault(name, {
            "project_display": name, "slugs": [], "sessions": 0, "subagents": 0,
            "bytes": 0, "last_ts": None, "cwd": None,
            "missing": 0, "expiring": 0, "unarchived": 0,
        })
        g["slugs"].append(r["project_slug"])
        g["sessions"] += r["sessions"] or 0
        g["subagents"] += r["subagents"] or 0
        g["bytes"] += r["bytes"] or 0
        if r["last_ts"] and (g["last_ts"] is None or r["last_ts"] > g["last_ts"]):
            g["last_ts"] = r["last_ts"]
        g["cwd"] = g["cwd"] or r["cwd"]

    for r in conn.execute("SELECT id, project_slug, jsonl_path FROM sessions"):
        g = groups.get(labels.display_name(r["project_slug"], aliases))
        if g is None:
            continue
        st = retention.file_status(r["jsonl_path"], days=period)
        if st["source_missing"]:
            g["missing"] += 1
        elif st["days_left"] is not None and st["days_left"] <= 7:
            g["expiring"] += 1
        if archived_ids is not None and r["id"] not in archived_ids:
            g["unarchived"] += 1

    rows = list(groups.values())
    for g in rows:
        g["slugs"].sort()
        # 顯示名稱等於唯一的 slug 時就不是別名，前端不必特別標示
        g["is_alias"] = len(g["slugs"]) > 1 or g["project_display"] not in g["slugs"]

    keys = {
        # 專案名混雜大小寫（8 個 D--、34 個 d--），不做大小寫無關比較會排成兩坨
        "name": lambda g: g["project_display"].casefold(),
        "last_ts": lambda g: (g["last_ts"] or "", ),
        "size": lambda g: g["bytes"],
        "sessions": lambda g: g["sessions"] + g["subagents"],
        "missing": lambda g: g["missing"],
        "unarchived": lambda g: g["unarchived"],
    }
    key = keys.get(order, keys["last_ts"])
    rows.sort(key=key, reverse=(order != "name"))
    return rows


_SESSION_SELECT = (
    f"SELECT {SESSION_COLUMNS},"
    " COALESCE(t.msg_count, 0) AS msg_count,"
    " COALESCE(t.in_tok, 0) AS in_tok,"
    " COALESCE(t.out_tok, 0) AS out_tok,"
    " COALESCE(t.cache_create_tok, 0) AS cache_create_tok,"
    " COALESCE(t.cache_read_tok, 0) AS cache_read_tok,"
    " t.models AS models,"
    " (SELECT COUNT(*) FROM sessions x WHERE x.parent_session_id = s.id)"
    "   AS subagent_count"
    " FROM sessions s" + _STATS_JOIN
)


def decorate(rows: list[dict], *, aliases: dict[str, str] | None = None,
             session_labels: dict[str, dict] | None = None) -> list[dict]:
    """把保留期狀態與使用者標籤併進每一列。

    display_title 是「要顯示哪個標題」的單一答案，前端不必自己判斷優先序：
    本工具的覆寫 > Claude Code UI 改的 custom-title > ai-title > 最後一句 prompt。
    """
    retention.annotate(rows)
    al = aliases if aliases is not None else labels.get_aliases()
    lb = session_labels if session_labels is not None else labels.get_session_labels()
    for row in rows:
        row["project_display"] = labels.display_name(row["project_slug"], al)
        meta = lb.get(row["id"], {})
        # 欄位名要跟 Claude Code 自己的 custom_title 區分開，否則會被蓋掉
        row["label_title"] = meta.get("label_title")
        row["hidden"] = bool(meta.get("hidden"))
        row["note"] = meta.get("note")
        # 顯示標題的優先序（前端不必自己判斷）：
        #   本工具的覆寫 > Claude Code UI 改的 custom-title > ai-title > 最後一句 prompt
        row["display_title"] = (row["label_title"] or row.get("custom_title")
                                or row.get("ai_title")
                                or row.get("last_prompt") or "(無標題)")
    return rows


def list_sessions(conn: sqlite3.Connection,
                  *,
                  project: str | None = None,
                  kind: str | None = "main",
                  parent: str | None = None,
                  since: str | None = None,
                  until: str | None = None,
                  missing: bool | None = None,
                  include_hidden: bool = False,
                  hidden_only: bool = False,
                  order: str = "last_ts",
                  limit: int = 100,
                  offset: int = 0) -> list[dict]:
    """列出 session。

    刻意不用 SQL 的 LIMIT/OFFSET：「原始檔還在不在」與「有沒有被隱藏」都不是
    SQL 能判斷的（一個在檔案系統、一個在另一個 DB），過濾必須在 Python 端做，
    在 SQL 就切頁會讓筆數對不上。session 總數是數百量級，全撈再切的成本可忽略。
    """
    where: list[str] = []
    params: list[object] = []
    if project:
        # 使用者點的是合併後的顯示名稱，要展開成所有對應的 slug
        slugs = labels.resolve_slugs(project)
        where.append("s.project_slug IN (" + ",".join("?" * len(slugs)) + ")")
        params.extend(slugs)
    if kind:
        where.append("s.kind = ?")
        params.append(kind)
    if parent:
        where.append("s.parent_session_id = ?")
        params.append(parent)
    if since:
        where.append("s.last_ts >= ?")
        params.append(since)
    if until:
        where.append("s.last_ts <= ?")
        params.append(until)

    order_sql = {
        "last_ts": "s.last_ts DESC",
        "first_ts": "s.first_ts DESC",
        "size": "s.jsonl_size DESC",
        "tokens": "COALESCE(t.out_tok, 0) DESC",
        "expiring": "s.jsonl_mtime ASC",   # 最快被清掉的排前面
        "title": "s.ai_title COLLATE NOCASE",
    }.get(order, "s.last_ts DESC")

    sql = (_SESSION_SELECT
           + (" WHERE " + " AND ".join(where) if where else "")
           + f" ORDER BY {order_sql}")
    rows = decorate([dict(r) for r in conn.execute(sql, params)])

    if missing is not None:
        rows = [r for r in rows if r["source_missing"] is missing]
    if hidden_only:
        rows = [r for r in rows if r["hidden"]]
    elif not include_hidden:
        rows = [r for r in rows if not r["hidden"]]

    # 手打的標題要參與排序，否則改了名字位置卻沒動
    if order == "title":
        rows.sort(key=lambda r: r["display_title"].casefold())

    return rows[offset:offset + limit]


def count_sessions(conn: sqlite3.Connection, **kwargs) -> int:
    """符合條件的總數。

    隱藏與「原始檔存在與否」的條件無法在 SQL 判斷，有帶這些條件時只能實際數過
    （list_sessions 已經是全撈再過濾，直接借用它）。
    """
    if (kwargs.get("missing") is not None or kwargs.get("hidden_only")
            or not kwargs.get("include_hidden", False)):
        conn_kwargs = {k: v for k, v in kwargs.items()
                       if k not in ("limit", "offset")}
        return len(list_sessions(conn, limit=10 ** 9, offset=0, **conn_kwargs))

    where: list[str] = []
    params: list[object] = []
    if kwargs.get("project"):
        slugs = labels.resolve_slugs(kwargs["project"])
        where.append("project_slug IN (" + ",".join("?" * len(slugs)) + ")")
        params.extend(slugs)
    for key, col in (("kind", "kind"), ("parent", "parent_session_id")):
        val = kwargs.get(key)
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    sql = "SELECT COUNT(*) AS n FROM sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(conn.execute(sql, params).fetchone()["n"])


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    sql = (
        f"SELECT {SESSION_COLUMNS},"
        " COALESCE(t.msg_count, 0) AS msg_count,"
        " COALESCE(t.in_tok, 0) AS in_tok,"
        " COALESCE(t.out_tok, 0) AS out_tok,"
        " COALESCE(t.cache_create_tok, 0) AS cache_create_tok,"
        " COALESCE(t.cache_read_tok, 0) AS cache_read_tok,"
        " t.models AS models"
        " FROM sessions s" + _STATS_JOIN +
        " WHERE s.id = ?"
    )
    row = conn.execute(sql, (session_id,)).fetchone()
    if row is None:
        return None
    # decorate 會一併補上保留期狀態（原始檔被清掉時索引裡這份就是唯一留存：
    # 內容還看得到，但沒辦法載入完整內容也沒辦法 resume）與使用者標籤
    out = decorate([dict(row)])[0]
    out["subagents"] = [
        dict(r) for r in conn.execute(
            "SELECT id, agent_id, first_ts, last_ts, jsonl_size FROM sessions"
            " WHERE parent_session_id = ? ORDER BY first_ts",
            (session_id,),
        )
    ]
    return out


def get_transcript(conn: sqlite3.Connection,
                   session_id: str,
                   *,
                   roles: list[str] | None = None,
                   limit: int = 500,
                   offset: int = 0) -> list[dict]:
    """回傳索引裡的（可能被截斷的）對話內容，適合快速瀏覽。"""
    where = ["b.session_id = ?"]
    params: list[object] = [session_id]
    if roles:
        where.append("b.role IN (" + ",".join("?" * len(roles)) + ")")
        params.extend(roles)
    sql = (
        "SELECT b.id, b.seq, b.role, b.tool_name, b.redacted, b.truncated, b.text,"
        " m.uuid AS message_uuid, m.ts, m.model, m.in_tok, m.out_tok,"
        " m.cache_create_tok, m.cache_read_tok, m.is_sidechain"
        " FROM blocks b JOIN messages m ON m.uuid = b.message_uuid"
        " WHERE " + " AND ".join(where) +
        " ORDER BY b.seq LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    return [dict(r) for r in conn.execute(sql, params)]


def get_block_full(conn: sqlite3.Connection, block_id: int) -> dict | None:
    """取單一 block 的完整內容（回原始 jsonl 重讀，不受索引截斷限制）。

    仍然套用遮罩：這段內容會送進瀏覽器，等同離開了本地檔案的保護範圍。
    """
    row = conn.execute(
        "SELECT b.seq, b.role, b.tool_name, b.message_uuid, m.file_offset,"
        " s.jsonl_path FROM blocks b"
        " JOIN messages m ON m.uuid = b.message_uuid"
        " JOIN sessions s ON s.id = b.session_id"
        " WHERE b.id = ?",
        (block_id,),
    ).fetchone()
    if row is None:
        return None

    path = Path(row["jsonl_path"])
    if not path.exists():
        return {"role": row["role"], "text": "", "error": "原始 jsonl 已不存在"}

    with path.open("rb") as fh:
        fh.seek(int(row["file_offset"]))
        raw = fh.readline()
    try:
        rec = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"role": row["role"], "text": "", "error": f"原始行無法解析：{exc}"}

    # 同一則訊息內可能有多個 block，用 message 內的相對序號對回去
    same_msg = conn.execute(
        "SELECT id, seq FROM blocks WHERE message_uuid = ? ORDER BY seq",
        (row["message_uuid"],),
    ).fetchall()
    index_in_msg = next((i for i, r in enumerate(same_msg)
                         if int(r["seq"]) == int(row["seq"])), 0)

    # 走跟索引時同一條拆解邏輯（差別只在不截斷），位置才對得上
    rtype = rec.get("type")
    if rtype == "attachment":
        blocks = parser.blocks_from_attachment(rec.get("attachment"), limit=None)
    else:
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = parser.blocks_from_content(content, outer_type=rtype, limit=None)

    if index_in_msg >= len(blocks):
        return {"role": row["role"], "text": "", "error": "區塊對位失敗"}

    blk = blocks[index_in_msg]
    return {"role": blk.role, "tool_name": blk.tool_name, "text": blk.text,
            "redacted": blk.redacted, "truncated": False}


def resume_command(session: dict) -> dict:
    """產生 resume 指令。subagent 逐字稿與原始檔已被清掉的 session 都無法 resume。"""
    if session.get("kind") != "main":
        return {"resumable": False,
                "reason": "subagent 逐字稿沒有獨立的 session，無法 resume"}
    if session.get("source_missing"):
        return {"resumable": False,
                "reason": "原始 jsonl 已被 Claude Code 的保留期清掉，"
                          "無法 resume；索引裡這份是唯一留存的記錄"}
    cwd = session.get("cwd") or ""
    cmd = f"claude --resume {session['id']}"
    return {
        "resumable": True,
        "cwd": cwd,
        "command": cmd,
        # PowerShell 用；cwd 走 -LiteralPath 免得路徑裡的 [] 被當通用字元
        "powershell": (f"Set-Location -LiteralPath {_ps_quote(cwd)}; {cmd}"
                       if cwd else cmd),
        "posix": (f"cd {shlex.quote(cwd)} && {cmd}" if cwd else cmd),
    }


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
