@echo off
REM ===============================================================
REM  Legacy debug launcher. The platform entry is start_platform.bat.
REM  Kept as a wrapper so existing muscle memory still works.
REM  Usage:  start_server.bat          -> http://127.0.0.1:8000
REM          start_server.bat 8001     -> custom port
REM  NOTE: keep this file ASCII-only (cmd.exe uses OEM codepage).
REM ===============================================================
call "%~dp0start_platform.bat" %*
