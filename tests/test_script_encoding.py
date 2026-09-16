"""腳本檔的編碼與行尾檢查。

這類 bug 的共同點是**症狀完全看不出原因**：
- `.ps1` 無 BOM → PowerShell 5.1 用 cp950 解，中文被當語法符號，
  錯誤訊息長得像「遺失 '}'」
- `.cmd` 用 LF 行尾 → cmd.exe 解析錯亂，`rem` 註解被當指令執行，
  跳出「'pen' 不是內部或外部命令」這種完全無關的訊息
- `.cmd` 含非 ASCII → cp950 的 lead byte 吃掉後續位元組，把 `rem` 推離行首，
  造成同樣的症狀

三者都踩過，所以用測試釘住而不是靠記得。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BOM = b"\xef\xbb\xbf"


def _lone_lf(raw: bytes) -> int:
    return raw.count(b"\n") - raw.count(b"\r\n")


class TestPowerShellScripts(unittest.TestCase):
    """含中文的 .ps1 必須是帶 BOM 的 UTF-8。"""

    def setUp(self) -> None:
        self.files = sorted(ROOT.glob("scripts/*.ps1"))
        self.assertTrue(self.files, "找不到任何 .ps1")

    def test_scripts_with_chinese_have_bom(self) -> None:
        for f in self.files:
            raw = f.read_bytes()
            body = raw[len(BOM):] if raw.startswith(BOM) else raw
            has_chinese = any(ord(ch) > 0x2E80
                              for ch in body.decode("utf-8", "replace"))
            if has_chinese:
                self.assertTrue(raw.startswith(BOM),
                                f"{f.name} 含中文但沒有 BOM —— "
                                f"PowerShell 5.1 會用 cp950 解讀")

    def test_scripts_are_valid_utf8(self) -> None:
        for f in self.files:
            raw = f.read_bytes()
            body = raw[len(BOM):] if raw.startswith(BOM) else raw
            try:
                body.decode("utf-8")
            except UnicodeDecodeError as exc:
                self.fail(f"{f.name} 不是合法的 UTF-8：{exc}")


class TestBatchScripts(unittest.TestCase):
    """.cmd 必須是純 ASCII + CRLF + 無 BOM。"""

    def setUp(self) -> None:
        self.files = sorted(ROOT.glob("*.cmd")) + sorted(ROOT.glob("scripts/*.cmd"))
        self.assertTrue(self.files, "找不到任何 .cmd")

    def test_no_bom(self) -> None:
        for f in self.files:
            self.assertFalse(f.read_bytes().startswith(BOM),
                             f"{f.name} 有 BOM —— cmd.exe 會把它併進第一行")

    def test_pure_ascii(self) -> None:
        """cp950 的 lead byte 會吃掉後續位元組，把 rem 推離行首。

        檔名本身可以是中文（檔案系統用 UTF-16），但**內容**不行 ——
        要引用自己的檔名請用 %~nx0。
        """
        for f in self.files:
            raw = f.read_bytes()
            bad = [(i, b) for i, b in enumerate(raw) if b > 127]
            self.assertEqual(
                bad, [],
                f"{f.name} 含 {len(bad)} 個非 ASCII 位元組（第一個在 offset "
                f"{bad[0][0] if bad else '-'}）—— 用 %~nx0 代替寫死檔名")

    def test_crlf_line_endings(self) -> None:
        """cmd.exe 對純 LF 的批次檔解析不可靠，rem 註解會被當指令執行。"""
        for f in self.files:
            raw = f.read_bytes()
            self.assertEqual(
                _lone_lf(raw), 0,
                f"{f.name} 有 {_lone_lf(raw)} 個裸 LF —— 必須全部是 CRLF")

    def test_every_non_rem_line_is_intended(self) -> None:
        """粗略檢查：非 rem 的行應該是可辨識的批次指令。

        LF 行尾造成解析錯亂時，症狀是註解文字被當指令 —— 這個檢查抓的是
        「檔案裡混進了不該執行的東西」，例如把說明文字忘了加 rem。
        """
        allowed = ("@echo", "rem", "setlocal", "endlocal", "cd ", "powershell",
                   "if ", ")", "echo", "pause", "exit", "@rem", "call", "set ")
        for f in self.files:
            for n, line in enumerate(f.read_text(encoding="ascii").splitlines(), 1):
                s = line.strip()
                if not s:
                    continue
                self.assertTrue(
                    s.lower().startswith(allowed),
                    f"{f.name}:{n} 看起來不是批次指令也沒加 rem：{s!r}")


if __name__ == "__main__":
    unittest.main()
