@echo off
chcp 65001 >nul
REM Повний цикл для здачі: встановлення, збір, backfill, експорт, дашборд, звіт.
cd /d "%~dp0"
if not exist .venv (
    py -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
if not exist .env copy .env.example .env >nul

python -m collector run
python -m collector backfill
python -m collector run
python -m collector stats
python -m collector export --format jsonl
python -m collector export --format csv
python -m collector dashboard
python -m collector report
echo.
echo Готово. Відкрийте data\dashboard.html та REPORT.md
pause
