@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.12 install.py
if errorlevel 1 (
  echo Installation failed. Install Python 3.12 and check README.md or README_RU.md.
)
pause
