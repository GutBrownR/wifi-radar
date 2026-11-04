@echo off
cd /d "%~dp0"
start "" http://radar.localhost:8765
python server.py
pause
