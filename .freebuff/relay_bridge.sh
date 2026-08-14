#!/bin/bash
# launchd-managed relay bridge for laptop-dourmouse (see relay/README.md)
cd "/Volumes/ATLAS /Atlas/dourmouse-4.0.0/atlas-strategy-lab" || exit 1
TOKEN=$(grep -E '^TOKEN=' relay/relay_config.txt | cut -d= -f2-)
RELAY=$(grep -E '^RELAY_URL=' relay/relay_config.txt | cut -d= -f2-)
exec python3 relay/agent_bridge.py --relay "$RELAY" --token "$TOKEN" --me laptop-dourmouse \
  >> "/Volumes/ATLAS /Atlas/dourmouse-4.0.0/.freebuff/bridge.log" 2>&1
