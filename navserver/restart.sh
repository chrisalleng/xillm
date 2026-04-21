#!/bin/bash
pkill -f "python server.py" 2>/dev/null
sleep 0.3
cd "$(dirname "$0")"
PYTHONPATH=recast_wrapper/build nohup /home/chris/workspace/xillm/.venv/bin/python server.py > /tmp/navserver.log 2>&1 &
echo "Started PID: $!"
