#!/bin/bash
# Deploy all agent addons + restart agent_core + reload them.
ASHITA=/home/chris/Faugus/xillm/drive_c/Ashita-v4beta

# nav addon (legacy IPC under config/addons/nav/)
mkdir -p $ASHITA/addons/nav $ASHITA/config/addons/nav/instances $ASHITA/config/addons/nav/dropoffs
cp /home/chris/workspace/xillm/nav/nav.lua       $ASHITA/addons/nav/
cp /home/chris/workspace/xillm/nav/entities.lua  $ASHITA/addons/nav/
cp /home/chris/workspace/xillm/nav/data/instances/*.json $ASHITA/config/addons/nav/instances/ 2>/dev/null
cp /home/chris/workspace/xillm/nav/data/dropoffs/*.json  $ASHITA/config/addons/nav/dropoffs/  2>/dev/null

# combat addon (publishes to config/addons/nav/state/<char>/ for now;
# moves to config/addons/agent/ when Phase 1b unifies the IPC layout)
mkdir -p $ASHITA/addons/combat
cp /home/chris/workspace/xillm/combat/combat.lua $ASHITA/addons/combat/

# cmdrelay (relays cmd_inbox.txt lines as /commands inside Ashita).
# Kept under our deployment so we can fix bugs in it without manual
# copies. Reloaded BEFORE issuing any other commands below so the
# in-game version matches the cmd_inbox content shape.
mkdir -p $ASHITA/addons/cmdrelay
cp /home/chris/workspace/xillm/cmdrelay/cmdrelay.lua $ASHITA/addons/cmdrelay/

"$(dirname "$0")/restart.sh"
# cmdrelay reads cmd_inbox.txt and reissues each line as a /command.
# v1.0 (older) consumed only the first line per poll; v1.1+ consumes
# every line. We bootstrap by reloading cmdrelay alone first, sleeping
# long enough for the next poll (30 frames @ 60fps = 0.5s; we use 2s
# to absorb timing jitter), then queueing the rest in one shot which
# v1.1 will drain in a single poll.
echo '/addon reload cmdrelay' > $ASHITA/config/addons/nav/cmd_inbox.txt
sleep 2
{
    echo '/addon reload nav'
    echo '/addon load combat'
    echo '/addon reload combat'
} > $ASHITA/config/addons/nav/cmd_inbox.txt
echo "Deployed and reloaded."
