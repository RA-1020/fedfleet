#!/usr/bin/env python3
"""Keep asking Oracle for the free Arm VM until capacity appears, then stop.

Free Arm (A1) capacity in busy regions is often full. This script makes one request
every few minutes (with random jitter), slows down if Oracle says "too many requests",
and stops as soon as the instance exists or a non-retryable error appears.

Setup (once):
    uv pip install oci
    (create an API key in the console and save ~/.oci/config; see the instructions)

Check your setup without creating anything:
    python deploy/oracle/grab_vm.py --dry-run

Run it (keep the Mac awake, lid open, plugged in):
    caffeinate -i python deploy/oracle/grab_vm.py
"""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import time
from datetime import datetime

import oci

SHAPE = "VM.Standard.A1.Flex"
TERMINAL_STATES = ("TERMINATED", "TERMINATING")


# ------------------------------------------------------------------ helpers
def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(title: str, text: str) -> None:
    log(f"*** {title}: {text}")
    print("\a", end="", flush=True)
    if sys.platform == "darwin":
        try:
            script = f'display notification "{text}" with title "{title}" sound name "Glass"'
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)
        except Exception:
            pass


def classify(exc: Exception) -> str:
    """Return 'capacity', 'rate_limited', 'transient' or 'fatal'."""
    if isinstance(exc, oci.exceptions.ServiceError):
        msg = (exc.message or "").lower()
        if exc.status == 429:
            return "rate_limited"
        if "capacity" in msg:
            return "capacity"
        if exc.status in (500, 502, 503, 504):
            return "transient"
        return "fatal"
    if isinstance(exc, (oci.exceptions.RequestException, oci.exceptions.ConnectTimeout,
                        ConnectionError, TimeoutError)):
        return "transient"
    return "fatal"


def read_public_key(path: str) -> str:
    path = os.path.expanduser(path)
    with open(path) as f:
        key = f.read().strip()
    if "PRIVATE KEY" in key:
        sys.exit(f"{path} is a PRIVATE key. Use the file that ends in .pub, and never share the private one.")
    if not key.startswith("ssh-"):
        sys.exit(f"{path} does not look like an SSH public key (it should start with 'ssh-').")
    return key


# ------------------------------------------------------------------ lookups
def discover(args, identity, compute, network):
    ads = [ad.name for ad in identity.list_availability_domains(args.compartment).data]
    log(f"Availability domains found: {', '.join(ads)}")

    images = compute.list_images(
        args.compartment,
        operating_system="Canonical Ubuntu",
        operating_system_version=args.ubuntu,
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        sys.exit(f"No Canonical Ubuntu {args.ubuntu} image found for {SHAPE} in this region.")
    image = images[0]
    log(f"Image: {image.display_name}")

    subnets = network.list_subnets(args.compartment, display_name=args.subnet_name).data
    if not subnets:
        sys.exit(f"No subnet named '{args.subnet_name}' found. Check the name in Networking, Virtual Cloud Networks.")
    log(f"Subnet: {subnets[0].display_name}")
    return ads, image.id, subnets[0].id


def find_existing(compute, args):
    found = compute.list_instances(args.compartment, display_name=args.name).data
    live = [i for i in found if i.lifecycle_state not in TERMINAL_STATES]
    return live[0] if live else None


def public_ip(compute, network, args, instance_id: str) -> str:
    for att in compute.list_vnic_attachments(args.compartment, instance_id=instance_id).data:
        vnic = network.get_vnic(att.vnic_id).data
        if vnic.public_ip:
            return vnic.public_ip
    return "(not assigned yet)"


def report(compute, network, args, instance) -> None:
    ip = public_ip(compute, network, args, instance.id)
    notify("FedFleet VM ready", f"{args.name} is {instance.lifecycle_state}. Public IP {ip}")
    log(f"Log in with:  ssh ubuntu@{ip}")


# ------------------------------------------------------------------ launch
def launch(compute, args, ad: str, image_id: str, subnet_id: str, ssh_key: str):
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=args.compartment,
        display_name=args.name,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=args.ocpus, memory_in_gbs=args.memory
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image_id),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id, assign_public_ip=True
        ),
        metadata={"ssh_authorized_keys": ssh_key},
    )
    # One request per call: never let the SDK add its own hidden retries.
    return compute.launch_instance(details, retry_strategy=oci.retry.NoneRetryStrategy()).data


