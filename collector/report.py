"""Генерує REPORT.md зі звітом про джерело, ліміти та зібрані дані — на основі реальних логів у БД."""
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .db import Storage, utcnow


def build(db: Storage, s: Settings, out: Path) -> Path:
    q = lambda sql: db.conn.execute(sql).fetchone()  # noqa: E731
    total = db.count_posts()
    authors, d_min, d_max = q("SELECT COUNT(DISTINCT author), MIN(date), MAX(date) FROM posts")
    with_media = q("SELECT COUNT(*) FROM posts WHERE media != '[]'")[0]
    n_req, n429, n5xx, n_net = q("SELECT COUNT(*), SUM(status=429), SUM(status>=500), SUM(status IS NULL) FROM http_log")
    lim, rem_min = q("SELECT MAX(ratelimit_limit), MIN(ratelimit_remaining) FROM http_log")
    retry_after = q("SELECT GROUP_CONCAT(DISTINCT retry_after) FROM http_log WHERE retry_after IS NOT NULL")[0]
    runs, run_err, dups = q("SELECT COUNT(*), SUM(status='error'), SUM(duplicates) FROM runs")
    avg_ms = q("SELECT CAST(AVG(elapsed_ms) AS INT) FROM http_log WHERE status=200")[0]
    targets = db.conn.execute("SELECT target, COUNT(*) FROM posts GROUP BY target ORDER BY 2 DESC").fetchall()
    errs = db.conn.execute("SELECT target, error, COUNT(*) FROM runs WHERE status='error' "
                           "GROUP BY target, error").fetchall()
    err_lines = "\n".join(f"  - `{t}` — {n} прогін(и): `{e[:160]}`" for t, e, n in errs) or "  - не було"
    resets = db.conn.execute("SELECT DISTINCT substr(ratelimit_reset,12,8) FROM http_log "
                             "WHERE ratelimit_reset IS NOT NULL ORDER BY 1 DESC LIMIT 5").fetchall()
    reset_list = ", ".join(r[0] for r in resets) or "—"
    tgt_lines = "\n".join(f"| `{t}` | {c} |" for t, c in targets)

    text = f"""# Звіт: інкрементний колектор Mastodon

*Згенеровано автоматично з логів у БД ({utcnow()}). Цифри нижче — виміряні, а не припущені.*

## Джерело
**Mastodon** (інстанс `{s.instance}`), офіційне публічне REST API: `GET /api/v1/timelines/tag/:tag`
та `GET /api/v1/accounts/:id/statuses`. Для публічних хештегів авторизація не потрібна,
тому немає ризику блокування особистого акаунта. Цілі збору: `{", ".join(s.targets_raw)}`.

## Які ліміти зустріли
- Сервер у кожній відповіді повертає `X-RateLimit-Limit / Remaining / Reset`.
  Виміряно: **Limit = {lim if lim is not None else "—"}**, мінімальний Remaining за весь збір = **{rem_min if rem_min is not None else "—"}**.
  (За документацією Mastodon це ~300 запитів на 5 хв з однієї IP / одного токена.)
- Сторінка — максимум **{s.page_limit}** записів (`limit=40`), більше API не віддає.
- Усього запитів: **{n_req or 0}**, відповідей **429: {n429 or 0}**, 5xx: {n5xx or 0}, мережевих збоїв: {n_net or 0}.
  Значення Retry-After, які приходили: {retry_after or "не приходили"}.{" Жодного 429 не отримали: пейсинг тримає темп нижче ліміту, а гілку обробки 429/Retry-After перевірено офлайн-тестом `test_429_respects_retry_after` (фейковий сервер віддає 429)." if not n429 else ""} Середній час відповіді: {avg_ms or "—"} мс.
- Значення `X-RateLimit-Reset` (UTC): {reset_list} — вікно ліміту скидається на межі кожних 5 хвилин.
- Збої, що трапились під час збору (ціль ізольовано, решта прогону продовжилась):
{err_lines}
- Деякі інстанси (напр. `infosec.exchange`) вимикають анонімний перегляд стрічок: API відповідає
  **422 «This method requires an authenticated user»**. Це не тимчасова помилка, тому колектор не повторює
  запит (не марнує ліміт), а фіксує збій у `runs`. Рішення — додати токен цього інстансу в `MASTODON_TOKENS`.

## Як обходили (поважали) ліміти
1. **Пейсинг:** пауза ≥ {s.min_interval} с між запитами до одного хоста → максимум ~{int(300 / max(s.min_interval, 0.01))} запитів за 5 хв, тобто з запасом нижче ліміту.
2. **Адаптивне гальмування:** коли `Remaining ≤ {s.low_remaining}`, колектор сам чекає до `X-RateLimit-Reset`, не доводячи до 429.
3. **429:** чекаємо `Retry-After` (або `X-RateLimit-Reset`), якщо їх нема — експоненційний backoff `{s.backoff_base}^n` + jitter, максимум {s.max_retries} повторів.
4. **5xx / таймаути / обриви мережі:** повтор з backoff; якщо ціль так і не відповіла — вона позначається `error` у таблиці `runs`, а інші цілі збираються далі.
5. **Невеликі партії:** {s.pages_per_run} сторінки на ціль за прогін; курсор зсувається лише після запису сторінки в БД.
6. **Ротація** (бонус): кілька токенів (`MASTODON_TOKENS`) × проксі (`PROXIES`); ідентичність, яка отримала 429, іде на cooldown, запит робить наступна.

## Що саме зберегли
Канонічна таблиця `posts` (SQLite) + експорт `data/posts.jsonl` / `data/posts.csv`:
`uid · url · source · target · author · author_name · author_url · date (UTC ISO-8601) · text · lang · media[] · tags[] · native_id · collected_at`.
Текст очищено від HTML; медіа — список `{{type, url, description}}`.

- Записів: **{total}**, унікальних авторів: **{authors}**, з медіа: {with_media}.
- Період: {d_min or "—"} … {d_max or "—"}.
- Прогонів: {runs or 0} (з помилкою: {run_err or 0}); відкинуто дублікатів: **{dups or 0}** (той самий пост під кількома хештегами / повторні прогони).

| Ціль | Записів |
|---|---|
{tgt_lines}

## Інкрементність і dedup
- `forward`: `min_id = останній зібраний id` → лише нові пости; повторний запуск продовжує з того ж місця.
- `backfill`: `max_id = найстаріший зібраний id` → одноразовий добір історії до кінця (`backfill_done = 1`).
- Унікальність: `uid` (PRIMARY KEY) і `url` (UNIQUE) + `INSERT OR IGNORE` → той самий пост під двома
  хештегами або при повторному прогоні не дублюється; прогін ідемпотентний.
"""
    out.write_text(text, encoding="utf-8")
    return out
