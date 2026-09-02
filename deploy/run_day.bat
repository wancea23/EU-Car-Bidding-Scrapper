@echo off
REM One auction day on a Windows host, e.g. the NAS. The Linux/Docker
REM equivalent is deploy/run_day.sh -- this exists because the NAS runs
REM Windows, which is also the exact environment the watcher is proven on.
REM
REM Install once on the host:
REM   pip install playwright && playwright install chromium
REM   setx GEMINI_API_KEY "..."   (machine scope: setx /M)
REM Schedule once, as SYSTEM so it needs nobody logged in:
REM   schtasks /create /tn alcopa-watch /sc daily /st 04:30 /ru SYSTEM ^
REM            /tr "\"C:\path\to\EU-Car-Auctions\deploy\run_day.bat\""
REM
REM No /wake flag anywhere: the host is always on, so nothing is ever woken.

cd /d "%~dp0.."
set ALCOPA_BROWSER=playwright
if not exist data mkdir data
for /f %%d in ('powershell -NoProfile -Command "(Get-Date -Format yyyy-MM-dd)"') do set DAY=%%d
set LOG=data\run_%DAY%.log

REM 1. Rebuild the watch list FIRST, every day, from the sale pages. A list
REM    carried over from yesterday makes the watcher print "nothing closing
REM    inside the horizon" and exit 0 -- a silent no-op that reads as healthy.
python -u alcopa_scrape.py sales --horizon 1209600 --out data\watchlist.json >> "%LOG%" 2>&1

REM 2. Sit on every sale closing in the next 24h. One process for the whole
REM    day: sales minutes apart must share a timeline, or the second sale's
REM    pre-close burst is slept through while the first one's is finishing.
python -u alcopa_scrape.py watch --lots data\watchlist.json ^
       --out data\prices-%DAY%.jsonl --horizon 86400 --workers 24 >> "%LOG%" 2>&1
