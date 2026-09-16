"""FastAPI 後端。

只綁 127.0.0.1。這支服務會把本機所有對話內容透過 HTTP 吐出來，
綁到 0.0.0.0 等於把整台機器的開發歷程開放給區網，絕對不要改。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from . import (archive, browse, config, db, indexer, labels, purge, rename,
               retention, search)

log = logging.getLogger("ccsm")

app = FastAPI(title="Claude Code Session Manager", docs_url="/api/docs")
STATIC_DIR = Path(__file__).parent / "static"

_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    """單一連線重複使用。

    check_same_thread=False 是必要的：uvicorn 會把同步路由丟到 threadpool，
    每次請求不一定同一條執行緒。這裡全是唯讀查詢，SQLite 在 WAL 下可安全併發讀。
    """
    global _conn
    if _conn is None:
        if not config.DB_PATH.exists():
            raise HTTPException(503, "索引還不存在，先執行 python -m src.cli index")
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        _conn = conn
    return _conn


def _reset_conn() -> None:
    """索引被寫入之後丟掉唯讀連線，讓下次請求重新開。

    唯讀連線開著 query_only，看不到別的連線剛提交的 schema/資料變更，
    不重開的話 UI 會顯示舊內容，看起來像「改了沒生效」。
    """
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


@app.get("/")
def index_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.html")


@app.get("/api/overview")
def api_overview() -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS sessions,"
        " SUM(kind = 'main') AS main_sessions,"
        " SUM(kind = 'subagent') AS subagents,"
        " COUNT(DISTINCT project_slug) AS projects,"
        " SUM(jsonl_size) AS jsonl_bytes,"
        " SUM(sidecar_bytes) AS sidecar_bytes,"
        " SUM(bad_lines) AS bad_lines,"
        " MIN(first_ts) AS since, MAX(last_ts) AS until"
        " FROM sessions"
    ).fetchone()
    tok = conn.execute(
        "SELECT SUM(in_tok) AS in_tok, SUM(out_tok) AS out_tok,"
        " SUM(cache_create_tok) AS cache_create_tok,"
        " SUM(cache_read_tok) AS cache_read_tok, COUNT(*) AS messages"
        " FROM messages"
    ).fetchone()
    blk = conn.execute(
        "SELECT COUNT(*) AS blocks, SUM(redacted) AS redacted,"
        " SUM(truncated) AS truncated FROM blocks"
    ).fetchone()
    # 原始檔還在不在是檔案系統的事，逐一 stat（數百筆，成本可忽略）
    period = retention.cleanup_period_days()
    alive = missing = expiring = 0
    missing_bytes = 0
    for r in conn.execute("SELECT jsonl_path, jsonl_size FROM sessions"):
        st = retention.file_status(r["jsonl_path"], days=period)
        if st["source_missing"]:
            missing += 1
            missing_bytes += r["jsonl_size"]
        else:
            alive += 1
            if st["days_left"] is not None and st["days_left"] <= 7:
                expiring += 1

    arc = archive.status()
    archived_ids = archive.archived_session_ids()
    all_ids = {r["id"] for r in conn.execute("SELECT id FROM sessions")}

    return {
        **{k: row[k] for k in row.keys()},
        **{k: tok[k] for k in tok.keys()},
        **{k: blk[k] for k in blk.keys()},
        "index_bytes": config.DB_PATH.stat().st_size,
        "source_alive": alive,
        "source_missing": missing,
        "source_missing_bytes": missing_bytes,
        "expiring_soon": expiring,
        "cleanup_period_days": period,
        "archive": arc,
        "archived_sessions": len(archived_ids & all_ids),
        "unarchived_sessions": len(all_ids - archived_ids),
    }


@app.get("/api/archive/status")
def api_archive_status() -> dict:
    return archive.status()


@app.get("/api/archive/files")
def api_archive_files(only_missing: bool = False,
                      pattern: str | None = None,
                      limit: int = Query(500, le=5000)) -> list[dict]:
    return archive.list_archived(only_missing=only_missing, pattern=pattern,
                                 limit=limit)


@app.post("/api/archive")
def api_archive_run() -> dict:
    """執行歸檔。只新增，不刪除原始資料，所以不需要額外確認。"""
    stats = archive.run()
    return {"scanned": stats.scanned, "added": stats.added,
            "unchanged": stats.unchanged, "bytes_added": stats.bytes_added,
            "vanished": stats.vanished, "errors": stats.errors,
            "status": archive.status()}


@app.get("/api/projects")
def api_projects(order: str = "last_ts") -> list[dict]:
    if order not in browse.PROJECT_ORDERS:
        raise HTTPException(400, f"order 只接受 {browse.PROJECT_ORDERS}")
    return browse.list_projects(get_conn(), order=order,
                                archived_ids=archive.archived_session_ids())


@app.post("/api/projects/{project_slug}/alias")
def api_set_alias(project_slug: str, body: dict = Body(default={})) -> dict:
    """設定或清除專案別名。多個 slug 指到同一個別名就會在清單上合併成一列。"""
    alias = body.get("alias")
    if alias is not None and not isinstance(alias, str):
        raise HTTPException(400, "alias 必須是字串或 null")
    labels.set_alias(project_slug, alias)
    return {"project_slug": project_slug,
            "alias": labels.get_aliases().get(project_slug)}


@app.post("/api/sessions/{session_id:path}/label")
def api_set_label(session_id: str, body: dict = Body(default={})) -> dict:
    """更名 / 隱藏 / 備註。

    只覆寫這個工具裡的顯示，不動原始 jsonl —— Claude Code 自己的 /resume
    選單仍會顯示原本的 ai-title，兩邊永久分歧。
    """
    conn = get_conn()
    if conn.execute("SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,)).fetchone() is None:
        raise HTTPException(404, "找不到這個 session")

    for key in ("title", "note"):
        if key in body and body[key] is not None and not isinstance(body[key], str):
            raise HTTPException(400, f"{key} 必須是字串或 null")
    if "hidden" in body and body["hidden"] is not None \
            and not isinstance(body["hidden"], bool):
        raise HTTPException(400, "hidden 必須是布林值")

    result = labels.set_session_label(
        session_id,
        title=body.get("title"),
        hidden=body.get("hidden"),
        note=body.get("note"),
        # title 傳 null 代表「清除覆寫、回到原本的 ai-title」；
        # 沒有傳 title 這個鍵則代表「這次不動標題」，兩者語意不同
        clear_title=("title" in body and body["title"] is None),
    )
    return {"session_id": session_id, **result}


@app.post("/api/sessions/{session_id:path}/rename")
def api_rename(session_id: str, body: dict = Body(default={})) -> dict:
    """真正的改名：往原始 jsonl 追加一筆 custom-title。

    這是「原始資料唯讀」的唯一例外。只追加、不改寫，並且會擋掉進行中的 session。
    成功後會順便清掉本工具的本地覆寫 —— 真名已經寫進去了，覆寫只會蓋住它。
    """
    title = body.get("title")
    if not isinstance(title, str):
        raise HTTPException(400, "title 必須是字串")

    conn = get_conn()
    result = rename.rename_session(conn, session_id, title,
                                   dry_run=bool(body.get("dry_run")))
    if not result.ok:
        # 409：不是使用者輸入錯，是當下的狀態不允許（例如 session 進行中）
        raise HTTPException(409, result.reason)

    if not body.get("dry_run"):
        labels.set_session_label(session_id, clear_title=True)
        # 立刻把那個檔案重新掃進索引，否則 UI 上看不到改名結果
        wconn = db.connect()
        try:
            for sf in indexer.discover():
                if sf.session_id == session_id:
                    indexer.index_file(wconn, sf, indexer.IndexStats())
                    break
        finally:
            wconn.close()
        _reset_conn()

    return {"ok": True, "session_id": session_id, "title": result.title,
            "previous": result.previous, "bytes_appended": result.bytes_appended,
            "idle_seconds": result.idle_seconds,
            "dry_run": bool(body.get("dry_run"))}


@app.get("/api/sessions/{session_id:path}/can-rename")
def api_can_rename(session_id: str) -> dict:
    ok, reason = rename.can_rename(get_conn(), session_id)
    return {"ok": ok, "reason": reason}


@app.post("/api/purge/plan")
def api_purge_plan(body: dict = Body(default={})) -> dict:
    """列出可移除的「原始檔已消失」session。不做任何修改。"""
    try:
        p = purge.plan(
            get_conn(),
            depth=body.get("depth", "index"),
            project=body.get("project"),
            session_ids=body.get("session_ids"),
            older_than_days=body.get("older_than_days"),
            include_backed_up=body.get("include_backed_up", True),
            include_unbacked=body.get("include_unbacked", True),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "depth": p.depth,
        "total": p.total,
        "unbacked": len(p.unbacked),
        "index_chars": p.index_chars,
        "raw_bytes": p.raw_bytes,
        "items": [c.__dict__ for c in p.candidates],
    }


@app.post("/api/purge")
def api_purge(body: dict = Body(default={})) -> dict:
    """實際移除。必須明確帶 confirm=true。"""
    if body.get("confirm") is not True:
        raise HTTPException(400, "需要 confirm=true 才會執行")
    ids = body.get("session_ids")
    if not ids or not isinstance(ids, list):
        # 刻意要求明確列出 id：這是不可逆操作，不接受「照條件全刪」的模糊指令
        raise HTTPException(400, "必須明確列出 session_ids（不接受條件式全刪）")

    conn_ro = get_conn()
    try:
        p = purge.plan(conn_ro, depth=body.get("depth", "index"),
                       session_ids=ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not p.candidates:
        raise HTTPException(400, "指定的 session 都不符合條件（原始檔還在，或不存在）")

    wconn = db.connect()
    try:
        res = purge.apply(wconn, p, confirm=True)
    finally:
        wconn.close()
    _reset_conn()
    return {**res.__dict__, "depth": p.depth}


@app.get("/api/sessions")
def api_sessions(project: str | None = None,
                 kind: str | None = "main",
                 parent: str | None = None,
                 missing: bool | None = None,
                 include_hidden: bool = False,
                 hidden_only: bool = False,
                 order: str = "last_ts",
                 limit: int = Query(100, le=500),
                 offset: int = 0) -> dict:
    conn = get_conn()
    filters = dict(project=project, kind=kind, parent=parent, missing=missing,
                   include_hidden=include_hidden, hidden_only=hidden_only)
    items = browse.list_sessions(conn, order=order, limit=limit, offset=offset,
                                 **filters)
    archived = archive.archived_session_ids()
    for it in items:
        it["archived"] = it["id"] in archived
    return {"total": browse.count_sessions(conn, **filters), "items": items}


@app.get("/api/sessions/{session_id:path}")
def api_session(session_id: str) -> dict:
    data = browse.get_session(get_conn(), session_id)
    if data is None:
        raise HTTPException(404, "找不到這個 session")
    data["resume"] = browse.resume_command(data)
    data["archived"] = session_id in archive.archived_session_ids()
    return data


@app.get("/api/transcript/{session_id:path}")
def api_transcript(session_id: str,
                   roles: str | None = None,
                   limit: int = Query(500, le=2000),
                   offset: int = 0) -> list[dict]:
    return browse.get_transcript(get_conn(), session_id,
                                 roles=_csv(roles), limit=limit, offset=offset)


@app.get("/api/block/{block_id}")
def api_block(block_id: int) -> dict:
    data = browse.get_block_full(get_conn(), block_id)
    if data is None:
        raise HTTPException(404, "找不到這個區塊")
    return data


@app.get("/api/search")
def api_search(q: str,
               roles: str | None = None,
               projects: str | None = None,
               kinds: str | None = None,
               since: str | None = None,
               until: str | None = None,
               group: bool = False,
               include_hidden: bool = False,
               limit: int = Query(50, le=200),
               offset: int = 0) -> dict:
    conn = get_conn()
    # 前端傳來的是合併後的顯示名稱，要展開成實際的 slug 才篩得到
    names = _csv(projects)
    slugs: list[str] | None = None
    if names:
        aliases = labels.get_aliases()
        slugs = sorted({s for n in names
                        for s in labels.resolve_slugs(n, aliases)})
    filters = dict(roles=_csv(roles), projects=slugs,
                   session_kinds=_csv(kinds), since=since, until=until,
                   exclude_session_ids=(None if include_hidden
                                        else labels.hidden_session_ids()))
    # 更名過的 session 在搜尋結果也要顯示新名字，否則改了名看起來像沒生效
    lb = labels.get_session_labels()
    aliases = labels.get_aliases()

    def title_of(session_id: str, custom_title: str | None,
                 ai_title: str | None) -> str:
        # 與 browse.decorate 同一組優先序，兩邊不能走偏
        meta = lb.get(session_id, {})
        return (meta.get("label_title") or custom_title or ai_title
                or session_id)

    if group:
        items = search.session_hit_counts(conn, q, limit=limit, **filters)
        for it in items:
            it["display_title"] = title_of(it["id"], it.get("custom_title"),
                                           it.get("ai_title"))
            it["project_display"] = labels.display_name(it["project_slug"], aliases)
        return {"grouped": True, "items": items}

    hits, slow = search.search(conn, q, limit=limit, offset=offset, **filters)
    items = []
    for h in hits:
        d = dict(h.__dict__)
        d["display_title"] = title_of(h.session_id, h.custom_title, h.ai_title)
        d["project_display"] = labels.display_name(h.project_slug, aliases)
        items.append(d)
    return {
        "grouped": False,
        "total": search.count(conn, q, **filters),
        "slow": slow,
        "items": items,
    }


@app.post("/api/reindex")
def api_reindex(prune: bool = False) -> dict:
    """重建索引。寫入需要獨立的可寫連線，不能用 query_only 的那條。

    prune 預設關閉，而且不要在 UI 上提供這個開關。
    Claude Code 有自己的保留期（cleanupPeriodDays，預設 30 天）會刪掉舊 session，
    被刪掉之後索引裡那份就是**唯一**的留存，prune 會把它一併清掉。
    實測 2026-08-31 這天一次消失 137 個檔案／34.8MB。
    """
    conn = db.connect()
    try:
        stats = indexer.reindex(conn)
        pruned = indexer.prune_missing(conn) if prune else 0
    finally:
        conn.close()
    _reset_conn()
    return {"scanned": stats.scanned, "rebuilt": stats.rebuilt,
            "appended": stats.appended, "skipped": stats.skipped,
            "messages": stats.messages, "blocks": stats.blocks,
            "pruned": pruned, "errors": stats.errors}


@app.exception_handler(sqlite3.Error)
def sqlite_error_handler(request, exc: sqlite3.Error) -> JSONResponse:
    log.exception("SQLite 錯誤")
    return JSONResponse({"detail": f"資料庫錯誤：{exc}"}, status_code=500)


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host=host, port=port, log_level="info")
