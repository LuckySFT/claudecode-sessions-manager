"""移除「原始檔已被 Claude Code 清掉」的 session 記錄。

【對象嚴格限定：source_missing 的 session】
原始檔還在的一律不動 —— 那些交給 Claude Code 自己的保留期機制處理。
所以這個模組完全不碰 `~/.claude/`，只清我們自己產出的東西。

【兩種深度，差別很大】
  index   只清 index.db 的列（sessions/messages/blocks/FTS）與 labels.db 的標籤。
          歸檔 blob 留著 —— 之後還能 restore 回來再重新索引。
  full    連歸檔 blob 一起刪。**不可逆**，內容從此不存在於任何地方。

【必須先看清楚才動手】
目前本機 140 個已消失的 session **沒有歸檔備份**（歸檔是在刪除事件之後才建的），
對這些 session 而言索引裡那份是唯一留存，`index` 深度也等於徹底銷毀。
所以 plan() 會分別統計「有備份」與「無備份」，讓呼叫端把差別講清楚。

預設一律 dry-run，要明確傳 confirm=True 才會真的刪。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import archive, indexer, labels

DEPTHS = ("index", "full")


@dataclass
class Candidate:
    session_id: str
    kind: str
    project_slug: str
    display_title: str
    last_ts: str | None
    jsonl_size: int
    msg_count: int
    block_chars: int
    archived: bool


@dataclass
class PurgePlan:
    candidates: list[Candidate] = field(default_factory=list)
    depth: str = "index"
    # 沒有歸檔備份的那些，刪掉就徹底消失，要單獨算出來示警
    unbacked: list[Candidate] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.candidates)

    @property
    def index_chars(self) -> int:
        return sum(c.block_chars for c in self.candidates)

    @property
    def raw_bytes(self) -> int:
        return sum(c.jsonl_size for c in self.candidates)


@dataclass
class PurgeResult:
    removed_sessions: int = 0
    removed_messages: int = 0
    removed_blocks: int = 0
    removed_labels: int = 0
    removed_blobs: int = 0
    freed_blob_bytes: int = 0
    errors: list[str] = field(default_factory=list)


def _archive_paths_for(session_id: str, kind: str) -> list[str]:
    """這個 session 在歸檔清單裡對應哪些 rel_path。

    主 session：`<slug>/<sessionId>.jsonl` 加上 `<slug>/<sessionId>/**`
    subagent：  `<slug>/<parent>/subagents/<stem>.jsonl`（含同名 .meta.json）
    """
    if kind == "subagent" and "/" in session_id:
        parent, stem = session_id.split("/", 1)
        return [f"%/{parent}/subagents/{stem}.jsonl",
                f"%/{parent}/subagents/{stem}.meta.json"]
    return [f"%/{session_id}.jsonl", f"%/{session_id}/%"]


def plan(conn: sqlite3.Connection, *,
         depth: str = "index",
         project: str | None = None,
         session_ids: list[str] | None = None,
         older_than_days: int | None = None,
         include_backed_up: bool = True,
         include_unbacked: bool = True) -> PurgePlan:
    """列出要移除的 session。不做任何修改。

    older_than_days 是以 session 的最後活動時間（last_ts）算的，
    不是以「什麼時候被刪掉」—— 後者無從得知，Claude Code 沒留記錄。
    """
    if depth not in DEPTHS:
        raise ValueError(f"depth 只接受 {DEPTHS}")

    where = ["1=1"]
    params: list[object] = []
    if project:
        slugs = labels.resolve_slugs(project)
        where.append("s.project_slug IN (" + ",".join("?" * len(slugs)) + ")")
        params.extend(slugs)
    if session_ids:
        where.append("s.id IN (" + ",".join("?" * len(session_ids)) + ")")
        params.extend(session_ids)
    if older_than_days is not None:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=older_than_days)).isoformat()
        # last_ts 為 NULL 的一併納入：沒有時間資訊代表更舊或更殘缺
        where.append("(s.last_ts IS NULL OR s.last_ts < ?)")
        params.append(cutoff)

    sql = (
        "SELECT s.id, s.kind, s.project_slug, s.jsonl_path, s.jsonl_size,"
        " s.custom_title, s.ai_title, s.last_prompt, s.last_ts,"
        " (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS msg_count,"
        " (SELECT COALESCE(SUM(LENGTH(text)), 0) FROM blocks b"
        "   WHERE b.session_id = s.id) AS block_chars"
        " FROM sessions s WHERE " + " AND ".join(where)
    )

    archived = archive.archived_session_ids()
    lb = labels.get_session_labels()
    out = PurgePlan(depth=depth)

    for r in conn.execute(sql, params):
        # 只處理原始檔已經不在的。還在的交給 Claude Code 自己的保留期。
        if Path(r["jsonl_path"]).exists():
            continue
        is_archived = r["id"] in archived
        if is_archived and not include_backed_up:
            continue
        if not is_archived and not include_unbacked:
            continue

        meta = lb.get(r["id"], {})
        title = (meta.get("label_title") or r["custom_title"] or r["ai_title"]
                 or r["last_prompt"] or "(無標題)")
        c = Candidate(
            session_id=r["id"], kind=r["kind"], project_slug=r["project_slug"],
            display_title=title, last_ts=r["last_ts"],
            jsonl_size=r["jsonl_size"], msg_count=r["msg_count"],
            block_chars=r["block_chars"], archived=is_archived,
        )
        out.candidates.append(c)
        if not is_archived:
            out.unbacked.append(c)

    out.candidates.sort(key=lambda c: (c.last_ts or "", c.session_id))
    return out


def apply(conn: sqlite3.Connection, plan_obj: PurgePlan, *,
          confirm: bool = False) -> PurgeResult:
    """實際執行移除。confirm 必須明確為 True。"""
    res = PurgeResult()
    if not confirm:
        res.errors.append("未確認，什麼都沒做（需要 confirm=True）")
        return res
    if not plan_obj.candidates:
        return res

    ids = [c.session_id for c in plan_obj.candidates]

    # ── 索引 ──
    conn.execute("BEGIN")
    try:
        for sid in ids:
            res.removed_messages += conn.execute(
                "SELECT COUNT(*) n FROM messages WHERE session_id = ?",
                (sid,)).fetchone()["n"]
            res.removed_blocks += conn.execute(
                "SELECT COUNT(*) n FROM blocks WHERE session_id = ?",
                (sid,)).fetchone()["n"]
            # 用 indexer 的 _purge：blocks 的 DELETE trigger 會同步清 FTS，
            # 自己寫一份很容易漏掉 FTS 那半，留下搜得到但點不開的鬼魂
            indexer._purge(conn, sid)
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            res.removed_sessions += 1
        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        res.errors.append(f"清索引失敗，已回滾：{exc!r}")
        return res

    # ── 標籤 ──
    lconn = labels.connect()
    try:
        for sid in ids:
            cur = lconn.execute("DELETE FROM session_labels WHERE session_id = ?",
                                (sid,))
            res.removed_labels += cur.rowcount if cur.rowcount > 0 else 0
    except Exception as exc:
        res.errors.append(f"清標籤失敗：{exc!r}")
    finally:
        lconn.close()

    # ── 歸檔（只有 full 深度才動）──
    if plan_obj.depth == "full":
        aconn = archive.connect()
        try:
            for c in plan_obj.candidates:
                patterns = _archive_paths_for(c.session_id, c.kind)
                clause = " OR ".join("rel_path LIKE ?" for _ in patterns)
                rows = aconn.execute(
                    f"SELECT rel_path, sha256 FROM current WHERE {clause}",
                    patterns).fetchall()
                for row in rows:
                    rel = row["rel_path"]
                    # 一個 sha 可能被多個路徑共用（內容定址會去重），
                    # 還有別的路徑指著就不能刪檔案，只解除這條對應
                    shas = [x["sha256"] for x in aconn.execute(
                        "SELECT sha256 FROM versions WHERE rel_path = ?", (rel,))]
                    aconn.execute("DELETE FROM versions WHERE rel_path = ?", (rel,))
                    aconn.execute("DELETE FROM current WHERE rel_path = ?", (rel,))
                    for sha in set(shas):
                        still = aconn.execute(
                            "SELECT COUNT(*) n FROM versions WHERE sha256 = ?",
                            (sha,)).fetchone()["n"]
                        if still:
                            continue
                        blob = archive._blob_file(sha)
                        size = 0
                        try:
                            if blob.exists():
                                size = blob.stat().st_size
                                blob.unlink()
                        except OSError as exc:
                            res.errors.append(f"刪 blob {sha[:12]} 失敗：{exc}")
                            continue
                        aconn.execute("DELETE FROM blobs WHERE sha256 = ?", (sha,))
                        res.removed_blobs += 1
                        res.freed_blob_bytes += size
        except Exception as exc:
            res.errors.append(f"清歸檔失敗：{exc!r}")
        finally:
            aconn.close()

    conn.execute("PRAGMA optimize")
    return res
