#!/usr/bin/env bash
# Create FedFleet's private certificate authority and the aggregator's TLS certificate.
#   deploy/tls/make_certs.sh fedfleet-agg 100.69.230.89
# Writes certificates/: ca.pem (give to every site), ca.key (keep private, never copy),
# server.pem and server.key (aggregator only).
set -euo pipefail
HOST="${1:-fedfleet-agg}"
IP="${2:?usage: make_certs.sh <hostname> <aggregator Tailscale IP>}"
OUT="${3:-certificates}"
mkdir -p "$OUT" && cd "$OUT"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-384 -nodes -days 365 \
  -keyout ca.key -out ca.pem -subj "/CN=FedFleet CA" 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-384 -nodes \
  -keyout server.key -out server.csr -subj "/CN=$HOST" 2>/dev/null
cat > server.ext << EOT
subjectAltName = DNS:$HOST, DNS:localhost, IP:$IP, IP:127.0.0.1
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
EOT
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 365 \
  -out server.pem -extfile server.ext 2>/dev/null
rm -f server.csr server.ext ca.srl
chmod 600 ca.key server.key
echo "certificates for $HOST ($IP) written to $(pwd)"
