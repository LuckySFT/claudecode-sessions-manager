"""session jsonl 的容錯解析。

兩個必須容錯的點：
1. 進行中的 session 會邊寫邊被讀，最後一行常是不完整的 JSON。
   解法：只有讀到以 \\n 結尾的完整行才處理，並且 offset 不跨過不完整的那一行，
   下次增量掃描會從同一位置重讀。
2. jsonl 的 schema 隨 Claude Code 版本變動（每筆都帶 version 欄位可證）。
   解法：一律用 .get() 取值，欄位缺了就當 None，不 assert 任何結構。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import config, redact


@dataclass
class Block:
    role: str          # user | assistant | thinking | tool_use | tool_result | attachment
    text: str
    tool_name: str | None = None
    redacted: bool = False
    truncated: bool = False


@dataclass
class Message:
    uuid: str
    parent_uuid: str | None
    kind: str          # user | assistant | attachment
    ts: str | None
    is_sidechain: bool
    model: str | None
    in_tok: int
    out_tok: int
    cache_create_tok: int
    cache_read_tok: int
    file_offset: int
    blocks: list[Block] = field(default_factory=list)


@dataclass
class SessionMeta:
    session_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    cc_version: str | None = None
    ai_title: str | None = None
    # 使用者在 Claude Code UI 裡手動改的名稱。優先於 ai_title。
    custom_title: str | None = None
    last_prompt: str | None = None
    entrypoint: str | None = None
    agent_id: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None


@dataclass
class ParseResult:
    meta: SessionMeta
    messages: list[Message]
    new_offset: int
    bad_lines: int


def iter_lines(path: Path, start_offset: int = 0) -> Iterator[tuple[int, bytes, int]]:
    """逐行讀取，只吐完整行。yield (行起始 offset, 內容, 行結束 offset)。

    不完整的尾行直接停止迭代，不 yield，讓呼叫端的 offset 停在該行開頭。
    """
    with path.open("rb") as fh:
        fh.seek(start_offset)
        offset = start_offset
        while True:
            raw = fh.readline()
            if not raw:
                return
            if not raw.endswith(b"\n"):
                # 尾行還沒寫完，這次不處理
                return
            end = offset + len(raw)
            line = raw.strip()
            if line:
                yield offset, line, end
            offset = end


# limit 參數的哨兵：DEFAULT 用 config 的上限，None 代表完全不截斷。
# 檢視完整內容時需要不截斷，但又必須走跟索引時「一模一樣」的區塊拆解邏輯，
# 否則空區塊被丟掉的位置會對不上，還原出來的是隔壁那一段。
DEFAULT_LIMIT = object()


def _resolve_limit(limit, fallback: int) -> int | None:
    if limit is DEFAULT_LIMIT:
        return fallback
    return limit


def _mk_block(role: str, text: str, *, tool_name: str | None = None,
              limit=DEFAULT_LIMIT, force_mask: bool = False) -> Block | None:
    if not text:
        return None
    if force_mask:
        return Block(role=role, text=redact.MASK, tool_name=tool_name, redacted=True)
    text, was_redacted = redact.redact(text)
    cap = _resolve_limit(limit, config.MAX_BLOCK_CHARS)
    truncated = cap is not None and len(text) > cap
    if truncated:
        text = text[:cap]
    return Block(role=role, text=text, tool_name=tool_name,
                 redacted=was_redacted, truncated=truncated)


def stringify(value: Any) -> str:
    """tool_use.input / tool_result.content 的型別不固定，統一壓成字串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(stringify(item))
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        if value.get("type") == "text":
            return str(value.get("text") or "")
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def blocks_from_content(content: Any, *, outer_type: str | None = None,
                        limit=DEFAULT_LIMIT) -> list[Block]:
    """把 message.content 拆成 block。

    空內容的 block 會被丟掉（例如只存簽章、thinking 文字為空的那些），
    所以還原完整內容時**必須**走這個函式，不能自己按原始 content 的索引取，
    否則位置會偏掉。
    """
    if isinstance(content, str):
        blk = _mk_block("assistant_or_user", content, limit=limit)
        out = [blk] if blk else []
    else:
        out = []
        if not isinstance(content, list):
            return out
        for item in content:
            if not isinstance(item, dict):
                continue
            btype = item.get("type")
            if btype == "text":
                blk = _mk_block("assistant_or_user", str(item.get("text") or ""),
                                limit=limit)
            elif btype == "thinking":
                blk = _mk_block("thinking",
                                str(item.get("thinking") or item.get("text") or ""),
                                limit=limit)
            elif btype == "tool_use":
                name = item.get("name")
                payload = stringify(item.get("input"))
                # Read/Edit/Write 的 input 帶檔案路徑，命中敏感檔就整段遮掉
                path_hint = ""
                if isinstance(item.get("input"), dict):
                    inp = item["input"]
                    path_hint = str(inp.get("file_path") or inp.get("path") or "")
                blk = _mk_block("tool_use", payload, tool_name=name, limit=limit,
                                force_mask=redact.is_secret_path(path_hint))
            elif btype == "tool_result":
                blk = _mk_block("tool_result", stringify(item.get("content")),
                                limit=limit)
            else:
                continue
            if blk:
                out.append(blk)

    # content 內的 text block 分不出是誰講的，靠外層 record 的 type 補
    if outer_type:
        for blk in out:
            if blk.role == "assistant_or_user":
                blk.role = outer_type
    return out


