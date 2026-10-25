"""HTTP-клієнт, що поважає ліміти джерела.

* пейсинг: мінімальна пауза між запитами до одного хоста;
* адаптивне гальмування: якщо X-RateLimit-Remaining близько до нуля — чекаємо до X-RateLimit-Reset;
* 429: чекаємо Retry-After / X-RateLimit-Reset, інакше експоненційний backoff з jitter;
* 5xx і мережеві збої: повтор з backoff, після MAX_RETRIES — виняток (але не падіння всього прогону);
* ротація «ідентичностей» (токен × проксі): ідентичність, що впіймала ліміт, іде на cooldown.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from itertools import cycle
from typing import Callable
from urllib.parse import urlsplit

import requests

from .config import Settings
from .db import Storage

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class FatalHTTPError(Exception):
    """Помилка, яку немає сенсу повторювати (404, 401, 403 ...)."""

    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status


class RetriesExhausted(Exception):
    pass


@dataclass
class Identity:
    token: str | None
    proxy: str | None
    cooldown_until: float = 0.0

    @property
    def label(self) -> str:
        t = f"token…{self.token[-4:]}" if self.token else "anon"
        p = urlsplit(self.proxy).hostname if self.proxy else "direct"
        return f"{t}/{p}"


def _parse_reset(value: str | None, now: float) -> float | None:
    """X-RateLimit-Reset у Mastodon — ISO-час; у деяких API — epoch або секунди."""
    if not value:
        return None
    try:
        num = float(value)
        return num if num > 1e9 else now + num
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_retry_after(value: str | None, now: float) -> float | None:
    """Retry-After: секунди або HTTP-дата."""
    if not value:
        return None
    try:
        return now + float(value)
    except ValueError:
        try:
            return parsedate_to_datetime(value).astimezone(timezone.utc).timestamp()
        except (TypeError, ValueError):
            return None


def _int_or_none(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


class PoliteClient:
    def __init__(self, settings: Settings, storage: Storage | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.s = settings
        self.storage = storage
        self.sleep = sleep
        self.clock = clock
        self.session = requests.Session()
        self.session.headers["User-Agent"] = settings.user_agent
        self.session.headers["Accept"] = "application/json"
        tokens = settings.tokens or [None]
        proxies = settings.proxies or [None]
        self.identities = [Identity(t, p) for t in tokens for p in proxies]
        self._rr = cycle(range(len(self.identities)))
        self._last_request: dict[str, float] = {}
        self.stats = {"requests": 0, "rate_limited": 0, "retries": 0, "slowdowns": 0}

    # --- вибір ідентичності --------------------------------------------------
    def _pick_identity(self) -> Identity:
        now = self.clock()
        for _ in range(len(self.identities)):
            ident = self.identities[next(self._rr)]
            if ident.cooldown_until <= now:
                return ident
        # усі на cooldown — чекаємо найранішу
        ident = min(self.identities, key=lambda i: i.cooldown_until)
        wait = ident.cooldown_until - now
        log.warning("Усі ідентичності на cooldown, чекаю %.1f с", wait)
        self.sleep(max(wait, 0))
        return ident

    def _pace(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self.s.min_interval - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_request[host] = self.clock()

    def _backoff(self, attempt: int) -> float:
        delay = min(self.s.backoff_max, self.s.backoff_base ** attempt)
        return delay + random.uniform(0, delay * 0.25)

    # --- основний метод ------------------------------------------------------
    def get(self, url: str, params: dict | None = None) -> requests.Response:
        host = urlsplit(url).netloc
        path = urlsplit(url).path
        last_error: Exception | None = None

        for attempt in range(self.s.max_retries + 1):
            ident = self._pick_identity()
            self._pace(host)
            headers = {"Authorization": f"Bearer {ident.token}"} if ident.token else {}
            proxies = {"http": ident.proxy, "https": ident.proxy} if ident.proxy else None
            started = self.clock()
            self.stats["requests"] += 1
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        proxies=proxies, timeout=self.s.timeout)
            except requests.RequestException as exc:
                last_error = exc
                delay = self._backoff(attempt)
                self._log(host, path, None, started, None, ident, note=f"{type(exc).__name__}")
                log.warning("Мережева помилка %s (%s), спроба %d/%d, чекаю %.1f с",
                            host, type(exc).__name__, attempt + 1, self.s.max_retries + 1, delay)
                self.stats["retries"] += 1
                self.sleep(delay)
                continue

            now = self.clock()
            limit = _int_or_none(resp.headers.get("X-RateLimit-Limit"))
            remaining = _int_or_none(resp.headers.get("X-RateLimit-Remaining"))
            reset_raw = resp.headers.get("X-RateLimit-Reset")
            retry_raw = resp.headers.get("Retry-After")
            self._log(host, path, resp.status_code, started, resp, ident)
            log.debug("%s %s -> %s remaining=%s/%s reset=%s", host, path, resp.status_code,
                      remaining, limit, reset_raw)

            if resp.status_code == 429:
                self.stats["rate_limited"] += 1
                until = (_parse_retry_after(retry_raw, now) or _parse_reset(reset_raw, now)
                         or now + self._backoff(attempt))
                until = min(until, now + self.s.backoff_max)
                ident.cooldown_until = until
                log.warning("429 від %s [%s]: Retry-After=%s Reset=%s → cooldown %.1f с",
                            host, ident.label, retry_raw, reset_raw, until - now)
                if len(self.identities) == 1:
                    self.sleep(max(until - now, 0))
                last_error = RuntimeError("429 Too Many Requests")
                continue

            if resp.status_code in RETRY_STATUSES:
                delay = self._backoff(attempt)
                log.warning("HTTP %s від %s, спроба %d/%d, чекаю %.1f с", resp.status_code, host,
                            attempt + 1, self.s.max_retries + 1, delay)
                self.stats["retries"] += 1
                last_error = RuntimeError(f"HTTP {resp.status_code}")
                self.sleep(delay)
                continue

            if resp.status_code >= 400:
                raise FatalHTTPError(resp.status_code, resp.url, resp.text)

            # Успіх. Адаптивне гальмування до вичерпання ліміту.
            if remaining is not None and remaining <= self.s.low_remaining:
                reset_at = _parse_reset(reset_raw, now)
                if reset_at and reset_at > now:
                    self.stats["slowdowns"] += 1
                    ident.cooldown_until = min(reset_at, now + self.s.backoff_max)
                    log.info("Ліміт майже вичерпано (%s/%s) для %s, пауза до reset (%.0f с)",
                             remaining, limit, ident.label, ident.cooldown_until - now)
            return resp

        raise RetriesExhausted(f"{url}: {last_error}")

    def _log(self, host, path, status, started, resp, ident, note=None) -> None:
        if not self.storage:
            return
        h = resp.headers if resp is not None else {}
        self.storage.log_http(
            host=host, path=path, status=status,
            elapsed_ms=int((self.clock() - started) * 1000),
            ratelimit_limit=_int_or_none(h.get("X-RateLimit-Limit")),
            ratelimit_remaining=_int_or_none(h.get("X-RateLimit-Remaining")),
            ratelimit_reset=h.get("X-RateLimit-Reset"),
            retry_after=h.get("Retry-After"),
            identity=ident.label, note=note,
        )
