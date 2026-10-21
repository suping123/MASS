@echo off
REM ===============================================================
REM  Stop local PostgreSQL (portable build)
REM  NOTE: keep this file ASCII-only (English comments/output).
REM ===============================================================
setlocal
set PGROOT=C:\Users\dell\WorkBuddy\2026-10-12-12-25-31\pg_install\pgsql
set PGDATA=C:\Users\dell\WorkBuddy\2026-10-12-12-25-31\pgdata

if not exist "%PGROOT%\bin\pg_ctl.exe" (
  echo [ERROR] PostgreSQL not found at: %PGROOT%
  pause
  exit /b 1
)

echo Stopping PostgreSQL ...
"%PGROOT%\bin\pg_ctl.exe" -D "%PGDATA%" stop -m fast

if %errorlevel%==0 goto ok
echo [WARN] Stop failed, or server was not running.
goto end

:ok
echo [OK] PostgreSQL stopped.

:end
endlocal
