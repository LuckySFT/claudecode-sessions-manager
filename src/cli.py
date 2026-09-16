"""命令列入口。

    python -m src.cli index          建立/更新索引
    python -m src.cli index --prune  同時清掉原始檔已消失的 session
    python -m src.cli status         看索引概況
"""
from __future__ import annotations

import argparse
import sys
import time

from . import archive, config, db, indexer, purge, rename


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def cmd_index(args: argparse.Namespace) -> int:
    files = indexer.discover()
    if not files:
        print(f"在 {config.PROJECTS_DIR} 找不到任何 session jsonl", file=sys.stderr)
        return 1

    total_bytes = sum(f.size for f in files)
    print(f"發現 {len(files)} 個 session，共 {_fmt_bytes(total_bytes)}")

    started = time.perf_counter()
    conn = db.connect()

    last_line = 0

    def progress(i: int, total: int, sf) -> None:
        nonlocal last_line
        label = f"[{i}/{total}] {sf.project_slug}/{sf.session_id[:8]}"
        pad = max(0, last_line - len(label))
        sys.stdout.write("\r" + label + " " * pad)
        sys.stdout.flush()
        last_line = len(label)

    try:
        stats = indexer.reindex(conn, files, progress=progress if not args.quiet else None)
        pruned = indexer.prune_missing(conn) if args.prune else 0
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()

    elapsed = time.perf_counter() - started
    sys.stdout.write("\r" + " " * (last_line + 2) + "\r")

    print(f"完成，耗時 {elapsed:.1f}s")
    print(f"  掃描 {stats.scanned}｜重建 {stats.rebuilt}｜增量 {stats.appended}｜跳過 {stats.skipped}")
    print(f"  訊息 {stats.messages}｜區塊 {stats.blocks}")
    if args.prune:
        print(f"  清除已消失的 session：{pruned}")
    if stats.bad_lines:
        print(f"  無法解析的行數：{stats.bad_lines}")
    if stats.errors:
        print(f"  失敗的檔案 {len(stats.errors)} 個：", file=sys.stderr)
        for e in stats.errors[:20]:
            print(f"    {e}", file=sys.stderr)
    print(f"  索引檔：{config.DB_PATH} ({_fmt_bytes(config.DB_PATH.stat().st_size)})")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if not config.DB_PATH.exists():
        print("索引還不存在，先跑 python -m src.cli index", file=sys.stderr)
        return 1
    conn = db.connect(read_only=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS sessions,"
            " COUNT(DISTINCT project_slug) AS projects,"
            " SUM(jsonl_size) AS bytes, SUM(bad_lines) AS bad,"
            " MIN(first_ts) AS since, MAX(last_ts) AS until FROM sessions"
        ).fetchone()
        msgs = conn.execute(
            "SELECT COUNT(*) AS n, SUM(in_tok) AS i, SUM(out_tok) AS o,"
            " SUM(cache_create_tok) AS cc, SUM(cache_read_tok) AS cr FROM messages"
        ).fetchone()
        blocks = conn.execute(
            "SELECT COUNT(*) AS n, SUM(redacted) AS r, SUM(truncated) AS t FROM blocks"
        ).fetchone()

        print(f"專案 {row['projects']}｜session {row['sessions']}｜"
              f"原始資料 {_fmt_bytes(row['bytes'] or 0)}")
        print(f"時間範圍 {row['since']} ~ {row['until']}")
        print(f"訊息 {msgs['n']}｜區塊 {blocks['n']}"
              f"（遮罩 {blocks['r'] or 0}、截斷 {blocks['t'] or 0}）")
        print(f"token 累計 輸入 {msgs['i'] or 0:,}｜輸出 {msgs['o'] or 0:,}｜"
              f"cache 寫 {msgs['cc'] or 0:,}｜cache 讀 {msgs['cr'] or 0:,}")
        if row["bad"]:
            print(f"無法解析的行數：{row['bad']}")
        print(f"索引檔 {config.DB_PATH} ({_fmt_bytes(config.DB_PATH.stat().st_size)})")

        print("\n最肥的 5 個專案：")
        for r in conn.execute(
            "SELECT project_slug, COUNT(*) AS n, SUM(jsonl_size) AS b FROM sessions"
            " GROUP BY project_slug ORDER BY b DESC LIMIT 5"
        ):
            print(f"  {_fmt_bytes(r['b']):>9}  {r['n']:>3} sessions  {r['project_slug']}")
    finally:
        conn.close()
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    if args.status:
        st = archive.status()
        if not st.get("exists"):
            print("還沒有歸檔，先跑 python -m src.cli archive")
            return 1
        print(f"歸檔目錄 {st['archive_dir']}")
        print(f"  追蹤檔案 {st['files']}｜其中原始檔已消失 {st['files_source_missing']}")
        print(f"  blob {st['blobs']} 個｜原始 {_fmt_bytes(st['raw_bytes'])}"
              f" -> 壓縮後 {_fmt_bytes(st['stored_bytes'])}"
              + (f"（{st['ratio']*100:.0f}%）" if st["ratio"] else ""))
        print(f"  版本記錄 {st['versions']} 筆")
        r = st.get("last_run")
        if r:
            print(f"  上次執行 {r['finished_at'][:19]}｜掃描 {r['scanned']}"
                  f"｜新增 {r['added']}｜未變 {r['unchanged']}｜消失 {r['vanished']}")
        return 0

    if args.verify:
        print("驗證中（解壓並重算雜湊）…")
        v = archive.verify(deep=True)
        print(f"  檢查 {v['checked']} 個 blob｜完好 {v['ok']}")
        if v["missing"]:
            print(f"  !! 遺失 {len(v['missing'])} 個: {v['missing'][:5]}", file=sys.stderr)
        if v["corrupt"]:
            print(f"  !! 損毀 {len(v['corrupt'])} 個: {v['corrupt'][:5]}", file=sys.stderr)
        return 1 if (v["missing"] or v["corrupt"]) else 0

    started = time.perf_counter()
    last = 0

    def progress(i: int, path) -> None:
        nonlocal last
        label = f"[{i}] {path.name[:52]}"
        sys.stdout.write("\r" + label + " " * max(0, last - len(label)))
        sys.stdout.flush()
        last = len(label)

    stats = archive.run(progress=None if args.quiet else progress)
    sys.stdout.write("\r" + " " * (last + 2) + "\r")

    print(f"歸檔完成，耗時 {time.perf_counter() - started:.1f}s")
    print(f"  掃描 {stats.scanned}（{_fmt_bytes(stats.raw_bytes)}）"
          f"｜新增 {stats.added}｜未變 {stats.unchanged}")
    print(f"  本次寫入 {_fmt_bytes(stats.bytes_added)}")
    if stats.vanished:
        print(f"  原始檔已消失但歸檔仍保留：{stats.vanished} 個")
    for e in stats.errors[:20]:
        print(f"  錯誤 {e}", file=sys.stderr)
    st = archive.status()
    print(f"  累計 {st['blobs']} blob｜{_fmt_bytes(st['raw_bytes'])}"
          f" -> {_fmt_bytes(st['stored_bytes'])}"
          + (f"（{st['ratio']*100:.0f}%）" if st["ratio"] else ""))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    if not archive.manifest_path().exists():
        print("還沒有歸檔可還原", file=sys.stderr)
        return 1
    if not (args.session or args.all or args.path):
        print("要指定 --session <id>、--path <相對路徑> 或 --all", file=sys.stderr)
        return 1

    res = archive.restore(session_id=args.session,
                          rel_paths=args.path or None,
                          overwrite=args.overwrite,
                          force=args.force,
                          keep_mtime=args.keep_mtime)
    print(f"還原 {len(res.restored)} 個檔案")
    for p in res.restored[:20]:
        print(f"  + {p}")
    if len(res.restored) > 20:
        print(f"  …另外 {len(res.restored) - 20} 個")
    if res.skipped:
        print(f"跳過 {len(res.skipped)} 個（目標已存在，要覆蓋請加 --overwrite）")
    if res.blocked:
        # 擋下來不是錯誤，是刻意保護，處理方式跟 failed 不同，訊息要分開講
        print(f"擋下 {len(res.blocked)} 個（目標是進行中的 session）：",
              file=sys.stderr)
        for b in res.blocked[:10]:
            print(f"  {b}", file=sys.stderr)
        print("  等那個 session 結束再試，或明確加 --force 覆蓋"
              "（會蓋掉正在進行的對話）", file=sys.stderr)
    if res.failed:
        print(f"失敗 {len(res.failed)} 個：", file=sys.stderr)
        for f in res.failed[:10]:
            print(f"  {f}", file=sys.stderr)
    if res.restored and not args.keep_mtime:
        print("還原檔的 mtime 已設為現在，避免下次啟動又被保留期清掉")
        print("記得跑 python -m src.cli index 讓索引跟上")
    return 1 if (res.failed or res.blocked) else 0


