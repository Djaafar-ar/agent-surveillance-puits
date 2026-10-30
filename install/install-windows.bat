@echo off
setlocal
rem ============================================================
rem  Installation of the well surveillance agent on Windows
rem  Right-click > Run as administrator. Works WITHOUT internet
rem  when the "wheels" folder is present (offline package).
rem ============================================================
cd /d "%~dp0.."
net session >nul 2>&1
if errorlevel 1 (
  echo Please run this file as administrator ^(right-click ^> Run as administrator^).
  pause & exit /b 1
)

echo [1/5] Python...
set PY=
where py >nul 2>&1 && py -3 --version >nul 2>&1 && set PY=py -3
if not defined PY ( python --version >nul 2>&1 && set PY=python )
if not defined PY (
  for %%f in (install\python-3*-amd64.exe) do (
    echo     Installing %%~nxf silently...
    "%%f" /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
    set "PY=C:\Program Files\Python3\python.exe"
  )
)
if not defined PY (
  echo Python 3.10 to 3.13 is required. Ask IT for the installer "python-3.12.x-amd64.exe",
  echo put it in the "install" folder and run this file again. ^(https://www.python.org/downloads/windows/^)
  pause & exit /b 1
)
if "%PY%"=="C:\Program Files\Python3\python.exe" (
  for /d %%d in ("C:\Program Files\Python3*") do if exist "%%d\python.exe" set "PY=%%d\python.exe"
)
"%PY:"=%" --version 2>nul || %PY% --version || goto :err

echo [2/5] Environment and libraries...
if not exist .venv %PY% -m venv .venv || goto :err
if exist wheels (
  .venv\Scripts\python -m pip install --no-index --find-links wheels -r requirements.txt || goto :err
) else (
  .venv\Scripts\python -m pip install -r requirements.txt || goto :err
)

echo [3/5] Folders and permissions...
if not exist data mkdir data
if exist config\secrets.env icacls config\secrets.env /inheritance:r /grant:r "SYSTEM:F" "Administrators:F" >nul

echo [4/5] Service ^(starts with Windows, restarts if it stops^)...
schtasks /create /tn "AgentSurveillancePuits" /tr "\"%CD%\install\start-agent.bat\"" /sc onstart /ru SYSTEM /rl HIGHEST /f >nul || goto :err
schtasks /run /tn "AgentSurveillancePuits" >nul

echo [5/5] Done.
echo.
if not exist config\config.yaml (
  echo The setup wizard opens in the browser: http://127.0.0.1:8080/setup
  timeout /t 8 /nobreak >nul
  start "" http://127.0.0.1:8080/setup
) else (
  echo The agent is running. Dashboard: http://127.0.0.1:8080
  start "" http://127.0.0.1:8080
)
echo Check that it runs:  .venv\Scripts\python -m agent status --config config\config.yaml
pause
exit /b 0
:err
echo Installation failed, see the messages above.
pause
exit /b 1
