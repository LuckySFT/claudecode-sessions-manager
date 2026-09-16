"""核心邏輯測試。

只測「容易壞而且壞了不容易發現」的部分：
不完整尾行、增量追加、檔案縮小後重建、遮罩、區塊對位、搜尋分流。

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import browse, config, db, indexer, parser, redact, search  # noqa: E402


def user_rec(uuid: str, text: str, *, ts: str = "2026-01-01T00:00:00Z",
             session: str = "s1") -> dict:
    return {"type": "user", "uuid": uuid, "parentUuid": None, "sessionId": session,
            "timestamp": ts, "cwd": "d:\\proj", "gitBranch": "main",
            "version": "2.1.220",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def assistant_rec(uuid: str, blocks: list[dict], *,
                  ts: str = "2026-01-01T00:01:00Z", session: str = "s1",
                  usage: dict | None = None) -> dict:
    return {"type": "assistant", "uuid": uuid, "parentUuid": None,
            "sessionId": session, "timestamp": ts, "version": "2.1.220",
            "message": {"role": "assistant", "model": "claude-sonnet-5",
                        "content": blocks,
                        "usage": usage or {"input_tokens": 10, "output_tokens": 20,
                                           "cache_creation_input_tokens": 5,
                                           "cache_read_input_tokens": 100}}}


class TempEnv(unittest.TestCase):
    """把 config 指向暫存目錄，絕不碰真正的 ~/.claude。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsm-test-"))
        self.projects = self.tmp / "projects"
        (self.projects / "proj-a").mkdir(parents=True)
        # LABELS_DB_PATH 一定要一起覆寫。它是 import 時就從 DATA_DIR 算好的常數，
        # 只改 DATA_DIR 不會連動，測試會跑去讀寫真正的 data/labels.db。
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

    def write_session(self, name: str, records: list[dict], *,
                      trailing_partial: str | None = None,
                      append: bool = False) -> Path:
        path = self.projects / "proj-a" / f"{name}.jsonl"
        mode = "ab" if append else "wb"
        with path.open(mode) as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False).encode() + b"\n")
            if trailing_partial is not None:
                fh.write(trailing_partial.encode())
        return path


class TestIterLines(TempEnv):
    def test_incomplete_trailing_line_is_not_consumed(self) -> None:
        """進行中的 session 尾行寫一半時，不能吐出來，offset 也不能跨過去。"""
        path = self.write_session("s1", [user_rec("u1", "hello")],
                                  trailing_partial='{"type":"user","uuid":"u2"')
        lines = list(parser.iter_lines(path, 0))
        self.assertEqual(len(lines), 1)
        offset_after = lines[-1][2]
        self.assertLess(offset_after, path.stat().st_size)

        # 補完那一行後，從同一個 offset 續讀就會拿到完整的第二筆
        with path.open("ab") as fh:
            fh.write(b',"parentUuid":null,"message":{"role":"user",'
                     b'"content":[{"type":"text","text":"world"}]}}\n')
        rest = list(parser.iter_lines(path, offset_after))
        self.assertEqual(len(rest), 1)
        self.assertEqual(json.loads(rest[0][1])["uuid"], "u2")

    def test_corrupt_complete_line_is_skipped_but_advances(self) -> None:
        path = self.projects / "proj-a" / "s2.jsonl"
        path.write_bytes(b'{"broken\n' +
                         json.dumps(user_rec("u1", "ok")).encode() + b"\n")
        result = parser.parse_session(path, 0)
        self.assertEqual(result.bad_lines, 1)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.new_offset, path.stat().st_size)


