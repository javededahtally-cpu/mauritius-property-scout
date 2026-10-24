@echo off
REM Optional (Option B): run one scan on this PC and open the app.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b)
python -m pip install --quiet -r requirements.txt
python scout.py
start "" http://localhost:8765/
python -m http.server 8765 -d docs
