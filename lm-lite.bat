@echo off
chcp 65001 >nul
title lm-lite
cd /d "%~dp0"

rem ---- stop 参数：停止正在运行的服务 ----
if /i "%~1"=="stop" (
  echo Stopping lm-lite ...
  powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 9000 -State Listen -EA 0 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA 0 }"
  powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8081 -State Listen -EA 0 | ForEach-Object { $p = Get-Process -Id $_.OwningProcess -EA 0; if($p -and $p.ProcessName -like 'llama*'){ Stop-Process -Id $p.Id -Force -EA 0 } }"
  echo Stopped.
  timeout /t 2 >nul
  exit /b 0
)

rem ---- 定位 Python：优先 PATH，其次 py 启动器，最后常见安装位置 ----
set PY=
where python >nul 2>&1 && set PY=python
if not defined PY ( where py >nul 2>&1 && set PY=py -3 )
if not defined PY (
  for %%D in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
  ) do ( if exist %%D set PY=%%D )
)
if not defined PY (
  echo [ERROR] Python 3.10+ not found in PATH.
  echo Install from https://www.python.org/downloads/ and re-run.
  pause
  exit /b 1
)

set PORT=9000
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo lm-lite is already running. Opening browser...
  start "" "http://127.0.0.1:%PORT%"
  timeout /t 2 >nul
  exit /b 0
)

echo ========================================
echo   lm-lite
echo ========================================
%PY% -u launcher.py
pause
