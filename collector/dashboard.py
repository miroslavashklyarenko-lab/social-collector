"""Статичний HTML-дашборд зі статистикою збору (відкривається у браузері без сервера)."""
from __future__ import annotations

import html
from pathlib import Path

from .db import Storage, utcnow


def _q(db: Storage, sql: str, *args):
    return db.conn.execute(sql, args).fetchall()


def _bars(rows: list[tuple[str, int]]) -> str:
    """Горизонтальні бари на чистому HTML/CSS — однаковий розмір тексту на будь-якій ширині."""
    if not rows:
        return "<p class='muted'>Немає даних</p>"
    peak = max(v for _, v in rows) or 1
    out = []
    for label, val in rows:
        pct = max(1.0, 100 * val / peak)
        out.append(f"<div class='row'><span class='lbl'>{html.escape(str(label))}</span>"
                   f"<span class='track'><span class='bar' style='width:{pct:.1f}%'></span></span>"
                   f"<span class='val'>{val}</span></div>")
    return "".join(out)


def _table(headers: list[str], rows) -> str:
    th = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    trs = "".join("<tr>" + "".join(f"<td>{html.escape('' if c is None else str(c))}</td>" for c in r) + "</tr>"
                  for r in rows)
    return f"<div class='tw'><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>"


def build(db: Storage, out: Path) -> Path:
    total = db.count_posts()
    authors = _q(db, "SELECT COUNT(DISTINCT author) FROM posts")[0][0]
    span = _q(db, "SELECT MIN(date), MAX(date) FROM posts")[0]
    req = _q(db, "SELECT COUNT(*), SUM(status=429), SUM(status IS NULL OR status>=500) FROM http_log")[0]
    min_rem = _q(db, "SELECT MIN(ratelimit_remaining), MAX(ratelimit_limit) FROM http_log")[0]
    by_target = [(r[0].split("@")[0], r[1]) for r in
                 _q(db, "SELECT target, COUNT(*) FROM posts GROUP BY target ORDER BY 2 DESC")]
    by_day = [(r[0], r[1]) for r in
              _q(db, "SELECT substr(date,1,10) d, COUNT(*) FROM posts GROUP BY d ORDER BY d DESC LIMIT 14")]
    by_status = [(str(r[0] if r[0] is not None else "net err"), r[1]) for r in
                 _q(db, "SELECT status, COUNT(*) FROM http_log GROUP BY status ORDER BY 2 DESC")]
    cursors = _q(db, "SELECT target, forward_cursor, backfill_cursor, "
                     "CASE backfill_done WHEN 1 THEN 'так' ELSE 'ні' END, updated_at FROM cursors")
    runs = _q(db, "SELECT id, mode, target, started_at, status, fetched, inserted, duplicates, "
                  "COALESCE(error,'') FROM runs ORDER BY id DESC LIMIT 25")
    limits = _q(db, "SELECT ts, host, status, ratelimit_remaining, ratelimit_limit, ratelimit_reset, "
                    "COALESCE(retry_after,''), identity FROM http_log "
                    "WHERE status=429 OR ratelimit_remaining IS NOT NULL ORDER BY id DESC LIMIT 20")

    kpi = [("Записів у БД", total), ("Авторів", authors), ("HTTP-запитів", req[0] or 0),
           ("Відповідей 429", req[1] or 0), ("Мережа/5xx", req[2] or 0),
           ("Мін. Remaining", f"{min_rem[0]}/{min_rem[1]}" if min_rem[0] is not None else "—")]
    kpi_html = "".join(f"<div class='kpi'><div class='k'>{html.escape(k)}</div>"
                       f"<div class='v'>{html.escape(str(v))}</div></div>" for k, v in kpi)

    doc = f"""<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Дашборд колектора</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e3de;--acc:#3b6fd8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#ececea;--muted:#9a9a94;--card:#1f1f1f;--line:#333;--acc:#7aa2ff}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 10px}}
.muted{{color:var(--muted)}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}
.k{{color:var(--muted);font-size:12px}} .v{{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.two{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:12.5px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{color:var(--muted);font-weight:500}}
.row{{display:grid;grid-template-columns:minmax(80px,140px) 1fr 48px;gap:8px;align-items:center;margin:4px 0}}
.lbl{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.5px}}
.track{{height:14px}} .bar{{display:block;height:100%;background:var(--acc);border-radius:3px}}
.val{{font-variant-numeric:tabular-nums;font-size:12.5px;color:var(--muted)}}
</style></head><body><main>
<h1>Колектор Mastodon — статистика збору</h1>
<p class="muted">Згенеровано {utcnow()} · період даних: {span[0] or '—'} … {span[1] or '—'}</p>
<div class="grid">{kpi_html}</div>
<div class="two">
<div><h2>Записів за ціллю</h2><div class="card">{_bars(by_target)}</div></div>
<div><h2>Записів за днями (UTC)</h2><div class="card">{_bars(by_day)}</div></div>
</div>
<h2>HTTP-коди відповідей</h2><div class="card">{_bars(by_status)}</div>
<h2>Курсори</h2><div class="card">{_table(["ціль","forward (min_id)","backfill (max_id)","історію добрано","оновлено"], cursors)}</div>
<h2>Останні прогони</h2><div class="card">{_table(["#","режим","ціль","старт","статус","отримано","нових","дублів","помилка"], runs)}</div>
<h2>Емпіричні ліміти (останні 20 записів)</h2><div class="card">{_table(["час","хост","код","Remaining","Limit","Reset","Retry-After","ідентичність"], limits)}</div>
</main></body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out