def cmd_rename(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        r = rename.rename_session(conn, args.session, args.title,
                                  dry_run=args.dry_run)
        if not r.ok:
            print(f"無法改名：{r.reason}", file=sys.stderr)
            return 1
        print(f"{'（dry-run）' if args.dry_run else ''}已改名")
        print(f"  session : {r.session_id}")
        print(f"  原名稱  : {r.previous or '(無)'}")
        print(f"  新名稱  : {r.title}")
        print(f"  追加     : {r.bytes_appended} bytes"
              f"（閒置 {int(r.idle_seconds or 0)} 秒）")
        if not args.dry_run:
            indexer.reindex(conn)
            print("  索引已更新")
    finally:
        conn.close()
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        try:
            p = purge.plan(conn, depth=args.depth, project=args.project,
                           session_ids=args.session or None,
                           older_than_days=args.older_than,
                           include_backed_up=not args.only_unbacked,
                           include_unbacked=not args.only_backed_up)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        if not p.candidates:
            print("沒有符合條件的 session（只會處理原始檔已消失的）")
            return 0

        print(f"符合條件：{p.total} 個 session（深度 {p.depth}）")
        print(f"  索引裡的文字量 {p.index_chars:,} 字｜原始檔曾佔 "
              f"{_fmt_bytes(p.raw_bytes)}")
        if p.unbacked:
            print(f"  !! 其中 {len(p.unbacked)} 個**沒有歸檔備份** —— "
                  f"移除後內容徹底消失，無法還原", file=sys.stderr)
        print()
        for c in p.candidates[: args.limit]:
            mark = "有備份" if c.archived else "無備份"
            print(f"  [{mark}] {(c.last_ts or '')[:10]}  {c.msg_count:>4} 訊息  "
                  f"{c.display_title[:44]}")
        if p.total > args.limit:
            print(f"  …另外 {p.total - args.limit} 個（用 --limit 看更多）")

        if not args.yes:
            print()
            print("以上是預覽，沒有刪除任何東西。確定要移除請加 --yes")
            if p.depth == "index":
                print("（深度 index：歸檔 blob 保留，之後可用 restore 取回）")
            else:
                print("（深度 full：連歸檔 blob 一起刪，**不可逆**）")
            return 0

        res = purge.apply(conn, p, confirm=True)
        print()
        print(f"已移除 {res.removed_sessions} 個 session")
        print(f"  訊息 {res.removed_messages}｜區塊 {res.removed_blocks}"
              f"｜標籤 {res.removed_labels}")
        if p.depth == "full":
            print(f"  歸檔 blob {res.removed_blobs} 個，"
                  f"釋出 {_fmt_bytes(res.freed_blob_bytes)}")
        for e in res.errors:
            print(f"  錯誤：{e}", file=sys.stderr)
        print("  索引檔本身要 VACUUM 才會縮小："
              "python -m src.cli vacuum")
        return 1 if res.errors else 0
    finally:
        conn.close()


def cmd_vacuum(args: argparse.Namespace) -> int:
    """VACUUM 讓刪除後的索引檔真的縮小（SQLite 預設只把頁面標成可重用）。"""
    if not config.DB_PATH.exists():
        print("索引還不存在", file=sys.stderr)
        return 1
    before = config.DB_PATH.stat().st_size
    conn = db.connect()
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = config.DB_PATH.stat().st_size
    print(f"索引 {_fmt_bytes(before)} -> {_fmt_bytes(after)}"
          f"（釋出 {_fmt_bytes(max(0, before - after))}）")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    if not config.DB_PATH.exists():
        print("索引還不存在，先跑 python -m src.cli index", file=sys.stderr)
        return 1
    from . import api
    url = f"http://{args.host}:{args.port}"
    print(url)
    if args.open:
        # 在背景等服務起來再開瀏覽器；uvicorn.run 會阻塞，不能等它回來才開
        import threading
        import webbrowser

        def opener() -> None:
            for _ in range(60):
                time.sleep(0.25)
                if _port_open(args.host, args.port):
                    webbrowser.open(url)
                    return

        threading.Thread(target=opener, daemon=True).start()
    api.serve(host=args.host, port=args.port)
    return 0


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ccsm", description="Claude Code session 管理工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="建立或更新索引")
    p_index.add_argument("--prune", action="store_true",
                         help="同時清掉原始 jsonl 已不存在的 session")
    p_index.add_argument("--quiet", action="store_true", help="不顯示進度")
    p_index.set_defaults(func=cmd_index)

    p_status = sub.add_parser("status", help="顯示索引概況")
    p_status.set_defaults(func=cmd_status)

    p_arc = sub.add_parser("archive", help="把原始 jsonl 歸檔到 archive/（只新增，不刪除）")
    p_arc.add_argument("--status", action="store_true", help="只顯示歸檔概況")
    p_arc.add_argument("--verify", action="store_true", help="驗證所有 blob 可解壓且雜湊相符")
    p_arc.add_argument("--quiet", action="store_true", help="不顯示進度")
    p_arc.set_defaults(func=cmd_archive)

    p_res = sub.add_parser("restore", help="從歸檔還原檔案")
    p_res.add_argument("--session", help="要還原的 session id")
    p_res.add_argument("--path", action="append", help="相對 projects/ 的路徑，可重複")
    p_res.add_argument("--all", action="store_true", help="還原全部")
    p_res.add_argument("--overwrite", action="store_true", help="目標已存在時覆蓋")
    p_res.add_argument("--force", action="store_true",
                       help="即使目標是進行中的 session 也覆蓋"
                            "（會蓋掉正在進行的對話，Claude Code 不會報錯）")
    p_res.add_argument("--keep-mtime", action="store_true",
                       help="保留原始 mtime（注意：會讓檔案下次啟動就被保留期清掉）")
    p_res.set_defaults(func=cmd_restore)

    p_ren = sub.add_parser("rename",
                           help="改 session 名稱（寫入原始 jsonl，Claude Code 也看得到）")
    p_ren.add_argument("--session", required=True, help="session id")
    p_ren.add_argument("--title", required=True, help="新名稱")
    p_ren.add_argument("--dry-run", action="store_true", help="只檢查不寫入")
    p_ren.set_defaults(func=cmd_rename)

    p_pur = sub.add_parser(
        "purge",
        help="移除「原始檔已被 Claude Code 清掉」的 session 記錄（預設只預覽）")
    p_pur.add_argument("--depth", choices=purge.DEPTHS, default="index",
                       help="index=只清索引（歸檔留著可還原）；full=連歸檔一起刪，不可逆")
    p_pur.add_argument("--project", help="限定某個專案（可用別名）")
    p_pur.add_argument("--session", action="append", help="指定 session id，可重複")
    p_pur.add_argument("--older-than", type=int, metavar="DAYS",
                       help="只處理最後活動早於 N 天前的")
    p_pur.add_argument("--only-unbacked", action="store_true",
                       help="只列沒有歸檔備份的")
    p_pur.add_argument("--only-backed-up", action="store_true",
                       help="只列有歸檔備份的（比較安全）")
    p_pur.add_argument("--limit", type=int, default=30, help="預覽顯示幾筆")
    p_pur.add_argument("--yes", action="store_true", help="確定執行（否則只預覽）")
    p_pur.set_defaults(func=cmd_purge)

    p_vac = sub.add_parser("vacuum", help="VACUUM 索引檔，讓刪除後的空間真的釋出")
    p_vac.set_defaults(func=cmd_vacuum)

    p_serve = sub.add_parser("serve", help="啟動本機 Web UI")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="預設只綁 127.0.0.1，不要改成 0.0.0.0")
    p_serve.add_argument("--open", action="store_true", help="服務起來後自動開瀏覽器")
    p_serve.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