class TestIncremental(TempEnv):
    def test_append_accumulates_without_duplicates(self) -> None:
        self.write_session("s1", [user_rec("u1", "第一題")])
        conn = db.connect()
        try:
            stats = indexer.reindex(conn)
            self.assertEqual((stats.rebuilt, stats.messages), (1, 1))

            # 沒改動 -> 跳過
            stats = indexer.reindex(conn)
            self.assertEqual((stats.skipped, stats.messages), (1, 0))

            # 追加兩筆 -> 只吃新的
            self.write_session("s1", [
                assistant_rec("a1", [{"type": "text", "text": "回答"}]),
                {"type": "ai-title", "aiTitle": "測試標題", "sessionId": "s1"},
            ], append=True)
            stats = indexer.reindex(conn)
            self.assertEqual((stats.appended, stats.messages), (1, 1))

            total = conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
            self.assertEqual(total, 2)
            title = conn.execute(
                "SELECT ai_title FROM sessions WHERE id='s1'").fetchone()["ai_title"]
            self.assertEqual(title, "測試標題")

            seqs = [r["seq"] for r in conn.execute(
                "SELECT seq FROM messages WHERE session_id='s1' ORDER BY seq")]
            self.assertEqual(seqs, [0, 1])
        finally:
            conn.close()

    def test_append_does_not_null_out_earlier_metadata(self) -> None:
        """新片段沒帶 cwd / ai-title 時，不能把既有值蓋成 NULL。"""
        self.write_session("s1", [
            user_rec("u1", "起頭"),
            {"type": "ai-title", "aiTitle": "原標題", "sessionId": "s1"},
        ])
        conn = db.connect()
        try:
            indexer.reindex(conn)
            # 這筆完全不帶 cwd / gitBranch / ai-title
            self.write_session("s1", [{
                "type": "assistant", "uuid": "a9", "parentUuid": None,
                "timestamp": "2026-01-02T00:00:00Z",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": "後續"}]},
            }], append=True)
            indexer.reindex(conn)
            row = conn.execute(
                "SELECT ai_title, cwd, git_branch, first_ts, last_ts"
                " FROM sessions WHERE id='s1'").fetchone()
            self.assertEqual(row["ai_title"], "原標題")
            self.assertEqual(row["cwd"], "d:\\proj")
            self.assertEqual(row["git_branch"], "main")
            self.assertTrue(row["first_ts"].startswith("2026-01-01"))
            self.assertTrue(row["last_ts"].startswith("2026-01-02"))
        finally:
            conn.close()

    def test_shrunk_file_triggers_full_rebuild(self) -> None:
        self.write_session("s1", [user_rec("u1", "a"), user_rec("u2", "b"),
                                  user_rec("u3", "c")])
        conn = db.connect()
        try:
            indexer.reindex(conn)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"], 3)

            # 檔案被改寫成更短的內容
            self.write_session("s1", [user_rec("z1", "重寫")])
            stats = indexer.reindex(conn)
            self.assertEqual(stats.rebuilt, 1)
            rows = [r["uuid"] for r in
                    conn.execute("SELECT uuid FROM messages")]
            self.assertEqual(rows, ["z1"])
            # FTS 也要跟著清乾淨，不能留舊資料
            hits, _ = search.search(conn, "重寫")
            self.assertEqual(len(hits), 1)
            self.assertEqual(search.count(conn, '"b"'), 0)
        finally:
            conn.close()

    def test_prune_missing_removes_vanished_sessions(self) -> None:
        path = self.write_session("s1", [user_rec("u1", "內容")])
        conn = db.connect()
        try:
            indexer.reindex(conn)
            path.unlink()
            self.assertEqual(indexer.prune_missing(conn), 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM blocks").fetchone()["n"], 0)
        finally:
            conn.close()


class TestSubagentDiscovery(TempEnv):
    def test_subagents_are_indexed_and_linked(self) -> None:
        self.write_session("s1", [user_rec("u1", "主線")])
        sub_dir = self.projects / "proj-a" / "s1" / "subagents"
        sub_dir.mkdir(parents=True)
        (sub_dir / "agent-abc123.jsonl").write_bytes(
            json.dumps({"type": "user", "uuid": "su1", "parentUuid": None,
                        "isSidechain": True, "agentId": "abc123",
                        "timestamp": "2026-01-01T00:05:00Z",
                        "message": {"role": "user",
                                    "content": "去查一下 cublas 的載入問題"}},
                       ensure_ascii=False).encode() + b"\n")
        conn = db.connect()
        try:
            indexer.reindex(conn)
            row = conn.execute(
                "SELECT id, kind, parent_session_id, agent_id FROM sessions"
                " WHERE kind='subagent'").fetchone()
            self.assertEqual(row["parent_session_id"], "s1")
            self.assertEqual(row["agent_id"], "abc123")
            self.assertEqual(row["id"], "s1/agent-abc123")

            # subagent 的內容要搜得到
            hits, _ = search.search(conn, "cublas")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].kind, "subagent")

            # 主 session 要能列出自己的 subagent
            detail = browse.get_session(conn, "s1")
            self.assertEqual(len(detail["subagents"]), 1)
            self.assertFalse(browse.resume_command(detail["subagents"][0]
                                                   | {"kind": "subagent"})["resumable"])

            # content 是純字串時，角色要跟著外層 type 而不是寫死
            role = conn.execute(
                "SELECT role FROM blocks WHERE session_id='s1/agent-abc123'"
            ).fetchone()["role"]
            self.assertEqual(role, "user")
        finally:
            conn.close()


