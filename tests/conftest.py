"""Make the package importable and load the shipped evidence.

The suite needs nothing. No container, no database, no network, no credentials.
That is the direct consequence of the simulated clock: there is nothing to wait
for, so there is nothing to run. It finishes in well under a second.
"""

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))


def _result(name):
    with open(os.path.join(REPO, "results", name + ".json"),
              encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def exp1():
    return _result("exp1_rate_limits")


@pytest.fixture(scope="session")
def exp2():
    return _result("exp2_incremental_loss")


@pytest.fixture(scope="session")
def exp3():
    return _result("exp3_webhooks_vs_polling")


@pytest.fixture(scope="session")
def exp4():
    return _result("exp4_reconciliation")
