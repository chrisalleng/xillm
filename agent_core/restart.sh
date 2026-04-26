#!/bin/bash
pkill -f "agent_core.main" 2>/dev/null
pkill -f "python.*agent_core.*main.py" 2>/dev/null
pkill -f "python server.py" 2>/dev/null  # legacy navserver name
sleep 0.3
# Run as a module from the repo root so the package's relative imports
# (`from . import config as _config`) resolve correctly. PYTHONPATH adds
# the recast_wrapper/build dir so `import navmesh` keeps working.
cd "$(dirname "$0")/.."
PYTHONPATH="agent_core/recast_wrapper/build" nohup /home/chris/workspace/xillm/.venv/bin/python -m agent_core.main > /tmp/agent_core.log 2>&1 &
echo "Started PID: $!"