def blocks_from_attachment(att: Any, *, limit=DEFAULT_LIMIT) -> list[Block]:
    """附件只索引使用者真的 @ 進來的檔案。

    skill_listing / deferred_tools_delta / mcp_instructions_delta 這類是每個 session
    都一樣的樣板，索引它們只會撐爆 trigram 索引又汙染搜尋結果，刻意跳過。
    """
    if not isinstance(att, dict) or att.get("type") != "file":
        return []

    path = att.get("filename") or att.get("displayPath") or ""
    content = att.get("content")
    body = ""
    if isinstance(content, dict):
        file_obj = content.get("file")
        if isinstance(file_obj, dict):
            body = str(file_obj.get("content") or "")
            path = file_obj.get("filePath") or path
        else:
            body = stringify(content)
    else:
        body = stringify(content)

    header = f"[附件] {path}\n" if path else ""
    if redact.is_secret_path(str(path)):
        return [Block(role="attachment", text=header + redact.MASK, redacted=True)]

    cap = _resolve_limit(limit, config.MAX_ATTACHMENT_CHARS)
    blk = _mk_block("attachment", header + body, limit=cap)
    return [blk] if blk else []


def parse_session(path: Path, start_offset: int = 0) -> ParseResult:
    meta = SessionMeta()
    messages: list[Message] = []
    bad_lines = 0
    new_offset = start_offset

    for offset, line, end in iter_lines(path, start_offset):
        new_offset = end
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 完整但壞掉的一行不會自己修好，計數後照樣前進，不要卡住整個檔案
            bad_lines += 1
            continue
        if not isinstance(rec, dict):
            bad_lines += 1
            continue

        rtype = rec.get("type")

        # session 層級的欄位散落在各種 record 上，看到就補進 meta
        if rec.get("sessionId"):
            meta.session_id = rec["sessionId"]
        if rec.get("cwd"):
            meta.cwd = rec["cwd"]
        if rec.get("gitBranch"):
            meta.git_branch = rec["gitBranch"]
        if rec.get("version"):
            meta.cc_version = rec["version"]
        if rec.get("entrypoint"):
            meta.entrypoint = rec["entrypoint"]
        if rec.get("agentId"):
            meta.agent_id = rec["agentId"]

        if rtype == "ai-title":
            meta.ai_title = rec.get("aiTitle") or meta.ai_title
            continue
        if rtype == "custom-title":
            # Claude Code 自己就是用「append 一筆新記錄」來實作改名的，
            # 不改寫既有內容，所以同一個 session 會累積很多筆，最後一筆勝出。
            # （實測某個 session 有 46 筆，因為編輯標題時是逐步存檔。）
            # 有些 session 只有 custom-title 而沒有 ai-title，漏讀它會顯示成「無標題」。
            meta.custom_title = rec.get("customTitle") or meta.custom_title
            continue
        if rtype == "last-prompt":
            meta.last_prompt = rec.get("lastPrompt") or meta.last_prompt
            continue
        if rtype in ("queue-operation", "file-history-snapshot", "file-history-delta"):
            continue

        ts = rec.get("timestamp")
        if ts:
            if meta.first_ts is None or ts < meta.first_ts:
                meta.first_ts = ts
            if meta.last_ts is None or ts > meta.last_ts:
                meta.last_ts = ts

        if rtype not in ("user", "assistant", "attachment"):
            continue

        uuid = rec.get("uuid")
        if not uuid:
            continue

        blocks: list[Block] = []
        model = None
        usage = {}

        if rtype == "attachment":
            blocks = blocks_from_attachment(rec.get("attachment"))
        else:
            msg = rec.get("message")
            if isinstance(msg, dict):
                model = msg.get("model")
                usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                blocks = blocks_from_content(msg.get("content"), outer_type=rtype)

        if not blocks and not usage:
            continue

        messages.append(Message(
            uuid=uuid,
            parent_uuid=rec.get("parentUuid"),
            kind=rtype,
            ts=ts,
            is_sidechain=bool(rec.get("isSidechain")),
            model=model,
            in_tok=int(usage.get("input_tokens") or 0),
            out_tok=int(usage.get("output_tokens") or 0),
            cache_create_tok=int(usage.get("cache_creation_input_tokens") or 0),
            cache_read_tok=int(usage.get("cache_read_input_tokens") or 0),
            file_offset=offset,
            blocks=blocks,
        ))

    return ParseResult(meta=meta, messages=messages,
                       new_offset=new_offset, bad_lines=bad_lines)
