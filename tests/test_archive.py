"""歸檔與還原的測試。

重點在「備份必須真的能還原」這件事本身 —— 一個沒驗證過還原的備份等於沒有備份。
"""
from __future__ import annotations

import lzma
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import archive, config, liveness  # noqa: E402


class ArchiveEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccsm-arc-"))
        self.src = self.tmp / "projects"
        (self.src / "proj-a").mkdir(parents=True)
        self._saved = (config.PROJECTS_DIR, config.ARCHIVE_DIR)
        config.PROJECTS_DIR = self.src
        config.ARCHIVE_DIR = self.tmp / "archive"

    def tearDown(self) -> None:
        config.PROJECTS_DIR, config.ARCHIVE_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, data: bytes) -> Path:
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p


class TestRoundTrip(ArchiveEnv):
    def test_archive_then_restore_is_byte_identical(self) -> None:
        payload = {
            "proj-a/s1.jsonl": b'{"type":"user","uuid":"u1"}\n{"type":"assistant"}\n',
            "proj-a/s1/subagents/agent-x.jsonl": b'{"type":"user","agentId":"x"}\n',
            "proj-a/s1/tool-results/out.txt": b"\xe4\xb8\xad\xe6\x96\x87 output\r\n",
            "proj-a/memory/MEMORY.md": "# 記憶\n- 一行\n".encode(),
        }
        for rel, data in payload.items():
            self.write(rel, data)

        stats = archive.run()
        self.assertEqual(stats.added, 4)
        self.assertEqual(stats.scanned, 4)

        dest = self.tmp / "restored"
        res = archive.restore(dest_root=dest)
        self.assertEqual(len(res.restored), 4)
        self.assertFalse(res.failed)
        for rel, data in payload.items():
            self.assertEqual((dest / rel).read_bytes(), data, rel)

    def test_binary_content_survives(self) -> None:
        """jsonl 內嵌 base64 圖片，不能被當文字處理而壞掉。"""
        blob = bytes(range(256)) * 300
        self.write("proj-a/s1.jsonl", blob)
        archive.run()
        dest = self.tmp / "restored"
        archive.restore(dest_root=dest)
        self.assertEqual((dest / "proj-a/s1.jsonl").read_bytes(), blob)


class TestIncremental(ArchiveEnv):
    def test_unchanged_files_are_not_restored_again(self) -> None:
        self.write("proj-a/s1.jsonl", b'{"a":1}\n')
        first = archive.run()
        self.assertEqual((first.added, first.unchanged), (1, 0))

        second = archive.run()
        self.assertEqual((second.added, second.unchanged), (0, 1))
        self.assertEqual(second.bytes_added, 0)

    def test_growth_creates_new_version_and_keeps_old_blob(self) -> None:
        p = self.write("proj-a/s1.jsonl", b'{"a":1}\n')
        archive.run()
        with p.open("ab") as fh:
            fh.write(b'{"a":2}\n')
        stats = archive.run()
        self.assertEqual(stats.added, 1)

        conn = archive.connect()
        try:
            versions = conn.execute(
                "SELECT COUNT(*) n FROM versions WHERE rel_path='proj-a/s1.jsonl'"
            ).fetchone()["n"]
            blobs = conn.execute("SELECT COUNT(*) n FROM blobs").fetchone()["n"]
        finally:
            conn.close()
        # 舊版本的 blob 必須留著，這是備份的意義
        self.assertEqual(versions, 2)
        self.assertEqual(blobs, 2)

    def test_same_content_two_paths_shares_one_blob(self) -> None:
        self.write("proj-a/s1.jsonl", b'{"same":1}\n')
        self.write("proj-a/s2.jsonl", b'{"same":1}\n')
        stats = archive.run()
        self.assertEqual(stats.scanned, 2)
        conn = archive.connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM blobs").fetchone()["n"], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM current").fetchone()["n"], 2)
        finally:
            conn.close()


