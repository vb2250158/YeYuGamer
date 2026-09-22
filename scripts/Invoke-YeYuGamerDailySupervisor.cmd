@echo off
rem Five-minute heartbeat that keeps the current game day moving toward
rem completion.  See scripts\Invoke-YeYuGamerDailySupervisor.py for the contract.
"C:\Users\Admin\AppData\Local\Programs\YeYuGamer\.venv\Scripts\python.exe" -I -B -X utf8 "C:\Projects\YeYuGamer\scripts\Invoke-YeYuGamerDailySupervisor.py" >> "C:\Projects\YeYuGamer\.cache\logs\daily-supervisor\stdout.log" 2>&1
