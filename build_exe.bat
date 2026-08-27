@echo off
REM Builds DiscordLibraryPanel.exe (single file). Run from this folder.
cd /d "%~dp0"
python -m pip install --quiet --upgrade pyinstaller || (echo pip failed & pause & exit /b)
python -m PyInstaller --noconfirm --onefile --name DiscordLibraryPanel ^
  --add-data "ui.html;." ^
  --hidden-import library --hidden-import optimizer ^
  --hidden-import discord_api --hidden-import keywatch ^
  panel.py
echo.
echo Built: %~dp0dist\DiscordLibraryPanel.exe
pause
