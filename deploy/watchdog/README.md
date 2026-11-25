# Watchdog: automatic resume after an aggregator crash (task 9.5)

systemd restarts `fedfleet-superlink` five seconds after it dies, and the sites reconnect on
their own. The run that was in progress is lost, though: Flower marks it `finished:failed` with
the details "No heartbeat received from the task" once its ServerApp has missed its heartbeats
(about a minute; Flower checks whenever runs are listed). `fedfleet_watchdog.py`, run every 30 s
by `fedfleet-watchdog.timer`, resumes such a run from its checkpoint through the control API,
with the run's original settings, exactly as `POST /runs {"resume": true}` by hand would.

It resumes only a run that was started through the control API with `auto_resume` (the default),
that Flower lists as failed for lack of heartbeat, that has a checkpoint and rounds left, at most
3 times per run folder and within 15 min of the crash. A completed or stopped run, or one whose
ServerApp failed with an error (for example a spent privacy budget), is never resumed. The control
API applies all its usual guards (privacy settings, budget, sites online, one run per folder, and
one run request at a time). Runs started before the watchdog was installed are ignored.

## Install (on the aggregator, after syncing the repo)

    sudo cp ~/fedfleet/deploy/watchdog/fedfleet-watchdog.service ~/fedfleet/deploy/watchdog/fedfleet-watchdog.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now fedfleet-watchdog.timer

## Watch it

    journalctl -u fedfleet-watchdog -n 30 --no-pager      # what it saw and did
    cat ~/fedfleet-watchdog/state.json                     # resumes per run folder
    systemctl list-timers fedfleet-watchdog.timer

Turn it off for one run with `"auto_resume": false` in the run request (the manual kill drill does
this); turn it off entirely with `sudo systemctl disable --now fedfleet-watchdog.timer`.

## Drill

    scripts/chaos.sh kill <name> --auto --yes [--dp-noise Z] [--secagg]

kills the aggregator inside round 4 and waits for the watchdog to resume the run, printing when
it did and the watchdog's journal lines.
