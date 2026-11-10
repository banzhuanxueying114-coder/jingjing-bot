@echo off
chcp 65001 >nul
rem 面板窗口开在后台，没有命令行。pythonw 不会留黑框。
cd /d "%~dp0"
if not exist "runtime\python\pythonw.exe" (
  echo [X] 找不到 runtime\python\pythonw.exe，这个包不完整。
  pause
  exit /b 1
)
start "" "runtime\python\pythonw.exe" "面板.py"
exit /b 0
