@echo off
REM Alcopa deadline watcher. Captures the final price in the seconds before
REM each sale closes -- Alcopa strips the price permanently once a lot sells,
REM so a missed window is data that cannot be recovered afterwards.
cd /d "%~dp0"
set ALCOPA_BROWSER=playwright
python -u alcopa_scrape.py watch --horizon 86400 --workers 12 --out data\watch_%DATE:~-4%%DATE:~3,2%%DATE:~0,2%.jsonl >> data\watch_run.log 2>&1
