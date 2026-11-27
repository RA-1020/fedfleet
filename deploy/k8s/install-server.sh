#!/usr/bin/env bash
# Install the k3s server on fedfleet-agg. Run ONLY in a slot agreed with Rohaan,
# after the baseline and a backup. Undo: sudo /usr/local/bin/k3s-uninstall.sh
set -euo pipefail
[ "$(hostname)" = "fedfleet-agg" ] || { echo "not fedfleet-agg, stopping"; exit 1; }
[ "${CONFIRM:-}" = "yes" ] || { echo "set CONFIRM=yes after Rohaan's go"; exit 1; }

TS_IP=$(tailscale ip -4 | head -n1)
echo "== checks (2.1)"
if sudo ss -ltnup | grep -E ':(6443|10250|2379|2380|8472|10010) '; then
  echo "a k3s port is in use, stopping"; exit 1
fi
sudo iptables-save | tee ~/iptables-before-k3s.txt >/dev/null
systemctl --no-pager status fedfleet-superlink fedfleet-control-api > ~/services-before-k3s.txt || true
free -h > ~/free-before-k3s.txt

echo "== files and CPU drop-in first, so k3s never starts at full weight"
sudo mkdir -p /var/lib/rancher/k3s/server/logs /etc/systemd/system/k3s.service.d
sudo install -m 600 deploy/k8s/audit.yaml /var/lib/rancher/k3s/server/audit.yaml
printf '[Service]\nCPUWeight=50\n' | sudo tee /etc/systemd/system/k3s.service.d/10-fedfleet.conf >/dev/null
sudo systemctl daemon-reload

echo "== install k3s server"
curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL=stable sh -s - server \
  --cluster-init \
  --node-ip "$TS_IP" --advertise-address "$TS_IP" --tls-san "$TS_IP" \
  --flannel-iface tailscale0 \
  --disable traefik --disable servicelb \
  --secrets-encryption \
  --write-kubeconfig-mode 600 \
  --node-label fedfleet.io/role=control --node-label fedfleet.io/cloud=oracle \
  --node-label fedfleet.io/region=ap-mumbai \
  --kube-apiserver-arg=audit-log-path=/var/lib/rancher/k3s/server/logs/audit.log \
  --kube-apiserver-arg=audit-policy-file=/var/lib/rancher/k3s/server/audit.yaml \
  --kube-apiserver-arg=audit-log-maxage=14

echo "== after install: compare with the saved state (2.1, 2.2)"
systemctl --no-pager is-active fedfleet-superlink fedfleet-control-api
sudo iptables -S FORWARD
free -h
