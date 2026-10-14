"""Direct-mode tests for SLAUptimeInsurance.

Fast, in-memory (no server). Validates input guards, subscriber accounting, the
slash math for each health outcome, payout distribution, and compensation
claims. Web render + LLM classification are mocked; the equivalence-principle
validator path is exercised by the integration tests, not here.
"""

import json


CONTRACT = "contracts/sla_uptime_insurance.py"
STATUS_URL = "https://status.example.com/v1/health"


def _hex(addr) -> str:
    """Normalise a direct-mode test address (raw bytes) to a 0x hex string."""
    if isinstance(addr, (bytes, bytearray)):
        return "0x" + bytes(addr).hex()
    return str(addr)


def _register(contract, vm, provider, collateral=1000, slash_rate_bps=200, target=9990):
    vm.sender = provider
    return contract.register_policy(
        "payments-api",
        STATUS_URL,
        target,
        collateral,
        slash_rate_bps,
    )


def _mock_health(vm, status_code, severity, breached, summary="auto"):
    """Mock the live status page (web render) and the LLM classification."""
    vm.mock_web(r".*status\.example\.com.*", {"status": 200, "body": "Service status page"})
    payload = {
        "status_code": status_code,
        "severity_score": severity,
        "is_sla_breached": breached,
        "summary": summary,
    }
    vm.mock_llm(r".*SLA monitor.*", json.dumps(payload))


def test_register_policy_and_read_back(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)

    pid = _register(contract, direct_vm, direct_alice)
    assert pid == 1

    policy = contract.get_policy(pid)
    assert policy["policy_id"] == 1
    assert policy["service_name"] == "payments-api"
    assert policy["status_url"] == STATUS_URL
    assert policy["target_uptime_bps"] == 9990
    assert policy["collateral_pool"] == 1000
    assert policy["slash_rate_bps"] == 200
    assert policy["total_checks"] == 0
    assert policy["failed_checks"] == 0
    assert policy["active"] is True
    assert policy["provider"].lower() == _hex(direct_alice).lower()

    # Next id increments monotonically.
    direct_vm.sender = direct_alice
    pid2 = contract.register_policy("other", STATUS_URL, 9990, 500, 100)
    assert pid2 == 2


def test_register_policy_input_validation(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice

    with direct_vm.expect_revert("status_url must be http"):
        contract.register_policy("svc", "ftp://bad.example.com", 9990, 1000, 200)

    with direct_vm.expect_revert("target_uptime_bps"):
        contract.register_policy("svc", STATUS_URL, 10001, 1000, 200)

    with direct_vm.expect_revert("slash_rate_bps"):
        contract.register_policy("svc", STATUS_URL, 9990, 1000, 0)

    with direct_vm.expect_revert("collateral_amount"):
        contract.register_policy("svc", STATUS_URL, 9990, 0, 200)

    with direct_vm.expect_revert("service_name"):
        contract.register_policy("   ", STATUS_URL, 9990, 1000, 200)

    # slash_rate above the 5% (500 bps) single-incident cap is rejected.
    with direct_vm.expect_revert("per-incident cap"):
        contract.register_policy("svc", STATUS_URL, 9990, 1000, 600)

    # Nothing valid was stored, so next id is still 1.
    assert contract.register_policy("svc", STATUS_URL, 9990, 1000, 200) == 1


def test_subscribe_and_prevent_duplicates(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)

    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)
    assert contract.get_policy(pid)["subscriber_count"] == 1

    with direct_vm.expect_revert("Already subscribed"):
        contract.subscribe_to_policy(pid)

    direct_vm.sender = direct_alice
    contract.subscribe_to_policy(pid)
    assert contract.get_policy(pid)["subscriber_count"] == 2


def test_subscribe_unknown_policy(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown policy"):
        contract.subscribe_to_policy(999)


def test_evaluate_operational_no_slash(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)

    _mock_health(direct_vm, "OPERATIONAL", 0, False)
    direct_vm.sender = direct_alice
    status = contract.evaluate_service_health(pid)
    assert status == "OPERATIONAL"

    policy = contract.get_policy(pid)
    assert policy["total_checks"] == 1
    assert policy["failed_checks"] == 0
    assert policy["collateral_pool"] == 1000  # untouched

    metrics = contract.get_compliance_metrics(pid)
    assert metrics["uptime_bps"] == 10000
    assert metrics["compliant"] is True


def test_evaluate_degraded_half_weight_slash(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)  # collateral 1000, rate 200bps -> base 20
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)  # 1 subscriber gets the full slash

    # Severity from the LLM is ignored; the contract derives 50 for DEGRADED.
    _mock_health(direct_vm, "DEGRADED", 50, True)
    direct_vm.sender = direct_alice
    status = contract.evaluate_service_health(pid)
    assert status == "DEGRADED"

    incident = contract.get_incident(1)
    assert incident["status_code"] == "DEGRADED"
    assert incident["severity_score"] == 50
    assert incident["slashed_amount"] == 10  # 20 base * 50% weight

    policy = contract.get_policy(pid)
    assert policy["failed_checks"] == 1
    assert policy["collateral_pool"] == 990  # 1000 - 10

    assert contract.get_claimable_balance(_hex(direct_bob)) == 10


