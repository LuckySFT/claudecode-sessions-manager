"""原始檔歸檔。

【為什麼不能拿索引當備份】
index.db 是有損的衍生物：文字截斷在 MAX_BLOCK_CHARS、遮罩不可逆、空區塊被丟掉、
tool-results 全文沒收、非對話類型的記錄整批跳過，連 JSON 結構本身都沒保留。
實測已被清掉的 140 個 session，36.8MB 原始資料在索引裡只剩 7.7MB 文字（21%）。
所以歸檔的對象是**原始位元組**，跟索引完全分開。

【儲存格式：內容定址 + 清單】
    archive/blobs/<sha 前 2 碼>/<sha>.xz    每個檔案獨立 lzma
    archive/manifest.db                     路徑 -> sha 的對照與歷史
內容定址讓三件事同時成立：沒變過的檔案自動去重、增量判斷就是比對雜湊、
還原單一檔案不用解開整包。

【為什麼用 lzma 不用 gzip】
session jsonl 有很大一部分是貼進對話的截圖，以 base64 存在
`message.content[].source.data`。實測最大的那個檔案 88% 是這種高熵資料。
圖片本身已經壓過了，gzip 只能吃掉 base64 的 4/3 膨脹（實測 55%），
lzma 能壓到 32%。歸檔是寫一次讀很少的用途，值得用慢一點換體積。

【這裡只做新增，永不刪除原始資料】
歸檔不碰 ~/.claude/ 底下任何東西。原始檔被 Claude Code 清掉之後，
blob 仍然留著，這正是這個模組存在的理由。
"""
from __future__ import annotations

import hashlib
import lzma
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import config, liveness

MANIFEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    sha256      TEXT PRIMARY KEY,
    raw_size    INTEGER NOT NULL,
    stored_size INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

-- 同一個路徑會有多個版本（session 是 append-only，每次成長就是新版本）
CREATE TABLE IF NOT EXISTS versions (
    rel_path   TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime      REAL NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    PRIMARY KEY (rel_path, sha256)
);
CREATE INDEX IF NOT EXISTS idx_versions_sha ON versions(sha256);

