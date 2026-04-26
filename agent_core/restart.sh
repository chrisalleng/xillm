#!/bin/bash
# Kill any lingering server process. The cmdline can be one of:
#   python -m agent_core.main          (current, restart.sh form)
#   python /repo/agent_core/main.py    (legacy direct invocation)
#   python main.py                     (legacy cwd=agent_core invocation)
#   python /repo/navserver/server.py   (pre-rename navserver)
# Match all of them so a stale leftover doesn't race the new process
# for nav_path.json writes.
pkill -f "agent_core.main"      2>/dev/null
pkill -f "agent_core/main.py"   2>/dev/null
pkill -f "python.* main\.py$"   2>/dev/null
pkill -f "navserver/server.py"  2>/dev/null
pkill -f "python server.py"     2>/dev/null
sleep 0.3
# Run as a module from the repo root so the package's relative imports
# (`from . import config as _config`) resolve correctly. PYTHONPATH adds
# the recast_wrapper/build dir so `import navmesh` keeps working.
cd "$(dirname "$0")/.."
PYTHONPATH="agent_core/recast_wrapper/build" nohup /home/chris/workspace/xillm/.venv/bin/python -m agent_core.main > /tmp/agent_core.log 2>&1 &
echo "Started PID: $!"
