@echo off
rem Loop Studio: starts the server, which opens the browser once it's up. ComfyUI must be running.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Making Loop Studio's Python environment...
  py -3 -m venv .venv 2>nul || python -m venv .venv 2>nul
)
if not exist .venv\Scripts\python.exe goto nopython
.venv\Scripts\python.exe -c "import sys; sys.exit(sys.version_info < (3, 12))" 2>nul || goto oldpython
rem packages missing (first run, or an install that failed last time): install them
.venv\Scripts\python.exe -c "import aiohttp, winpty" 2>nul || (
  echo Installing Loop Studio's packages...
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto failed
)
.venv\Scripts\python.exe server.py --open
if errorlevel 1 pause
exit /b

:nopython
echo Loop Studio needs Python 3.12 or newer. Install it from https://www.python.org/downloads/ and run start.bat again.
pause
exit /b 1

:oldpython
echo Loop Studio needs Python 3.12 or newer, and its .venv folder was made with an older one. Install it from
echo https://www.python.org/downloads/, delete the .venv folder here and run start.bat again.
pause
exit /b 1

:failed
echo Installing the packages failed (see above). Check the internet connection and run start.bat again.
pause
exit /b 1
