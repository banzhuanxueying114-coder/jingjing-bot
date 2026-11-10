@echo off
chcp 65001 >nul
setlocal
title 鲸鲸bot
cd /d "%~dp0"
set "PY=%CD%\runtime\python\python.exe"
set "APPDIR=%CD%\qqbot\AstrBot"
set "NAPDIR=%CD%\qqbot\napcat"

if not exist "%PY%" goto nopy
if not exist "%APPDIR%\main.py" goto nopy
if not exist "%APPDIR%\data\cmd_config.json" (
  echo 这个包还没装过。
  echo 先双击「安装.bat」走一遍安装向导，再来启动。
  pause
  exit /b 1
)

echo ============================================================
echo   鲸鲸bot 启动器
echo   管理面板: http://127.0.0.1:6185
echo ============================================================
echo.

echo [1/2] 启动机器人 ...
start "鲸鲸bot" /min /d "%APPDIR%" "%PY%" main.py

echo       等它就绪 ...
set /a n=0
:wait
set /a n+=1
powershell -NoProfile -Command "try{(Invoke-WebRequest -Uri 'http://127.0.0.1:6185/' -UseBasicParsing -TimeoutSec 2).StatusCode|Out-Null;exit 0}catch{exit 1}" >nul 2>&1
if not errorlevel 1 goto ready
if %n% geq 40 goto slow
ping -n 3 127.0.0.1 >nul
goto wait

:ready
echo       已就绪: http://127.0.0.1:6185
goto napcat

:slow
echo       [!] 起得有点慢，先继续启动 NapCat，稍后自己去浏览器看。

:napcat
echo.
rem 上次登录过的 QQ 号能从配置文件名里认出来，认出就不用再扫码。
set "NAPCAT_QUICK_ACCOUNT="
set "QFILE="
for /f "delims=" %%f in ('dir /b /o-d "%NAPDIR%\config\onebot11_*.json" 2^>nul') do if not defined QFILE set "QFILE=%%~nf"
if defined QFILE set "NAPCAT_QUICK_ACCOUNT=%QFILE:onebot11_=%"
if defined NAPCAT_QUICK_ACCOUNT (
  echo       上次登录过 %NAPCAT_QUICK_ACCOUNT%，走快速登录。
  echo       手机 QQ 弹确认就点「确认」。
) else (
  echo       还没登录过，稍后用手机 QQ 扫码，只有第一次要扫。
)
echo.
tasklist /FI "IMAGENAME eq QQ.exe" 2>nul | find /I "QQ.exe" >nul
if not errorlevel 1 (
  echo [!] QQ 正在运行。NapCat 要自己拉起 QQ，先把 QQ 退干净。
  echo     退出 QQ 后按任意键继续 ...
  pause >nul
)
echo [2/2] 启动 NapCat ...
echo       会弹权限框，点「是」。
echo.
cd /d "%NAPDIR%"
call launcher.bat %*

echo.
echo 这个窗口关了，机器人还在后台跑。
echo   关掉机器人：结束 NapCat 的窗口，再在任务管理器里结束 python.exe
echo   掉登录了：重新双击本文件，一般还是快速登录
pause
exit /b 0

:nopy
echo [X] 找不到运行时或机器人程序，这个包不完整。
echo     重新解压一次，或者先跑「安装.bat」。
pause
exit /b 1
