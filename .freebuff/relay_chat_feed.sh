#!/bin/bash
# launchd-managed chat feed dashboard for laptop-dourmouse (see relay/README.md)
cd "/Volumes/ATLAS /Atlas/dourmouse-4.0.0/atlas-strategy-lab" || exit 1
TOKEN=$(grep -E '^TOKEN=' relay/relay_config.txt | cut -d= -f2-)
RELAY=$(grep -E '^RELAY_URL=' relay/relay_config.txt | cut -d= -f2-)
exec python3 relay/chat_feed.py --relay "$RELAY" --token "$TOKEN" --me laptop-dourmouse \
  --port 8789 --send-token laptop-dash-2026 \
  >> "/Volumes/ATLAS /Atlas/dourmouse-4.0.0/.freebuff/chat_feed.log" 2>&1
