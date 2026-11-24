"""SecAgg+ spike (WBS 7.1, 14.4): legacy ServerApp, FedAvg, optionally under Flower's SecAgg+ workflow.

Not part of FedFleet's pipeline. Each round it writes the aggregate's mean, min and max and the
round time to the JSON file named by the run setting `out`, so plain and SecAgg+ runs compare.
Sites return the global vector plus 0.01 x (partition-id + 1), weighted 100 x (partition-id + 1),
so the exact weighted mean is known in advance (0.023333 per round with three sites).
"""
import json
import os
import time

import numpy as np
from flwr.common import ndarrays_to_parameters, parameters_to_ndarrays
from flwr.server import LegacyContext, ServerApp, ServerConfig
from flwr.server.strategy import FedAvg
from flwr.server.workflow import DefaultWorkflow, SecAggPlusWorkflow


class Logged(FedAvg):
    def __init__(self, out, **kw):
        super().__init__(**kw)
        self.out, self.rows, self.t = out, [], time.time()

    def aggregate_fit(self, server_round, results, failures):
        params, metrics = super().aggregate_fit(server_round, results, failures)
        v = parameters_to_ndarrays(params)[0] if params is not None else None
        now = time.time()
        self.rows.append({"round": server_round, "seconds": round(now - self.t, 2),
                          "replies": len(results), "failures": len(failures),
                          "mean": None if v is None else float(np.mean(v)),
                          "min": None if v is None else float(np.min(v)),
                          "max": None if v is None else float(np.max(v)),
                          "bytes_up": sum(len(t) for _, r in results for t in r.parameters.tensors)})
        self.t = now
        with open(self.out, "w") as f:
            json.dump(self.rows, f)
        return params, metrics


app = ServerApp()


@app.main()
def main(grid, context):
    cfg = context.run_config
    secagg = bool(cfg["secagg"])
    strategy = Logged(os.path.expanduser(str(cfg["out"])),
                      fraction_fit=1.0, fraction_evaluate=0.0, min_fit_clients=3, min_available_clients=3,
                      initial_parameters=ndarrays_to_parameters([np.zeros(int(cfg["size"]), np.float32)]))
    context = LegacyContext(context=context, config=ServerConfig(num_rounds=int(cfg["num-rounds"])), strategy=strategy)
    fit = SecAggPlusWorkflow(num_shares=3, reconstruction_threshold=2) if secagg else None
    DefaultWorkflow(fit_workflow=fit)(grid, context)
