@echo off
rem YeYuGamer: clear stuck game-client instances so the daily can always launch.
rem Runs from the scheduled task \YeYuGamer\StuckClientHealer every 10 minutes.
rem Exits immediately unless a restart is both needed and safe; see
rem scripts\heartbeat\heal-stuck-clients.py for the full guard list.
cd /d C:\Projects\YeYuGamer
"C:\Users\Admin\AppData\Local\Programs\YeYuGamer\.venv\Scripts\python.exe" "C:\Projects\YeYuGamer\scripts\heartbeat\heal-stuck-clients.py" %*
set RC=%ERRORLEVEL%
rem 0 healthy / 3 queue busy / 4 refused / 5 restarted are all normal outcomes.
rem Only a real error (1) should surface as a failed task run.
if "%RC%"=="1" exit /b 1
exit /b 0
