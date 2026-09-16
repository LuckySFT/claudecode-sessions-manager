"""真改名（寫入原始 jsonl）與移除已刪除 session 的測試。

改名是「原始資料唯讀」的唯一例外，所以防護必須被測到位：
尾端不完整、進行中、subagent、含換行的標題，全部要擋下來。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import (archive, browse, config, db, indexer, labels,  # noqa: E402
                 purge, rename, search)


def user_rec(uuid: str, text: str, *, ts: str = "2026-01-01T00:00:00Z") -> dict:
    return {"type": "user", "uuid": uuid, "parentUuid": None,
            "timestamp": ts, "cwd": "d:\\proj",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


class Env(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsm-rn-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir(parents=True)
        self._saved = (config.PROJECTS_DIR, config.DB_PATH, config.DATA_DIR,
                       config.LABELS_DB_PATH, config.ARCHIVE_DIR)
        config.PROJECTS_DIR = self.projects
        config.DATA_DIR = self.tmp / "data"
        config.DB_PATH = config.DATA_DIR / "index.db"
        config.LABELS_DB_PATH = config.DATA_DIR / "labels.db"
        config.ARCHIVE_DIR = self.tmp / "archive"

    def tearDown(self) -> None:
        (config.PROJECTS_DIR, config.DB_PATH, config.DATA_DIR,
         config.LABELS_DB_PATH, config.ARCHIVE_DIR) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, slug: str, name: str, records: list[dict], *,
              partial: str | None = None) -> Path:
        d = self.projects / slug
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.jsonl"
        with p.open("wb") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False).encode() + b"\n")
            if partial is not None:
                fh.write(partial.encode())
        return p

    def age(self, path: Path, seconds: float) -> None:
        """把 mtime 往前推，模擬閒置。"""
        import os
        t = time.time() - seconds
        os.utime(path, (t, t))

    def build(self):
        conn = db.connect()
        indexer.reindex(conn)
        return conn


class TestRenameGuards(Env):
    def test_rejects_active_session(self) -> None:
        """剛寫入過的 session 不給改名。"""
        self.write("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            r = rename.rename_session(conn, "s1", "新名字")
            self.assertFalse(r.ok)
            self.assertIn("進行中", r.reason)
        finally:
            conn.close()

    def test_rejects_incomplete_trailing_line(self) -> None:
        """尾端不是換行代表最後一行還沒寫完，append 會把兩行黏在一起。"""
        p = self.write("proj-a", "s1", [user_rec("u1", "x")],
                       partial='{"type":"user","uuid":"u2"')
        conn = self.build()
        try:
            self.age(p, 9999)          # 就算閒置很久也不行
            r = rename.rename_session(conn, "s1", "新名字")
            self.assertFalse(r.ok)
            self.assertIn("最後一行還沒寫完", r.reason)
        finally:
            conn.close()

    def test_rejects_subagent(self) -> None:
        self.write("proj-a", "s1", [user_rec("u1", "main")])
        sub = self.projects / "proj-a" / "s1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-x.jsonl").write_bytes(
            json.dumps({"type": "user", "uuid": "su1", "agentId": "x",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "message": {"role": "user", "content": "hi"}},
                       ensure_ascii=False).encode() + b"\n")
        conn = self.build()
        try:
            r = rename.rename_session(conn, "s1/agent-x", "新名字")
            self.assertFalse(r.ok)
            self.assertIn("subagent", r.reason)
        finally:
            conn.close()

    def test_rejects_newline_in_title(self) -> None:
        """標題含換行會把一筆 JSON 拆成兩行，破壞整個檔案。"""
        p = self.write("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            self.age(p, 9999)
            for bad in ("有\n換行", "有\r換行"):
                r = rename.rename_session(conn, "s1", bad)
                self.assertFalse(r.ok, bad)
                self.assertIn("換行", r.reason)
        finally:
            conn.close()

    def test_rejects_empty_and_overlong(self) -> None:
        p = self.write("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            self.age(p, 9999)
            self.assertFalse(rename.rename_session(conn, "s1", "   ").ok)
            self.assertFalse(
                rename.rename_session(conn, "s1", "字" * 300).ok)
        finally:
            conn.close()

    def test_rejects_missing_source(self) -> None:
        p = self.write("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            p.unlink()
            r = rename.rename_session(conn, "s1", "新名字")
            self.assertFalse(r.ok)
            self.assertIn("已不存在", r.reason)
        finally:
            conn.close()

    def test_dry_run_writes_nothing(self) -> None:
        p = self.write("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            self.age(p, 9999)
            before = p.read_bytes()
            r = rename.rename_session(conn, "s1", "新名字", dry_run=True)
            self.assertTrue(r.ok)
            self.assertGreater(r.bytes_appended, 0)
            self.assertEqual(p.read_bytes(), before)
        finally:
            conn.close()


class TestRenameSuccess(Env):
    def test_appends_valid_record_without_touching_existing_bytes(self) -> None:
        p = self.write("proj-a", "s1", [
            user_rec("u1", "內容"),
            {"type": "ai-title", "aiTitle": "自動標題"},
        ])
        conn = self.build()
        try:
            self.age(p, 9999)
            before = p.read_bytes()

            r = rename.rename_session(conn, "s1", "我改的名字")
            self.assertTrue(r.ok, r.reason)
            self.assertEqual(r.previous, "自動標題")

            after = p.read_bytes()
            # 既有位元組必須一個字都沒動
            self.assertTrue(after.startswith(before))
            added = after[len(before):]
            rec = json.loads(added)
            self.assertEqual(rec, {"type": "custom-title",
                                   "customTitle": "我改的名字",
                                   "sessionId": "s1"})
            self.assertTrue(added.endswith(b"\n"))

            # 重新索引後就顯示新名字
            indexer.reindex(conn)
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["custom_title"], "我改的名字")
            self.assertEqual(row["display_title"], "我改的名字")
            self.assertEqual(row["ai_title"], "自動標題")
        finally:
            conn.close()

    def test_rename_twice_last_wins(self) -> None:
        p = self.write("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            self.age(p, 9999)
            self.assertTrue(rename.rename_session(conn, "s1", "第一次").ok)
            self.age(p, 9999)
            self.assertTrue(rename.rename_session(conn, "s1", "第二次").ok)
            indexer.reindex(conn)
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["display_title"], "第二次")
            # 兩筆都在檔案裡，舊的沒被刪
            body = p.read_text(encoding="utf-8")
            self.assertIn("第一次", body)
            self.assertIn("第二次", body)
        finally:
            conn.close()

    def test_appended_record_survives_incremental_index(self) -> None:
        """append 之後走增量掃描（不是重建）也要讀到。"""
        p = self.write("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            self.age(p, 9999)
            rename.rename_session(conn, "s1", "增量測試")
            stats = indexer.reindex(conn)
            self.assertEqual(stats.appended, 1)
            self.assertEqual(stats.rebuilt, 0)
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["display_title"], "增量測試")
        finally:
            conn.close()

    def test_unicode_title_round_trips(self) -> None:
        p = self.write("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            self.age(p, 9999)
            title = '中文與「引號」和 emoji 🚀 與 \\ 反斜線'
            self.assertTrue(rename.rename_session(conn, "s1", title).ok)
            indexer.reindex(conn)
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["display_title"], title)
        finally:
            conn.close()

    def test_can_rename_matches_rename_outcome(self) -> None:
        p = self.write("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            ok, reason = rename.can_rename(conn, "s1")
            self.assertFalse(ok)
            self.assertIn("進行中", reason)
            self.age(p, 9999)
            ok, reason = rename.can_rename(conn, "s1")
            self.assertTrue(ok, reason)
        finally:
            conn.close()


class TestPurge(Env):
    def setUp(self) -> None:
        super().setUp()
        self.p1 = self.write("proj-a", "gone-backed", [
            user_rec("u1", "已備份的內容 zzq1", ts="2026-01-01T00:00:00Z")])
        self.p2 = self.write("proj-a", "gone-unbacked", [
            user_rec("u2", "沒備份的內容 zzq2", ts="2026-06-01T00:00:00Z")])
        self.p3 = self.write("proj-b", "alive", [
            user_rec("u3", "還活著 zzq3", ts="2026-06-01T00:00:00Z")])
        self.conn = self.build()      # 三個都先進索引

        # 順序很重要：要讓 gone-unbacked 真的「從沒被歸檔過」，
        # 必須在第一次 archive.run() 之前就刪掉它。
        # （這重現了真實情境 —— 歸檔功能是在 Claude Code 已經清掉一批檔案之後才建的。）
        self.p2.unlink()
        archive.run()                 # 只歸檔到 p1 與 p3
        self.p1.unlink()              # p1 已備份，現在讓它「消失」
        archive.run()                 # 讓 manifest 認得 p1 的原始檔不見了

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()

    def test_plan_only_targets_missing_sources(self) -> None:
        p = purge.plan(self.conn)
        ids = sorted(c.session_id for c in p.candidates)
        self.assertEqual(ids, ["gone-backed", "gone-unbacked"])
        # 原始檔還在的絕對不能入列
        self.assertNotIn("alive", ids)

    def test_plan_separates_backed_up_from_unbacked(self) -> None:
        p = purge.plan(self.conn)
        self.assertEqual([c.session_id for c in p.unbacked], ["gone-unbacked"])
        backed = {c.session_id: c.archived for c in p.candidates}
        self.assertTrue(backed["gone-backed"])
        self.assertFalse(backed["gone-unbacked"])

    def test_plan_filters(self) -> None:
        p = purge.plan(self.conn, include_unbacked=False)
        self.assertEqual([c.session_id for c in p.candidates], ["gone-backed"])
        p = purge.plan(self.conn, include_backed_up=False)
        self.assertEqual([c.session_id for c in p.candidates], ["gone-unbacked"])
        p = purge.plan(self.conn, project="proj-a")
        self.assertEqual(len(p.candidates), 2)
        p = purge.plan(self.conn, session_ids=["gone-backed"])
        self.assertEqual(len(p.candidates), 1)

    def test_apply_without_confirm_does_nothing(self) -> None:
        p = purge.plan(self.conn)
        res = purge.apply(self.conn, p, confirm=False)
        self.assertEqual(res.removed_sessions, 0)
        self.assertTrue(res.errors)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"], 3)

    def test_index_depth_removes_rows_and_fts_but_keeps_blobs(self) -> None:
        before_blobs = archive.status()["blobs"]
        p = purge.plan(self.conn, depth="index",
                       session_ids=["gone-backed", "gone-unbacked"])
        res = purge.apply(self.conn, p, confirm=True)
        self.assertEqual(res.removed_sessions, 2)

        left = [r["id"] for r in self.conn.execute("SELECT id FROM sessions")]
        self.assertEqual(left, ["alive"])
        # FTS 也要清乾淨，否則會留下搜得到但點不開的鬼魂
        self.assertEqual(search.count(self.conn, "zzq1"), 0)
        self.assertEqual(search.count(self.conn, "zzq2"), 0)
        self.assertEqual(search.count(self.conn, "zzq3"), 1)
        # 歸檔 blob 必須留著
        self.assertEqual(archive.status()["blobs"], before_blobs)

    def test_index_depth_stays_restorable(self) -> None:
        p = purge.plan(self.conn, depth="index", session_ids=["gone-backed"])
        purge.apply(self.conn, p, confirm=True)
        res = archive.restore(session_id="gone-backed", dest_root=self.projects)
        self.assertEqual(len(res.restored), 1)
        self.assertTrue((self.projects / "proj-a/gone-backed.jsonl").exists())

    def test_full_depth_deletes_blobs(self) -> None:
        p = purge.plan(self.conn, depth="full", session_ids=["gone-backed"])
        res = purge.apply(self.conn, p, confirm=True)
        self.assertGreaterEqual(res.removed_blobs, 1)
        self.assertGreater(res.freed_blob_bytes, 0)
        self.assertFalse(res.errors, res.errors)
        # 之後就再也還原不回來了
        r = archive.restore(session_id="gone-backed", dest_root=self.projects)
        self.assertFalse(r.restored)

    def test_purge_removes_labels(self) -> None:
        labels.set_session_label("gone-unbacked", title="標籤", hidden=True)
        self.assertIn("gone-unbacked", labels.get_session_labels())
        p = purge.plan(self.conn, session_ids=["gone-unbacked"])
        res = purge.apply(self.conn, p, confirm=True)
        self.assertEqual(res.removed_labels, 1)
        self.assertNotIn("gone-unbacked", labels.get_session_labels())

    def test_purged_session_can_be_reindexed_after_restore(self) -> None:
        """清了索引之後，還原原始檔再掃一次要能完整回來。"""
        p = purge.plan(self.conn, depth="index", session_ids=["gone-backed"])
        purge.apply(self.conn, p, confirm=True)
        archive.restore(session_id="gone-backed", dest_root=self.projects)
        indexer.reindex(self.conn)
        ids = sorted(r["id"] for r in self.conn.execute("SELECT id FROM sessions"))
        self.assertIn("gone-backed", ids)
        self.assertEqual(search.count(self.conn, "zzq1"), 1)


if __name__ == "__main__":
    unittest.main()


class TestSchemaMigration(Env):
    """schema 變更必須用 ALTER TABLE，不能砍檔重建。

    砍檔重建會銷毀「原始檔已被 Claude Code 清掉」的 session 記錄 ——
    那些是唯一留存，沒有別的來源。2026-09-03 已經因此損失 140 個 session。
    """

    def test_migrate_adds_missing_column_without_touching_rows(self) -> None:
        self.write("proj-a", "s1", [
            user_rec("u1", "重要內容 mig1"),
            {"type": "custom-title", "customTitle": "不能消失", "sessionId": "s1"},
        ])
        conn = self.build()
        try:
            # 模擬舊版索引：把欄位拿掉（SQLite 3.35+ 支援 DROP COLUMN）
            conn.execute("ALTER TABLE sessions DROP COLUMN custom_title")
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
            self.assertNotIn("custom_title", cols)
            before = conn.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
        finally:
            conn.close()

        # 重新連線應該自動補回欄位，而且列數不變
        conn = db.connect()
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
            self.assertIn("custom_title", cols)
            after = conn.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
            self.assertEqual(after, before)
            # 內容也還在
            self.assertEqual(search.count(conn, "mig1"), 1)
        finally:
            conn.close()

    def test_migrate_is_idempotent(self) -> None:
        self.write("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            self.assertEqual(db.migrate(conn), [])
            self.assertEqual(db.migrate(conn), [])
        finally:
            conn.close()

    def test_orphan_session_record_survives_reconnect(self) -> None:
        """原始檔已消失的 session，重新連線（含 migrate）之後記錄必須還在。"""
        p = self.write("proj-a", "s1", [user_rec("u1", "唯一留存 orph1")])
        conn = self.build()
        conn.close()
        p.unlink()

        conn = db.connect()
        try:
            indexer.reindex(conn)      # 掃描時檔案已不存在
            ids = [r["id"] for r in conn.execute("SELECT id FROM sessions")]
            self.assertIn("s1", ids)
            self.assertEqual(search.count(conn, "orph1"), 1)
        finally:
            conn.close()


class TestPurgeSingle(Env):
    """單筆移除：從 session 詳細頁直接處理一筆，不影響其他 session。"""

    def setUp(self) -> None:
        super().setUp()
        self.a = self.write("proj-a", "aaa", [user_rec("u1", "內容 alpha")])
        self.b = self.write("proj-a", "bbb", [user_rec("u2", "內容 beta")])
        self.c = self.write("proj-a", "ccc", [user_rec("u3", "內容 gamma")])
        self.conn = self.build()
        # bbb 在歸檔之前就刪掉 -> 永遠沒有備份
        self.b.unlink()
        archive.run()
        self.a.unlink()          # aaa 已備份，之後才消失
        archive.run()

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()

    def test_removing_one_leaves_others_intact(self) -> None:
        p = purge.plan(self.conn, session_ids=["bbb"])
        self.assertEqual(p.total, 1)
        res = purge.apply(self.conn, p, confirm=True)
        self.assertEqual(res.removed_sessions, 1)

        left = sorted(r["id"] for r in self.conn.execute("SELECT id FROM sessions"))
        self.assertEqual(left, ["aaa", "ccc"])
        self.assertEqual(search.count(self.conn, "beta"), 0)
        self.assertEqual(search.count(self.conn, "alpha"), 1)
        self.assertEqual(search.count(self.conn, "gamma"), 1)

    def test_single_index_purge_stays_restorable(self) -> None:
        """單筆走 index 深度之後，還原 + 重新索引要能完整回來。"""
        p = purge.plan(self.conn, depth="index", session_ids=["aaa"])
        purge.apply(self.conn, p, confirm=True)
        self.assertEqual(search.count(self.conn, "alpha"), 0)
        # blob 必須還在
        self.assertGreater(archive.status()["blobs"], 0)

        archive.restore(session_id="aaa", dest_root=self.projects)
        indexer.reindex(self.conn)
        self.assertEqual(search.count(self.conn, "alpha"), 1)

    def test_single_full_purge_is_irreversible(self) -> None:
        p = purge.plan(self.conn, depth="full", session_ids=["aaa"])
        res = purge.apply(self.conn, p, confirm=True)
        self.assertGreaterEqual(res.removed_blobs, 1)
        r = archive.restore(session_id="aaa", dest_root=self.projects)
        self.assertFalse(r.restored)
        # 別人的 blob 不能被牽連
        self.assertEqual(search.count(self.conn, "gamma"), 1)

    def test_cannot_purge_a_session_whose_source_still_exists(self) -> None:
        p = purge.plan(self.conn, session_ids=["ccc"])
        self.assertEqual(p.total, 0)
