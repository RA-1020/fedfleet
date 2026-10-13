#!/usr/bin/env bash
# Simulate a site going offline: kill SuperNode <i> (default 1). Restart it with
# scripts/start_node.sh <i> <num_nodes> to watch it rejoin.
pkill -f "[p]artition-id=${1:-1} " && echo "node ${1:-1} is offline"
