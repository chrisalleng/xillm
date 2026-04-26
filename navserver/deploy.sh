#!/bin/bash
# Deploy nav addon + restart navserver + reload addon
cp /home/chris/workspace/xillm/nav/nav.lua /home/chris/Faugus/xillm/drive_c/Ashita-v4beta/addons/nav/
cp /home/chris/workspace/xillm/nav/entities.lua /home/chris/Faugus/xillm/drive_c/Ashita-v4beta/addons/nav/
mkdir -p /home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/addons/nav/instances/
cp /home/chris/workspace/xillm/nav/data/instances/*.json /home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/addons/nav/instances/ 2>/dev/null
mkdir -p /home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/addons/nav/dropoffs/
cp /home/chris/workspace/xillm/nav/data/dropoffs/*.json /home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/addons/nav/dropoffs/ 2>/dev/null
"$(dirname "$0")/restart.sh"
echo '/addon reload nav' > /home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/addons/nav/cmd_inbox.txt
echo "Deployed and reloaded."
