"""The SecAgg+ drop switch fires only for its site, round and stage, and never by default."""
from types import SimpleNamespace

import pytest

pytest.importorskip("torch")
from flwr.app import ConfigRecord, RecordDict  # noqa: E402
from flwr.common.secure_aggregation.secaggplus_constants import RECORD_KEY_CONFIGS, Key, Stage  # noqa: E402

from fedfleet.client_app import _chaos_secagg_drop  # noqa: E402


def msg(stage, rnd):
    rd = RecordDict()
    rd.config_records[RECORD_KEY_CONFIGS] = ConfigRecord({Key.STAGE: stage})
    rd.config_records["fitins.config"] = ConfigRecord({"server-round": rnd})
    return SimpleNamespace(content=rd)


def ctx(target, site):
    return SimpleNamespace(run_config={"chaos-secagg-drop": target}, node_config={"partition-id": site})


@pytest.mark.parametrize("target,site,stage,rnd,fires", [
    ("2:2", 2, Stage.COLLECT_MASKED_VECTORS, 2, True),     # the one case
    ("2:2", 1, Stage.COLLECT_MASKED_VECTORS, 2, False),    # another site
    ("2:2", 2, Stage.COLLECT_MASKED_VECTORS, 3, False),    # another round
    ("2:2", 2, Stage.SHARE_KEYS, 2, False),                # before key sharing is done
    ("2:2", 2, Stage.UNMASK, 2, False),
    ("", 2, Stage.COLLECT_MASKED_VECTORS, 2, False),       # default: off
    ("two:2", 2, Stage.COLLECT_MASKED_VECTORS, 2, False),  # malformed: never fires
])
def test_drop_switch(target, site, stage, rnd, fires):
    assert _chaos_secagg_drop(msg(stage, rnd), ctx(target, site)) is fires