class TestRedaction(unittest.TestCase):
    def test_inline_secrets_masked_but_field_name_kept(self) -> None:
        text, hit = redact.redact(
            "Server=db01;Database=erp;User Id=sa;Password=P@ssw0rd!;")
        self.assertTrue(hit)
        self.assertNotIn("P@ssw0rd", text)
        self.assertIn("Password=", text)      # 欄位名要留著才搜得到

    def test_token_prefixes(self) -> None:
        for secret in ("sk-ant-api03-abcdefghijklmnop",
                       "ghp_abcdefghijklmnopqrstuvwxyz01",
                       "AKIAIOSFODNN7EXAMPLE",
                       "xoxb-1234567890-abcdefg"):
            text, hit = redact.redact(f"token: {secret}")
            self.assertTrue(hit, secret)
            self.assertNotIn(secret, text)

    def test_url_embedded_credentials(self) -> None:
        text, hit = redact.redact("postgres://admin:hunter2@10.0.0.5:5432/db")
        self.assertTrue(hit)
        self.assertNotIn("hunter2", text)

    def test_secret_paths(self) -> None:
        for p in ("d:\\proj\\.env", "/app/.env.production", "keys/server.pem",
                  "C:\\Users\\x\\.ssh\\config", "legacy/Form1.dfm",
                  "secrets/db.json", "svc/credentials.json"):
            self.assertTrue(redact.is_secret_path(p), p)
        for p in ("src/.venv/pyvenv.cfg", "src/environment.ts", "docs/keynote.md",
                  "app/monkey.py"):
            self.assertFalse(redact.is_secret_path(p), p)

    def test_no_false_positive_on_plain_text(self) -> None:
        text, hit = redact.redact("這段只是在講 password 的設計，沒有值")
        self.assertFalse(hit)
        self.assertEqual(text, "這段只是在講 password 的設計，沒有值")


class TestRedactionInIndex(TempEnv):
    def test_env_attachment_is_fully_masked(self) -> None:
        self.write_session("s1", [{
            "type": "attachment", "uuid": "at1", "parentUuid": None,
            "timestamp": "2026-01-01T00:00:00Z",
            "attachment": {"type": "file", "filename": "d:\\proj\\.env",
                           "content": {"type": "text", "file": {
                               "filePath": "d:\\proj\\.env",
                               "content": "DB_PASSWORD=supersecret\nAPI_KEY=abc123"}}},
        }])
        conn = db.connect()
        try:
            indexer.reindex(conn)
            row = conn.execute(
                "SELECT text, redacted FROM blocks").fetchone()
            self.assertEqual(row["redacted"], 1)
            self.assertNotIn("supersecret", row["text"])
            # 明文不得留在索引裡的任何角落
            self.assertEqual(search.count(conn, "supersecret"), 0)
        finally:
            conn.close()

    def test_tool_use_reading_pem_is_masked(self) -> None:
        self.write_session("s1", [assistant_rec("a1", [{
            "type": "tool_use", "id": "t1", "name": "Read",
            "input": {"file_path": "d:\\certs\\server.key"},
        }])])
        conn = db.connect()
        try:
            indexer.reindex(conn)
            row = conn.execute("SELECT text, redacted FROM blocks").fetchone()
            self.assertEqual(row["redacted"], 1)
            self.assertNotIn("server.key", row["text"])
        finally:
            conn.close()


class TestBlockAlignment(TempEnv):
    def test_empty_thinking_does_not_shift_block_lookup(self) -> None:
        """空 thinking 區塊在索引時被丟掉，還原完整內容時位置不能跟著偏。"""
        long_text = "字" * (config.MAX_BLOCK_CHARS + 500)
        self.write_session("s1", [assistant_rec("a1", [
            {"type": "thinking", "thinking": "", "signature": "sig"},   # 會被丟掉
            {"type": "text", "text": long_text},
        ])])
        conn = db.connect()
        try:
            indexer.reindex(conn)
            rows = conn.execute(
                "SELECT id, role, truncated FROM blocks ORDER BY seq").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["role"], "assistant")
            self.assertEqual(rows[0]["truncated"], 1)

            full = browse.get_block_full(conn, rows[0]["id"])
            self.assertEqual(full["role"], "assistant")
            self.assertEqual(len(full["text"]), len(long_text))
        finally:
            conn.close()

    def test_multi_block_message_alignment(self) -> None:
        self.write_session("s1", [assistant_rec("a1", [
            {"type": "thinking", "thinking": "推理內容"},
            {"type": "text", "text": "結論"},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "echo hi"}},
        ])])
        conn = db.connect()
        try:
            indexer.reindex(conn)
            rows = conn.execute(
                "SELECT id, role FROM blocks ORDER BY seq").fetchall()
            self.assertEqual([r["role"] for r in rows],
                             ["thinking", "assistant", "tool_use"])
            for r in rows:
                full = browse.get_block_full(conn, r["id"])
                self.assertEqual(full["role"], r["role"])
            self.assertIn("echo hi", browse.get_block_full(conn, rows[2]["id"])["text"])
        finally:
            conn.close()


