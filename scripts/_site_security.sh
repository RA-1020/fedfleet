# Shared by run_sites.sh and start_node.sh: secure flags for site <i> if its files exist.
# TLS needs certificates/ca.pem; authentication needs keys/site-<i>.key.
site_security_flags() {
  local i="$1"
  if [ -f certificates/ca.pem ] && [ -f "keys/site-$i.key" ]; then
    echo "--root-certificates certificates/ca.pem --auth-supernode-private-key keys/site-$i.key"
  else
    echo "--insecure"
  fi
}
