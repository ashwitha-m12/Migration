@echo off
setlocal
cd /d "%~dp0"
title Cloud Account Migration

rem ---- 1. find a suitable Python (3.12 preferred; 3.15+ is too new for some libraries)
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY (py -3.13 --version >nul 2>&1 && set "PY=py -3.13")
if not defined PY (py -3.11 --version >nul 2>&1 && set "PY=py -3.11")
if not defined PY (python --version >nul 2>&1 && set "PY=python")
if not defined PY (
    echo.
    echo  Python is not installed.
    echo  Install Python 3.12 from python.org - tick "Add python.exe to PATH" - then run Start.bat again.
    start "" https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

rem ---- 2. first run: create a private environment for the tool
if not exist ".venv\Scripts\python.exe" (
    echo  First run - setting up, this takes 2-5 minutes ...
    %PY% -m venv .venv || goto :fail
)

rem ---- 3. install / update libraries only when requirements.txt changed
fc /b requirements.txt .venv\requirements.installed >nul 2>&1
if errorlevel 1 (
    echo  Installing libraries ...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail
    copy /y requirements.txt .venv\requirements.installed >nul
)

rem ---- 4. skip Streamlit's one-time "enter your email" question
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    mkdir "%USERPROFILE%\.streamlit" >nul 2>&1
    > "%USERPROFILE%\.streamlit\credentials.toml" echo [general]
    >> "%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)

rem ---- 5. open the app in the browser
echo.
echo  Opening the app in your browser. Keep this window open while you use it; close it to stop.
".venv\Scripts\python.exe" -m streamlit run app.py --browser.gatherUsageStats false --server.headless false
goto :eof

:fail
echo.
echo  Setup failed - scroll up for the error. If it mentions building a wheel,
echo  install Python 3.12 from python.org and run Start.bat again.
pause
exit /b 1
