#!/bin/bash
# Deploy all agent addons + restart agent_core + reload them.
ASHITA=/home/chris/Faugus/xillm/drive_c/Ashita-v4beta
# Shared IPC root (was config/addons/nav/ until 2026-04-30, when state
# was factored out from under the nav addon's directory).
IPC=$ASHITA/config/xillm

# nav addon (IPC under config/xillm/)
mkdir -p $ASHITA/addons/nav $IPC/instances $IPC/dropoffs
cp /home/chris/workspace/xillm/nav/nav.lua       $ASHITA/addons/nav/
cp /home/chris/workspace/xillm/nav/entities.lua  $ASHITA/addons/nav/
cp /home/chris/workspace/xillm/nav/data/instances/*.json $IPC/instances/ 2>/dev/null
cp /home/chris/workspace/xillm/nav/data/dropoffs/*.json  $IPC/dropoffs/  2>/dev/null

# combat addon (publishes to config/xillm/state/<char>/)
mkdir -p $ASHITA/addons/combat
cp /home/chris/workspace/xillm/combat/combat.lua $ASHITA/addons/combat/

# chat addon (Phase 5): rolling chat buffer + chat_received events
mkdir -p $ASHITA/addons/chat
cp /home/chris/workspace/xillm/chat/chat.lua $ASHITA/addons/chat/

# inventory addon (Phase 6): per-container item snapshot + equipped gear
mkdir -p $ASHITA/addons/inventory
cp /home/chris/workspace/xillm/inventory/inventory.lua $ASHITA/addons/inventory/

# goals addon: in-game UI overlay showing the agent's goal tree, the
# active leaf, and recent decision events. Read-only status panel,
# shift+drag to reposition.
mkdir -p $ASHITA/addons/goals
cp /home/chris/workspace/xillm/goals/goals.lua $ASHITA/addons/goals/

# interact addon: bridges agent_core to NPC dialog / vendor / trade
# menus. Publishes state/<char>/menu.json, drains commands/<char>/
# interact.json. Phase A scaffold; advance_text works via stepdialog
# pattern (TkEventMsg2 key injection), select_option / buy / sell
# stubbed until packet structures are verified in-game.
mkdir -p $ASHITA/addons/interact
cp /home/chris/workspace/xillm/interact/interact.lua $ASHITA/addons/interact/

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
echo '/addon reload cmdrelay' > $IPC/cmd_inbox.txt
sleep 2
{
    echo '/addon reload nav'
    echo '/addon load combat'
    echo '/addon reload combat'
    echo '/addon load chat'
    echo '/addon reload chat'
    echo '/addon load inventory'
    echo '/addon reload inventory'
    echo '/addon load goals'
    echo '/addon reload goals'
    echo '/addon load interact'
    echo '/addon reload interact'
} > $IPC/cmd_inbox.txt
echo "Deployed and reloaded."
