"""Integration test: drive SLAUptimeInsurance end-to-end on GenLayer studionet.

Excluded from the default run (see pytest.ini `-m "not integration"`) because it
talks to the live shared studionet endpoint. Run it explicitly with:

    pytest -m integration tests/test_sla_integration.py -v -s

It reuses the same driver as `scripts/deploy_studionet.py` so a green test run
and the collected on-chain evidence always come from identical code. Requires
GENLAYER_PRIVATE_KEY in the environment (loaded from .env). If the key is absent
the whole module skips so CI without secrets stays green.

The assertions are coherence checks around the single enforced SLA-breach rule:
whatever uptime the live consensus audit actually measured, `is_sla_breached`,
the settlement outcome and the slashed amount must always agree, a compliant
policy must be slashed zero and fully refunded, and a subscriber compensation
claim on a compliant policy must revert on chain.
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


def _assert_case_coherence(case: dict, collateral: int):
    """Every seeded policy must obey the ONE SLA-breach rule consistently."""
    assert case["policy_id"] >= 1
    # Coherence: the view predicate, the stored compliance mirror and the metrics
    # view all agree on the same target-vs-measured comparison.
    breached = case["is_sla_breached"]
    metrics = case["metrics"]
    assert metrics["compliant"] is (not breached)

    # A settled policy recorded a settlement with slash + refund == collateral.
    settle = case["settlement"]
    assert settle is not None, "settlement record must exist on chain"
    assert (settle["outcome"] == "BREACHED") is breached
    assert settle["slash_amount"] + settle["provider_refund"] == collateral

    # The slashed amount is exactly what the predicate implies.
    if breached:
        assert settle["slash_amount"] > 0
        assert settle["slash_amount"] == case["slashed_amount"]
        expected = (collateral * (case["declared_target_uptime_bps"] - metrics["verified_uptime_bps"])) // 10000
        if expected == 0:
            expected = 1
        assert settle["slash_amount"] == min(expected, collateral)
    else:
        # COMPLIANT: nothing is slashed, the whole pool refunds to the provider.
        assert case["slashed_amount"] == 0
        assert settle["slash_amount"] == 0
        assert settle["provider_refund"] == collateral


def _assert_guard_case(guard: dict, collateral: int):
    """Deterministic, no-web/LLM on-chain proofs of the enforced predicate."""
    assert guard.get("claim_requires_evaluation_reverted") is True
    assert "has not been evaluated" in str(guard.get("claim_reason"))
    assert guard.get("outsider_settle_reverted") is True
    assert "Only the provider or a subscriber may settle" in str(guard.get("outsider_settle_reason"))
    # An un-audited policy is never slashed: settlement takes the compliant path.
    assert guard["settle_exec_status"] == "return"
    assert guard["slashed_amount"] == 0
    assert guard["settlement"]["outcome"] == "COMPLIANT"
    assert guard["settlement"]["provider_refund"] == collateral


@skip_no_key
def test_deploy_and_seed_both_cases_studionet():
    from genlayer_py import create_account, create_client, studionet

    account = create_account(os.environ["GENLAYER_PRIVATE_KEY"])
    client = create_client(studionet, account=account)

    reused = os.environ.get("SLA_CONTRACT_ADDRESS")
    if reused:
        address = reused
        deploy_tx = os.environ.get("SLA_DEPLOY_TX", "(reused)")
    else:
        address, deploy_tx = _driver.deploy_contract(client, account)

    # A real contract address exists.
    assert address.startswith("0x")
    assert len(address) == 42

    evidence = _driver.drive(client, account, address)

    # Every submitted transaction reached FINALIZED.
    assert all(t["finalized"] for t in evidence["transactions"])
    assert evidence["finalized_count"] == len(evidence["transactions"])

    # The deterministic guard case always proves the enforced predicate on chain.
    _assert_guard_case(evidence["guard_policy"], _driver.DEMO_COLLATERAL)

    compliant = evidence["compliant_policy"]
    breach = evidence["breach_policy"]
    assert "error" not in compliant, compliant.get("error")
    assert "error" not in breach, breach.get("error")

    # The live consensus audit must have applied for both seeded policies.
    assert compliant["evaluated_on_chain"], "compliant policy audit did not apply on chain"
    assert breach["evaluated_on_chain"], "breach policy audit did not apply on chain"

    # A healthy public service satisfies the low target: not a breach, and its
    # subscriber compensation claim reverts on chain (never slashed).
    assert compliant["is_sla_breached"] is False
    assert compliant["subscriber_compensation_guard_reverted"] is True
    assert "SLA target met" in str(compliant["subscriber_compensation_guard_reason"])

    # Both live cases must obey the ONE predicate, whichever way the audit fell.
    _assert_case_coherence(compliant, _driver.DEMO_COLLATERAL)
    _assert_case_coherence(breach, _driver.DEMO_COLLATERAL)

    # Adversarial input must still revert on chain.
    assert evidence["invalid_reverted"], "invalid register_policy should revert on studionet"

    # 3-way source integrity: local file bytes == on-chain code bytes.
    local_bytes = _driver.CONTRACT.read_text(encoding="utf-8").encode("utf-8")
    chain_bytes = _driver.fetch_chain_code(client, address)
    assert _driver.sha256_hex(local_bytes) == _driver.sha256_hex(chain_bytes)

