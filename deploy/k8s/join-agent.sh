#!/usr/bin/env bash
# Join fedfleet-site2 (Azure) as a k3s agent. Rohaan starts the VM first.
# The token is read from a root-only file, never passed on the command line or committed.
# Usage: CONFIRM=yes deploy/k8s/join-agent.sh <server Tailscale IP>
# Undo: sudo /usr/local/bin/k3s-agent-uninstall.sh
set -euo pipefail
[ "$(hostname)" = "fedfleet-site2" ] || { echo "not fedfleet-site2, stopping"; exit 1; }
[ "${CONFIRM:-}" = "yes" ] || { echo "set CONFIRM=yes after Rohaan's go"; exit 1; }
SERVER_IP="${1:?usage: join-agent.sh <server Tailscale IP>}"
sudo test -s /root/k3s-token || { echo "/root/k3s-token missing"; exit 1; }
TS_IP=$(tailscale ip -4 | head -n1)
curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL=stable K3S_URL="https://${SERVER_IP}:6443" \
  K3S_TOKEN_FILE=/root/k3s-token sh -s - agent \
  --node-ip "$TS_IP" --flannel-iface tailscale0 \
  --node-label fedfleet.io/cloud=azure --node-label fedfleet.io/site=site2 \
  --node-label fedfleet.io/residency=apac
