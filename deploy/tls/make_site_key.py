"""Create a site's identity key pair (NIST P-384, OpenSSH format) for SuperNode authentication.

    python deploy/tls/make_site_key.py site-0

Writes keys/site-0.key (stays on the site, never shared) and keys/site-0.pub (registered
with the aggregator through `flwr supernode register keys/site-0.pub <connection>`).
"""

from __future__ import annotations

import os
import sys

from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ec


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python deploy/tls/make_site_key.py <site-name>")
    name = sys.argv[1]
    os.makedirs("keys", exist_ok=True)
    priv_path, pub_path = f"keys/{name}.key", f"keys/{name}.pub"
    if os.path.exists(priv_path):
        sys.exit(f"{priv_path} already exists; delete it first if you really want a new identity.")
    key = ec.generate_private_key(ec.SECP384R1())
    private = key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, ser.NoEncryption())
    public = key.public_key().public_bytes(ser.Encoding.OpenSSH, ser.PublicFormat.OpenSSH)
    with open(priv_path, "wb") as f:
        f.write(private)
    os.chmod(priv_path, 0o600)
    with open(pub_path, "wb") as f:
        f.write(public + f" {name}\n".encode())
    print(f"{priv_path} (private, never share) and {pub_path} (register this one) created")


if __name__ == "__main__":
    main()