-- 每個路徑最近一次掃描的狀態。快速路徑靠它：size 與 mtime 都沒變就不重算雜湊。
CREATE TABLE IF NOT EXISTS current (
    rel_path       TEXT PRIMARY KEY,
    sha256         TEXT NOT NULL,
    size           INTEGER NOT NULL,
    mtime          REAL NOT NULL,
    source_missing INTEGER NOT NULL DEFAULT 0,
    checked_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    scanned     INTEGER NOT NULL DEFAULT 0,
    added       INTEGER NOT NULL DEFAULT 0,
    unchanged   INTEGER NOT NULL DEFAULT 0,
    bytes_added INTEGER NOT NULL DEFAULT 0,
    vanished    INTEGER NOT NULL DEFAULT 0,
    errors      TEXT
);
"""

# 歸檔對象：projects/ 底下的全部內容。
# jsonl 是逐字稿，tool-results/*.txt 是外置的大型工具輸出（主檔只留 2KB 預覽），
# memory/*.md 是專案記憶。三者都無法從索引重建，所以一起收。
ARCHIVE_SUFFIXES = {".jsonl", ".txt", ".md", ".json"}
CHUNK = 1 << 20


@dataclass
class ArchiveStats:
    scanned: int = 0
    added: int = 0
    unchanged: int = 0
    bytes_added: int = 0
    raw_bytes: int = 0
    vanished: int = 0
    errors: list[str] = field(default_factory=list)


def manifest_path() -> Path:
    return config.ARCHIVE_DIR / "manifest.db"


def blob_dir() -> Path:
    return config.ARCHIVE_DIR / "blobs"


def connect() -> sqlite3.Connection:
    config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(manifest_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(MANIFEST_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blob_file(sha: str) -> Path:
    return blob_dir() / sha[:2] / f"{sha}.xz"


def sha256_of(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def iter_source_files(root: Path | None = None) -> Iterator[Path]:
    base = Path(root or config.PROJECTS_DIR)
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*")):
        try:
            if path.is_file() and path.suffix.lower() in ARCHIVE_SUFFIXES:
                yield path
        except OSError:
            continue


def _store_blob(conn: sqlite3.Connection, src: Path, sha: str, raw_size: int) -> int:
    """把檔案壓成 blob。已存在就直接跳過（內容定址，同 sha 必定同內容）。"""
    dest = _blob_file(sha)
    if dest.exists():
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".xz.part")
    try:
        with src.open("rb") as fin, lzma.open(tmp, "wb", preset=6) as fout:
            shutil.copyfileobj(fin, fout, CHUNK)
        # 先寫暫存再改名：中途失敗不會留下半截的 blob 被當成完好的
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    stored = dest.stat().st_size
    conn.execute(
        "INSERT OR IGNORE INTO blobs(sha256, raw_size, stored_size, created_at)"
        " VALUES (?,?,?,?)", (sha, raw_size, stored, _now()))
    return stored


def run(*, root: Path | None = None,
        progress: Callable[[int, Path], None] | None = None) -> ArchiveStats:
    """掃描並歸檔。只新增，不刪除任何東西。"""
    base = Path(root or config.PROJECTS_DIR)
    conn = connect()
    stats = ArchiveStats()
    now = _now()

    cur = conn.execute("INSERT INTO runs(started_at) VALUES (?)", (now,))
    run_id = cur.lastrowid

    known = {r["rel_path"]: r for r in conn.execute(
        "SELECT rel_path, sha256, size, mtime FROM current")}
    seen: set[str] = set()

    try:
        for path in iter_source_files(base):
            stats.scanned += 1
            rel = path.relative_to(base).as_posix()
            seen.add(rel)
            if progress:
                progress(stats.scanned, path)

            try:
                st = path.stat()
            except OSError as exc:
                stats.errors.append(f"{rel}: stat 失敗 {exc!r}")
                continue

            prev = known.get(rel)
            # 快速路徑：大小與 mtime 都沒動，內容不可能變，省下整檔雜湊
            if prev and int(prev["size"]) == st.st_size \
                    and float(prev["mtime"]) == st.st_mtime:
                stats.unchanged += 1
                stats.raw_bytes += st.st_size
                conn.execute(
                    "UPDATE current SET source_missing=0, checked_at=?"
                    " WHERE rel_path=?", (now, rel))
                conn.execute(
                    "UPDATE versions SET last_seen=? WHERE rel_path=? AND sha256=?",
                    (now, rel, prev["sha256"]))
                continue

            try:
                sha, raw_size = sha256_of(path)
            except OSError as exc:
                stats.errors.append(f"{rel}: 讀取失敗 {exc!r}")
                continue

            stats.raw_bytes += raw_size
            if prev and prev["sha256"] == sha:
                # 內容沒變，只是 mtime 被動過
                stats.unchanged += 1
            else:
                try:
                    stats.bytes_added += _store_blob(conn, path, sha, raw_size)
                except OSError as exc:
                    stats.errors.append(f"{rel}: 寫入 blob 失敗 {exc!r}")
                    continue
                stats.added += 1

            conn.execute(
                "INSERT INTO versions(rel_path, sha256, size, mtime, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(rel_path, sha256) DO UPDATE SET"
                "  last_seen=excluded.last_seen, mtime=excluded.mtime",
                (rel, sha, raw_size, st.st_mtime, now, now))
            conn.execute(
                "INSERT INTO current(rel_path, sha256, size, mtime, source_missing, checked_at)"
                " VALUES (?,?,?,?,0,?)"
                " ON CONFLICT(rel_path) DO UPDATE SET"
                "  sha256=excluded.sha256, size=excluded.size, mtime=excluded.mtime,"
                "  source_missing=0, checked_at=excluded.checked_at",
                (rel, sha, raw_size, st.st_mtime, now))

        # 這輪沒掃到的路徑 = 原始檔已經不在了。標記，但 blob 一律保留。
        for rel in known:
            if rel not in seen:
                stats.vanished += 1
                conn.execute(
                    "UPDATE current SET source_missing=1, checked_at=? WHERE rel_path=?",
                    (now, rel))

        conn.execute(
            "UPDATE runs SET finished_at=?, scanned=?, added=?, unchanged=?,"
            " bytes_added=?, vanished=?, errors=? WHERE id=?",
            (_now(), stats.scanned, stats.added, stats.unchanged,
             stats.bytes_added, stats.vanished,
             "\n".join(stats.errors) or None, run_id))
    finally:
        conn.close()
    return stats


def status() -> dict:
    """歸檔概況。"""
    if not manifest_path().exists():
        return {"exists": False}
    conn = connect()
    try:
        b = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(raw_size),0) raw,"
            " COALESCE(SUM(stored_size),0) stored FROM blobs").fetchone()
        c = conn.execute(
            "SELECT COUNT(*) n, SUM(source_missing) missing,"
            " COALESCE(SUM(size),0) bytes FROM current").fetchone()
        v = conn.execute("SELECT COUNT(*) n FROM versions").fetchone()
        r = conn.execute(
            "SELECT started_at, finished_at, scanned, added, unchanged, vanished,"
            " bytes_added FROM runs WHERE finished_at IS NOT NULL"
            " ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "exists": True,
            "files": c["n"],
            "files_source_missing": c["missing"] or 0,
            "tracked_bytes": c["bytes"],
            "blobs": b["n"],
            "raw_bytes": b["raw"],
            "stored_bytes": b["stored"],
            "ratio": (b["stored"] / b["raw"]) if b["raw"] else None,
            "versions": v["n"],
            "last_run": dict(r) if r else None,
            "archive_dir": str(config.ARCHIVE_DIR),
        }
    finally:
        conn.close()


def list_archived(*, only_missing: bool = False, pattern: str | None = None,
                  limit: int = 500) -> list[dict]:
    conn = connect()
    try:
        where, params = [], []
        if only_missing:
            where.append("c.source_missing = 1")
        if pattern:
            where.append("c.rel_path LIKE ?")
            params.append(f"%{pattern}%")
        sql = (
            "SELECT c.rel_path, c.sha256, c.size, c.mtime, c.source_missing,"
            " c.checked_at, b.stored_size,"
            " (SELECT COUNT(*) FROM versions v WHERE v.rel_path = c.rel_path) AS versions"
            " FROM current c LEFT JOIN blobs b ON b.sha256 = c.sha256"
            + (" WHERE " + " AND ".join(where) if where else "") +
            " ORDER BY c.rel_path LIMIT ?"
        )
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def archived_session_ids() -> set[str]:
    """回傳已歸檔的 session id 集合（主 session 與 subagent 都算）。

    路徑形如 `<slug>/<sessionId>.jsonl`
             `<slug>/<sessionId>/subagents/agent-<id>.jsonl`
    對應索引裡的 id：`<sessionId>` 與 `<sessionId>/agent-<id>`。
    """
    if not manifest_path().exists():
        return set()
    conn = connect()
    try:
        out: set[str] = set()
        for r in conn.execute("SELECT rel_path FROM current"):
            parts = r["rel_path"].split("/")
            if len(parts) == 2 and parts[1].endswith(".jsonl"):
                out.add(parts[1][:-6])
            elif len(parts) == 4 and parts[2] == "subagents" \
                    and parts[3].endswith(".jsonl"):
                out.add(f"{parts[1]}/{parts[3][:-6]}")
        return out
    finally:
        conn.close()


def read_blob(sha: str) -> bytes:
    path = _blob_file(sha)
    if not path.exists():
        raise FileNotFoundError(f"blob 不存在：{sha}")
    with lzma.open(path, "rb") as fh:
        return fh.read()


def verify(*, deep: bool = True, limit: int | None = None) -> dict:
    """檢查 blob 是否完好。deep=True 會實際解壓並重算雜湊。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT sha256, raw_size FROM blobs ORDER BY created_at"
            + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    finally:
        conn.close()

    result = {"checked": 0, "ok": 0, "missing": [], "corrupt": []}
    for r in rows:
        sha = r["sha256"]
        result["checked"] += 1
        path = _blob_file(sha)
        if not path.exists():
            result["missing"].append(sha)
            continue
        if not deep:
            result["ok"] += 1
            continue
        try:
            h = hashlib.sha256()
            with lzma.open(path, "rb") as fh:
                while chunk := fh.read(CHUNK):
                    h.update(chunk)
            if h.hexdigest() == sha:
                result["ok"] += 1
            else:
                result["corrupt"].append(sha)
        except (OSError, EOFError, lzma.LZMAError):
            result["corrupt"].append(sha)
    return result


