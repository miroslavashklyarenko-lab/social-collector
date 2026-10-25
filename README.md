# Інкрементний колектор Mastodon

Збирає публічні пости з **Mastodon** (за хештегами або акаунтами) через офіційне API
і складає їх в одну канонічну таблицю `author · url · date · text (+ media)` у SQLite,
з експортом у JSONL і CSV.

Що вміє:

- **Інкрементний збір.** Курсор `min_id` для нових постів і `max_id` для історії. Повторний запуск продовжує з того місця, де зупинився попередній.
- **Dedup.** `uid` як PRIMARY KEY, `url` як UNIQUE, запис через `INSERT OR IGNORE`. Повторний прогін ідемпотентний.
- **Повага до лімітів.** Пауза між запитами, читання `X-RateLimit-*`, обробка 429 через `Retry-After`, експоненційний backoff з jitter.
- **Стійкість.** Збій однієї цілі не зупиняє решту. Усі HTTP-відповіді з заголовками лімітів пишуться в таблицю `http_log`.
- **Бонуси:** backfill історії, ротація токенів і проксі, HTML-дашборд, Docker / docker-compose, офлайн-тести.

## Структура

```
collector/
  config.py        налаштування з .env (секретів у коді немає)
  db.py            схема SQLite: posts · cursors · runs · http_log
  http_client.py   пейсинг, 429/backoff, ротація токенів і проксі
  sources/mastodon.py  запити до API + нормалізація в канонічну модель
  runner.py        курсори, backfill, ізоляція збоїв
  export.py        JSONL / CSV
  dashboard.py     data/dashboard.html
  report.py        REPORT.md з виміряними лімітами
  __main__.py      CLI
tests/             фейковий Mastodon-сервер і 9 тестів (працюють без інтернету)
```

## Запуск на Windows (найпростіший спосіб)

1. Встановіть Python 3.10 або новіший з https://www.python.org/downloads/. В інсталяторі поставте галочку **Add Python to PATH**.
2. Розпакуйте папку проєкту, наприклад у `Документи\social-collector`.
3. Двічі клацніть **`run_windows.bat`**. Скрипт сам:
   створить віртуальне середовище, встановить залежності, скопіює `.env.example` у `.env`,
   виконає збір і backfill, а потім експорт, дашборд і звіт.
4. Результат з'явиться у файлах:
   - `data/posts.jsonl`, `data/posts.csv`: приклад даних для здачі;
   - `data/dashboard.html`: дашборд, відкривається у браузері;
   - `REPORT.md`: звіт з реальними цифрами лімітів.

Якщо записів менше 200, запустіть `run_windows.bat` ще раз або збільште `BACKFILL_PAGES` у `.env`.

## Запуск вручну (будь-яка ОС)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env

python -m collector run        # нові пости (за перший раз курсори встановлюються)
python -m collector backfill   # одноразовий добір старих постів
python -m collector stats      # що зібрано, де курсори, скільки 429
python -m collector export --format jsonl   # data/posts.jsonl
python -m collector export --format csv     # data/posts.csv
python -m collector dashboard  # data/dashboard.html
python -m collector report     # REPORT.md
python -m collector loop --interval 900 --backfill   # безперервно, кожні 15 хв
```

Тести, що працюють офлайн на фейковому сервері: `python -m pytest -q`.

## Docker

```bash
cp .env.example .env
docker compose up -d --build         # loop кожні 15 хв, дані в ./data
docker compose run --rm collector stats
```

## Конфігурація (`.env`)

| Змінна | За замовчуванням | Що робить |
|---|---|---|
| `TARGETS` | `tag:osint,tag:ukraine,…` | цілі: `tag:x`, `tag:x@інстанс`, `account:user@інстанс` |
| `MASTODON_TOKENS` | порожньо | токени через кому для ротації (необов'язково) |
| `PROXIES` | порожньо | проксі через кому для ротації (необов'язково) |
| `PAGES_PER_RUN` / `BACKFILL_PAGES` | 3 / 5 | розмір партії за прогін |
| `MIN_INTERVAL` | 1.5 | пауза між запитами, секунди |
| `LOW_REMAINING` | 10 | поріг, з якого колектор чекає до `X-RateLimit-Reset` |
| `MAX_RETRIES`, `BACKOFF_BASE`, `BACKOFF_MAX` | 5, 2.0, 300 | параметри повторів |

Файл `.env` внесено до `.gitignore`, тож секрети не потрапляють у репозиторій.
Системні змінні `HTTPS_PROXY`/`HTTP_PROXY` бібліотека `requests` теж підхоплює автоматично.

## Канонічна модель (`posts`)

| Поле | Опис |
|---|---|
| `uid` | `mastodon:<інстанс>:<id>`, первинний ключ |
| `url` | канонічний URL поста (UNIQUE): той самий пост з різних хештегів чи інстансів зберігається один раз |
| `author` | `@user@instance` (+ `author_name`, `author_url`) |
| `date` | час публікації, ISO-8601 UTC |
| `text` | текст, очищений від HTML; CW-попередження додається префіксом |
| `media` | JSON `[{type, url, description}]` |
| `tags`, `lang`, `target`, `native_id`, `collected_at` | службові поля |

Інші таблиці: `cursors` (forward і backfill курсор для кожної цілі), `runs` (кожен прогін: скільки отримано, нових, дублів, помилки), `http_log` (код відповіді, `X-RateLimit-Limit/Remaining/Reset`, `Retry-After`, яка ідентичність робила запит).

## Як працює інкрементність

1. Перший `run` бере найсвіжішу сторінку і запам'ятовує `forward = max(id)` і `backfill = min(id)`.
2. Наступні `run` роблять запит `?min_id=forward`. Повертаються лише пости, новіші за курсор, сторінка за сторінкою, до `PAGES_PER_RUN`.
3. `backfill` робить запит `?max_id=backfill` і рухається в минуле. Коли API повертає порожню сторінку, ставиться `backfill_done=1`.
4. Курсор зсувається тільки після того, як сторінку записано в БД. Якщо прогін обірветься, наступний продовжить без пропусків, а можливі повтори відсіє dedup.
