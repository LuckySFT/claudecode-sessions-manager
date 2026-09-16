"""判斷 session 檔案是否「進行中」—— 寫入 `~/.claude/` 之前的共用防護。

【為什麼要抽出來】
rename 原本自己實作了這套檢查，restore 卻沒有。兩者都會寫進 `~/.claude/`，
漏掉任何一邊都會無聲砸掉正在進行的對話（實測進行中的 jsonl 沒有被獨占鎖住，
可以正常覆寫與刪除，而且 Claude Code 那端不會報錯）。共用一份才不會再漏。

【兩種寫入的檢查不一樣，不能混用】
- append（rename）：**必須**檢查尾端位元組是換行。最後一行沒寫完就追加，
  會把兩行黏成一行，那是唯一會造成資料破壞的情境。
- overwrite（restore）：尾端完不完整無所謂（反正整檔換掉），
  但危險程度更高 —— append 最多多一行垃圾，覆寫是整個對話被舊版本蓋掉。

兩者共用的是 mtime 閒置判斷：不管怎麼寫，都不該去動使用者正在講話的 session。
"""
from __future__ import annotations

import time
from pathlib import Path

# 至少閒置這麼久才視為「不在進行中」。10 分鐘是保守值：
# 使用者在對話中途去查資料、泡咖啡都可能超過 5 分鐘。
MIN_IDLE_SECONDS = 600


def idle_seconds(path: str | Path) -> float | None:
    """距離上次寫入幾秒。檔案不存在回 None。"""
    try:
        return time.time() - Path(path).stat().st_mtime
    except OSError:
        return None


def is_active(path: str | Path, *, min_idle: int = MIN_IDLE_SECONDS) -> bool:
    """檔案還在被寫入嗎。不存在的檔案不算 active。"""
    idle = idle_seconds(path)
    return idle is not None and idle < min_idle


def _idle_check(path: Path, min_idle: int) -> tuple[bool, str, float | None]:
    idle = idle_seconds(path)
    if idle is None:
        return False, "原始 jsonl 已不存在（可能已被 Claude Code 的保留期清掉）", None
    if idle < min_idle:
        mins = int(min_idle / 60)
        return False, (f"這個 session {int(idle)} 秒前還在寫入，判定為進行中"
                       f"（需閒置 {mins} 分鐘以上）"), idle
    return True, "", idle


def check_appendable(path: str | Path, *,
                     min_idle: int = MIN_IDLE_SECONDS
                     ) -> tuple[bool, str, float | None]:
    """能不能安全地在檔尾追加一行。回傳 (可否, 理由, 閒置秒數)。"""
    p = Path(path)
    ok, reason, idle = _idle_check(p, min_idle)
    if not ok:
        return ok, reason, idle

    try:
        size = p.stat().st_size
    except OSError as exc:
        return False, f"讀取檔案狀態失敗：{exc}", idle
    if size == 0:
        return False, "原始 jsonl 是空檔案，不像有效的 session", idle

    # 尾端必須是換行，這是 append 唯一會造成破壞的情境
    try:
        with p.open("rb") as fh:
            fh.seek(-1, 2)
            last = fh.read(1)
    except OSError as exc:
        return False, f"讀取檔尾失敗：{exc}", idle
    if last != b"\n":
        return False, ("原始 jsonl 的最後一行還沒寫完（尾端不是換行），"
                       "現在追加會把兩行黏在一起。請等 Claude Code 寫完再試"), idle

    return True, "", idle


def check_overwritable(path: str | Path, *,
                       min_idle: int = MIN_IDLE_SECONDS
                       ) -> tuple[bool, str, float | None]:
    """能不能安全地整檔覆寫。

    目標不存在時回 True —— 那是 restore 的主要用途（原始檔已被清掉），
    完全沒有覆蓋風險。
    """
    p = Path(path)
    if not p.exists():
        return True, "", None
    ok, reason, idle = _idle_check(p, min_idle)
    if not ok and idle is not None:
        # 覆寫比追加嚴重，理由要講清楚後果
        return False, (reason + "。整檔覆寫會讓這段對話被舊版本蓋掉，"
                       "且 Claude Code 那端不會報錯"), idle
    return ok, reason, idle
