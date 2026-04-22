#!/bin/bash
# Deploy mapper + restart navserver + reload addon
cp /home/chris/workspace/xillm/mapper/mapper.lua /home/chris/Faugus/xillm/drive_c/Ashita-v4beta/addons/mapper/
cp /home/chris/workspace/xillm/mapper/entities.lua /home/chris/Faugus/xillm/drive_c/Ashita-v4beta/addons/mapper/
"$(dirname "$0")/restart.sh"
echo '/addon reload mapper' > /home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/addons/mapper/cmd_inbox.txt
echo "Deployed and reloaded."
