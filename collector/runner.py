"""Оркестрація: інкрементний рух курсора, backfill, ізоляція збоїв по цілях."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Settings, Target
from .db import Storage
from .http_client import PoliteClient
from .sources.mastodon import MastodonSource, normalize

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    target: str
    mode: str
    status: str = "ok"
    pages: int = 0
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    error: str | None = None


@dataclass
class Summary:
    results: list[RunResult] = field(default_factory=list)

    @property
    def inserted(self) -> int:
        return sum(r.inserted for r in self.results)

    @property
    def failed(self) -> list[RunResult]:
        return [r for r in self.results if r.status == "error"]


def _ids(statuses: list[dict]) -> list[int]:
    return [int(s["id"]) for s in statuses]


class Collector:
    def __init__(self, settings: Settings, storage: Storage, client: PoliteClient | None = None):
        self.s = settings
        self.db = storage
        self.client = client or PoliteClient(settings, storage)
        self.source = MastodonSource(self.client, settings)

    # --- одна сторінка → БД --------------------------------------------------
    def _store(self, statuses: list[dict], target: Target, res: RunResult) -> None:
        posts = []
        for st in statuses:
            try:
                posts.append(normalize(st, target))
            except (KeyError, ValueError) as exc:  # один битий запис не зупиняє партію
                log.warning("Пропускаю запис %s: %r", st.get("id"), exc)
        ins, dup = self.db.save_posts(posts)
        res.pages += 1
        res.fetched += len(statuses)
        res.inserted += ins
        res.duplicates += dup

    # --- рух уперед ----------------------------------------------------------
    def forward(self, target: Target, pages: int | None = None) -> RunResult:
        res = RunResult(target.key, "forward")
        pages = pages or self.s.pages_per_run
        row = self.db.get_cursor(target.key)
        cursor = row["forward_cursor"] if row else None

        if cursor is None:
            # Перший запуск: беремо найсвіжішу сторінку й фіксуємо обидва курсори.
            page = self.source.fetch_page(target)
            if page:
                self._store(page, target, res)
                ids = _ids(page)
                self.db.set_cursor(target.key, forward=str(max(ids)), backfill=str(min(ids)))
            log.info("[%s] старт: %d записів, курсори встановлено", target.key, res.inserted)
            return res

        for _ in range(pages):
            page = self.source.fetch_page(target, min_id=cursor)
            if not page:
                break
            self._store(page, target, res)
            cursor = str(max(_ids(page)))
            self.db.set_cursor(target.key, forward=cursor)  # курсор рухається лише після коміту
            if len(page) < self.s.page_limit:
                break
        log.info("[%s] forward: сторінок=%d нових=%d дублікатів=%d курсор=%s",
                 target.key, res.pages, res.inserted, res.duplicates, cursor)
        return res

    # --- backfill історії ----------------------------------------------------
    def backfill(self, target: Target, pages: int | None = None) -> RunResult:
        res = RunResult(target.key, "backfill")
        pages = pages or self.s.backfill_pages
        row = self.db.get_cursor(target.key)
        if row is None or row["backfill_cursor"] is None:
            self.forward(target)
            row = self.db.get_cursor(target.key)
            if row is None:
                return res
        if row["backfill_done"]:
            log.info("[%s] backfill уже завершено", target.key)
            return res
        cursor = row["backfill_cursor"]
        for _ in range(pages):
            page = self.source.fetch_page(target, max_id=cursor)
            if not page:
                self.db.set_cursor(target.key, backfill_done=True)
                log.info("[%s] backfill дійшов до початку історії", target.key)
                break
            self._store(page, target, res)
            cursor = str(min(_ids(page)))
            self.db.set_cursor(target.key, backfill=cursor)
        log.info("[%s] backfill: сторінок=%d нових=%d курсор=%s",
                 target.key, res.pages, res.inserted, cursor)
        return res

    # --- прогін по всіх цілях з ізоляцією збоїв -----------------------------
    def run(self, mode: str = "forward", pages: int | None = None) -> Summary:
        summary = Summary()
        for target in self.s.targets:
            run_id = self.db.start_run(mode, target.key)
            try:
                res = self.backfill(target, pages) if mode == "backfill" else self.forward(target, pages)
            except Exception as exc:  # noqa: BLE001 — свідомо: збій цілі не валить прогін
                log.error("[%s] збій: %s", target.key, exc, exc_info=log.isEnabledFor(logging.DEBUG))
                res = RunResult(target.key, mode, status="error", error=f"{type(exc).__name__}: {exc}")
            self.db.finish_run(run_id, res.status, res.fetched, res.inserted, res.duplicates, res.error)
            summary.results.append(res)
        log.info("Прогін завершено: нових=%d, усього в БД=%d, збоїв=%d, HTTP=%s",
                 summary.inserted, self.db.count_posts(), len(summary.failed), self.client.stats)
        return summary
