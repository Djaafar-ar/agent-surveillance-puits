@echo off
rem Runs the agent and restarts it if it ever stops. Started at boot by the scheduled task.
cd /d "%~dp0.."
if not exist data mkdir data
:loop
.venv\Scripts\python -m agent run --config config\config.yaml >> data\agent.log 2>&1
echo %date% %time% agent stopped, restarting in 30 s >> data\agent.log
timeout /t 30 /nobreak >nul
goto loop
