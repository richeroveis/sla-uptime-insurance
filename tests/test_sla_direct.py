"""Direct-mode tests for SLAUptimeInsurance.

Fast, in-memory (no server). Validates input guards, subscriber accounting, the
slash math for each health outcome, payout distribution, compensation claims, the
provider collateral withdrawal path, and the griefing guards added in the
hardening pass. Web render plus the LLM classification are mocked; the
equivalence-principle validator path is exercised by the integration tests, not
here.
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


def test_register_status_url_scheme_and_length_bounds(
    direct_vm, direct_deploy, direct_alice
):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice

    # Wrong scheme.
    with direct_vm.expect_revert("must be http(s)"):
        contract.register_policy("svc", "ftp://bad.example.com", 9990, 1000, 200)

    # Too short (below the 8 char floor).
    with direct_vm.expect_revert("8..512"):
        contract.register_policy("svc", "http://", 9990, 1000, 200)

    # Too long (above the 512 char ceiling).
    with direct_vm.expect_revert("8..512"):
        contract.register_policy("svc", "https://" + "a" * 600, 9990, 1000, 200)

    # A valid boundary registration still succeeds after the reverts.
    assert contract.register_policy("svc", STATUS_URL, 9990, 1000, 200) == 1


def test_register_status_url_whitespace_rejected(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice

    # Embedded space.
    with direct_vm.expect_revert("whitespace"):
        contract.register_policy("svc", "https://exa mple.com/health", 9990, 1000, 200)

    # Embedded newline.
    with direct_vm.expect_revert("whitespace"):
        contract.register_policy("svc", "https://example.com/\nhealth", 9990, 1000, 200)


def test_register_service_name_length_cap(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice

    # service_name is capped at 64 characters.
    with direct_vm.expect_revert("service_name"):
        contract.register_policy("s" * 65, STATUS_URL, 9990, 1000, 200)

    # Empty after stripping is rejected too.
    with direct_vm.expect_revert("service_name"):
        contract.register_policy("   ", STATUS_URL, 9990, 1000, 200)

    # Exactly 64 characters is accepted.
    assert contract.register_policy("n" * 64, STATUS_URL, 9990, 1000, 200) == 1


def test_register_numeric_bounds(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice

    # target_uptime_bps must sit in 1..10000.
    with direct_vm.expect_revert("target_uptime_bps"):
        contract.register_policy("svc", STATUS_URL, 10001, 1000, 200)
    with direct_vm.expect_revert("target_uptime_bps"):
        contract.register_policy("svc", STATUS_URL, 0, 1000, 200)

    # collateral_amount must sit in 1..1e30.
    with direct_vm.expect_revert("collateral_amount"):
        contract.register_policy("svc", STATUS_URL, 9990, 0, 200)
    with direct_vm.expect_revert("collateral_amount"):
        contract.register_policy("svc", STATUS_URL, 9990, 10**30 + 1, 200)

    # slash_rate_bps must sit in 1..5000.
    with direct_vm.expect_revert("slash_rate_bps"):
        contract.register_policy("svc", STATUS_URL, 9990, 1000, 0)
    with direct_vm.expect_revert("slash_rate_bps"):
        contract.register_policy("svc", STATUS_URL, 9990, 1000, 5001)

    # The 5000 bps ceiling itself is allowed.
    assert contract.register_policy("svc", STATUS_URL, 9990, 1000, 5000) == 1


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


def test_evaluate_operational_no_slash(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)  # the guard needs at least one subscriber

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


def test_evaluate_zero_subscribers_reverts(direct_vm, direct_deploy, direct_alice):
    # Griefing guard: an outage on a policy with no insured subscriber must not
    # burn collateral, so evaluation reverts before it reaches the nondet block.
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)

    _mock_health(direct_vm, "MAJOR_OUTAGE", 100, True)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("no subscribers"):
        contract.evaluate_service_health(pid)

    # No state moved: collateral is intact and no incident was recorded.
    assert contract.get_policy(pid)["collateral_pool"] == 1000
    assert contract.get_policy(pid)["total_checks"] == 0


def test_evaluate_inactive_policy_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)

    # Provider withdraws, which flips active to False.
    direct_vm.sender = direct_alice
    contract.deactivate_and_withdraw_collateral(pid)

    _mock_health(direct_vm, "MAJOR_OUTAGE", 100, True)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("inactive"):
        contract.evaluate_service_health(pid)


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


def test_deactivate_withdraw_returns_collateral_to_provider(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    # Locked-funds fix: the provider can deactivate and pull the remaining
    # collateral back through the same claim path subscribers use.
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice, collateral=1000)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)

    direct_vm.sender = direct_alice
    withdrawn = contract.deactivate_and_withdraw_collateral(pid)
    assert withdrawn == 1000

    policy = contract.get_policy(pid)
    assert policy["collateral_pool"] == 0
    assert policy["active"] is False

    # The collateral becomes the provider's claimable balance, not new minted
    # units: total claimable equals what left the pool.
    assert contract.get_claimable_balance(_hex(direct_alice)) == 1000
    assert contract.claim_compensation() == 1000
    assert contract.get_claimable_balance(_hex(direct_alice)) == 0


def test_deactivate_partial_after_slash(direct_vm, direct_deploy, direct_alice, direct_bob):
    # Withdraw returns only what is left after an earlier slash, conserving value.
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice, collateral=1000)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)

    _mock_health(direct_vm, "MAJOR_OUTAGE", 100, True)  # slashes 20
    direct_vm.sender = direct_alice
    contract.evaluate_service_health(pid)

    direct_vm.sender = direct_alice
    assert contract.deactivate_and_withdraw_collateral(pid) == 980
    assert contract.get_policy(pid)["collateral_pool"] == 0


def test_deactivate_non_provider_rejected(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("provider"):
        contract.deactivate_and_withdraw_collateral(pid)

    # Rejection leaves the policy fully intact.
    assert contract.get_policy(pid)["collateral_pool"] == 1000
    assert contract.get_policy(pid)["active"] is True


def test_deactivate_twice_rejected(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)

    direct_vm.sender = direct_alice
    assert contract.deactivate_and_withdraw_collateral(pid) == 1000

    with direct_vm.expect_revert("inactive"):
        contract.deactivate_and_withdraw_collateral(pid)


def test_deactivate_unknown_policy_reverts(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown policy"):
        contract.deactivate_and_withdraw_collateral(4242)


def test_incident_summary_truncated_to_cap(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)

    _mock_health(direct_vm, "OPERATIONAL", 0, False, summary="y" * 500)
    direct_vm.sender = direct_alice
    contract.evaluate_service_health(pid)

    # The stored summary is clamped to the 300 character ceiling.
    assert len(contract.get_incident(1)["summary"]) == 300


def test_evaluate_unknown_policy(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown policy"):
        contract.evaluate_service_health(42)


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
