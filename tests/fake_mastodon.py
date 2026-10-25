"""Мінімальний фейковий Mastodon API для офлайн-тестів (min_id/max_id/limit, 429, 500)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def make_status(i: int, tag: str = "osint") -> dict:
    created = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
    return {
        "id": str(100000 + i),
        "uri": f"https://example.social/users/u{i % 7}/statuses/{100000 + i}",
        "url": f"https://example.social/@u{i % 7}/{100000 + i}",
        "created_at": created.isoformat().replace("+00:00", ".000Z"),
        "content": f"<p>Пост №{i} про <a href='#'>#{tag}</a><br>другий рядок &amp; ще</p>",
        "spoiler_text": "",
        "language": "uk",
        "account": {"acct": f"u{i % 7}", "display_name": f"User {i % 7}",
                    "url": f"https://example.social/@u{i % 7}"},
        "media_attachments": ([{"type": "image", "url": f"https://example.social/m/{i}.jpg",
                                "description": "alt"}] if i % 10 == 0 else []),
        "tags": [{"name": tag}],
        "reblog": None,
    }


class FakeState:
    def __init__(self, n: int = 300):
        self.timelines: dict[str, list[dict]] = {"osint": [make_status(i) for i in range(n)]}
        self.fail_tags = {"broken"}
        self.throttle_next = 0                 # скільки наступних запитів віддати 429
        self.seen_auth: list[str | None] = []
        self.requests = 0


def _handler(state: FakeState):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body, headers: dict | None = None):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-RateLimit-Limit", "300")
            self.send_header("X-RateLimit-Remaining", str(max(0, 300 - state.requests)))
            self.send_header("X-RateLimit-Reset", "2030-01-01T00:00:00.000Z")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            state.requests += 1
            state.seen_auth.append(self.headers.get("Authorization"))
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if state.throttle_next > 0:
                state.throttle_next -= 1
                return self._send(429, {"error": "Too many requests"}, {"Retry-After": "1"})
            parts = u.path.strip("/").split("/")  # api v1 timelines tag <name>
            if parts[:4] != ["api", "v1", "timelines", "tag"]:
                return self._send(404, {"error": "not found"})
            tag = parts[4]
            if tag in state.fail_tags:
                return self._send(500, {"error": "boom"})
            items = sorted(state.timelines.get(tag, []), key=lambda s: int(s["id"]))
            limit = int(q.get("limit", 20))
            if "min_id" in q:
                newer = [s for s in items if int(s["id"]) > int(q["min_id"])]
                page = newer[:limit]
            else:
                if "max_id" in q:
                    items = [s for s in items if int(s["id"]) < int(q["max_id"])]
                page = items[-limit:]
            page = sorted(page, key=lambda s: int(s["id"]), reverse=True)  # як у Mastodon: нові зверху
            return self._send(200, page)
    return H


class FakeServer:
    def __init__(self, n: int = 300):
        self.state = FakeState(n)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.state))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
