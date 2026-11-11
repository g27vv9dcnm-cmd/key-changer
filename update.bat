@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem YouTube が読み込めなくなったときに yt-dlp を更新します
call start.bat --update
