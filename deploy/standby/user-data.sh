#!/bin/bash
# First-boot script for the cold-standby aggregator VM (task 2.10a). scripts/standby.sh up fills in
# the three placeholders and passes the result to Azure as custom data; it runs once, as root.
#   __TS_AUTHKEY__  one-off, ephemeral, pre-approved Tailscale key tagged tag:aggregator and
#                   tag:region-ap-indiasouthcentral. Azure keeps custom data with the VM, so the key
#                   must be one-off: it is spent here, and a copy read later cannot join anything.
#   __TS_HOSTNAME__ the standby's tailnet name (fedfleet-standby)
#   __IMAGE__       the FedFleet image (ghcr.io/ra-1020/fedfleet:<tag>), the same one CI built
# Nothing here opens a port: the VM has no inbound rule except Tailscale's UDP from two addresses,
# and the SuperLink listens on the Tailscale address only. No keys, certificates or data are
# written here; scripts/standby.sh failover copies them later, over Tailscale SSH.
set -euxo pipefail
exec > >(tee -a /var/log/fedfleet-standby.log) 2>&1
echo "fedfleet standby first boot $(date -u +%FT%TZ)"
mkdir -p /var/lib/fedfleet-standby
date +%s.%N > /var/lib/fedfleet-standby/boot_started

curl -fsSL https://tailscale.com/install.sh | sh
set +x                                   # never trace the key into the logs
umask 077
printf '%s' '__TS_AUTHKEY__' > /run/fedfleet-ts-join
tailscale up --auth-key=file:/run/fedfleet-ts-join --hostname='__TS_HOSTNAME__' --ssh
rm -f /run/fedfleet-ts-join
umask 022
set -x
date +%s.%N > /var/lib/fedfleet-standby/tailscale_up

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io sqlite3
systemctl enable --now docker
docker pull '__IMAGE__'
docker image inspect --format '{{index .RepoDigests 0}}' '__IMAGE__' > /var/lib/fedfleet-standby/image_digest

# The container runs as uid 10001 (see deploy/local/Dockerfile) with /srv/fedfleet as its home.
mkdir -p /srv/fedfleet/fedfleet-state /srv/fedfleet/certificates /srv/fedfleet/fedfleet/data \
         /srv/fedfleet/fedfleet/deploy
chown -R 10001:10001 /srv/fedfleet
chmod 700 /srv/fedfleet/certificates
date +%s.%N > /var/lib/fedfleet-standby/ready
echo "fedfleet standby ready $(date -u +%FT%TZ)"
