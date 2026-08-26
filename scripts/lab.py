"""Shared helpers: building a run, and writing a result file.

Standard library only, no network, no container, no database. The entire
repository runs with `python3` and nothing else, which is a first for this
portfolio and is the direct consequence of the simulated clock: there is
nothing to wait for, so there is nothing to run.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RESULTS = os.path.join(REPO, "results")
sys.path.insert(0, REPO)

from sim import limiters, vendors, world as W          # noqa: E402
from sim.clock import Clock                            # noqa: E402
from connector.sync import Connector, Store, SyncConfig  # noqa: E402


VENDORS = {"atlas": vendors.Atlas, "beacon": vendors.Beacon}


def build_run(vendor_name, strategy="token_bucket", share=0.5, config=None):
    """A fresh clock, vendor, limiter and connector. Nothing is shared between
    runs, so one sweep point cannot contaminate the next."""
    clock = Clock()
    vendor = VENDORS[vendor_name](clock)
    documented = (vendor.burst_limit / vendor.burst_window_seconds
                  if hasattr(vendor, "burst_limit") else vendor.per_second_limit)
    lim = limiters.build(strategy, clock, documented, share=share)
    conn = Connector(vendor, clock, lim, Store(), config or SyncConfig())
    return clock, vendor, lim, conn


def write_result(name, payload):
    """results/<name>.json, sorted, with the input manifest stamped in.

    The generated world is not committed; it is a pure function of the seed,
    so this hash is what lets a reader who regenerates prove they hold the
    same input these numbers came from.
    """
    payload = dict(payload, input_manifest_sha256=W.manifest()["sha256"])
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, name + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def read_result(name):
    with open(os.path.join(RESULTS, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)
