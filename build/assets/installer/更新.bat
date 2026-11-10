@echo off
chcp 65001 >nul
setlocal
title 鲸鲸bot 更新
cd /d "%~dp0"

if not exist "runtime\python\python.exe" (
  echo [X] 找不到 runtime\python\python.exe，这个包不完整。
  pause
  exit /b 1
)
mode con: cols=100 lines=36 >nul 2>&1

echo ============================================================
echo   鲸鲸bot 更新
echo   会替掉程序本身，保留你的配置、聊天记录、人设和唱过的歌。
echo ============================================================
echo.
echo 更新前先把机器人关掉最稳（关掉 NapCat 窗口 + 任务管理器结束 python.exe）。
echo 没关也能更，但要重启一次才生效。
echo.
pause

"runtime\python\python.exe" "tools\update.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo 更新完成。记得重新双击「启动机器人.bat」。
) else (
  echo 更新没做成，上面写了原因。
)
echo.
pause
exit /b %RC%
