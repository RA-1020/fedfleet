# Contributing to FedFleet

FedFleet runs a live federation (an Oracle aggregator, an Azure site, laptop sites). These rules
keep it safe while three people work on it.

## Workflow

1. One branch per task, or per phase of a work plan, from an up-to-date `main`.
2. Open a pull request with the template filled in. CI must pass: `python` (lint, unit tests,
   coverage), `gateway`, `shell`, `helm` (once the Helm chart is on main), `federation` (a real 3-site federation, plain and
   SecAgg+) and `secrets`. Changes to the image also run `image`.
3. `main` needs one approving review; CODEOWNERS requests the right reviewers.
4. Merge with **squash and merge**, then delete the branch.

Anything that will run on the live aggregator (an install script, a firewall or tailnet change)
is reviewed **before** it runs, and runs only in an agreed slot with a written rollback.

## Run the checks locally

```bash
ruff check .
pytest -q
scripts/ci_federation.sh 2 3     # about 3 minutes: aggregator + 3 sites on this machine
scripts/ci_secrets.sh            # needs: pip install detect-secrets==1.5.0
shellcheck --severity=warning scripts/*.sh deploy/*/*.sh
```

## Never commit

Keys and certificates (`certificates/`, `keys/`), tokens (`~/.fedfleet-api-token`, the tunnel
token, the origin key, the Telegram token), Azure SAS URLs, the service principal, cloud
passwords, Tailscale auth keys, data files and run folders. `.gitignore`, `.dockerignore` and the
`secrets` check help, but they are not a substitute for looking at the diff.

## On the live machines

Do not stop, restart or edit the FedFleet services (aggregator, control API, watchdog, elasticity
controller, exporter, backup, tunnel, Prometheus, Grafana), anything under `/home/ubuntu` or
`/etc/fedfleet`, or what listens on ports 8000, 8088, 9092, 9090 and 3000. Never create or resize
an Oracle instance: the Always Free Arm allowance is fully used, and exceeding it can disable the
aggregator. Ask first.

## Commit messages

Imperative and specific, with the task id where there is one:
`k8s: run submission as a Job (4.4)`, `Image (3.6): non-root user, multi-arch build`.
