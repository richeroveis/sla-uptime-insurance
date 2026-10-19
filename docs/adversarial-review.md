# Adversarial security review: `SLAUptimeInsurance`

Reviewed with the contract-adversarial-review method. Assumption: every caller is
adversarial (providers, subscribers, anyone who can trigger an evaluation or a
settlement, and anyone who can influence the fetched status page). This document
records the state machine, the invariant, every finding, and the test that proves
each fix.

Status legend: FIXED means the code change is in
`contracts/sla_uptime_insurance.py`, `genvm-lint` is clean, and a named direct test
in `tests/test_sla_direct.py` asserts the behavior. OPEN would mean still to do.

## 0. Steward fix: one coherent, enforced SLA-breach rule

Reviewer feedback (Joaquin): the settlement path must apply the declared
`target_uptime_bps` and the resulting compliance state consistently with
`is_sla_breached`, so a policy that is still within its SLA target is never
slashed and a true breach is.

Before this pass the contract had TWO different notions of "breach":

- `evaluate_service_health` slashed whenever the consensus status was DEGRADED or
  MAJOR_OUTAGE, scaled by a severity weight and a `slash_rate_bps`. That path
  never consulted `target_uptime_bps`.
- `get_compliance_metrics` defined compliance as `uptime_bps >= target_uptime_bps`.

A policy could be measured as COMPLIANT by one and slashed by the other. That is
the incoherence Joaquin flagged.

After this pass there is exactly ONE predicate, `_breach_for(evaluated,
verified_uptime_bps, target_uptime_bps)`, and it is the single source of truth for
compliance AND for every fund movement:

- COMPLIANT (`is_sla_breached == False`) when an evaluation has recorded
  `verified_uptime_bps >= target_uptime_bps`.
- BREACHED (`is_sla_breached == True`) ONLY when a recorded evaluation has
  `verified_uptime_bps < target_uptime_bps`.
- No evaluation recorded means "not breached"; nothing is slashed.

`is_sla_breached`, `get_compliance_metrics`, the audit step
(`evaluate_service_health`) and both settlement paths (`settle_policy` and
`claim_sla_compensation`) all call `_breach_for`. The audit step no longer moves
any funds; all value transfer happens at settlement, derived from the shortfall
`shortfall_bps = target_uptime_bps - verified_uptime_bps`. The per-incident
`slash_rate_bps`/severity mechanism that caused the mismatch was removed.

Proven by: `test_policy_within_sla_target_is_not_slashed`,
`test_policy_on_sla_target_boundary_is_compliant_and_cannot_be_slashed`,
`test_true_sla_breach_below_target_is_slashed_and_compensated`, and
`test_is_sla_breached_and_settlement_use_identical_target_uptime_predicate`.

## 1. State machine

A policy carries `active`, `evaluated` and `settled` flags.

| Status | Method | Who may call | Next status | Effect on funds |
|--------|--------|--------------|-------------|-----------------|
| (none) | `register_policy` | any account (becomes provider) | active, unevaluated, unsettled | locks `collateral_amount` into the pool |
| active | `subscribe_to_policy` | any account | active | none (adds a protected subscriber) |
| active, subscribers > 0, unsettled | `evaluate_service_health` | any account | active (records `verified_uptime_bps`, `sla_breached`) | NONE (audit only, no slash) |
| active, evaluated, unsettled | `settle_policy` | provider or subscriber | inactive, settled | COMPLIANT: 0 slash, full refund to provider. BREACHED: slash = `collateral * shortfall_bps / 10000` (min 1, cap collateral) split to subscribers, remainder refunded to provider |
| active, evaluated, unsettled | `claim_sla_compensation` | subscriber only | inactive, settled | Reverts if compliant ("SLA target met..."); on breach applies the same slash as `settle_policy` |
| active, unsettled | `deactivate_and_withdraw_collateral` | provider only | inactive | returns remaining pool to provider; REVERTS if the policy is in a confirmed breach |
| any | `claim_compensation` | the balance owner | unchanged | pays out and zeroes the caller's claimable |
| inactive or settled | evaluate / settle / claim / withdraw | rejected | unchanged | none |

## 2. Liveness: no stuck funds

Every unit of `collateral_pool` has exactly one of two exits, both available at all
times:
- settlement, which on a breach credits subscribers and refunds the remainder to
  the provider, and on a compliant policy refunds the whole pool to the provider;
  or
- `deactivate_and_withdraw_collateral` for a compliant or unevaluated policy, which
  returns the provider's own remaining collateral.

