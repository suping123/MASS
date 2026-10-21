@echo off
REM ===============================================================
REM  Agent launcher: 4 resident agents (dispatch/carrier/exception/shipper)
REM
REM  Usage:  start_agents.bat            -> start all four, keep watching
REM          start_agents.bat --status   -> show heartbeats and exit
REM          start_agents.bat --stop     -> stop all agents (incl. orphans)
REM          start_agents.bat --restart  -> restart a dead agent
REM
REM  Each agent is a separate OS process with its own log file:
REM      logs\agent_dispatch.log
REM      logs\agent_carrier.log
REM      logs\agent_exception.log
REM      logs\agent_shipper.log
REM
REM  NOTE: keep this file ASCII-only + CRLF line endings. cmd.exe decodes
REM        batch files with the OEM codepage (GBK on zh-CN); non-ASCII
REM        comments get tokenized as special chars and the window opens
REM        and closes with no output.
REM ===============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto novenv

REM --status / --stop need only the database, not the ports
if /I "%~1"=="--status" goto status
if /I "%~1"=="--stop" goto stopagents

if not exist "logs" mkdir "logs"

echo [1/3] Checking MySQL on 127.0.0.1:3306 ...
netstat -ano | findstr ":3306" | findstr LISTENING >nul
if errorlevel 1 goto nomysql

echo [2/3] Checking Redis on 127.0.0.1:6379 ...
netstat -ano | findstr ":6379" | findstr LISTENING >nul
if errorlevel 1 goto noredis
echo       Redis is up: message doorbells are active.

echo [3/3] Starting agents ...
if exist "sql\platform_v7.sql" (
  echo       Reminder: if this is a first run, apply sql\platform_v7.sql
  echo       ^(message lease columns + agent heartbeat^).
)
echo.

.venv\Scripts\python.exe -m sca.platform.agents.launcher %*
echo.
echo [agents] launcher exited.
pause
exit /b 0

:status
.venv\Scripts\python.exe -m sca.platform.agents.launcher --status
pause
exit /b 0

:stopagents
.venv\Scripts\python.exe -m sca.platform.agents.launcher --stop
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
echo         Agents are NOT started: their inbox lives in MySQL.
echo.
pause
exit /b 1

:noredis
echo.
echo [ERROR] Redis is NOT listening on 127.0.0.1:6379.
echo         Agents can run without Redis (pure polling), but the
echo         platform itself needs Redis for login, so start it first.
echo.
pause
exit /b 1
