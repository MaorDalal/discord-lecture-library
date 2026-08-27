@echo off
title Discord Library Panel
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found - install it from python.org & pause & exit /b)
REM absolute path so the Hebrew folder name survives regardless of shell
python "%~dp0panel.py"
pause
