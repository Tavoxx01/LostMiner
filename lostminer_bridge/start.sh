#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
exec python3 server.py --host 0.0.0.0 --mc-port 25565 --lostminer-port 25570 --world world/world.json --slice-z 0 --slice-depth 1