A breached policy intentionally CANNOT be withdrawn by the provider (finding F9):
its only exit is settlement, which pays the subscribers first. Settlement is
callable by any subscriber, so an adversary provider cannot freeze compensation by
refusing to settle. `claim_compensation` zeroes before returning, so a payout can
never be claimed twice. Nothing can hold value in a terminal state with no exit.

## 3. Race conditions between competing transitions

The methods that move value out of `collateral_pool` are `settle_policy`,
`claim_sla_compensation` and `deactivate_and_withdraw_collateral`. They are made
mutually exclusive by status flags and commit state in a single transition:

- Every settlement path first calls `_require_settleable` (rejects `settled`, then
  `inactive`), so the first mover wins and every later call reverts. A breach can
  be settled exactly once regardless of whether the provider or a subscriber
  triggers it (`test_double_settle_reverts`).
- A provider can neither deactivate out of a breach nor withdraw after settlement:
  the deactivate path recomputes the SAME predicate and reverts on a breach
  (`test_deactivate_blocks_provider_from_dodging_a_confirmed_breach`), and both
  settle paths mark `settled = True` and `active = False` atomically.
- After settlement the policy is inactive, so `evaluate_service_health` reverts
  (`test_evaluate_inactive_policy_reverts`).
- Non-provider withdraw is rejected by the caller check before any state read
  (`test_deactivate_non_provider_rejected`).

## 4. Accounting invariant

Invariant, enforced in code and tests:

    collateral_pool never decreases except by an amount that is credited in the
    SAME transition (slash to subscribers, refund to the provider); nothing is
    minted, nothing is burned, a claim never replays.

- On a breach settlement `slash_amount + provider_refund == collateral` exactly,
  because `_distribute` credits the whole slash (any indivisible remainder to the
  earliest subscribers) and the provider receives `collateral - slash`. No dust is
  minted or lost (the old "proposed vs moved" split was replaced by full
  distribution).
- On a compliant settlement the provider is refunded the full pool and subscribers
  receive 0, so `0 slash + full refund == collateral`.
- The minimum-one-unit slash on a tiny-collateral breach keeps the pool conserved
  (`test_breach_minimum_slash_on_tiny_collateral`: 1 slashed, 499 refunded).
- `claim_compensation` zeroes before returning (`test_claim_compensation_resets_balance`).
- The equivalence test asserts `slash_amount + provider_refund == collateral` for
  every one of its eleven boundary pairs.

## 5. Input validation at every entry point

| Input | Bounds enforced | Proving test |
|-------|-----------------|--------------|
| `service_name` | 1..64 chars after strip | `test_register_service_name_length_cap` |
| `status_url` | 8..512 chars | `test_register_status_url_scheme_and_length_bounds` |
| `status_url` | must start with `http://` or `https://` | same |
| `status_url` | no whitespace or newline anywhere | `test_register_status_url_whitespace_rejected` |
| `target_uptime_bps` | 0 < x <= 10000 | `test_register_numeric_bounds` |
| `collateral_amount` | 0 < x <= 10^30 | `test_register_numeric_bounds` |
| consensus `status_code` | one of OPERATIONAL/DEGRADED/MAJOR_OUTAGE or `[LLM_ERROR]` revert | `_normalize_status`, exercised via `_mock_health` |
| consensus `verified_uptime_bps` | clamped to 0..10000; unusable value falls back to the status-derived sample | `_coerce_uptime` |
| `summary` | truncated to 300 chars | `test_incident_summary_truncated_to_cap` |
| fetched page | 6000 char window fed to the prompt | enforced in `evaluate_service_health` |

Validation rejects (reverts) rather than clamps, except the documented `summary`
truncation, the fetched-page window, and the `verified_uptime_bps` clamp, none of
which let a caller push the measurement outside the basis-points domain.

## 6. Consensus safety (GenLayer)

- The slash is never taken from free-form LLM output. `_breach_for` compares the
  recorded `verified_uptime_bps` to `target_uptime_bps` and `_slash_from_shortfall`
  computes the penalty with pure integer arithmetic on chain.
- The measured uptime is the ONE numeric decision field crossing the determinism
  boundary. It is clamped to 0..10000 and is included in the fields compared by
  `gl.eq_principle.prompt_comparative` (agreement required on `status_code` AND
  `verified_uptime_bps`); the free-text `summary` is not compared. A validator that
  disagrees on either forces rotation rather than persisting a bad number, so a
  single node cannot set the measurement that drives the payout.
- Unreachable or empty pages raise `[TRANSIENT]` so consensus rotates instead of
  persisting a fabricated reading; a classification outside the fixed enumeration
  raises `[LLM_ERROR]`, which always disagrees and forces rotation.
