@echo off
cd /d "%~dp0"
echo Deleting scheduled task WorkBuddy-AutoClaim ...
"C:\Users\24508\.workbuddy\binaries\python\envs\default\Scripts\python.exe" "%~dp0install_task.py" --delete
echo.
pause
