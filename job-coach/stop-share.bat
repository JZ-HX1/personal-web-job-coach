@echo off
setlocal
cd /d "%~dp0"
python -B -X utf8 "%~dp0share_host.py" --stop
set "share_exit=%ERRORLEVEL%"
pause
exit /b %share_exit%
