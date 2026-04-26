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

"$(dirname "$0")/restart.sh"
# cmdrelay reads cmd_inbox.txt and reissues each line as an in-game
# command. Reload both addons; future addons append a line here.
{
    echo '/addon reload nav'
    echo '/addon load combat'
    echo '/addon reload combat'
} > $ASHITA/config/addons/nav/cmd_inbox.txt
echo "Deployed and reloaded."
