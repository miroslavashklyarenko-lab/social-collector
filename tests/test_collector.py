"""Офлайн-тести: інкрементність, dedup, ідемпотентність, 429/backoff, ізоляція збоїв, ротація токенів."""
from __future__ import annotations

import json

import pytest

from collector.config import Settings, Target
from collector.db import Storage
from collector.http_client import PoliteClient
from collector.runner import Collector
from collector.sources.mastodon import html_to_text

from .fake_mastodon import FakeServer, make_status


@pytest.fixture
def server():
    with FakeServer(n=300) as srv:
        yield srv


def make(server, tmp_path, targets=("tag:osint",), tokens=()):
    s = Settings(instance=server.host, api_scheme="http", targets_raw=list(targets),
                 tokens=list(tokens), proxies=[], min_interval=0, backoff_base=1.01,
                 page_limit=40, pages_per_run=3, backfill_pages=20, max_retries=3,
                 db_path=tmp_path / "t.db")
    db = Storage(s.db_path)
    sleeps: list[float] = []
    client = PoliteClient(s, db, sleep=sleeps.append)
    return Collector(s, db, client), db, sleeps


def test_first_run_then_incremental(server, tmp_path):
    c, db, _ = make(server, tmp_path)
    c.run("forward")
    assert db.count_posts() == 40                       # перший запуск = найсвіжіша сторінка
    cur = db.get_cursor(f"tag:osint@{server.host}")
    assert cur["forward_cursor"] == "100299" and cur["backfill_cursor"] == "100260"

    # з'явилось 5 нових постів — забираємо лише їх
    server.state.timelines["osint"] += [make_status(i) for i in range(300, 305)]
    res = c.run("forward").results[0]
    assert (res.inserted, res.duplicates) == (5, 0)
    assert db.count_posts() == 45

    # повторний запуск без нових даних — нічого не змінюється (ідемпотентність)
    res = c.run("forward").results[0]
    assert res.inserted == 0 and db.count_posts() == 45


def test_backfill_collects_full_history_once(server, tmp_path):
    c, db, _ = make(server, tmp_path)
    c.run("forward")
    c.run("backfill")
    assert db.count_posts() == 300
    assert db.get_cursor(f"tag:osint@{server.host}")["backfill_done"] == 1
    res = c.run("backfill").results[0]                  # повторний backfill — без запитів і записів
    assert res.pages == 0 and db.count_posts() == 300


def test_dedup_by_url_across_targets(server, tmp_path):
    server.state.timelines["osint2"] = [make_status(i) for i in range(280, 300)]  # ті самі пости
    c, db, _ = make(server, tmp_path, targets=("tag:osint", "tag:osint2"))
    summary = c.run("forward")
    assert db.count_posts() == 40
    assert summary.results[1].duplicates == 20


def test_429_respects_retry_after(server, tmp_path):
    server.state.throttle_next = 2
    c, db, sleeps = make(server, tmp_path)
    c.run("forward")
    assert db.count_posts() == 40
    assert c.client.stats["rate_limited"] == 2
    assert sum(1 for x in sleeps if 0.5 <= x <= 1.5) >= 2   # чекали саме Retry-After=1 с
    codes = [r[0] for r in db.conn.execute("SELECT status FROM http_log ORDER BY id")]
    assert codes[:3] == [429, 429, 200]


def test_one_failing_target_does_not_break_run(server, tmp_path):
    c, db, _ = make(server, tmp_path, targets=("tag:broken", "tag:osint"))
    summary = c.run("forward")
    assert [r.status for r in summary.results] == ["error", "ok"]
    assert db.count_posts() == 40
    row = db.conn.execute("SELECT status, error FROM runs WHERE target LIKE 'tag:broken%'").fetchone()
    assert row["status"] == "error" and "RetriesExhausted" in row["error"]


def test_token_rotation_switches_on_429(server, tmp_path):
    server.state.throttle_next = 1
    c, db, _ = make(server, tmp_path, tokens=("tokenAAAA", "tokenBBBB"))
    c.run("forward")
    assert server.state.seen_auth[:2] == ["Bearer tokenAAAA", "Bearer tokenBBBB"]
    assert db.count_posts() == 40


def test_canonical_fields(server, tmp_path):
    c, db, _ = make(server, tmp_path)
    c.run("forward")
    r = db.conn.execute("SELECT * FROM posts WHERE native_id='100290'").fetchone()
    assert r["author"] == "@u3@" + server.host
    assert r["url"] == "https://example.social/@u3/100290"
    assert r["date"] == "2026-10-01T04:50:00+00:00"
    assert r["text"] == "Пост №290 про #osint\nдругий рядок & ще"
    assert json.loads(r["media"])[0]["type"] == "image"


def test_target_parse():
    t = Target.parse("account:@Gargron@mastodon.social", "x.y")
    assert (t.kind, t.name, t.instance) == ("account", "Gargron", "mastodon.social")
    assert Target.parse("tag:#osint", "infosec.exchange").key == "tag:osint@infosec.exchange"


def test_html_to_text():
    assert html_to_text("<p>a<br/>b</p><p>c &lt;3</p>") == "a\nb\nc <3"
