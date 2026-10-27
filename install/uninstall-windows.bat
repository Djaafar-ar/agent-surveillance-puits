@echo off
schtasks /end /tn "AgentSurveillancePuits" >nul 2>&1
schtasks /delete /tn "AgentSurveillancePuits" /f
taskkill /f /fi "WINDOWTITLE eq agent*" >nul 2>&1
echo Service removed. The data folder (history, logs) was kept.
pause
