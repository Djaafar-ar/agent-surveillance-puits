@echo off
setlocal
rem ============================================================
rem  Installation of the well surveillance agent on Windows
rem  Run as administrator (right-click > Run as administrator)
rem ============================================================
cd /d "%~dp0.."
net session >nul 2>&1
if errorlevel 1 (
  echo Please run this file as administrator.
  pause & exit /b 1
)
where py >nul 2>&1 && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.10 or newer is required: https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  pause & exit /b 1
)
echo [1/4] Creating the Python environment...
if not exist .venv %PY% -m venv .venv || goto :err
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -r requirements.txt || goto :err

echo [2/4] Configuration files...
if not exist config\config.yaml copy config\config.example.yaml config\config.yaml >nul
if not exist config\secrets.env copy config\secrets.env.example config\secrets.env >nul
icacls config\secrets.env /inheritance:r /grant:r "SYSTEM:F" "Administrators:F" >nul

echo [3/4] Service (starts with Windows, restarts if it stops)...
schtasks /create /tn "AgentSurveillancePuits" /tr "\"%CD%\install\start-agent.bat\"" /sc onstart /ru SYSTEM /rl HIGHEST /f >nul || goto :err

echo [4/4] Done.
echo.
echo Next steps:
echo   1. Edit config\config.yaml  (SCADA address, tags, wells, on-call phones, e-mail)
echo   2. Edit config\secrets.env  (passwords)
echo   3. Check the tags:   .venv\Scripts\python -m agent check --config config\config.yaml
echo   4. Test SMS/e-mail:  .venv\Scripts\python -m agent test-sms --config config\config.yaml
echo   5. Start now:        schtasks /run /tn AgentSurveillancePuits
echo   Dashboard: http://127.0.0.1:8080
pause
exit /b 0
:err
echo Installation failed, see the messages above.
pause
exit /b 1
