"""Конфігурація колектора: усе читається з .env / змінних оточення, секретів у коді немає."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Target:
    """Одна ціль збору: хештег або акаунт на конкретному інстансі Mastodon.

    Формати рядка в TARGETS:
      tag:osint                  -> хештег на інстансі за замовчуванням
      tag:osint@infosec.exchange -> хештег на іншому інстансі
      account:Gargron@mastodon.social -> пости акаунта
    """
    kind: str          # "tag" | "account"
    name: str          # osint / Gargron
    instance: str      # mastodon.social

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}@{self.instance}"

    @classmethod
    def parse(cls, spec: str, default_instance: str) -> "Target":
        kind, _, rest = spec.partition(":")
        kind = kind.strip().lower()
        if kind not in {"tag", "account"} or not rest:
            raise ValueError(f"Невірний формат цілі: {spec!r}")
        name, _, instance = rest.lstrip("#@").partition("@")
        return cls(kind=kind, name=name.strip(), instance=(instance or default_instance).strip().lower())


@dataclass
class Settings:
    instance: str = field(default_factory=lambda: os.getenv("MASTODON_INSTANCE", "mastodon.social"))
    api_scheme: str = field(default_factory=lambda: os.getenv("API_SCHEME", "https"))  # http — лише для тестів
    targets_raw: list[str] = field(default_factory=lambda: _list("TARGETS", "tag:osint,tag:ukraine,tag:infosec"))
    tokens: list[str] = field(default_factory=lambda: _list("MASTODON_TOKENS"))
    proxies: list[str] = field(default_factory=lambda: _list("PROXIES"))
    page_limit: int = field(default_factory=lambda: _int("PAGE_LIMIT", 40))          # макс. для Mastodon = 40
    pages_per_run: int = field(default_factory=lambda: _int("PAGES_PER_RUN", 3))     # невелика партія за прогін
    backfill_pages: int = field(default_factory=lambda: _int("BACKFILL_PAGES", 5))
    min_interval: float = field(default_factory=lambda: _float("MIN_INTERVAL", 1.5))  # пауза між запитами, с
    max_retries: int = field(default_factory=lambda: _int("MAX_RETRIES", 5))
    backoff_base: float = field(default_factory=lambda: _float("BACKOFF_BASE", 2.0))
    backoff_max: float = field(default_factory=lambda: _float("BACKOFF_MAX", 300.0))
    low_remaining: int = field(default_factory=lambda: _int("LOW_REMAINING", 10))     # поріг «пригальмувати»
    timeout: float = field(default_factory=lambda: _float("HTTP_TIMEOUT", 20.0))
    db_path: Path = field(default_factory=lambda: Path(os.getenv("DB_PATH", "data/collector.db")))
    log_path: Path = field(default_factory=lambda: Path(os.getenv("LOG_PATH", "data/collector.log")))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    user_agent: str = field(default_factory=lambda: os.getenv(
        "USER_AGENT", "edu-social-collector/1.0 (homework; contact: set USER_AGENT in .env)"))

    @property
    def targets(self) -> list[Target]:
        return [Target.parse(s, self.instance) for s in self.targets_raw]
