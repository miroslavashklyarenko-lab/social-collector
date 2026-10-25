"""Джерело Mastodon: публічні хештеги та акаунти через офіційне REST API.

Пагінація за id (snowflake, зростають з часом):
  * min_id=X  — сторінка постів, що йдуть одразу ПІСЛЯ X (рух уперед без пропусків);
  * max_id=X  — сторінка постів, СТАРШИХ за X (backfill історії).
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from html.parser import HTMLParser

from ..config import Settings, Target
from ..db import Post
from ..http_client import PoliteClient

log = logging.getLogger(__name__)
SOURCE = "mastodon"


class _TextExtractor(HTMLParser):
    BREAKS = {"br", "p", "li"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(raw: str) -> str:
    p = _TextExtractor()
    p.feed(raw or "")
    text = html.unescape("".join(p.parts))
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _iso_utc(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize(status: dict, target: Target) -> Post:
    """Mastodon status → канонічна модель Post."""
    acct = status["account"]["acct"]
    if "@" not in acct:
        acct = f"{acct}@{target.instance}"
    text = html_to_text(status.get("content", ""))
    if status.get("spoiler_text"):
        text = f"[CW: {status['spoiler_text']}]\n{text}"
    media = [
        {"type": m.get("type"), "url": m.get("url") or m.get("remote_url"),
         "description": m.get("description")}
        for m in status.get("media_attachments", [])
    ]
    return Post(
        uid=f"{SOURCE}:{target.instance}:{status['id']}",
        url=status.get("url") or status["uri"],
        source=SOURCE,
        target=target.key,
        author=f"@{acct}",
        author_name=status["account"].get("display_name") or None,
        author_url=status["account"].get("url"),
        date=_iso_utc(status["created_at"]),
        text=text,
        lang=status.get("language"),
        media=media,
        tags=[t["name"].lower() for t in status.get("tags", [])],
        native_id=str(status["id"]),
    )


class MastodonSource:
    def __init__(self, client: PoliteClient, settings: Settings):
        self.client = client
        self.s = settings
        self._account_ids: dict[str, str] = {}

    def _endpoint(self, target: Target) -> tuple[str, dict]:
        base = f"{self.s.api_scheme}://{target.instance}/api/v1"
        if target.kind == "tag":
            return f"{base}/timelines/tag/{target.name}", {}
        if target.name not in self._account_ids:
            r = self.client.get(f"{base}/accounts/lookup", params={"acct": target.name})
            self._account_ids[target.name] = r.json()["id"]
        return f"{base}/accounts/{self._account_ids[target.name]}/statuses", {"exclude_reblogs": "true"}

    def fetch_page(self, target: Target, *, min_id: str | None = None,
                   max_id: str | None = None) -> list[dict]:
        url, params = self._endpoint(target)
        params["limit"] = self.s.page_limit
        if min_id:
            params["min_id"] = min_id
        if max_id:
            params["max_id"] = max_id
        data = self.client.get(url, params=params).json()
        if not isinstance(data, list):
            raise ValueError(f"Неочікувана відповідь API: {str(data)[:200]}")
        # пости-буcти (reblog) у стрічках тегів не трапляються, але відсіємо про всяк випадок
        return [st for st in data if not st.get("reblog")]
