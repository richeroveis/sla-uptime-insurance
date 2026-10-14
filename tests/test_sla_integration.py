"""Integration test: drive SLAUptimeInsurance end-to-end on GenLayer studionet.

Excluded from the default run (see pytest.ini `-m "not integration"`) because it
talks to the live shared studionet endpoint. Run it explicitly with:

    pytest -m integration tests/test_sla_integration.py -v -s

It reuses the same driver as `scripts/deploy_studionet.py` so a green test run
and the collected on-chain evidence always come from identical code. Requires
GENLAYER_PRIVATE_KEY in the environment (loaded from .env). If the key is absent
the whole module skips so CI without secrets stays green.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "deploy_studionet.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location("deploy_studionet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_studionet"] = module
    spec.loader.exec_module(module)
    return module


# Load env + credentials before deciding to skip.
_driver = _load_driver()
_driver.load_dotenv()

pytestmark = pytest.mark.integration

skip_no_key = pytest.mark.skipif(
    not os.environ.get("GENLAYER_PRIVATE_KEY"),
    reason="GENLAYER_PRIVATE_KEY not set; integration test needs a studionet deployer",
)


@skip_no_key
def test_deploy_and_drive_studionet():
    from genlayer_py import create_account, create_client, studionet

    account = create_account(os.environ["GENLAYER_PRIVATE_KEY"])
    client = create_client(studionet, account=account)

    address, deploy_tx = _driver.deploy_contract(client, account)

    # Deployment produced a real contract address.
    assert address.startswith("0x")
    assert len(address) == 42
    assert deploy_tx.startswith("0x")

    evidence = _driver.drive(client, account, address)

    # The provider policy is readable back through the view interface.
    policy = evidence["policy_after"]
    assert policy["service_name"] == "payments-api"
    assert policy["target_uptime_bps"] == 9990
    assert policy["slash_rate_bps"] == 200
    assert policy["provider"].lower() == account.address.lower()

    # The deployer subscribed, so exactly one protected subscriber exists.
    assert policy["subscriber_count"] == 1

    # With no evaluations yet, uptime is 100% and the policy is compliant.
    metrics = evidence["metrics"]
    assert metrics["total_checks"] == 0
    assert metrics["uptime_bps"] == 10000
    assert metrics["compliant"] is True

    # No incident has fired, so nothing is claimable yet.
    assert int(evidence["claimable"]) == 0

    # Adversarial input must revert on chain, exactly as it does in direct mode.
    assert evidence["invalid_reverted"], "invalid register_policy should revert on studionet"

    # At least the deploy + two accepted writes were finalized.
    assert len(evidence["transactions"]) >= 2
