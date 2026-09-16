"""Claude Code 的 session 保留期。

Claude Code 啟動時會刪掉 `~/.claude/projects/` 底下超過 cleanupPeriodDays（預設 30 天）
的 session 檔案，`<sessionId>/` 附屬目錄跟著主檔一起走。
實測記錄見 docs/known-issues/claude-code-session-retention.md。

這個模組只負責「還剩幾天」的計算，讓 UI 能提前示警。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import config

DEFAULT_CLEANUP_DAYS = 30
_SECONDS_PER_DAY = 86400

# settings.json 不常變，快取住避免每列 session 都重讀
_cache: tuple[float, int] | None = None
_CACHE_TTL = 30.0


def cleanup_period_days() -> int:
    """讀使用者設定的保留天數；沒設或讀不到就回預設值。"""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL:
        return _cache[1]

    days = DEFAULT_CLEANUP_DAYS
    path = config.CLAUDE_HOME / "settings.json"
    try:
        # utf-8-sig：手動編輯過的設定檔可能帶 BOM
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        value = data.get("cleanupPeriodDays")
        if isinstance(value, int) and value > 0:
            days = value
    except (OSError, json.JSONDecodeError, ValueError):
        # 設定檔壞掉不該讓整個清單掛掉，退回預設值就好
        pass

    _cache = (now, days)
    return days


def file_status(jsonl_path: str | Path, *, days: int | None = None) -> dict:
    """回傳單一 session 檔案的存活狀態。

    days_left 是「距離被 Claude Code 清掉還有幾天」，負數代表已超過保留期
    （但清理只在啟動時跑，所以超過了還在磁碟上是正常的）。
    """
    path = Path(jsonl_path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"source_missing": True, "days_left": None, "age_days": None}

    period = days if days is not None else cleanup_period_days()
    age_days = (time.time() - mtime) / _SECONDS_PER_DAY
    return {
        "source_missing": False,
        "age_days": round(age_days, 1),
        "days_left": round(period - age_days, 1),
    }


def annotate(rows: list[dict], *, path_key: str = "jsonl_path") -> list[dict]:
    """就地把存活狀態併進每一列。設定只讀一次。"""
    period = cleanup_period_days()
    for row in rows:
        row.update(file_status(row.get(path_key, ""), days=period))
    return rows