def wait_running(compute, instance_id: str, timeout: int = 900):
    end = time.time() + timeout
    while time.time() < end:
        inst = compute.get_instance(instance_id).data
        if inst.lifecycle_state == "RUNNING":
            return inst
        time.sleep(10)
    return compute.get_instance(instance_id).data


# ------------------------------------------------------------------ main loop
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--every", type=int, default=300, help="seconds between attempts (default 300)")
    p.add_argument("--jitter", type=int, default=60, help="random extra seconds, 0 to this value (default 60)")
    p.add_argument("--max-backoff", type=int, default=1800, help="longest wait after 'too many requests' (default 1800)")
    p.add_argument("--ocpus", type=int, default=2)
    p.add_argument("--memory", type=int, default=12, help="GB of memory")
    p.add_argument("--name", default="fedfleet-agg")
    p.add_argument("--subnet-name", default="public subnet-fedfleet-vcn")
    p.add_argument("--ubuntu", default="24.04", help="Ubuntu version (the plain one, not Minimal)")
    p.add_argument("--ssh-key", default="~/.ssh/id_ed25519.pub")
    p.add_argument("--compartment", default=None, help="compartment OCID (default: your tenancy root)")
    p.add_argument("--profile", default="DEFAULT")
    p.add_argument("--dry-run", action="store_true", help="check credentials and lookups, create nothing")
    args = p.parse_args(argv)

    config = oci.config.from_file(profile_name=args.profile)
    oci.config.validate_config(config)
    args.compartment = args.compartment or config["tenancy"]
    log(f"Region: {config['region']}")

    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)
    ssh_key = read_public_key(args.ssh_key)

    ads, image_id, subnet_id = discover(args, identity, compute, network)
    if args.dry_run:
        log(f"Dry run OK: would request {args.ocpus} OCPU / {args.memory} GB {SHAPE} named {args.name}.")
        return 0

    attempt, backoff = 0, 0
    while True:
        attempt += 1
        existing = find_existing(compute, args)
        if existing:
            log(f"An instance named {args.name} already exists ({existing.lifecycle_state}); not creating another.")
            report(compute, network, args, existing)
            return 0

        outcome = "capacity"
        for ad in ads:
            log(f"Attempt {attempt}: asking for {args.ocpus} OCPU / {args.memory} GB in {ad}")
            try:
                inst = launch(compute, args, ad, image_id, subnet_id, ssh_key)
            except Exception as exc:  # noqa: BLE001 - classified below
                outcome = classify(exc)
                reason = getattr(exc, "message", None) or str(exc)
                if outcome == "fatal":
                    log(f"Oracle rejected the request and retrying will not help: {reason}")
                    return 1
                log(f"  {outcome}: {reason}")
                if outcome == "rate_limited":
                    break
                time.sleep(5)  # small gap before trying the next domain
                continue
            log(f"Oracle accepted the request ({inst.id}). Waiting for it to start...")
            report(compute, network, args, wait_running(compute, inst.id))
            return 0

        if outcome == "rate_limited":
            backoff = min(max(backoff * 2, args.every * 2), args.max_backoff)
            wait = backoff
            log(f"Too many requests: slowing down, next try in {wait // 60} min.")
        else:
            backoff = 0
            wait = args.every + random.randint(0, args.jitter)
            log(f"No capacity yet. Next try in {wait // 60} min {wait % 60} s. (Ctrl+C to stop)")
        time.sleep(wait)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Stopped by you.")
        sys.exit(130)
