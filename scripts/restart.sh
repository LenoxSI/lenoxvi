#!/bin/bash
# lenoxvi bot restart (run on appliance-host).
# Pattern 'bin/python bot.py' avoids matching this script or ssh cmdlines.
# LD_LIBRARY_PATH: CTranslate2 needs libcublas.so.12/cudnn from the pip
# nvidia-* wheels — the NVIDIA driver alone doesn't provide them.
# -9: a graceful SIGTERM can hang forever when a live WebRTC session is
# attached (uvicorn waits for the connection to close — observed a zombie
# bot surviving a restart this way, 2026-09-15).
pkill -9 -f 'bin/python bot.py' 2>/dev/null
sleep 1
cd ~/projects/lenoxvi
NV=~/venvs/lenoxvi/lib/python3.14/site-packages/nvidia
export LD_LIBRARY_PATH="$NV/cublas/lib:$NV/cudnn/lib:$NV/cuda_nvrtc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
nohup ~/venvs/lenoxvi/bin/python bot.py --host 0.0.0.0 --port 7860 > logs/bot.log 2>&1 &
echo RESTARTED-PID=$!