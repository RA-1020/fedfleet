# Cold standby aggregator (task 2.10a)

The primary aggregator is the Oracle VM in Mumbai. The standby is a second aggregator on a second
cloud (Azure), created only when it is needed. Two things are replicated continuously so that it can
take over:

| What | How | Recovery point |
|---|---|---|
| SuperLink state database (registered site keys, runs, app bundles) | `deploy/backup/state_snapshot.py` every 5 min (systemd timer): SQLite online backup on a read-only connection, integrity-checked, written only if the content changed; uploaded by the Blob backup | content at most 5 min old; it changes only when a site is registered or a run starts |
| Run folders (checkpoint, rounds.jsonl, privacy ledger) | `deploy/backup/blob_backup.py`, every 15 s, changed files only (since 20 Nov) | the last checkpoint, at most one round plus 15 s |

The state database cannot be copied with `cp`: the SuperLink runs SQLite in WAL mode, and most of
the content sits in `state.db-wal`. In a local test the main file was 4 KB while the WAL held
1.6 MB; the snapshot was 348 KB with every table.

Everything else is either rebuilt or comes from the Mac:
- the standby's TLS certificate is issued at failover time by the CA on the Mac, for the
  standby's own name and address; its private key goes straight to the standby over SSH and is
  never stored in Blob, git or chat. The CA key never leaves the Mac;
- backbone, public test set and rate card are copied from the Mac;
- the software is the image CI builds (`ghcr.io/ra-1020/fedfleet`), so the standby runs the same
  Flower and PyTorch as CI tested.

## Install the snapshot on the primary (once)

```bash
rsync -av --exclude .venv --exclude .git --exclude __pycache__ --exclude logs --exclude data --exclude certificates --exclude keys --exclude .wrangler --exclude .pytest_cache ~/fedfleet/ ubuntu@fedfleet-agg:~/fedfleet/
ssh ubuntu@fedfleet-agg 'sudo cp ~/fedfleet/deploy/backup/fedfleet-state-snapshot.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now fedfleet-state-snapshot.timer && sudo systemctl start fedfleet-state-snapshot.service && sudo systemctl restart fedfleet-backup'
```

The snapshot only reads the live database; it does not stop or change the SuperLink.

## A drill

1. Tailnet policy: `deploy/tailscale/policy.hujson` adds `tag:region-ap-indiasouthcentral` (an APAC
   region, so APAC sites may fail over to it; EU-only sites may not).
2. A one-off Tailscale key for each drill (Settings > Keys): Reusable off, 1 day, Ephemeral on,
   Pre-approved on, tags `tag:aggregator`, `tag:region-ap-indiasouthcentral`; save it as
   `~/.fedfleet-ts-standby-key` (mode 600). `up` deletes it once used.
3. `scripts/cloud_up.sh`, then `scripts/standby.sh drill fo-a`.
4. `scripts/standby.sh failback fo-a-sb`, `scripts/standby.sh down`, `scripts/cloud_down.sh`.

The drill starts an 8-round run on the primary, waits for round 3, and then declares the primary
lost by stopping the run (the SuperLink service itself is not touched). From that moment (T0) it
creates the standby, restores onto it, moves the sites and resumes under a new name
(`<name>-sb`), printing a timeline: VM created, on the tailnet, image ready, restore, SuperLink
listening, 3 sites online, run resumed, first new round. `failback` copies the standby's run folder
to the primary, where the Blob backup and the dashboards pick it up, and moves the sites back.

Scope: plain head-only runs. DP and SecAgg+ runs are refused by `failover` for now (the
aggregator would also refuse a DP resume with other settings). No control API, watchdog or
monitoring on the standby: it is an aggregator only, enough to finish the federation's training.

Cost: a B2s with a public IP and a 30 GB disk, for the length of the drill (about $0.045/h for the
VM at list price); `down` deletes its whole resource group, `fedfleet-standby-rg`.