def test_evaluate_major_outage_full_weight_distribution(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie
):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)
    direct_vm.sender = direct_charlie
    contract.subscribe_to_policy(pid)

    _mock_health(direct_vm, "MAJOR_OUTAGE", 100, True)
    direct_vm.sender = direct_alice
    status = contract.evaluate_service_health(pid)
    assert status == "MAJOR_OUTAGE"

    incident = contract.get_incident(1)
    assert incident["severity_score"] == 100
    assert incident["slashed_amount"] == 20  # 20 base * 100% weight

    # Split equally across the two subscribers: 10 each.
    assert contract.get_claimable_balance(_hex(direct_bob)) == 10
    assert contract.get_claimable_balance(_hex(direct_charlie)) == 10

    policy = contract.get_policy(pid)
    assert policy["collateral_pool"] == 980
    assert policy["failed_checks"] == 1

    metrics = contract.get_compliance_metrics(pid)
    assert metrics["total_checks"] == 1
    assert metrics["failed_checks"] == 1
    assert metrics["uptime_bps"] == 0
    assert metrics["compliant"] is False


def test_claim_compensation_resets_balance(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)

    _mock_health(direct_vm, "MAJOR_OUTAGE", 100, True)
    direct_vm.sender = direct_alice
    contract.evaluate_service_health(pid)

    direct_vm.sender = direct_bob
    claimed = contract.claim_compensation()
    assert claimed == 20  # single subscriber receives the full slash
    assert contract.get_claimable_balance(_hex(direct_bob)) == 0

    # Reclaim is a no-op once reset.
    assert contract.claim_compensation() == 0


def test_evaluate_zero_subscribers_forfeits_slash(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)

    # Slash 20 with no subscribers: collateral is still reduced (forfeit), but
    # nothing becomes claimable because there is nobody to pay.
    _mock_health(direct_vm, "MAJOR_OUTAGE", 100, True)
    direct_vm.sender = direct_alice
    contract.evaluate_service_health(pid)

    assert contract.get_policy(pid)["collateral_pool"] == 980
    assert contract.get_incident(1)["slashed_amount"] == 20
    assert contract.get_claimable_balance(_hex(direct_alice)) == 0


def test_evaluate_rounding_dust_conserves_collateral(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie
):
    # Proposed slash is 10 across 3 subscribers -> 3 each (moved 9), 1 dust unit
    # stays with the provider. The recorded slash equals what actually moved.
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)  # base slash 20
    for who in (direct_alice, direct_bob, direct_charlie):
        direct_vm.sender = who
        contract.subscribe_to_policy(pid)

    _mock_health(direct_vm, "DEGRADED", 50, True)  # proposed 20 * 50% = 10
    direct_vm.sender = direct_alice
    contract.evaluate_service_health(pid)

    assert contract.get_claimable_balance(_hex(direct_alice)) == 3
    assert contract.get_claimable_balance(_hex(direct_bob)) == 3
    assert contract.get_claimable_balance(_hex(direct_charlie)) == 3
    # Collateral drops by exactly the distributed 9 (dust 1 retained), and the
    # incident records the real slashed amount (9), never the proposed 10.
    assert contract.get_policy(pid)["collateral_pool"] == 991
    assert contract.get_incident(1)["slashed_amount"] == 9


def test_view_methods_reject_unknown_ids(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown policy"):
        contract.get_policy(7)
    with direct_vm.expect_revert("Unknown policy"):
        contract.get_compliance_metrics(7)
    with direct_vm.expect_revert("Unknown incident"):
        contract.get_incident(7)
    # Unknown account simply reads a zero balance (no revert).
    assert contract.get_claimable_balance(_hex(direct_alice)) == 0


def test_evaluate_unknown_policy(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown policy"):
        contract.evaluate_service_health(42)
