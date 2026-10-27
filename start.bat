@echo off
rem Runs the bot and restarts it automatically if it stops.
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
)
if not exist logs mkdir logs
:loop
.venv\Scripts\python main.py >> logs\bot.log 2>&1
echo Bot stopped, restarting in 10 seconds... >> logs\bot.log
timeout /t 10 /nobreak > nul
goto loop
