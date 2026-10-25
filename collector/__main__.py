"""CLI: python -m collector <команда>

  run        — інкрементний збір нового (рух курсора вперед)
  backfill   — одноразовий добір старих записів (рух назад)
  loop       — run у циклі з інтервалом (для Docker)
  stats      — коротка статистика
  export     — вивантаження у JSONL / CSV
  dashboard  — HTML-дашборд
  report     — REPORT.md з виміряними лімітами
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .config import Settings
from .dashboard import build as build_dashboard
from .db import Storage
from .export import export
from .report import build as build_report
from .runner import Collector


def setup_logging(s: Settings) -> None:
    s.log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(s.log_path, encoding="utf-8")]
    logging.basicConfig(level=s.log_level.upper(), format=fmt, handlers=handlers, force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def print_stats(db: Storage) -> None:
    print(f"Усього записів: {db.count_posts()}")
    for r in db.conn.execute("SELECT target, COUNT(*), MIN(date), MAX(date) FROM posts GROUP BY target"):
        print(f"  {r[0]:<40} {r[1]:>5}  {r[2]} … {r[3]}")
    print("Курсори:")
    for r in db.conn.execute("SELECT * FROM cursors"):
        print(f"  {r['target']:<40} fwd={r['forward_cursor']} back={r['backfill_cursor']} "
              f"done={bool(r['backfill_done'])}")
    r = db.conn.execute("SELECT COUNT(*), SUM(status=429), MIN(ratelimit_remaining), MAX(ratelimit_limit) "
                        "FROM http_log").fetchone()
    print(f"HTTP: запитів={r[0]} 429={r[1] or 0} мін.remaining={r[2]} limit={r[3]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collector", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "backfill"):
        p = sub.add_parser(name)
        p.add_argument("--pages", type=int, help="скільки сторінок за прогін на ціль")
    p = sub.add_parser("loop")
    p.add_argument("--interval", type=int, default=900, help="секунд між прогонами")
    p.add_argument("--backfill", action="store_true", help="також добирати історію в кожному циклі")
    sub.add_parser("stats")
    p = sub.add_parser("export")
    p.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    p.add_argument("--out", type=Path)
    p = sub.add_parser("dashboard")
    p.add_argument("--out", type=Path, default=Path("data/dashboard.html"))
    p = sub.add_parser("report")
    p.add_argument("--out", type=Path, default=Path("REPORT.md"))
    args = ap.parse_args(argv)

    s = Settings()
    setup_logging(s)
    db = Storage(s.db_path)
    try:
        if args.cmd in ("run", "backfill"):
            summary = Collector(s, db).run(args.cmd if args.cmd == "backfill" else "forward", args.pages)
            # ненульовий код лише якщо впали ВСІ цілі
            return 1 if summary.results and len(summary.failed) == len(summary.results) else 0
        if args.cmd == "loop":
            collector = Collector(s, db)
            while True:
                collector.run("forward")
                if args.backfill:
                    collector.run("backfill")
                build_dashboard(db, Path("data/dashboard.html"))
                logging.info("Наступний прогін через %d с", args.interval)
                time.sleep(args.interval)
        if args.cmd == "stats":
            print_stats(db)
        elif args.cmd == "export":
            out = args.out or Path(f"data/posts.{args.format}")
            n = export(db, out, args.format)
            print(f"Експортовано {n} записів → {out}")
        elif args.cmd == "dashboard":
            print(f"Дашборд: {build_dashboard(db, args.out)}")
        elif args.cmd == "report":
            print(f"Звіт: {build_report(db, s, args.out)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
