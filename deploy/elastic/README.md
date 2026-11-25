# Elasticity controller (tasks 8.1, 8.3, 8.5)

`fedfleet_elastic.py` runs on the Oracle aggregator as `fedfleet-elastic.service`. It starts the
Azure site VM when a run is waiting for sites, submits the run once enough sites are online, and
deallocates the VM after 10 minutes without work, within a daily budget of VM minutes.

- **Demand:** `POST /runs` with `"wait_for_sites": true`. If too few sites are online, the control
  API queues the run (`~/fedfleet-elastic/queue/<name>.json`) and answers 202 `{"queued": true}`
  instead of 409.
- **Scale out:** a queued run and a deallocated VM: start the VM (Azure REST API), unless today's
  VM minutes have reached the budget. The VM boots, `fedfleet-site.service` starts site 2, and it
  registers with the aggregator.
- **Dispatch:** enough sites online and no run active: the controller submits the queued request
  to the control API (all the API's guards apply). A run that cannot start within 15 min is moved
  to `queue/failed` (for example it needs the Mac sites, which the controller cannot start).
- **Scale in:** only a VM the controller started, nothing queued, no run for 10 min: deallocate.
  A VM started with `scripts/cloud_up.sh` is left alone (its minutes still count).
- **Cost guard:** minutes are counted whenever the VM runs; no scale-out once the UTC day's minutes
  reach `ELASTIC_BUDGET_MIN_PER_DAY` (180 = 3 h, about $0.13 at the B2s list price). It never stops
  a VM during a run.
- **Record:** `~/fedfleet-elastic/events.jsonl` (every decision, timestamped), `state.json`
  (shown in `/status` under `elastic`, and as `fedfleet_elastic_*` gauges in Prometheus).

## Least privilege

The controller signs in as a service principal whose only role is the custom
`FedFleet VM power` (`fedfleet-vm-power-role.json`): read, instanceView/read, start and
deallocate, assigned on the one VM. It cannot create, resize, delete or reach anything else.
Its secret is `/etc/fedfleet/azure-sp.json` (root, mode 600); systemd hands a copy to the
service alone (`LoadCredential=azure-sp`), so the ubuntu login shell cannot read it.

## Setup (once, from the Mac, signed in with `az login`)

1. Custom role (wait a minute afterwards for Azure to propagate it):

       SUB=$(az account show --query id -o tsv)
       sed "s/SUBSCRIPTION_ID/$SUB/" deploy/elastic/fedfleet-vm-power-role.json > /tmp/fedfleet-role.json
       az role definition create --role-definition @/tmp/fedfleet-role.json --query roleName -o tsv

2. Service principal on the VM only; its JSON goes straight into a root-only file on the
   aggregator and is never shown:

       VMID=$(az vm show -g fedfleet-rg -n fedfleet-site2 --query id -o tsv)
       az ad sp create-for-rbac --name fedfleet-elastic --role "FedFleet VM power" --scopes "$VMID" -o json \
         | ssh ubuntu@fedfleet-agg 'sudo sh -c "umask 077; cat > /etc/fedfleet/azure-sp.json"'

3. Settings (not secret):

       sed "s/<subscription id>/$SUB/" deploy/elastic/elastic.env.example \
         | ssh ubuntu@fedfleet-agg 'sudo tee /etc/fedfleet/elastic.env >/dev/null'

4. Site 2 as a service on the Azure VM (VM running):

       scp scripts/site_service.sh ubuntu@fedfleet-site2:~/fedfleet/scripts/
       scp deploy/site/fedfleet-site.service ubuntu@fedfleet-site2:/tmp/
       ssh ubuntu@fedfleet-site2 'sudo mv /tmp/fedfleet-site.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable fedfleet-site'

5. On the aggregator, after syncing the repo:

       sudo cp ~/fedfleet/deploy/elastic/fedfleet-elastic.service /etc/systemd/system/
       sudo systemctl daemon-reload && sudo systemctl enable --now fedfleet-elastic
       journalctl -u fedfleet-elastic -n 20 --no-pager

## Turning it off

`sudo systemctl disable --now fedfleet-elastic`. To remove its access entirely:
`az ad sp delete --id $(az ad sp list --display-name fedfleet-elastic --query "[0].appId" -o tsv)`.
