#!/usr/bin/env bash
# Rebuild the labelled FedFleet dev cluster on this machine (k3d). Touches nothing shared.
set -euo pipefail
CLUSTER=fedfleet-dev

k3d cluster delete "$CLUSTER" 2>/dev/null || true
k3d cluster create "$CLUSTER" --servers 1 --agents 3 \
  --k3s-arg "--disable=traefik@server:0" \
  --k3s-arg "--disable=servicelb@server:0" \
  --wait

kubectl label node "k3d-$CLUSTER-server-0" fedfleet.io/role=control fedfleet.io/cloud=oracle --overwrite
kubectl label node "k3d-$CLUSTER-agent-0" fedfleet.io/cloud=edge fedfleet.io/site=site0 --overwrite
kubectl label node "k3d-$CLUSTER-agent-1" fedfleet.io/cloud=edge fedfleet.io/site=site1 --overwrite
kubectl label node "k3d-$CLUSTER-agent-2" fedfleet.io/cloud=azure fedfleet.io/site=site2 fedfleet.io/residency=eu --overwrite

kubectl get nodes -o wide --show-labels
