"""搜尋。

trigram tokenizer 做子字串比對，中文可用，但**查詢詞必須 >= 3 字元**才會有結果
（實測 '中文' 兩字命中 0 筆，'語法符號' 四字正常）。
所以查詢詞按長度分流：
  >= 3 字元 -> 走 FTS MATCH，快
  <  3 字元 -> 走 LIKE 掃描，慢但正確
兩者可以並存：先用 FTS 把候選縮到很小，再用 LIKE 過濾短詞。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

MIN_TRIGRAM_LEN = 3
SNIPPET_RADIUS = 90


@dataclass
class Hit:
    block_id: int
    session_id: str
    project_slug: str
    ai_title: str | None
    custom_title: str | None
    kind: str
    role: str
    tool_name: str | None
    ts: str | None
    cwd: str | None
    redacted: bool
    truncated: bool
    snippet: str
    spans: list[tuple[int, int]] = field(default_factory=list)


def _fts_quote(term: str) -> str:
    """包成 FTS5 字串字面值。內部的雙引號要成對重複，否則會被當語法。"""
    return '"' + term.replace('"', '""') + '"'


def split_terms(query: str) -> list[str]:
    """支援用雙引號把含空白的片語框起來，其餘按空白切。"""
    terms: list[str] = []
    for quoted, bare in re.findall(r'"([^"]*)"|(\S+)', query):
        term = (quoted or bare).strip()
        if term:
            terms.append(term)
    return terms


def _make_snippet(text: str, terms: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """以第一個命中的詞為中心裁一段，並回傳片段內所有命中的位置。"""
    if not text:
        return "", []
    low = text.lower()
    first = len(text)
    for t in terms:
        pos = low.find(t.lower())
        if pos != -1:
            first = min(first, pos)
    if first == len(text):
        return text[: SNIPPET_RADIUS * 2], []

    start = max(0, first - SNIPPET_RADIUS)
    end = min(len(text), first + SNIPPET_RADIUS)
    frag = text[start:end]
    if start > 0:
        frag = "…" + frag
    if end < len(text):
        frag = frag + "…"

    frag_low = frag.lower()
    spans: list[tuple[int, int]] = []
    for t in terms:
        tl = t.lower()
        pos = frag_low.find(tl)
        while pos != -1:
            spans.append((pos, pos + len(t)))
            pos = frag_low.find(tl, pos + len(t))
    spans.sort()
    return frag, spans


@dataclass
class _Plan:
    terms: list[str]
    from_clause: str
    where: str
    params: list[object]
    slow: bool          # 有走 LIKE 全表掃描


def _like_param(term: str) -> str:
    escaped = (term.replace("\\", "\\\\")
                   .replace("%", "\\%")
                   .replace("_", "\\_"))
    return f"%{escaped}%"


def _plan(query: str,
          roles: list[str] | None,
          projects: list[str] | None,
          session_kinds: list[str] | None,
          since: str | None,
          until: str | None,
          exclude_session_ids: set[str] | None = None) -> _Plan | None:
    """把查詢條件組成 SQL 片段，search 與 count 共用同一份，避免兩邊條件走偏。"""
    terms = split_terms(query)
    if not terms:
        return None

    fts_terms = [t for t in terms if len(t) >= MIN_TRIGRAM_LEN]
    like_terms = [t for t in terms if len(t) < MIN_TRIGRAM_LEN]

    where: list[str] = []
    params: list[object] = []
    joins = ""

    if fts_terms:
        joins = " JOIN blocks_fts f ON f.rowid = b.id"
        where.append("blocks_fts MATCH ?")
        # 多個詞是 AND 而不是片語；要片語就在查詢裡用雙引號框起來
        params.append(" AND ".join(_fts_quote(t) for t in fts_terms))

    # 短詞（以及 FTS 完全走不通時的全部詞）只能靠 LIKE 過濾
    for t in (like_terms if fts_terms else terms):
        where.append("b.text LIKE ? ESCAPE '\\'")
        params.append(_like_param(t))

    if roles:
        where.append("b.role IN (" + ",".join("?" * len(roles)) + ")")
        params.extend(roles)
    if projects:
        where.append("s.project_slug IN (" + ",".join("?" * len(projects)) + ")")
        params.extend(projects)
    if session_kinds:
        where.append("s.kind IN (" + ",".join("?" * len(session_kinds)) + ")")
        params.extend(session_kinds)
    if since:
        where.append("COALESCE(m.ts, s.last_ts) >= ?")
        params.append(since)
    if until:
        where.append("COALESCE(m.ts, s.last_ts) <= ?")
        params.append(until)
    if exclude_session_ids:
        # 被隱藏的 session 不該從搜尋結果冒出來。隱藏數量少，直接列舉即可。
        where.append("s.id NOT IN ("
                     + ",".join("?" * len(exclude_session_ids)) + ")")
        params.extend(sorted(exclude_session_ids))

    from_clause = (
        " FROM blocks b" + joins +
        " JOIN messages m ON m.uuid = b.message_uuid"
        " JOIN sessions s ON s.id = b.session_id"
    )
    return _Plan(terms=terms, from_clause=from_clause,
                 where=" AND ".join(where), params=params,
                 slow=not bool(fts_terms))


def search(conn: sqlite3.Connection,
           query: str,
           *,
           roles: list[str] | None = None,
           projects: list[str] | None = None,
           session_kinds: list[str] | None = None,
           since: str | None = None,
           until: str | None = None,
           exclude_session_ids: set[str] | None = None,
           limit: int = 50,
           offset: int = 0) -> tuple[list[Hit], bool]:
    """回傳 (命中列表, 是否走了慢速 LIKE 掃描)。"""
    plan = _plan(query, roles, projects, session_kinds, since, until,
                 exclude_session_ids)
    if plan is None:
        return [], False

    sql = (
        "SELECT b.id, b.session_id, b.role, b.tool_name, b.redacted, b.truncated,"
        " b.text, m.ts, s.project_slug, s.ai_title, s.custom_title, s.kind, s.cwd"
        + plan.from_clause +
        " WHERE " + plan.where +
        " ORDER BY COALESCE(m.ts, s.last_ts) DESC"
        " LIMIT ? OFFSET ?"
    )

    hits: list[Hit] = []
    for r in conn.execute(sql, [*plan.params, limit, offset]):
        snippet, spans = _make_snippet(r["text"] or "", plan.terms)
        hits.append(Hit(
            block_id=r["id"], session_id=r["session_id"],
            project_slug=r["project_slug"], ai_title=r["ai_title"],
            custom_title=r["custom_title"],
            kind=r["kind"], role=r["role"], tool_name=r["tool_name"],
            ts=r["ts"], cwd=r["cwd"],
            redacted=bool(r["redacted"]), truncated=bool(r["truncated"]),
            snippet=snippet, spans=spans,
        ))

    return hits, plan.slow


def count(conn: sqlite3.Connection,
          query: str,
          *,
          roles: list[str] | None = None,
          projects: list[str] | None = None,
          session_kinds: list[str] | None = None,
          since: str | None = None,
          until: str | None = None,
          exclude_session_ids: set[str] | None = None) -> int:
    plan = _plan(query, roles, projects, session_kinds, since, until,
                 exclude_session_ids)
    if plan is None:
        return 0
    sql = "SELECT COUNT(*) AS n" + plan.from_clause + " WHERE " + plan.where
    return int(conn.execute(sql, plan.params).fetchone()["n"])


def session_hit_counts(conn: sqlite3.Connection,
                       query: str,
                       *,
                       roles: list[str] | None = None,
                       projects: list[str] | None = None,
                       session_kinds: list[str] | None = None,
                       since: str | None = None,
                       until: str | None = None,
                       exclude_session_ids: set[str] | None = None,
                       limit: int = 100) -> list[dict]:
    """按 session 聚合命中數，給「哪幾個 session 談過這件事」的檢視用。"""
    plan = _plan(query, roles, projects, session_kinds, since, until,
                 exclude_session_ids)
    if plan is None:
        return []
    sql = (
        "SELECT s.id, s.kind, s.project_slug, s.ai_title, s.custom_title, s.cwd,"
        " MAX(COALESCE(m.ts, s.last_ts)) AS ts, COUNT(*) AS hits"
        + plan.from_clause +
        " WHERE " + plan.where +
        " GROUP BY s.id ORDER BY ts DESC LIMIT ?"
    )
    return [dict(r) for r in conn.execute(sql, [*plan.params, limit])]