class TestVanishedSource(ArchiveEnv):
    def test_deleted_source_keeps_blob_and_is_flagged(self) -> None:
        """Claude Code 清掉原始檔之後，歸檔必須還在 —— 這是整個模組的存在理由。"""
        p = self.write("proj-a/s1.jsonl", b'{"important":true}\n')
        archive.run()
        p.unlink()

        stats = archive.run()
        self.assertEqual(stats.vanished, 1)
        self.assertEqual(stats.scanned, 0)

        st = archive.status()
        self.assertEqual(st["files_source_missing"], 1)
        self.assertEqual(st["blobs"], 1)

        rows = archive.list_archived(only_missing=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rel_path"], "proj-a/s1.jsonl")

        # 而且還原得回來
        res = archive.restore(dest_root=self.src)
        self.assertEqual(res.restored, ["proj-a/s1.jsonl"])
        self.assertEqual(p.read_bytes(), b'{"important":true}\n')


class TestRestoreSafety(ArchiveEnv):
    def test_does_not_overwrite_by_default(self) -> None:
        self.write("proj-a/s1.jsonl", b"original\n")
        archive.run()
        self.write("proj-a/s1.jsonl", b"local edit\n")

        res = archive.restore(dest_root=self.src)
        self.assertEqual(res.skipped, ["proj-a/s1.jsonl"])
        self.assertEqual((self.src / "proj-a/s1.jsonl").read_bytes(), b"local edit\n")

        # 目標剛寫好（mtime 是現在），會被進行中防護擋下 ——
        # 這裡要測的是 overwrite 本身，用 force 明確繞過那一層
        res = archive.restore(dest_root=self.src, overwrite=True, force=True)
        self.assertEqual(res.restored, ["proj-a/s1.jsonl"])
        self.assertEqual((self.src / "proj-a/s1.jsonl").read_bytes(), b"original\n")

    def test_restored_mtime_is_now_not_original(self) -> None:
        """還原成舊 mtime 會讓檔案下次啟動立刻又被保留期清掉。"""
        p = self.write("proj-a/s1.jsonl", b"x\n")
        old = time.time() - 90 * 86400
        import os
        os.utime(p, (old, old))
        archive.run()
        p.unlink()

        archive.restore(dest_root=self.src)
        age = time.time() - (self.src / "proj-a/s1.jsonl").stat().st_mtime
        self.assertLess(age, 60, "還原後的 mtime 應該是現在")

        # 明確要求時才保留原始時間戳
        (self.src / "proj-a/s1.jsonl").unlink()
        archive.restore(dest_root=self.src, keep_mtime=True)
        age = time.time() - (self.src / "proj-a/s1.jsonl").stat().st_mtime
        self.assertGreater(age, 80 * 86400)

    def test_restore_by_session_id_includes_sidecar_files(self) -> None:
        self.write("proj-a/sess1.jsonl", b"main\n")
        self.write("proj-a/sess1/subagents/agent-a.jsonl", b"sub\n")
        self.write("proj-a/sess1/tool-results/o.txt", b"tool\n")
        self.write("proj-a/other.jsonl", b"unrelated\n")
        archive.run()

        dest = self.tmp / "r"
        res = archive.restore(session_id="sess1", dest_root=dest)
        self.assertEqual(len(res.restored), 3)
        self.assertNotIn("proj-a/other.jsonl", res.restored)


class TestVerify(ArchiveEnv):
    def test_verify_passes_on_good_archive(self) -> None:
        self.write("proj-a/s1.jsonl", b'{"a":1}\n' * 100)
        archive.run()
        v = archive.verify(deep=True)
        self.assertEqual(v["ok"], v["checked"])
        self.assertFalse(v["missing"])
        self.assertFalse(v["corrupt"])

    def test_verify_detects_missing_and_corrupt_blobs(self) -> None:
        self.write("proj-a/s1.jsonl", b"aaa\n")
        self.write("proj-a/s2.jsonl", b"bbb\n")
        archive.run()

        blobs = sorted(archive.blob_dir().rglob("*.xz"))
        self.assertEqual(len(blobs), 2)
        blobs[0].unlink()                       # 遺失
        blobs[1].write_bytes(b"not lzma data")  # 損毀

        v = archive.verify(deep=True)
        self.assertEqual(len(v["missing"]), 1)
        self.assertEqual(len(v["corrupt"]), 1)
        self.assertEqual(v["ok"], 0)

    def test_corrupt_blob_fails_restore_loudly(self) -> None:
        self.write("proj-a/s1.jsonl", b"data\n")
        archive.run()
        blob = next(archive.blob_dir().rglob("*.xz"))
        blob.write_bytes(b"garbage")

        res = archive.restore(dest_root=self.tmp / "r")
        self.assertFalse(res.restored)
        self.assertEqual(len(res.failed), 1)
        # 失敗時不能留下半截檔案冒充成功
        self.assertFalse((self.tmp / "r/proj-a/s1.jsonl").exists())


class TestSessionIdMapping(ArchiveEnv):
    def test_archived_session_ids_match_index_ids(self) -> None:
        self.write("proj-a/aaaa-bbbb.jsonl", b"m\n")
        self.write("proj-a/aaaa-bbbb/subagents/agent-x1.jsonl", b"s\n")
        self.write("proj-a/aaaa-bbbb/tool-results/o.txt", b"t\n")
        self.write("proj-a/memory/MEMORY.md", b"# m\n")
        archive.run()

        ids = archive.archived_session_ids()
        # 與 indexer 產生的 id 格式一致
        self.assertIn("aaaa-bbbb", ids)
        self.assertIn("aaaa-bbbb/agent-x1", ids)
        # 非 session 的檔案不該混進來
        self.assertEqual(len(ids), 2)


if __name__ == "__main__":
    unittest.main()


class TestRestoreLivenessGuard(ArchiveEnv):
    """restore 覆寫既有檔案前必須擋掉進行中的 session。

    這一層原本只有 rename 有、restore 漏掉了。而 restore 更危險 ——
    rename 是 append（最多多一行垃圾），restore 是**整檔覆寫**
    （整個對話被舊版本蓋掉），且 Claude Code 那端不會報錯。
    """

    ARCHIVED = b'{"v":1}\n'
    LOCAL = '{"v":2,"note":"現場較新"}\n'.encode()

    def setUp(self) -> None:
        super().setUp()
        self.src_file = self.write("proj-a/s1.jsonl", self.ARCHIVED)
        archive.run()
        # 讓歸檔裡的版本與現場不同，才看得出有沒有被覆寫
        self.write("proj-a/s1.jsonl", self.LOCAL)

    def _age(self, path, seconds: float) -> None:
        import os
        t = time.time() - seconds
        os.utime(path, (t, t))

    def test_blocks_overwrite_of_active_session(self) -> None:
        res = archive.restore(dest_root=self.src, overwrite=True)
        self.assertFalse(res.restored)
        self.assertEqual(len(res.blocked), 1)
        self.assertIn("進行中", res.blocked[0])
        # 現場內容一個位元組都不能動
        self.assertEqual((self.src / "proj-a/s1.jsonl").read_bytes(), self.LOCAL)

    def test_allows_overwrite_when_idle(self) -> None:
        self._age(self.src / "proj-a/s1.jsonl", 9999)
        res = archive.restore(dest_root=self.src, overwrite=True)
        self.assertEqual(res.restored, ["proj-a/s1.jsonl"])
        self.assertFalse(res.blocked)
        self.assertEqual((self.src / "proj-a/s1.jsonl").read_bytes(), self.ARCHIVED)

    def test_force_bypasses_the_guard(self) -> None:
        res = archive.restore(dest_root=self.src, overwrite=True, force=True)
        self.assertEqual(res.restored, ["proj-a/s1.jsonl"])
        self.assertFalse(res.blocked)
        self.assertEqual((self.src / "proj-a/s1.jsonl").read_bytes(), self.ARCHIVED)

    def test_missing_target_is_never_blocked(self) -> None:
        """原始檔已被清掉是 restore 的主要用途，不能被這層防護擋到。"""
        (self.src / "proj-a/s1.jsonl").unlink()
        res = archive.restore(dest_root=self.src)
        self.assertEqual(res.restored, ["proj-a/s1.jsonl"])
        self.assertFalse(res.blocked)

    def test_default_still_skips_without_overwrite(self) -> None:
        """沒加 overwrite 時維持原行為：跳過，不是擋下。兩者語意不同。"""
        res = archive.restore(dest_root=self.src)
        self.assertEqual(res.skipped, ["proj-a/s1.jsonl"])
        self.assertFalse(res.blocked)
        self.assertFalse(res.restored)


class TestLivenessModule(ArchiveEnv):
    def test_appendable_requires_trailing_newline(self) -> None:
        p = self.write("proj-a/s1.jsonl", b'{"a":1}')     # 沒有換行
        import os
        t = time.time() - 9999
        os.utime(p, (t, t))
        ok, reason, _ = liveness.check_appendable(p)
        self.assertFalse(ok)
        self.assertIn("最後一行還沒寫完", reason)

    def test_overwritable_ignores_trailing_newline(self) -> None:
        """整檔覆寫不在意尾端完不完整 —— 反正整個換掉。"""
        p = self.write("proj-a/s1.jsonl", b'{"a":1}')     # 沒有換行
        import os
        t = time.time() - 9999
        os.utime(p, (t, t))
        ok, reason, _ = liveness.check_overwritable(p)
        self.assertTrue(ok, reason)

    def test_both_reject_active_files(self) -> None:
        p = self.write("proj-a/s1.jsonl", b'{"a":1}\n')
        self.assertFalse(liveness.check_appendable(p)[0])
        self.assertFalse(liveness.check_overwritable(p)[0])
        self.assertTrue(liveness.is_active(p))

    def test_missing_file_differs_between_the_two(self) -> None:
        p = self.src / "proj-a/nope.jsonl"
        # append 到不存在的檔案沒有意義
        self.assertFalse(liveness.check_appendable(p)[0])
        # 覆寫不存在的檔案是安全的（就是 restore 的正常路徑）
        self.assertTrue(liveness.check_overwritable(p)[0])
        self.assertIsNone(liveness.idle_seconds(p))
        self.assertFalse(liveness.is_active(p))
