#!/usr/bin/env bash
# Install the k3s server on the aggregator. Its hostname is fedfleet-vcn; fedfleet-agg is only
# its Tailscale name. Run ONLY in a slot agreed with Rohaan, after the baseline and a backup.
# Single server on k3s's embedded SQLite store (no --cluster-init: it would switch to etcd).
#
# Undo: sudo /usr/local/bin/k3s-uninstall.sh
#   and remove the two FORWARD rules this script adds (k3s does not know about them):
#   sudo iptables -D FORWARD -s 10.42.0.0/16 -j ACCEPT
#   sudo iptables -D FORWARD -d 10.42.0.0/16 -j ACCEPT
# Afterwards Rohaan makes the two rules permanent by hand in /etc/iptables/rules.v4, above the
# REJECT line. Do NOT use netfilter-persistent save: it would also freeze k3s's own rules.
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root, so deploy/k8s/audit.yaml resolves wherever this is called from
[ "$(hostname)" = "fedfleet-vcn" ] || { echo "not fedfleet-vcn, stopping"; exit 1; }
[ "${CONFIRM:-}" = "yes" ] || { echo "set CONFIRM=yes after Rohaan's go"; exit 1; }

K3S_VERSION="v1.36.6+k3s1"   # pinned; join-agent.sh must use the same version
POD_CIDR="10.42.0.0/16"

# The live aggregator must be healthy and accepting sites: both services active, :9092 listening.
aggregator_ok() {
  local bad=0 s out
  for s in fedfleet-superlink fedfleet-control-api; do
    systemctl is-active --quiet "$s" || { echo "FAIL: $s is not active"; bad=1; }
  done
  out=$(sudo ss -ltn)
  grep -qE ':9092[[:space:]]' <<<"$out" || { echo "FAIL: nothing is listening on :9092"; bad=1; }
  return "$bad"
}

TS_IP=$(tailscale ip -4 | head -n1)
echo "== checks (2.1)"
if sudo ss -ltnup | grep -E ':(6443|10250|2379|2380|8472|10010) '; then
  echo "a k3s port is in use, stopping"; exit 1
fi
aggregator_ok || { echo "the aggregator is not healthy before the install, stopping"; exit 1; }
sudo iptables-save | tee ~/iptables-before-k3s.txt >/dev/null
systemctl --no-pager status fedfleet-superlink fedfleet-control-api > ~/services-before-k3s.txt || true
free -h > ~/free-before-k3s.txt

echo "== pod traffic must pass Oracle's FORWARD chain (it ends in REJECT); saved state is above"
for flag in -s -d; do
  sudo iptables -C FORWARD "$flag" "$POD_CIDR" -j ACCEPT 2>/dev/null \
    || sudo iptables -I FORWARD 1 "$flag" "$POD_CIDR" -j ACCEPT
done
sudo iptables -S FORWARD | head -n 5

echo "== files and CPU drop-in first, so k3s never starts at full weight"
sudo mkdir -p /var/lib/rancher/k3s/server/logs /etc/systemd/system/k3s.service.d
sudo install -m 600 deploy/k8s/audit.yaml /var/lib/rancher/k3s/server/audit.yaml
printf '[Service]\nCPUWeight=50\n' | sudo tee /etc/systemd/system/k3s.service.d/10-fedfleet.conf >/dev/null
sudo systemctl daemon-reload

echo "== install k3s server ${K3S_VERSION}"
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - server \
  --node-ip "$TS_IP" --advertise-address "$TS_IP" --tls-san "$TS_IP" \
  --flannel-iface tailscale0 \
  --disable traefik --disable servicelb \
  --secrets-encryption \
  --write-kubeconfig-mode 600 \
  --node-label fedfleet.io/role=control --node-label fedfleet.io/cloud=oracle \
  --node-label fedfleet.io/region=ap-mumbai \
  --kube-apiserver-arg=audit-log-path=/var/lib/rancher/k3s/server/logs/audit.log \
  --kube-apiserver-arg=audit-policy-file=/var/lib/rancher/k3s/server/audit.yaml \
  --kube-apiserver-arg=audit-log-maxage=14 \
  --kube-apiserver-arg=audit-log-maxsize=100 \
  --kube-apiserver-arg=audit-log-maxbackup=5

echo "== after install: compare with the saved state (2.1, 2.2)"
sudo iptables -S FORWARD
free -h
aggregator_ok || { echo "POST-INSTALL CHECK FAILED: compare with ~/services-before-k3s.txt and decide with Rohaan about the undo"; exit 1; }
echo "post-install check passed: fedfleet-superlink and fedfleet-control-api active, :9092 listening"
echo "Next: Rohaan makes the two FORWARD rules permanent in /etc/iptables/rules.v4 (by hand)."
