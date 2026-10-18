@echo off
cd /d "%~dp0"
echo ============================================
echo   WorkBuddy Auto Claim - run now
echo ============================================
echo.
"C:\Users\24508\.workbuddy\binaries\python\envs\default\Scripts\python.exe" "%~dp0main.py"
set RC=%ERRORLEVEL%
echo.
echo ExitCode = %RC%
echo   0 = claimed OK / already claimed today
echo   2 = points card not found
echo   3 = WorkBuddy window not found and cannot start
echo   4 = clicked but state unknown
echo   6 = screen is locked (will retry after you unlock)
if not "%RC%"=="0" echo See the captures\ and logs\ folders for details.
echo.
pause
