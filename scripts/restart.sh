#!/bin/bash
# lenoxvi bot restart (run on appliance-host).
# Pattern 'bin/python bot.py' avoids matching this script or ssh cmdlines.
pkill -f 'bin/python bot.py' 2>/dev/null
sleep 1
cd ~/projects/lenoxvi
nohup ~/venvs/lenoxvi/bin/python bot.py --host 0.0.0.0 --port 7860 > logs/bot.log 2>&1 &
echo RESTARTED-PID=$!