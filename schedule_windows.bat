@echo off
REM Optional (Option B): scan every hour on this PC while it is switched on.
cd /d "%~dp0"
schtasks /Create /F /SC HOURLY /MO 1 /TN "Property Scout scan" /TR "cmd /c cd /d \"%~dp0\" && python scout.py >> scout.log 2>&1"
echo Done. The scan now runs every hour while this PC is on. To stop it: schtasks /Delete /TN "Property Scout scan" /F
pause
