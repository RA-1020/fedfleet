"""Toy site: returns the global vector plus a site-specific offset, so the exact average is known."""
import numpy as np
from flwr.client import ClientApp, NumPyClient
from flwr.client.mod import secaggplus_mod
from flwr.common.secure_aggregation.secaggplus_constants import RECORD_KEY_CONFIGS


class Site(NumPyClient):
    def __init__(self, pid):
        self.pid = pid

    def fit(self, parameters, config):
        new = parameters[0] + 0.01 * (self.pid + 1)
        return [new.astype(np.float32)], 100 * (self.pid + 1), {}


def client_fn(context):
    return Site(int(context.node_config["partition-id"])).to_client()


def maybe_secagg(msg, ctxt, call_next):
    """Use the SecAgg+ mod only when the server runs the SecAgg+ workflow."""
    if RECORD_KEY_CONFIGS in msg.content.config_records:
        return secaggplus_mod(msg, ctxt, call_next)
    return call_next(msg, ctxt)


app = ClientApp(client_fn=client_fn, mods=[maybe_secagg])
