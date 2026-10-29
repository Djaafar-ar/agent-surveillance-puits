@echo off
rem DEMONSTRATION ONLY: starts the field simulator and the agent connected to it.
cd /d "%~dp0.."
if not exist .venv (py -3 -m venv .venv & .venv\Scripts\python -m pip install -r requirements.txt)
.venv\Scripts\python -m simulator.history --config config\config.sim.yaml --days 365
start "Simulateur de champ" .venv\Scripts\python -m simulator.sim_server --speed 30 --scenario demo --night
timeout /t 3 /nobreak >nul
start "Agent (demo)" .venv\Scripts\python -m agent run --config config\config.sim.yaml
timeout /t 4 /nobreak >nul
start http://127.0.0.1:8090
start http://127.0.0.1:8080
