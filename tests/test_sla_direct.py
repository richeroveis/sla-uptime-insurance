"""Direct-mode tests for SLAUptimeInsurance.

Fast, in-memory (no server). Validates input guards, subscriber accounting, the
ONE enforced SLA-breach rule (compliance and slashing both derive from comparing
the recorded verified_uptime_bps against target_uptime_bps), the settlement /
compensation payout math, collateral conservation, the provider exit path and the
griefing/authorization guards. Web render plus the LLM classification are mocked;
the equivalence-principle validator path is exercised by the integration tests, not
here.
"""

import json
import re


CONTRACT = "contracts/sla_uptime_insurance.py"
STATUS_URL = "https://status.example.com/v1/health"


def _hex(addr) -> str:
    """Normalise a direct-mode test address (raw bytes) to a 0x hex string."""
    if isinstance(addr, (bytes, bytearray)):
        return "0x" + bytes(addr).hex()
    return str(addr)


def _register(contract, vm, provider, collateral=1000, target=9990):
    vm.sender = provider
    return contract.register_policy("payments-api", STATUS_URL, target, collateral)


def _mock_health(vm, status_code, verified_uptime_bps, service_name="payments-api", summary="auto"):
    """Mock the live status page (web render) and the LLM classification.

    The LLM mock is keyed to the policy's unique ``service_name`` so that several
    policies audited inside one test each resolve their own measured uptime,
    because the direct-mode mock registry keeps the first matching pattern.
    """
    vm.mock_web(r".*status\.example\.com.*", {"status": 200, "body": "Service status page"})
    payload = {
        "status_code": status_code,
        "verified_uptime_bps": verified_uptime_bps,
        "summary": summary,
    }
    vm.mock_llm(rf".*{re.escape(service_name)}.*", json.dumps(payload))


def _audited_policy(contract, vm, provider, subscriber, target, verified, collateral=1_000_000, status="OPERATIONAL", summary="auto"):
    """Register + subscribe + run the consensus audit so the policy has a recorded
    verified_uptime_bps. Returns the policy_id. Leaves sender on the provider.

    Each policy carries a unique ``service_name`` so its LLM mock is distinct.
    """
    service_name = f"svc-{target}-{verified}"
    vm.sender = provider
    pid = contract.register_policy(service_name, STATUS_URL, target, collateral)
    vm.sender = subscriber
    contract.subscribe_to_policy(pid)
    _mock_health(vm, status, verified, service_name=service_name, summary=summary)
    vm.sender = provider
    contract.evaluate_service_health(pid)
    return pid


# --- registration & input validation ---------------------------------------


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
    assert policy["verified_uptime_bps"] == 0
    assert policy["total_checks"] == 0
    assert policy["failed_checks"] == 0
    assert policy["evaluated"] is False
    assert policy["sla_breached"] is False
    assert policy["settled"] is False
    assert policy["active"] is True
    assert policy["provider"].lower() == _hex(direct_alice).lower()

    # Next id increments monotonically.
    direct_vm.sender = direct_alice
    pid2 = contract.register_policy("other", STATUS_URL, 9990, 500)
    assert pid2 == 2


