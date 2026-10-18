# Shared by run_sites.sh and start_node.sh: secure flags for site <i> if its files exist.
# TLS needs certificates/ca.pem; authentication needs keys/site-<i>.key.
# FEDFLEET_INSECURE=1 forces --insecure, for tests against the local insecure SuperLink
# started by scripts/run_local.sh.
site_security_flags() {
  local i="$1"
  if [ "${FEDFLEET_INSECURE:-0}" != "1" ] && [ -f certificates/ca.pem ] && [ -f "keys/site-$i.key" ]; then
    echo "--root-certificates certificates/ca.pem --auth-supernode-private-key keys/site-$i.key"
  else
    echo "--insecure"
  fi
}
