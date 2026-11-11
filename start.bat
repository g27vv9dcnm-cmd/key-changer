@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem キー変更アプリを起動します (Windows)
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
echo Python が見つかりません。README の手順で Python をインストールしてください。
:end
echo.
pause
