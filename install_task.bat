@echo off
cd /d "%~dp0"
echo ============================================
echo   Register task: WorkBuddy-AutoClaim
echo   Daily 09:00 / 12:30 / 16:30
echo   + top-up right after unlock (needs admin)
echo ============================================
echo.
echo Uses the Task Scheduler COM API (schtasks.exe is not needed).
echo.
"C:\Users\24508\.workbuddy\binaries\python\envs\default\Scripts\python.exe" "%~dp0install_task.py" %*
echo.
pause
