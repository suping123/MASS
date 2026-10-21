@echo off
REM ===============================================================
REM  Platform launcher: MySQL -> Redis -> (PostgreSQL) -> API
REM  Usage:  start_platform.bat        -> http://127.0.0.1:8000
REM          start_platform.bat 8001   -> custom port
REM  NOTE: keep this file ASCII-only. cmd.exe decodes batch files
REM        with the OEM codepage (GBK on zh-CN); non-ASCII comments
REM        can be tokenized as special chars and abort the script.
REM ===============================================================
setlocal
cd /d "%~dp0"

set PORT=%~1
if "%PORT%"=="" set PORT=8000

if not exist ".venv\Scripts\python.exe" goto novenv

echo [1/4] Checking MySQL on 127.0.0.1:3306 ...
netstat -ano | findstr ":3306" | findstr LISTENING >nul
if errorlevel 1 goto nomysql

echo [2/4] Checking Redis on 127.0.0.1:6379 ...
netstat -ano | findstr ":6379" | findstr LISTENING >nul
if errorlevel 1 goto noredis

echo [3/4] Checking PostgreSQL on 127.0.0.1:5432 (knowledge base) ...
netstat -ano | findstr ":5432" | findstr LISTENING >nul
if errorlevel 1 goto warnpg
echo       PostgreSQL is up: knowledge base uses pgvector retrieval.
goto startapi

:warnpg
echo       [WARN] PostgreSQL is NOT running.
echo              The knowledge base will fall back to keyword search:
echo              exception handling still works, but semantic recall is lost.
echo              Start it with start_pg.bat, then run:
echo                  .venv\Scripts\python.exe scripts\init_knowledge_pg.py
echo.

:startapi
echo [4/4] Starting API server ...
echo       Open http://127.0.0.1:%PORT% in your browser (Ctrl+C to stop).
echo       Login: shipper1 / dispatcher1 / carrier1   password 123456
echo.

.venv\Scripts\python.exe -m uvicorn sca.platform.main:app --host 127.0.0.1 --port %PORT% --loop sca.win_loop:win_selector_loop_factory

pause
exit /b 0

:novenv
echo [ERROR] .venv not found.
echo         Create it first:
echo             python -m venv .venv
echo             .venv\Scripts\pip install -r requirements.txt
pause
exit /b 1

:nomysql
echo.
echo [ERROR] MySQL is NOT listening on 127.0.0.1:3306.
echo         The API is NOT started: every login and order call would fail.
echo.
echo         Start MySQL 8 service first, then verify:
echo             netstat -ano ^| findstr :3306
echo.
pause
exit /b 1

:noredis
echo.
echo [ERROR] Redis is NOT listening on 127.0.0.1:6379.
echo         Verification codes are stored in Redis; without it login
echo         returns HTTP 503 by design (fail-closed, never bypassed).
echo.
echo         Start Redis first, then verify:
echo             netstat -ano ^| findstr :6379
echo.
pause
exit /b 1
