# FedFleet

Communication-efficient federated learning on a hybrid multicloud.

Each site keeps a **frozen, pre-trained backbone** and trains only a small **task head**. Only the head travels to the aggregator and back: **28,248 bytes per update instead of 1,847,058 (98.47% less)**. This repo is the Layer 1 starter: aggregation service (Σ), node service (N), per-round communication metrics, checkpoints, and dropout tolerance.

Built on **Flower 1.38.0**. Many Flower tutorials online use the older API and will not match this code.

## 1. Setup (macOS, Apple Silicon)

Commands in this README contain no `#` comments, because zsh (the macOS default shell) treats `#` in a typed command as an argument unless `interactivecomments` is on.

If you do not have the code yet, clone the repository root (not a `/tree/main` URL):

```bash
git clone https://github.com/RA-1020/fedfleet.git
cd fedfleet
```

Then, inside the `fedfleet` folder:

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e .
echo 'export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1' >> ~/.zshrc
export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1
```

The environment variable matters: by default Flower 1.38 reinstalls the app's dependencies (including PyTorch) into a fresh environment for every run. With it set, runs use this venv. The `echo` line makes it permanent for new terminals; the `export` line sets it in the current one.

If a conda environment is active (the prompt shows `(base)`), run `conda deactivate` before activating the venv so the two do not mix.

If you ever started a run before setting the variable, restart the local SuperLink so it picks it up:

```bash
pkill -f flower-superlink
```

## 2. Check the communication numbers

```bash
python scripts/count_bytes.py
```

## 3. Run a simulation (all sites on one machine)

Set the number of simulated sites once (repeat it if the local SuperLink restarts), then run:

```bash
flwr federation simulation-config --num-supernodes 3
flwr run . --stream --run-config "num-server-rounds=5"
```

Each round prints a line such as:

```
[FedFleet] round 2: 3/3 nodes, 28,248 B per update, 84,744 B up, 2.7 s, acc=0.39
```

Compare with the full-model baseline:

```bash
flwr run . --stream --run-config 'num-server-rounds=5 head-only=false metrics-dir="~/fedfleet-runs-full"'
```

Without real data and a pre-trained backbone, the code uses synthetic signals and a random backbone. That tests the plumbing; the accuracy numbers mean nothing yet (see section 6).

`global_acc` is measured by the aggregator on a fixed public test set, so the convergence curve stays comparable even when sites drop out. `site_acc` is the average of the sites' own local test sets, which changes with whoever is online.

If you see `ActorPool is empty` or the wrong number of nodes, the local SuperLink has restarted and lost its settings: run the `simulation-config` command again.

## 4. Run in deployment mode (separate aggregator and site processes)

This is the same topology the cloud deployment uses: a SuperLink plus independent SuperNodes.

Add the connection settings once:

```bash
cat deploy/flwr-config-snippet.toml >> ~/.flwr/config.toml
```

Start a SuperLink and two SuperNodes, then run training against them:

```bash
scripts/run_local.sh 2
flwr run . fedfleet-local --stream --run-config "num-server-rounds=6 min-nodes=1 round-timeout=25.0"
```

Stop everything when finished:

```bash
scripts/stop_local.sh
```

In Flower 1.38 the Control API that `flwr run` talks to is an HTTP API on port **8000** (not the older gRPC port 9093). The Fleet API that SuperNodes connect to is on **9092**.

## 5. Dropout demo: the instructor's whiteboard

With the deployment from section 4 running a long run (`num-server-rounds=10`), open a second terminal:

Take site 1 offline:

```bash
scripts/kill_node.sh 1
```

The next round finishes with 1/2 nodes. Then bring site 1 back; it rejoins at the following round:

```bash
scripts/start_node.sh 1 2
```

`min-nodes=1` lets a round finish with a single site; `round-timeout` is how long the aggregator waits for a slow or missing site.

## 6. Real data and a real backbone

1. Download **RadioML 2016.10a** (`RML2016.10a_dict.pkl`) from DeepSig's datasets page and put it in `data/`. The download may require filling in a form.
2. Pre-train and freeze the backbone on the public pre-training split. This uses the Mac GPU (MPS); Kaggle works too.
   ```bash
   python scripts/pretrain_backbone.py --data-path data/RML2016.10a_dict.pkl --epochs 15
   ```
3. Train federated with both:
   ```bash
   flwr run . --stream --run-config 'data-path="data/RML2016.10a_dict.pkl" backbone-path="backbone.pt"'
   ```

Use absolute paths if the runs cannot find the files.

## 7. Docker (local)

```bash
docker compose -f deploy/local/docker-compose.yml up --build
flwr run . fedfleet-docker --stream
```

The Docker files mirror the process setup from section 4. The process setup was tested; the Docker build was not, so expect to fix small issues on the first try.

## Data splits

All splits are fixed by a seed and disjoint:

| Split | Share | Used for |
| --- | --- | --- |
| Pre-training | 15% | Training the backbone once, before federation |
| Public test | 5% | The aggregator's convergence curve (`global_acc`) |
| Sites | 80% | Divided between sites (Dirichlet, non-IID); each keeps 80% train, 20% local test |

## Metrics

Every round appends one JSON line to `<metrics-dir>/rounds.jsonl` (default `~/fedfleet-runs`):

| Field | Meaning |
| --- | --- |
| `nodes_asked`, `nodes_replied`, `nodes_dropped` | Participation and dropouts |
| `bytes_per_update`, `bytes_down`, `bytes_up` | Communication volume |
| `train_round_seconds` | Time from sending the round to aggregating it |
| `global_acc`, `global_loss` | Accuracy on the fixed public test set |
| `train_loss`, `eval_acc`, `eval_loss` | Training loss and the sites' own test accuracy |

A checkpoint (`latest_checkpoint.pt`) is written after every round. Restart with `--run-config "resume=true"` to continue from it.

## Configuration (`pyproject.toml`, `[tool.flwr.app.config]`)

| Key | Default | Purpose |
| --- | --- | --- |
| `head-only` | `true` | Exchange only the task head (`false` = full-model baseline) |
| `num-server-rounds` | 10 | Rounds per run |
| `min-nodes` | 2 | Minimum sites for a round to proceed |
| `round-timeout` | 120 | Seconds to wait for slow or offline sites |
| `local-epochs`, `batch-size`, `lr` | 1, 64, 0.005 | Local training |
| `dirichlet-alpha` | 0.5 | How non-IID the site partitions are |
| `data-path`, `backbone-path` | empty | Real data and pre-trained backbone |
| `metrics-dir` | `~/fedfleet-runs` | Metrics and checkpoints |

## Layout

```
fedfleet/model.py        tiny ViT: backbone (frozen) + task head (exchanged)
fedfleet/data.py         RadioML -> spectrograms, Dirichlet split, synthetic fallback
fedfleet/task.py         local train / evaluate
fedfleet/client_app.py   node service (N)
fedfleet/server_app.py   aggregation service (Σ)
fedfleet/comms.py        FedAvg + communication metrics + checkpoints
scripts/                 pre-training, byte counts, local deployment, dropout demo
deploy/                  Docker and Flower CLI configuration
```
