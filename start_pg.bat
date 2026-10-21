@echo off
REM ===============================================================
REM  Start local PostgreSQL 16 + pgvector (portable build)
REM  data dir : ..\pgdata      database: sca    user: sca (trust)
REM  NOTE: keep this file ASCII-only (English comments/output),
REM        otherwise cmd decodes UTF-8 as GBK and breaks the lines.
REM ===============================================================
setlocal
set PGROOT=C:\Users\dell\WorkBuddy\2026-10-12-12-25-31\pg_install\pgsql
set PGDATA=C:\Users\dell\WorkBuddy\2026-10-12-12-25-31\pgdata
set PGLOG=%PGDATA%\logfile.txt

if not exist "%PGROOT%\bin\pg_ctl.exe" (
  echo [ERROR] PostgreSQL not found at: %PGROOT%
  echo         Deploy it first, or ask the assistant to redeploy.
  pause
  exit /b 1
)

if not exist "%PGDATA%\PG_VERSION" (
  echo [ERROR] Data dir not initialized: %PGDATA%
  pause
  exit /b 1
)

rem  Idempotent guard: if a server already accepts (or is starting) connections
rem  on 5432, skip the start -- otherwise pg_ctl warns "another server might
rem  be running" and blocks up to 60s, which looks like the launcher hung.
"%PGROOT%\bin\pg_isready.exe" -h 127.0.0.1 -p 5432 -q >nul 2>&1
if not errorlevel 2 (
  echo [OK] PostgreSQL already running/starting on port 5432 - skip start.
  goto end
)

echo Starting PostgreSQL on port 5432 (db: sca) ...
"%PGROOT%\bin\pg_ctl.exe" -D "%PGDATA%" -l "%PGLOG%" -o "-p 5432" start

if %errorlevel%==0 goto ok
echo.
echo [WARN] pg_ctl returned non-zero ^(it may already be running^).
echo        Check the log: %PGLOG%
goto end

:ok
echo.
echo [OK] PostgreSQL started.
echo      Connect: "%PGROOT%\bin\psql.exe" -U sca -h 127.0.0.1 -p 5432 -d sca

:end
endlocal
