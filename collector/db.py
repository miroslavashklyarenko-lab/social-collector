"""SQLite-сховище: канонічна таблиця posts, курсори, журнал прогонів і HTTP-запитів."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
-- Канонічна модель: однакова для будь-якого джерела.
CREATE TABLE IF NOT EXISTS posts (
    uid          TEXT PRIMARY KEY,          -- dedup-ключ: <source>:<instance>:<native_id>
    url          TEXT NOT NULL UNIQUE,      -- другий dedup-ключ: канонічний URL поста
    source       TEXT NOT NULL,             -- mastodon
    target       TEXT NOT NULL,             -- ціль, через яку пост вперше знайдено
    author       TEXT NOT NULL,             -- @user@instance
    author_name  TEXT,
    author_url   TEXT,
    date         TEXT NOT NULL,             -- ISO-8601, UTC
    text         TEXT NOT NULL,             -- очищений від HTML текст
    lang         TEXT,
    media        TEXT NOT NULL DEFAULT '[]',-- JSON: [{type,url,description}]
    tags         TEXT NOT NULL DEFAULT '[]',-- JSON: ["osint", ...]
    native_id    TEXT NOT NULL,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_date   ON posts(date);
CREATE INDEX IF NOT EXISTS idx_posts_target ON posts(target);

-- Курсори: де зупинились для кожної цілі.
CREATE TABLE IF NOT EXISTS cursors (
    target          TEXT PRIMARY KEY,
    forward_cursor  TEXT,      -- найновіший зібраний id (рух уперед: min_id)
    backfill_cursor TEXT,      -- найстаріший зібраний id (рух назад: max_id)
    backfill_done   INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

-- Журнал прогонів (для звіту та дашборду).
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mode         TEXT NOT NULL,           -- forward | backfill
    target       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,           -- ok | error | partial
    fetched      INTEGER NOT NULL DEFAULT 0,
    inserted     INTEGER NOT NULL DEFAULT 0,
    duplicates   INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

-- Емпіричний журнал HTTP: код відповіді та заголовки лімітів.
CREATE TABLE IF NOT EXISTS http_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    host             TEXT NOT NULL,
    path             TEXT NOT NULL,
    status           INTEGER,             -- NULL = мережева помилка
    elapsed_ms       INTEGER,
    ratelimit_limit  INTEGER,
    ratelimit_remaining INTEGER,
    ratelimit_reset  TEXT,
    retry_after      TEXT,
    identity         TEXT,                -- який токен/проксі використано (маска)
    note             TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Post:
    uid: str
    url: str
    source: str
    target: str
    author: str
    date: str
    text: str
    native_id: str
    author_name: str | None = None
    author_url: str | None = None
    lang: str | None = None
    media: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        return {
            "uid": self.uid, "url": self.url, "source": self.source, "target": self.target,
            "author": self.author, "author_name": self.author_name, "author_url": self.author_url,
            "date": self.date, "text": self.text, "lang": self.lang,
            "media": json.dumps(self.media, ensure_ascii=False),
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "native_id": self.native_id, "collected_at": utcnow(),
        }


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- posts ---------------------------------------------------------------
    def save_posts(self, posts: list[Post]) -> tuple[int, int]:
        """Ідемпотентний запис. Повертає (inserted, duplicates).

        INSERT OR IGNORE спрацьовує і на uid, і на url — повторний запуск
        або той самий пост під двома хештегами не створюють дублікатів.
        """
        inserted = 0
        with self.conn:
            for p in posts:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO posts
                       (uid,url,source,target,author,author_name,author_url,date,text,lang,media,tags,native_id,collected_at)
                       VALUES (:uid,:url,:source,:target,:author,:author_name,:author_url,:date,:text,:lang,:media,:tags,:native_id,:collected_at)""",
                    p.as_row(),
                )
                inserted += cur.rowcount
        return inserted, len(posts) - inserted

    def count_posts(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    # --- cursors -------------------------------------------------------------
    def get_cursor(self, target: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM cursors WHERE target=?", (target,)).fetchone()

    def set_cursor(self, target: str, *, forward: str | None = None,
                   backfill: str | None = None, backfill_done: bool | None = None) -> None:
        row = self.get_cursor(target)
        fwd = forward if forward is not None else (row["forward_cursor"] if row else None)
        bwd = backfill if backfill is not None else (row["backfill_cursor"] if row else None)
        done = int(backfill_done) if backfill_done is not None else (row["backfill_done"] if row else 0)
        with self.conn:
            self.conn.execute(
                """INSERT INTO cursors(target,forward_cursor,backfill_cursor,backfill_done,updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(target) DO UPDATE SET forward_cursor=excluded.forward_cursor,
                     backfill_cursor=excluded.backfill_cursor, backfill_done=excluded.backfill_done,
                     updated_at=excluded.updated_at""",
                (target, fwd, bwd, done, utcnow()),
            )

    # --- runs ----------------------------------------------------------------
    def start_run(self, mode: str, target: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs(mode,target,started_at,status) VALUES (?,?,?, 'running')",
                (mode, target, utcnow()))
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, fetched: int, inserted: int,
                   duplicates: int, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE runs SET finished_at=?, status=?, fetched=?, inserted=?, duplicates=?, error=?
                   WHERE id=?""",
                (utcnow(), status, fetched, inserted, duplicates, error, run_id))

    # --- http log ------------------------------------------------------------
    def log_http(self, **kw) -> None:
        kw.setdefault("ts", utcnow())
        cols = ",".join(kw)
        marks = ",".join("?" for _ in kw)
        with self.conn:
            self.conn.execute(f"INSERT INTO http_log({cols}) VALUES ({marks})", tuple(kw.values()))

    def close(self) -> None:
        self.conn.close()