- Fetched text and the LLM answer are untrusted: the page is windowed and the
  classification is normalized through `_parse_health`. Injected instructions
  inside the page can only change the (uncompared) summary wording, never the
  verdict rule, which is fixed code.

Honest limitation: direct-mode tests mock the fetch and the LLM, so they exercise
the leader path only. They prove the deterministic slash math and every guard, but
they do NOT prove validator agreement. Validator behavior and the live audit are
covered by the `studionet` integration run and the collected evidence.

## 7. Findings

| ID | Class | Before | After | Status |
|----|-------|--------|-------|--------|
| F1 | Locked funds | No provider exit; a healthy policy trapped collateral forever | `deactivate_and_withdraw_collateral` returns the remainder; settlement always refunds | FIXED, proven by `test_deactivate_withdraw_returns_collateral_to_provider`, `test_deactivate_allows_compliant_provider_exit` |
| F2 | Griefing / wasted collateral | Evaluation slashed with zero subscribers (a pure burn) | Audit reverts unless `subscriber_count > 0`; the audit no longer moves funds at all | FIXED, proven by `test_evaluate_zero_subscribers_reverts` |
| F3 | Input hygiene | Loose caps and no whitespace rule | 1..64 name, 8..512 http(s) URL with no whitespace, target <=10000, collateral <=10^30, uptime clamped 0..10000, summary 300, page window 6000 | FIXED, proven by the input-validation tests in section 5 |
| F4 | Authorization | n/a | Withdraw provider-only; settlement restricted to the provider or a subscriber; breach compensation restricted to subscribers | FIXED, proven by `test_deactivate_non_provider_rejected`, `test_settler_must_be_a_stakeholder` |
| F5 | Race / double action | n/a | All value paths share `_require_settleable`; settle-once via the `settled` flag | FIXED, proven by `test_double_settle_reverts`, `test_evaluate_inactive_policy_reverts` |
| F6 | Accounting (rounding) | proposed vs moved split left dust | Whole slash distributed (remainder to earliest subscribers); slash + refund conserved exactly | FIXED, proven by `test_breach_compensation_path_slashes_and_multi_subscriber_distribution`, `test_breach_minimum_slash_on_tiny_collateral`, `test_is_sla_breached_and_settlement_use_identical_target_uptime_predicate` |
| F7 | Settlement / compliance incoherence (steward) | Slash keyed to status label + `slash_rate_bps`; compliance keyed to `target_uptime_bps`; a compliant policy could be slashed and a breach could pass | One `_breach_for` predicate drives `is_sla_breached`, metrics, audit and both settlement paths; compliant => 0 slash + full refund; breach => shortfall-formula slash; the subscriber compensation path reverts on a compliant policy | FIXED, proven by `test_policy_within_sla_target_is_not_slashed`, `test_policy_on_sla_target_boundary_is_compliant_and_cannot_be_slashed`, `test_true_sla_breach_below_target_is_slashed_and_compensated`, `test_is_sla_breached_and_settlement_use_identical_target_uptime_predicate` |
| F8 | Outsider force-settle | n/a (settlement is new) | `settle_policy` / `claim_sla_compensation` require the caller to be the provider or an insured subscriber, so a third party cannot grief by closing a policy early | FIXED, proven by `test_settler_must_be_a_stakeholder` |
| F9 | Provider dodging a confirmed slash | Withdraw was always allowed, so a provider could exit before settlement and escape the slash | A policy in a confirmed breach cannot be withdrawn; settlement (which pays subscribers first) is its only exit | FIXED, proven by `test_deactivate_blocks_provider_from_dodging_a_confirmed_breach` |

No findings remain OPEN. The oracle trust boundary (a dishonest provider pointing
`status_url` at a permanently green page, or an LLM that cannot read a precise
uptime) is an economic/precision assumption, not a code flaw: every validator
re-fetches the page, the decision fields must agree exactly or consensus rotates, and
a value never moves without that agreement.

## Verification performed in this pass

- `genvm-lint check contracts/sla_uptime_insurance.py`: Lint passed, Validation
  passed, 13 methods (7 write, 6 view), 0 errors.
- `pytest tests/test_sla_direct.py`: 30 passed, covering the single-predicate
  coherence, compliant-not-slashed, true-breach slash, boundary pairs, settlement
  races, authorization, collateral conservation and every input guard.
- Live `studionet` run and 3-way source integrity: see `evidence/studionet-deploy.json`
  and the README deployment section.
