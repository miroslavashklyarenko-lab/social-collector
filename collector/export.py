"""Експорт канонічної таблиці у JSONL / CSV."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .db import Storage

FIELDS = ["uid", "source", "target", "author", "author_name", "author_url", "url",
          "date", "text", "lang", "media", "tags", "native_id", "collected_at"]


def export(storage: Storage, out: Path, fmt: str) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = storage.conn.execute(f"SELECT {','.join(FIELDS)} FROM posts ORDER BY date DESC").fetchall()
    if fmt == "jsonl":
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                d = dict(r)
                d["media"] = json.loads(d["media"])
                d["tags"] = json.loads(d["tags"])
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
    elif fmt == "csv":
        # utf-8-sig — щоб Excel на Windows коректно показав кирилицю
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow(dict(r))
    else:
        raise ValueError(f"Невідомий формат: {fmt}")
    return len(rows)
