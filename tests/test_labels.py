"""session 更名／隱藏、專案別名合併、專案排序的測試。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import browse, config, db, indexer, labels, search  # noqa: E402


def user_rec(uuid: str, text: str, *, ts: str = "2026-01-01T00:00:00Z") -> dict:
    return {"type": "user", "uuid": uuid, "parentUuid": None,
            "timestamp": ts, "cwd": "d:\\proj",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


class LabelEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsm-lbl-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir(parents=True)
        self._saved = (config.PROJECTS_DIR, config.DB_PATH, config.DATA_DIR,
                       config.LABELS_DB_PATH)
        config.PROJECTS_DIR = self.projects
        config.DATA_DIR = self.tmp / "data"
        config.DB_PATH = config.DATA_DIR / "index.db"
        config.LABELS_DB_PATH = config.DATA_DIR / "labels.db"

    def tearDown(self) -> None:
        (config.PROJECTS_DIR, config.DB_PATH, config.DATA_DIR,
         config.LABELS_DB_PATH) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def session(self, slug: str, name: str, records: list[dict]) -> None:
        d = self.projects / slug
        d.mkdir(parents=True, exist_ok=True)
        with (d / f"{name}.jsonl").open("wb") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False).encode() + b"\n")

    def build(self) -> db.sqlite3.Connection:
        conn = db.connect()
        indexer.reindex(conn)
        return conn


class TestClaudeCodeCustomTitle(LabelEnv):
    """Claude Code 自己的改名機制：append 一筆 custom-title 記錄，最後一筆勝出。

    漏讀這個類型的後果：只有 custom-title 而沒有 ai-title 的 session 會顯示成
    「(無標題)」，有 ai-title 的則顯示過期的舊名字（實測本機有 7 個這種 session）。
    """

    def test_custom_title_beats_ai_title(self) -> None:
        self.session("proj-a", "s1", [
            user_rec("u1", "內容"),
            {"type": "ai-title", "aiTitle": "AI 產生的標題"},
            {"type": "custom-title", "customTitle": "我在 CC 改的名字",
             "sessionId": "s1"},
        ])
        conn = self.build()
        try:
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["custom_title"], "我在 CC 改的名字")
            self.assertEqual(row["ai_title"], "AI 產生的標題")
            self.assertEqual(row["display_title"], "我在 CC 改的名字")
        finally:
            conn.close()

    def test_last_custom_title_wins(self) -> None:
        """改名是 append，同一個 session 會累積很多筆（實測某個有 46 筆）。"""
        self.session("proj-a", "s1", [
            user_rec("u1", "內容"),
            {"type": "custom-title", "customTitle": "第一版", "sessionId": "s1"},
            {"type": "custom-title", "customTitle": "第二版", "sessionId": "s1"},
            {"type": "custom-title", "customTitle": "最終版", "sessionId": "s1"},
        ])
        conn = self.build()
        try:
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["display_title"], "最終版")
        finally:
            conn.close()

    def test_custom_title_only_session_is_not_untitled(self) -> None:
        """沒有 ai-title 只有 custom-title 的 session 不能顯示成「(無標題)」。"""
        self.session("proj-a", "s1", [
            user_rec("u1", "內容"),
            {"type": "custom-title", "customTitle": "Top 10 emails",
             "sessionId": "s1"},
        ])
        conn = self.build()
        try:
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertIsNone(row["ai_title"])
            self.assertEqual(row["display_title"], "Top 10 emails")
        finally:
            conn.close()

    def test_custom_title_survives_incremental_append(self) -> None:
        """後續片段沒帶 custom-title 時，不能把既有值蓋成 NULL。"""
        self.session("proj-a", "s1", [
            user_rec("u1", "起頭"),
            {"type": "custom-title", "customTitle": "保留這個名字",
             "sessionId": "s1"},
        ])
        conn = self.build()
        try:
            path = self.projects / "proj-a" / "s1.jsonl"
            with path.open("ab") as fh:
                fh.write(json.dumps(user_rec("u2", "後續"),
                                    ensure_ascii=False).encode() + b"\n")
            indexer.reindex(conn)
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["display_title"], "保留這個名字")
        finally:
            conn.close()

    def test_local_label_beats_claude_code_custom_title(self) -> None:
        """兩層覆寫的優先序：本工具的 > Claude Code 的 > ai-title。"""
        self.session("proj-a", "s1", [
            user_rec("u1", "內容"),
            {"type": "ai-title", "aiTitle": "AI 標題"},
            {"type": "custom-title", "customTitle": "CC 改的", "sessionId": "s1"},
        ])
        conn = self.build()
        try:
            labels.set_session_label("s1", title="本工具改的")
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["label_title"], "本工具改的")
            self.assertEqual(row["custom_title"], "CC 改的")
            self.assertEqual(row["display_title"], "本工具改的")

            # 清除本工具的覆寫後要退回 Claude Code 的名稱，不是 ai-title
            labels.set_session_label("s1", clear_title=True)
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["display_title"], "CC 改的")
        finally:
            conn.close()

    def test_search_results_show_custom_title(self) -> None:
        self.session("proj-a", "s1", [
            user_rec("u1", "獨特關鍵字 zzqq"),
            {"type": "ai-title", "aiTitle": "舊名字"},
            {"type": "custom-title", "customTitle": "新名字", "sessionId": "s1"},
        ])
        conn = self.build()
        try:
            hits, _ = search.search(conn, "zzqq")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].custom_title, "新名字")
            self.assertEqual(hits[0].ai_title, "舊名字")
        finally:
            conn.close()


class TestSessionRename(LabelEnv):
    def test_local_label_overrides_ai_title(self) -> None:
        self.session("proj-a", "s1", [
            user_rec("u1", "內容"),
            {"type": "ai-title", "aiTitle": "自動產生的標題"},
        ])
        conn = self.build()
        try:
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["display_title"], "自動產生的標題")
            self.assertIsNone(row["label_title"])

            labels.set_session_label("s1", title="我自己取的名字")
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertEqual(row["display_title"], "我自己取的名字")
            self.assertEqual(row["label_title"], "我自己取的名字")
            # 原始的 ai_title 必須保持不動，才能還原
            self.assertEqual(row["ai_title"], "自動產生的標題")
        finally:
            conn.close()

    def test_clear_title_falls_back_to_ai_title(self) -> None:
        self.session("proj-a", "s1", [
            user_rec("u1", "x"), {"type": "ai-title", "aiTitle": "原標題"}])
        conn = self.build()
        try:
            labels.set_session_label("s1", title="改過的")
            labels.set_session_label("s1", clear_title=True)
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertIsNone(row["label_title"])
            self.assertEqual(row["display_title"], "原標題")
        finally:
            conn.close()

    def test_blank_title_is_treated_as_cleared(self) -> None:
        self.session("proj-a", "s1", [
            user_rec("u1", "x"), {"type": "ai-title", "aiTitle": "原標題"}])
        conn = self.build()
        try:
            labels.set_session_label("s1", title="   ")
            row = browse.list_sessions(conn, limit=5)[0]
            self.assertIsNone(row["label_title"])
            self.assertEqual(row["display_title"], "原標題")
        finally:
            conn.close()

    def test_labels_survive_index_rebuild(self) -> None:
        """索引是可拋棄的，標籤不是 —— 砍掉重建索引後標籤必須還在。"""
        self.session("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        labels.set_session_label("s1", title="保留我", hidden=True)
        conn.close()

        for suffix in ("", "-wal", "-shm"):
            p = Path(str(config.DB_PATH) + suffix)
            p.unlink(missing_ok=True)

        conn = self.build()
        try:
            row = browse.list_sessions(conn, include_hidden=True, limit=5)[0]
            self.assertEqual(row["label_title"], "保留我")
            self.assertTrue(row["hidden"])
        finally:
            conn.close()

    def test_empty_label_row_is_removed(self) -> None:
        self.session("proj-a", "s1", [user_rec("u1", "x")])
        conn = self.build()
        try:
            labels.set_session_label("s1", title="abc")
            labels.set_session_label("s1", clear_title=True)
            lconn = labels.connect()
            try:
                n = lconn.execute(
                    "SELECT COUNT(*) n FROM session_labels").fetchone()["n"]
            finally:
                lconn.close()
            self.assertEqual(n, 0)
        finally:
            conn.close()


class TestSessionHide(LabelEnv):
    def setUp(self) -> None:
        super().setUp()
        self.session("proj-a", "keep", [user_rec("u1", "保留的內容 cp950")])
        self.session("proj-a", "junk", [user_rec("u2", "垃圾測試 cp950")])
        self.conn = self.build()
        labels.set_session_label("junk", hidden=True)

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()

    def test_hidden_excluded_by_default(self) -> None:
        ids = [r["id"] for r in browse.list_sessions(self.conn, limit=10)]
        self.assertEqual(ids, ["keep"])

    def test_include_hidden_shows_both(self) -> None:
        ids = sorted(r["id"] for r in browse.list_sessions(
            self.conn, include_hidden=True, limit=10))
        self.assertEqual(ids, ["junk", "keep"])

    def test_hidden_only_lists_just_hidden(self) -> None:
        ids = [r["id"] for r in browse.list_sessions(
            self.conn, hidden_only=True, limit=10)]
        self.assertEqual(ids, ["junk"])

    def test_count_matches_filtered_list(self) -> None:
        self.assertEqual(browse.count_sessions(self.conn, kind="main"), 1)
        self.assertEqual(
            browse.count_sessions(self.conn, kind="main", include_hidden=True), 2)
        self.assertEqual(
            browse.count_sessions(self.conn, kind="main", hidden_only=True), 1)

    def test_hidden_session_excluded_from_search(self) -> None:
        """隱藏的 session 不該從搜尋結果冒出來，否則等於沒隱藏。"""
        hidden = labels.hidden_session_ids()
        self.assertEqual(hidden, {"junk"})

        hits, _ = search.search(self.conn, "cp950")
        self.assertEqual(len(hits), 2)          # 不排除時兩筆都在

        hits, _ = search.search(self.conn, "cp950", exclude_session_ids=hidden)
        self.assertEqual([h.session_id for h in hits], ["keep"])
        self.assertEqual(
            search.count(self.conn, "cp950", exclude_session_ids=hidden), 1)

    def test_unhide_restores_visibility(self) -> None:
        labels.set_session_label("junk", hidden=False)
        ids = sorted(r["id"] for r in browse.list_sessions(self.conn, limit=10))
        self.assertEqual(ids, ["junk", "keep"])


class TestProjectAlias(LabelEnv):
    def setUp(self) -> None:
        super().setUp()
        # 同一個專案搬過目錄，歷史被拆成兩個 slug
        self.session("d--work-old", "s1", [
            user_rec("u1", "舊路徑的工作 alpha", ts="2026-01-01T00:00:00Z")])
        self.session("d--work-new", "s2", [
            user_rec("u2", "新路徑的工作 alpha", ts="2026-02-01T00:00:00Z")])
        self.session("d--other", "s3", [
            user_rec("u3", "不相關 alpha", ts="2026-03-01T00:00:00Z")])
        self.conn = self.build()

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()

    def test_without_alias_projects_are_separate(self) -> None:
        rows = browse.list_projects(self.conn)
        self.assertEqual(len(rows), 3)

    def test_alias_merges_projects_into_one_row(self) -> None:
        labels.set_alias("d--work-old", "work")
        labels.set_alias("d--work-new", "work")
        rows = browse.list_projects(self.conn)
        self.assertEqual(len(rows), 2)

        merged = next(r for r in rows if r["project_display"] == "work")
        self.assertEqual(sorted(merged["slugs"]), ["d--work-new", "d--work-old"])
        self.assertEqual(merged["sessions"], 2)
        self.assertTrue(merged["is_alias"])
        # 合併後的最後活動時間要取兩者的較新者
        self.assertTrue(merged["last_ts"].startswith("2026-02-01"))

    def test_filtering_by_alias_covers_all_member_slugs(self) -> None:
        labels.set_alias("d--work-old", "work")
        labels.set_alias("d--work-new", "work")
        self.assertCountEqual(labels.resolve_slugs("work"),
                              ["d--work-old", "d--work-new"])

        ids = sorted(r["id"] for r in browse.list_sessions(
            self.conn, project="work", limit=10))
        self.assertEqual(ids, ["s1", "s2"])
        self.assertEqual(browse.count_sessions(self.conn, project="work",
                                               kind="main"), 2)

    def test_unknown_name_is_treated_as_slug(self) -> None:
        """沒設過別名的名稱要當成 slug 直接用，不能查不到東西。"""
        self.assertEqual(labels.resolve_slugs("d--other"), ["d--other"])
        ids = [r["id"] for r in browse.list_sessions(
            self.conn, project="d--other", limit=10)]
        self.assertEqual(ids, ["s3"])

    def test_clearing_alias_splits_again(self) -> None:
        labels.set_alias("d--work-old", "work")
        labels.set_alias("d--work-new", "work")
        labels.set_alias("d--work-new", None)
        rows = browse.list_projects(self.conn)
        self.assertEqual(len(rows), 3)

    def test_alias_equal_to_slug_is_not_stored(self) -> None:
        labels.set_alias("d--other", "d--other")
        self.assertNotIn("d--other", labels.get_aliases())


class TestProjectSort(LabelEnv):
    def setUp(self) -> None:
        super().setUp()
        # 大小寫混雜，重現真實資料的狀況
        self.session("D--zeta", "s1", [user_rec("u1", "z" * 10)])
        self.session("d--alpha", "s2", [user_rec("u2", "a" * 400)])
        self.session("D--mid", "s3", [user_rec("u3", "m" * 100)])
        self.session("d--alpha", "s4", [user_rec("u4", "a2")])
        self.conn = self.build()

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()

    def test_name_order_is_case_insensitive(self) -> None:
        """SQLite 預設大小寫敏感，會把 D-- 全排在 d-- 前面，變成兩坨。"""
        names = [r["project_display"]
                 for r in browse.list_projects(self.conn, order="name")]
        self.assertEqual(names, ["d--alpha", "D--mid", "D--zeta"])

    def test_size_order(self) -> None:
        rows = browse.list_projects(self.conn, order="size")
        self.assertEqual(rows[0]["project_display"], "d--alpha")
        sizes = [r["bytes"] for r in rows]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_sessions_order(self) -> None:
        rows = browse.list_projects(self.conn, order="sessions")
        self.assertEqual(rows[0]["project_display"], "d--alpha")
        self.assertEqual(rows[0]["sessions"], 2)

    def test_unarchived_order_counts_only_missing_from_archive(self) -> None:
        rows = browse.list_projects(self.conn, order="unarchived",
                                    archived_ids={"s2", "s4"})
        by_name = {r["project_display"]: r for r in rows}
        self.assertEqual(by_name["d--alpha"]["unarchived"], 0)
        self.assertEqual(by_name["D--zeta"]["unarchived"], 1)
        # 未備份最多的排最前面
        self.assertGreater(rows[0]["unarchived"], 0)

    def test_alias_merge_respects_name_order(self) -> None:
        labels.set_alias("D--zeta", "aaa-merged")
        labels.set_alias("D--mid", "aaa-merged")
        names = [r["project_display"]
                 for r in browse.list_projects(self.conn, order="name")]
        self.assertEqual(names, ["aaa-merged", "d--alpha"])


if __name__ == "__main__":
    unittest.main()
