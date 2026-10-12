@echo off
cd /d %~dp0
py -m pip install -r requirements.txt
py server.py --host 0.0.0.0 --mc-port 25565 --lostminer-port 25570 --world world\world.json --slice-z 0 --slice-depth 1
pause
