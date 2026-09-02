@echo off
REM Start everything for a trading day, in one go.
REM
REM Written because the morning is tight: waking at 10:00 with the first sale
REM closing at 10:30 leaves ~20 minutes, and the watcher has to be up before
REM 10:23 (it wakes ~7 minutes early to mint a WAF token). A checklist is the
REM wrong tool for that; this is one double-click.
REM
REM   start_all.bat            site + watcher
REM   start_all.bat nosite     watcher only
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set ALCOPA_BROWSER=playwright
for /f %%d in ('powershell -NoProfile -Command "(Get-Date -Format yyyy-MM-dd)"') do set DAY=%%d

if not exist data mkdir data

if /i not "%~1"=="nosite" (
  echo [1/2] site   -^> http://localhost:8020
  start "" /b python -u serve_cars.py --port 8020 ^
      1>>"data\site.log" 2>>"data\site.err.log"
)

REM The watch list from last night's crawl is reused on purpose: rebuilding it
REM takes ~20 minutes and the first sale will not wait. Refresh it AFTER the
REM day starts, or overnight.
echo [2/2] watcher -^> data\watch_%DAY%.jsonl
start "" /b python -u alcopa_scrape.py watch ^
    --lots data\watchlist.json ^
    --out "data\watch_%DAY%.jsonl" ^
    --horizon 86400 --workers 24 --sweep 900 ^
    1>>"data\watch_%DAY%.log" 2>>"data\watch_%DAY%.err.log"

timeout /t 6 /nobreak >nul
echo.
echo --- what the watcher thinks it is doing ---
powershell -NoProfile -Command "Get-Content 'data\watch_%DAY%.log' -Tail 14"
echo.
echo Site:    http://localhost:8020
echo Watcher: data\watch_%DAY%.log        (tail it to follow the day)
echo Stop:    taskkill /F /IM python.exe  (kills the site too)
endlocal
