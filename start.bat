@echo off
rem Starts the Key Changer app on Windows.
rem Keep this file ASCII-only. With "chcp 65001" and non-ASCII text in the
rem same batch file, cmd.exe loses its place in the file and runs the middle
rem of a later line as a command ("'hon' is not recognized ...").
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 launcher.py %*
  goto end
)
where python >nul 2>nul
if %errorlevel%==0 (
  python launcher.py %*
  goto end
)
echo Python was not found. Install Python 3.10 or later from
echo https://www.python.org/downloads/ and run start.bat again. See README.md.
if exist "docs\python-not-found.txt" powershell -NoProfile -Command "Get-Content -Encoding UTF8 -LiteralPath 'docs\python-not-found.txt'" 2>nul
:end
echo.
pause
