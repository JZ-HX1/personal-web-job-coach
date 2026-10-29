@echo off
setlocal
cd /d "%~dp0"
python -B -u -X utf8 "%~dp0share_host.py" %*
set "share_exit=%ERRORLEVEL%"
if not "%share_exit%"=="0" pause
exit /b %share_exit%
