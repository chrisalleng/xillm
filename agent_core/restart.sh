#!/bin/bash
pkill -f "python.*agent_core.*main.py" 2>/dev/null
pkill -f "python server.py" 2>/dev/null  # legacy navserver name
sleep 0.3
cd "$(dirname "$0")"
PYTHONPATH=recast_wrapper/build nohup /home/chris/workspace/xillm/.venv/bin/python main.py > /tmp/agent_core.log 2>&1 &
echo "Started PID: $!"
