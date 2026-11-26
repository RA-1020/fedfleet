#!/usr/bin/env bash
# Rebuild the labelled FedFleet dev cluster on this machine (k3d). Touches nothing shared.
# Labels mirror the live setup: all three sites join the Mumbai aggregator, so residency=apac.
# Use residency=eu only to test the refusal case on purpose (and say so in NOTES.md).
set -euo pipefail
CLUSTER=fedfleet-dev

k3d cluster delete "$CLUSTER" 2>/dev/null || true
k3d cluster create "$CLUSTER" --servers 1 --agents 3 \
  --k3s-arg "--disable=traefik@server:0" \
  --k3s-arg "--disable=servicelb@server:0" \
  --wait

kubectl label node "k3d-$CLUSTER-server-0" fedfleet.io/role=control fedfleet.io/cloud=oracle fedfleet.io/region=ap-mumbai --overwrite
kubectl label node "k3d-$CLUSTER-agent-0" fedfleet.io/cloud=edge fedfleet.io/site=site0 fedfleet.io/residency=apac --overwrite
kubectl label node "k3d-$CLUSTER-agent-1" fedfleet.io/cloud=edge fedfleet.io/site=site1 fedfleet.io/residency=apac --overwrite
kubectl label node "k3d-$CLUSTER-agent-2" fedfleet.io/cloud=azure fedfleet.io/site=site2 fedfleet.io/residency=apac --overwrite

kubectl get nodes -o wide --show-labels
