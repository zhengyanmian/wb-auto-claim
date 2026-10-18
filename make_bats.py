# -*- coding: utf-8 -*-
"""
重新生成 .bat：
  1) 纯 ASCII 内容 —— 不依赖任何代码页，中文 Windows / UTF-8 Beta 模式都不会乱码
  2) CRLF 换行 —— bat 必须是 CRLF
  3) 用 goto 代替 if(...)else(...) 块 —— 避开块内引号/括号解析坑
中文提示交给 main.py 自己打印（Python 走 WriteConsoleW，任何代码页都能正常显示中文）。
"""
import os

BASE = r"C:\Users\24508\WorkBuddy\2026-09-18-17-08-41\wb-auto-claim"
PY = r"C:\Users\24508\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
PYW = r"C:\Users\24508\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"

run_now = r"""@echo off
cd /d "%~dp0"
echo ============================================
echo   WorkBuddy Auto Claim - run now
echo ============================================
echo.
"__PY__" "%~dp0main.py"
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
"""

install_task = r"""@echo off
cd /d "%~dp0"
echo ============================================
echo   Register task: WorkBuddy-AutoClaim
echo   Daily 09:00 / 12:30 / 16:30
echo   + top-up right after unlock (needs admin)
echo ============================================
echo.
echo Uses the Task Scheduler COM API (schtasks.exe is not needed).
echo.
"__PY__" "%~dp0install_task.py" %*
echo.
pause
"""

remove_task = r"""@echo off
cd /d "%~dp0"
echo Deleting scheduled task WorkBuddy-AutoClaim ...
"__PY__" "%~dp0install_task.py" --delete
echo.
pause
"""

status_bat = r"""@echo off
cd /d "%~dp0"
"__PY__" "%~dp0status.py" %*
echo.
pause
"""

files = {
    "run_now.bat": run_now.replace("__PY__", PY),
    "install_task.bat": install_task.replace("__PY__", PY),
    "remove_task.bat": remove_task.replace("__PY__", PY),
    "status.bat": status_bat.replace("__PY__", PY),
}

for name, text in files.items():
    assert all(ord(c) < 128 for c in text), name + " 含有非 ASCII 字符"
    t = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(os.path.join(BASE, name), "wb") as f:
        f.write(t.encode("ascii"))
    print(name, os.path.getsize(os.path.join(BASE, name)), "bytes -> pure ASCII + CRLF")