@dataclass
class RestoreResult:
    restored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # 因為目標是進行中的 session 而被擋下來的。跟 failed 分開：
    # failed 是出錯，blocked 是刻意拒絕，兩者要給使用者不同的處理方式。
    blocked: list[str] = field(default_factory=list)


def restore(*, rel_paths: list[str] | None = None,
            session_id: str | None = None,
            dest_root: Path | None = None,
            overwrite: bool = False,
            force: bool = False,
            keep_mtime: bool = False) -> RestoreResult:
    """把歸檔的檔案寫回去。

    keep_mtime 預設 False 是刻意的：Claude Code 依 mtime 判斷保留期，
    還原成原始的舊 mtime 會讓檔案在下次啟動時**立刻又被刪掉**。
    保留當下時間才能真的活下來。要做鑑識用途的原樣還原才把它打開。

    【overwrite 與 force 是兩件不同的事】
    - overwrite：目標已存在也要寫。不加就跳過（restore 的主要用途是原始檔已被
      清掉，那時目標不存在，根本不會走到這裡）。
    - force：即使目標是**進行中的 session** 也要寫。
      這一層是後來補的 —— 原本只有 rename 有進行中防護，restore 漏了，
      而 restore 是**整檔覆寫**，比 rename 的 append 危險得多：
      append 最多多一行垃圾，覆寫是整個對話被舊版本蓋掉，而且 Claude Code
      那端不會報錯。所以 overwrite 不含 force，要明確再要一次。
    """
    base = Path(dest_root or config.PROJECTS_DIR)
    conn = connect()
    res = RestoreResult()
    try:
        if session_id:
            rows = conn.execute(
                "SELECT rel_path, sha256 FROM current"
                " WHERE rel_path LIKE ? OR rel_path LIKE ?",
                (f"%/{session_id}.jsonl", f"%/{session_id}/%")).fetchall()
        elif rel_paths:
            q = ",".join("?" * len(rel_paths))
            rows = conn.execute(
                f"SELECT rel_path, sha256 FROM current WHERE rel_path IN ({q})",
                rel_paths).fetchall()
        else:
            rows = conn.execute("SELECT rel_path, sha256 FROM current").fetchall()

        for r in rows:
            rel, sha = r["rel_path"], r["sha256"]
            target = base / rel
            if target.exists() and not overwrite:
                res.skipped.append(rel)
                continue
            # 要覆蓋既有檔案就得先確認它不是進行中的 session。
            # 只在真的會覆寫時檢查 —— 目標不存在是 restore 的正常路徑，
            # 不該為此付 stat 的成本，也不該擋。
            if target.exists() and not force:
                ok, reason, _ = liveness.check_overwritable(target)
                if not ok:
                    res.blocked.append(f"{rel}: {reason}")
                    continue
            try:
                data = read_blob(sha)
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(data)
                tmp.replace(target)
                if keep_mtime:
                    m = conn.execute(
                        "SELECT mtime FROM current WHERE rel_path=?", (rel,)
                    ).fetchone()
                    if m:
                        import os
                        os.utime(target, (time.time(), float(m["mtime"])))
                res.restored.append(rel)
            except (OSError, FileNotFoundError, EOFError, lzma.LZMAError) as exc:
                res.failed.append(f"{rel}: {exc!r}")
    finally:
        conn.close()
    return res
