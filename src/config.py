"""路徑與可調參數。

原始資料一律唯讀：CLAUDE_HOME 底下的任何檔案都不得寫入。
唯一的寫入目標是 DATA_DIR。
"""
from __future__ import annotations

import os
from pathlib import Path

# Claude Code 的資料根目錄。可用環境變數覆寫，方便拿測試資料集跑。
CLAUDE_HOME = Path(os.environ.get("CCSM_CLAUDE_HOME", Path.home() / ".claude"))

PROJECTS_DIR = CLAUDE_HOME / "projects"
FILE_HISTORY_DIR = CLAUDE_HOME / "file-history"
HISTORY_FILE = CLAUDE_HOME / "history.jsonl"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CCSM_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = DATA_DIR / "index.db"
# 三個路徑都可用環境變數覆寫，才能起一個完全隔離的實例來驗證破壞性功能
# （移除、還原這類東西不該拿真實資料試）
ARCHIVE_DIR = Path(os.environ.get("CCSM_ARCHIVE_DIR", PROJECT_ROOT / "archive"))

# 使用者手打的標題、隱藏狀態、專案別名。
# 刻意跟 index.db 分開：索引是可拋棄的衍生物（改 schema 就砍掉重建），
# 這些標籤是唯一來源，砍索引時絕對不能一起消失。
LABELS_DB_PATH = DATA_DIR / "labels.db"

# 索引時每個 block 保留的字元上限。超過就截斷並標記 truncated。
# 完整內容不進 DB，檢視時靠 messages.file_offset 回原始 jsonl 撈。
# 這是拿搜尋完整度換索引體積 —— 186MB 原始資料若全灌進 trigram FTS 會膨脹數倍。
MAX_BLOCK_CHARS = 8000
# 附件（使用者 @ 進來的檔案內容）截更短，它們對搜尋的價值低但體積大。
MAX_ATTACHMENT_CHARS = 2000

# Claude Code 會在 %TEMP%\claude\<專案 slug>\<session-id>\scratchpad\ 底下開暫存
# 工作區，在那裡啟動的 session 會被 Claude Code 記成一個獨立「專案」，slug 長這樣：
#   C--Users-<user>-AppData-Local-Temp-claude-<專案>-<session-id>-scratchpad-<名字>
# 那是拋棄式的暫存區不是真專案（實測內容都是 skill eval 的測試探針），預設不納管。
#
# 這是本專案唯一「主動放棄留存」的地方 —— 詳見 CLAUDE.md，不要當成 bug 修掉。
# regex 要求 -Temp-claude- 與 -scratchpad 兩者都命中且有先後順序，
# 避免誤殺真的叫 scratchpad 的專案。設成空字串即關閉排除、全部納管。
EXCLUDE_SLUG_RE = os.environ.get("CCSM_EXCLUDE_SLUG_RE", r"-Temp-claude-.*-scratchpad")

# 一次交易內累積多少 block 才 flush
BATCH_SIZE = 2000
