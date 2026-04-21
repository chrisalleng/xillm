#!/bin/bash
# Deploy mapper addon files to Ashita addons folder and trigger reload
DEST="/home/chris/Faugus/xillm/drive_c/Ashita-v4beta/addons/mapper"
SRC="/home/chris/workspace/xillm/mapper"
INBOX="/home/chris/Faugus/xillm/drive_c/Ashita-v4beta/config/addons/mapper/cmd_inbox.txt"

cp "$SRC"/mapper.lua "$DEST"/
for dir in core exploration graph movement geometry planning data tools; do
    if [ -d "$SRC/$dir" ]; then
        mkdir -p "$DEST/$dir"
        cp "$SRC/$dir"/*.lua "$DEST/$dir/" 2>/dev/null
    fi
done

echo "Deployed to $DEST"

if [ "$1" = "--reload" ]; then
    echo "/addon reload mapper" > "$INBOX"
    echo "Reload command queued"
fi
