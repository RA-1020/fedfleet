#!/usr/bin/env bash
# Bring SuperNode <i> back (it rejoins the federation at the next round).
i="${1:-1}"; N="${2:-2}"
export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1
nohup flower-supernode --insecure --superlink 127.0.0.1:9092 \
  --node-config "partition-id=$i num-partitions=$N" --port $((9094 + i)) \
  >> "logs/supernode-$i.log" 2>&1 &
echo "node $i is back online"
