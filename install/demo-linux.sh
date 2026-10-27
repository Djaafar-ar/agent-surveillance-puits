#!/bin/sh
# DEMONSTRATION ONLY: field simulator + agent connected to it.
cd "$(dirname "$0")/.."
python3 -m simulator.sim_server --speed 30 --scenario demo --night &
sleep 3
python3 -m agent run --config config/config.sim.yaml
