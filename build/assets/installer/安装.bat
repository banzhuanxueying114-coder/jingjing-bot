@echo off
chcp 65001 >nul
setlocal
title 鲸鲸bot 安装向导
cd /d "%~dp0"

if not exist "runtime\python\python.exe" (
  echo [X] 找不到 runtime\python\python.exe
  echo     这个包不完整，重新解压一次，或者重新下载。
  pause
  exit /b 1
)

rem A console this small wraps every Chinese line and looks broken, so widen it.
mode con: cols=100 lines=40 >nul 2>&1

"runtime\python\python.exe" "tools\install.py"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo 安装没有完成，上面写了原因。
) else (
  echo 安装完成。
)
echo.
pause
exit /b %RC%
