"""真正的 session 改名 —— 寫入原始 jsonl。

【這是「原始資料唯讀」規則的唯一例外，範圍嚴格限定】
只做一件事：往主 session 的 jsonl **檔尾追加一行** `custom-title` 記錄。
絕不改寫、絕不刪除、絕不動任何既有位元組。

這是 Claude Code 官方自己的改名機制（實測 74 筆這種記錄，某個 session 累積 46 筆，
因為編輯標題時逐步存檔）。格式極簡，只有三個欄位、沒有 timestamp、沒有 uuid：

    {"type":"custom-title","customTitle":"...","sessionId":"..."}

最後一筆勝出。所以「改名」就是再 append 一筆，舊的留著當歷史。

【兩層防護，缺一不可】
實作在 liveness.check_appendable()，與 restore 的覆寫防護共用同一組判斷 ——
原本這套只有 rename 有，restore 漏掉了（見 liveness 模組註解）。

1. 尾端位元組必須是 \\n —— **事實檢查**。最後一行沒寫完就 append 會把兩行
   黏成一行，那是唯一會造成資料破壞的情境。
2. mtime 必須閒置足夠久 —— **行為檢查**。即使尾端完整，正在進行的對話隨時會
   再寫入；跟使用者正在講話的 session 搶著寫沒有意義，而且 Claude Code 可能
   有自己的記憶體狀態，改了它也不見得會重讀。

只擋一層不夠：只看 mtime 會漏掉「剛好卡在寫一半」的瞬間；
只看尾端會允許對正在對話的 session 動手。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import liveness

# 保留這個名字給既有呼叫端；實際值由 liveness 統一定義，避免兩邊各有一個門檻
MIN_IDLE_SECONDS = liveness.MIN_IDLE_SECONDS

MAX_TITLE_CHARS = 200


@dataclass
class RenameResult:
    ok: bool
    reason: str = ""
    session_id: str = ""
    title: str = ""
    previous: str | None = None
    bytes_appended: int = 0
    idle_seconds: float | None = None


def rename_session(conn: sqlite3.Connection,
                   session_id: str,
                   new_title: str,
                   *,
                   min_idle: int = MIN_IDLE_SECONDS,
                   dry_run: bool = False) -> RenameResult:
    """把新標題 append 進原始 jsonl。

    只支援主 session：subagent 逐字稿沒有獨立的 /resume 入口，改名沒有意義，
    而且它的記錄不帶 sessionId。
    """
    title = (new_title or "").strip()
    if not title:
        return RenameResult(False, "標題不能為空。要清除本工具的覆寫請用 label 端點",
                            session_id=session_id)
    if len(title) > MAX_TITLE_CHARS:
        return RenameResult(False, f"標題超過 {MAX_TITLE_CHARS} 字",
                            session_id=session_id)
    # 記錄是一行 JSON，標題裡的換行會把一筆拆成兩行、破壞整個檔案
    if "\n" in title or "\r" in title:
        return RenameResult(False, "標題不能含換行", session_id=session_id)

    row = conn.execute(
        "SELECT kind, jsonl_path, custom_title, ai_title FROM sessions WHERE id = ?",
        (session_id,)).fetchone()
    if row is None:
        return RenameResult(False, "找不到這個 session", session_id=session_id)
    if row["kind"] != "main":
        return RenameResult(False, "subagent 逐字稿無法改名（沒有獨立的 session）",
                            session_id=session_id)

    path = Path(row["jsonl_path"])
    ok, reason, idle = liveness.check_appendable(path, min_idle=min_idle)
    if not ok:
        return RenameResult(False, reason, session_id=session_id, title=title,
                            previous=row["custom_title"] or row["ai_title"],
                            idle_seconds=idle)

    # sessionId 用索引的 id（主 session 的 id 就是 sessionId），
    # 而不是檔名 —— 兩者一致，但用 id 語意更明確
    line = json.dumps({"type": "custom-title",
                       "customTitle": title,
                       "sessionId": session_id},
                      ensure_ascii=False).encode("utf-8") + b"\n"

    if dry_run:
        return RenameResult(True, "dry-run：沒有實際寫入", session_id=session_id,
                            title=title,
                            previous=row["custom_title"] or row["ai_title"],
                            bytes_appended=len(line), idle_seconds=idle)

    try:
        with path.open("ab") as fh:
            # 一次 write 一整行，不要分次寫；再次確認位置在檔尾
            fh.write(line)
            fh.flush()
    except OSError as exc:
        return RenameResult(False, f"寫入失敗：{exc}", session_id=session_id,
                            title=title, idle_seconds=idle)

    return RenameResult(True, "", session_id=session_id, title=title,
                        previous=row["custom_title"] or row["ai_title"],
                        bytes_appended=len(line), idle_seconds=idle)


def can_rename(conn: sqlite3.Connection, session_id: str,
               *, min_idle: int = MIN_IDLE_SECONDS) -> tuple[bool, str]:
    """只做檢查不寫入，讓 UI 可以先把按鈕變灰並說明原因。"""
    row = conn.execute(
        "SELECT kind, jsonl_path FROM sessions WHERE id = ?",
        (session_id,)).fetchone()
    if row is None:
        return False, "找不到這個 session"
    if row["kind"] != "main":
        return False, "subagent 逐字稿無法改名"
    ok, reason, _ = liveness.check_appendable(row["jsonl_path"],
                                              min_idle=min_idle)
    return ok, reason