def test_register_status_url_scheme_and_length_bounds(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice

    # Wrong scheme.
    with direct_vm.expect_revert("must be http(s)"):
        contract.register_policy("svc", "ftp://bad.example.com", 9990, 1000)

    # Too short (below the 8 char floor).
    with direct_vm.expect_revert("8..512"):
        contract.register_policy("svc", "http://", 9990, 1000)

    # Too long (above the 512 char ceiling).
    with direct_vm.expect_revert("8..512"):
        contract.register_policy("svc", "https://" + "a" * 600, 9990, 1000)

    # A valid boundary registration still succeeds after the reverts.
    assert contract.register_policy("svc", STATUS_URL, 9990, 1000) == 1


def test_register_status_url_whitespace_rejected(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice

    # Embedded space.
    with direct_vm.expect_revert("whitespace"):
        contract.register_policy("svc", "https://exa mple.com/health", 9990, 1000)

    # Embedded newline.
    with direct_vm.expect_revert("whitespace"):
        contract.register_policy("svc", "https://example.com/\nhealth", 9990, 1000)


def test_register_service_name_length_cap(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice

    with direct_vm.expect_revert("service_name"):
        contract.register_policy("s" * 65, STATUS_URL, 9990, 1000)

    with direct_vm.expect_revert("service_name"):
        contract.register_policy("   ", STATUS_URL, 9990, 1000)

    assert contract.register_policy("n" * 64, STATUS_URL, 9990, 1000) == 1


def test_register_numeric_bounds(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice

    # target_uptime_bps must sit in 1..10000.
    with direct_vm.expect_revert("target_uptime_bps"):
        contract.register_policy("svc", STATUS_URL, 10001, 1000)
    with direct_vm.expect_revert("target_uptime_bps"):
        contract.register_policy("svc", STATUS_URL, 0, 1000)

    # collateral_amount must sit in 1..1e30.
    with direct_vm.expect_revert("collateral_amount"):
        contract.register_policy("svc", STATUS_URL, 9990, 0)
    with direct_vm.expect_revert("collateral_amount"):
        contract.register_policy("svc", STATUS_URL, 9990, 10**30 + 1)

    # The 10000 target ceiling itself is allowed.
    assert contract.register_policy("svc", STATUS_URL, 10000, 1000) == 1


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


# --- the audit step records state but never moves money --------------------


def test_evaluate_operational_records_compliant_state_no_slash(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice, collateral=1000, target=9900)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)

    _mock_health(direct_vm, "OPERATIONAL", 10000)
    direct_vm.sender = direct_alice
    status = contract.evaluate_service_health(pid)
    assert status == "OPERATIONAL"

    policy = contract.get_policy(pid)
    assert policy["total_checks"] == 1
    assert policy["verified_uptime_bps"] == 10000
    assert policy["evaluated"] is True
    assert policy["sla_breached"] is False
    # The audit never touches the pool.
    assert policy["collateral_pool"] == 1000

    assert contract.is_sla_breached(pid) is False
    metrics = contract.get_compliance_metrics(pid)
    assert metrics["verified_uptime_bps"] == 10000
    assert metrics["compliant"] is True


def test_evaluate_unknown_policy(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown policy"):
        contract.evaluate_service_health(42)


def test_evaluate_zero_subscribers_reverts(direct_vm, direct_deploy, direct_alice):
    # Griefing guard: an audit on a policy with no insured subscriber reverts.
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)

    _mock_health(direct_vm, "MAJOR_OUTAGE", 0)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("no subscribers"):
        contract.evaluate_service_health(pid)

    assert contract.get_policy(pid)["collateral_pool"] == 1000
    assert contract.get_policy(pid)["total_checks"] == 0


def test_evaluate_inactive_policy_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)

    direct_vm.sender = direct_alice
    contract.deactivate_and_withdraw_collateral(pid)

    _mock_health(direct_vm, "MAJOR_OUTAGE", 0)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("inactive"):
        contract.evaluate_service_health(pid)


def test_incident_summary_truncated_to_cap(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _audited_policy(contract, direct_vm, direct_alice, direct_bob, target=9900, verified=10000, summary="y" * 500)

    assert len(contract.get_incident(1)["summary"]) == 300
    assert contract.get_incident(1)["verified_uptime_bps"] == 10000
    assert contract.get_incident(1)["sla_breached"] is False


# --- Joaquin's explicit requirement: compliant policies are NOT slashed ----


def test_policy_within_sla_target_is_not_slashed(direct_vm, direct_deploy, direct_alice, direct_bob):
    collateral = 1_000_000
    target = 9900

    # Measured uptime strictly above target (9950 >= 9900) is COMPLIANT.
    contract = direct_deploy(CONTRACT)
    pid = _audited_policy(contract, direct_vm, direct_alice, direct_bob, target, 9950, collateral)
    assert contract.is_sla_breached(pid) is False
    assert contract.get_compliance_metrics(pid)["compliant"] is True

    # Settlement of a compliant policy slashes ZERO and refunds the whole pool.
    direct_vm.sender = direct_alice
    slashed = contract.settle_policy(pid)
    assert slashed == 0

    settle = contract.get_settlement(pid)
    assert settle["outcome"] == "COMPLIANT"
    assert settle["shortfall_bps"] == 0
    assert settle["slash_amount"] == 0
    assert settle["provider_refund"] == collateral

    # 100% of collateral returns to the provider; the subscriber receives nothing.
    assert contract.get_claimable_balance(_hex(direct_alice)) == collateral
    assert contract.get_claimable_balance(_hex(direct_bob)) == 0
    assert contract.get_policy(pid)["collateral_pool"] == 0


def test_policy_on_sla_target_boundary_is_compliant_and_cannot_be_slashed(direct_vm, direct_deploy, direct_alice, direct_bob):
    collateral = 1_000_000
    target = 9900

    # Two boundary/at-target policies on one contract, both COMPLIANT.
    contract = direct_deploy(CONTRACT)
    # Exactly on target (9900 >= 9900).
    pid_on = _audited_policy(contract, direct_vm, direct_alice, direct_bob, target, 9900, collateral)
    # Well above target (9990 >= 9900).
    pid_above = _audited_policy(contract, direct_vm, direct_alice, direct_bob, target, 9990, collateral)

    assert contract.is_sla_breached(pid_on) is False
    assert contract.is_sla_breached(pid_above) is False

    # The subscriber-initiated compensation path refuses to slash a compliant policy.
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("SLA target met: policy is compliant and cannot be slashed"):
        contract.claim_sla_compensation(pid_on)

    # Settlement of the on-target policy still slashes nothing.
    direct_vm.sender = direct_alice
    assert contract.settle_policy(pid_on) == 0
    assert contract.get_settlement(pid_on)["provider_refund"] == collateral


# --- a true breach below target is slashed and compensated -----------------


def test_true_sla_breach_below_target_is_slashed_and_compensated(direct_vm, direct_deploy, direct_alice, direct_bob):
    collateral = 1_000_000
    target = 9900
    verified = 9500

    contract = direct_deploy(CONTRACT)
    pid = _audited_policy(contract, direct_vm, direct_alice, direct_bob, target, verified, collateral)

    assert contract.is_sla_breached(pid) is True
    metrics = contract.get_compliance_metrics(pid)
    assert metrics["compliant"] is False
    assert metrics["shortfall_bps"] == target - verified  # 400

    direct_vm.sender = direct_alice
    slashed = contract.settle_policy(pid)

    # Exact shortfall formula: collateral * shortfall_bps / 10000.
    expected_slash = (collateral * (target - verified)) // 10000  # 40000
    assert slashed == expected_slash
    assert slashed > 0

    settle = contract.get_settlement(pid)
    assert settle["outcome"] == "BREACHED"
    assert settle["shortfall_bps"] == 400
    assert settle["slash_amount"] == expected_slash
    assert settle["provider_refund"] == collateral - expected_slash  # 960000

    # The single subscriber is compensated the full slash; provider keeps refund.
    assert contract.get_claimable_balance(_hex(direct_bob)) == expected_slash
    assert contract.get_claimable_balance(_hex(direct_alice)) == collateral - expected_slash

    # Collateral is conserved exactly: subscribers + provider == original pool.
    assert contract.get_policy(pid)["collateral_pool"] == 0
    assert contract.get_policy(pid)["settled"] is True
    assert contract.get_policy(pid)["active"] is False
    total = (
        contract.get_claimable_balance(_hex(direct_bob))
        + contract.get_claimable_balance(_hex(direct_alice))
    )
    assert total == collateral


def test_breach_compensation_path_slashes_and_multi_subscriber_distribution(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    # The subscriber-initiated claim path slashes a real breach and splits it.
    collateral = 1_000_000
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    pid = contract.register_policy("payments-api", STATUS_URL, 9900, collateral)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)
    direct_vm.sender = direct_charlie
    contract.subscribe_to_policy(pid)

    _mock_health(direct_vm, "DEGRADED", 9000)  # shortfall 900 -> slash 90000
    direct_vm.sender = direct_alice
    contract.evaluate_service_health(pid)

    assert contract.is_sla_breached(pid) is True
    direct_vm.sender = direct_bob
    slashed = contract.claim_sla_compensation(pid)
    assert slashed == (collateral * 900) // 10000  # 90000

    # Even split across the two subscribers.
    assert contract.get_claimable_balance(_hex(direct_bob)) == 45000
    assert contract.get_claimable_balance(_hex(direct_charlie)) == 45000
    assert contract.get_claimable_balance(_hex(direct_alice)) == collateral - 90000  # provider refund 910000
    assert contract.get_policy(pid)["collateral_pool"] == 0


def test_compensation_claim_requires_evaluation(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)  # never evaluated
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("has not been evaluated"):
        contract.claim_sla_compensation(pid)


# --- is_sla_breached and settlement obey the identical predicate -----------


def test_is_sla_breached_and_settlement_use_identical_target_uptime_predicate(direct_vm, direct_deploy, direct_alice, direct_bob):
    collateral = 1_000_000
    # (target, verified) pairs straddling the boundary in both directions.
    pairs = [
        (9900, 9950),
        (9900, 9900),
        (9900, 9990),
        (9900, 10000),
        (9900, 9500),
        (9900, 5000),
        (9990, 9990),
        (9990, 9989),
        (10000, 10000),
        (10000, 9999),
        (9900, 0),
    ]
    contract = direct_deploy(CONTRACT)
    for target, verified in pairs:
        pid = _audited_policy(contract, direct_vm, direct_alice, direct_bob, target, verified, collateral)

        breached = contract.is_sla_breached(pid)
        # The stored compliance mirror and the metrics view agree with the predicate.
        assert contract.get_policy(pid)["sla_breached"] is breached
        assert contract.get_compliance_metrics(pid)["compliant"] is (not breached)

        direct_vm.sender = direct_alice
        slashed = contract.settle_policy(pid)

        # Core equivalence Joaquin asked for: breach iff the provider is slashed.
        assert breached == (slashed > 0)

        settle = contract.get_settlement(pid)
        # The settlement outcome string agrees with the predicate too.
        assert (settle["outcome"] == "BREACHED") is breached
        # Collateral is conserved for this policy: slash + refund == original pool.
        assert settle["slash_amount"] + settle["provider_refund"] == collateral

        # When breached, the slash is exactly the shortfall formula below target.
        if breached:
            assert settle["shortfall_bps"] == target - verified
            expected = (collateral * (target - verified)) // 10000
            if expected == 0:
                expected = 1  # a confirmed breach always costs at least one unit
            assert slashed == min(expected, collateral)
        else:
            assert slashed == 0
            assert settle["shortfall_bps"] == 0
            assert settle["provider_refund"] == collateral


# --- settlement guards, conservation and claim semantics -------------------


def test_double_settle_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _audited_policy(contract, direct_vm, direct_alice, direct_bob, 9900, 9500)
    direct_vm.sender = direct_alice
    contract.settle_policy(pid)

    with direct_vm.expect_revert("already settled"):
        contract.settle_policy(pid)
    with direct_vm.expect_revert("already settled"):
        contract.claim_sla_compensation(pid)


def test_settle_unknown_policy_reverts(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown policy"):
        contract.settle_policy(123)


def test_claim_compensation_resets_balance(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _audited_policy(contract, direct_vm, direct_alice, direct_bob, 9900, 9500, collateral=1_000_000)

    direct_vm.sender = direct_alice
    contract.settle_policy(pid)

    direct_vm.sender = direct_bob
    claimed = contract.claim_compensation()
    assert claimed == 40000  # single subscriber receives the full slash
    assert contract.get_claimable_balance(_hex(direct_bob)) == 0

    # Reclaim is a no-op once reset.
    assert contract.claim_compensation() == 0


def test_deactivate_withdraw_returns_collateral_to_provider(direct_vm, direct_deploy, direct_alice, direct_bob):
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
    assert contract.get_claimable_balance(_hex(direct_alice)) == 1000
    assert contract.claim_compensation() == 1000
    assert contract.get_claimable_balance(_hex(direct_alice)) == 0


def test_deactivate_blocks_provider_from_dodging_a_confirmed_breach(direct_vm, direct_deploy, direct_alice, direct_bob):
    # A breached policy cannot be exited by the provider to dodge the slash; the
    # only exit is settlement, which applies the predicate first.
    contract = direct_deploy(CONTRACT)
    pid = _audited_policy(contract, direct_vm, direct_alice, direct_bob, 9900, 9500, collateral=1_000_000)
    assert contract.is_sla_breached(pid) is True

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("in confirmed SLA breach"):
        contract.deactivate_and_withdraw_collateral(pid)

    # Withdrawal was refused, so the pool is untouched and settlement still slashes.
    assert contract.get_policy(pid)["collateral_pool"] == 1_000_000
    assert contract.settle_policy(pid) > 0


def test_deactivate_allows_compliant_provider_exit(direct_vm, direct_deploy, direct_alice, direct_bob):
    # A compliant (at/above target) policy is never slashed and can be exited.
    contract = direct_deploy(CONTRACT)
    pid = _audited_policy(contract, direct_vm, direct_alice, direct_bob, 9900, 9950, collateral=1_000_000)
    direct_vm.sender = direct_alice
    assert contract.deactivate_and_withdraw_collateral(pid) == 1_000_000
    assert contract.get_claimable_balance(_hex(direct_bob)) == 0


def test_settler_must_be_a_stakeholder(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    # charlie is neither the provider nor a subscriber, so cannot force a settlement.
    contract = direct_deploy(CONTRACT)
    pid = _audited_policy(contract, direct_vm, direct_alice, direct_bob, 9900, 9500, collateral=1_000_000)

    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("Only the provider or a subscriber may settle"):
        contract.settle_policy(pid)
    with direct_vm.expect_revert("Only a subscriber may claim compensation"):
        contract.claim_sla_compensation(pid)

    # Nothing moved; the insured subscriber still can.
    assert contract.get_policy(pid)["collateral_pool"] == 1_000_000
    direct_vm.sender = direct_bob
    assert contract.claim_sla_compensation(pid) > 0


def test_deactivate_non_provider_rejected(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT)
    pid = _register(contract, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    contract.subscribe_to_policy(pid)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("provider"):
        contract.deactivate_and_withdraw_collateral(pid)

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


def test_breach_minimum_slash_on_tiny_collateral(direct_vm, direct_deploy, direct_alice, direct_bob):
    # Even a breach with a 1-unit shortfall on a small pool slashes at least one
    # unit, so is_sla_breached and slash_amount > 0 stay equivalent.
    contract = direct_deploy(CONTRACT)
    pid = _audited_policy(contract, direct_vm, direct_alice, direct_bob, target=9900, verified=9899, collateral=500)
    direct_vm.sender = direct_alice
    slashed = contract.settle_policy(pid)
    # collateral * 1 // 10000 == 0, so the floor lifts it to 1.
    assert slashed == 1
    assert contract.is_sla_breached(pid) is True
    assert contract.get_claimable_balance(_hex(direct_bob)) == 1
    assert contract.get_claimable_balance(_hex(direct_alice)) == 499


def test_view_methods_reject_unknown_ids(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown policy"):
        contract.get_policy(7)
    with direct_vm.expect_revert("Unknown policy"):
        contract.get_compliance_metrics(7)
    with direct_vm.expect_revert("Unknown policy"):
        contract.is_sla_breached(7)
    with direct_vm.expect_revert("Unknown incident"):
        contract.get_incident(7)
    with direct_vm.expect_revert("No settlement"):
        contract.get_settlement(7)
    # Unknown account simply reads a zero balance (no revert).
    assert contract.get_claimable_balance(_hex(direct_alice)) == 0