class TestSearch(TempEnv):
    def setUp(self) -> None:
        super().setUp()
        self.write_session("s1", [
            user_rec("u1", "Windows PowerShell 5.1 對無 BOM 檔案用 cp950 解碼"),
            assistant_rec("a1", [{"type": "text",
                                  "text": "中文註解必須是 Big5 可編碼字元"}]),
        ])
        self.conn = db.connect()
        indexer.reindex(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()

    def test_three_char_query_uses_fts(self) -> None:
        hits, slow = search.search(self.conn, "cp950")
        self.assertFalse(slow)
        self.assertEqual(len(hits), 1)

    def test_two_char_chinese_falls_back_to_like(self) -> None:
        """trigram 對 2 字查詢會回 0 筆，必須靠 LIKE 補上。"""
        hits, slow = search.search(self.conn, "中文")
        self.assertTrue(slow)
        self.assertEqual(len(hits), 1)

    def test_quoted_phrase_vs_and_terms(self) -> None:
        # 片語：要連在一起
        self.assertEqual(search.count(self.conn, '"BOM 檔案"'), 1)
        self.assertEqual(search.count(self.conn, '"檔案 BOM"'), 0)
        # 無引號：各自出現即可，順序無關
        self.assertEqual(search.count(self.conn, "檔案 BOM"), 1)

    def test_snippet_marks_all_matches(self) -> None:
        hits, _ = search.search(self.conn, "cp950")
        self.assertTrue(hits[0].spans)
        a, b = hits[0].spans[0]
        self.assertEqual(hits[0].snippet[a:b].lower(), "cp950")

    def test_role_and_project_filters(self) -> None:
        self.assertEqual(len(search.search(self.conn, "cp950", roles=["user"])[0]), 1)
        self.assertEqual(
            len(search.search(self.conn, "cp950", roles=["assistant"])[0]), 0)
        self.assertEqual(
            len(search.search(self.conn, "cp950", projects=["proj-a"])[0]), 1)
        self.assertEqual(
            len(search.search(self.conn, "cp950", projects=["nope"])[0]), 0)

    def test_like_wildcards_are_escaped(self) -> None:
        """使用者輸入的 % 不能被當成通用字元，否則什麼都會命中。"""
        self.assertEqual(search.count(self.conn, "%"), 0)
        self.assertEqual(search.count(self.conn, "_"), 0)

    def test_fts_special_chars_do_not_crash(self) -> None:
        for q in ['AND', 'OR', 'NEAR(a b)', 'foo"bar', '(((', '*', 'a-b-c']:
            search.search(self.conn, q)      # 不該丟例外

    def test_count_matches_search_result_length(self) -> None:
        for q in ("cp950", "中文", "檔案 BOM"):
            hits, _ = search.search(self.conn, q, limit=500)
            self.assertEqual(search.count(self.conn, q), len(hits), q)


class TestTokenAggregation(TempEnv):
    def test_usage_only_messages_still_counted(self) -> None:
        """thinking 只存簽章、沒有可索引文字的訊息，token 仍必須算進去。"""
        self.write_session("s1", [
            assistant_rec("a1", [{"type": "thinking", "thinking": "",
                                  "signature": "sig"}],
                          usage={"input_tokens": 3, "output_tokens": 700,
                                 "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 50}),
            assistant_rec("a2", [{"type": "text", "text": "結果"}],
                          usage={"input_tokens": 7, "output_tokens": 300,
                                 "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 50}),
        ])
        conn = db.connect()
        try:
            indexer.reindex(conn)
            s = browse.get_session(conn, "s1")
            self.assertEqual(s["out_tok"], 1000)
            self.assertEqual(s["in_tok"], 10)
            self.assertEqual(s["cache_read_tok"], 100)
            self.assertEqual(s["msg_count"], 2)
            # 但只有一個 block 有內容
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM blocks").fetchone()["n"], 1)
        finally:
            conn.close()


class TestResumeCommand(unittest.TestCase):
    def test_powershell_quoting_handles_apostrophes(self) -> None:
        cmd = browse.resume_command(
            {"kind": "main", "id": "abc", "cwd": "d:\\it's [work]"})
        self.assertIn("''s", cmd["powershell"])       # 單引號要成對重複
        self.assertIn("-LiteralPath", cmd["powershell"])   # [] 不能被當通用字元

    def test_subagent_is_not_resumable(self) -> None:
        cmd = browse.resume_command({"kind": "subagent", "id": "x/agent-1"})
        self.assertFalse(cmd["resumable"])


if __name__ == "__main__":
    unittest.main()
