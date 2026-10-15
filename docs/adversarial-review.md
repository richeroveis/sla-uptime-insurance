# Adversarial security review: `SLAUptimeInsurance`

Reviewed with the contract-adversarial-review method. Assumption: every caller is
adversarial (providers, subscribers, anyone who can trigger an evaluation, and
anyone who can influence the fetched status page). This document records the
state machine, the invariant, every finding, and the test that proves each fix.

Status legend: FIXED means the code change is in
`contracts/sla_uptime_insurance.py`, lint is clean, and a named direct test in
`tests/test_sla_direct.py` asserts the behavior. OPEN would mean still to do.

## 1. State machine

A policy has one boolean lifecycle flag, `active`.

| Status | Method | Who may call | Next status | Effect on funds |
|--------|--------|--------------|-------------|-----------------|
| (none) | `register_policy` | any account (becomes provider) | active | locks `collateral_amount` into the pool |
| active | `subscribe_to_policy` | any account | active | none (adds a protected subscriber) |
| active, subscribers > 0 | `evaluate_service_health` | any account | active | on DEGRADED/MAJOR_OUTAGE: slashes, credits subscribers |
| active | `deactivate_and_withdraw_collateral` | provider only | inactive | moves remaining pool to provider claimable, zeroes pool |
| inactive | `evaluate_service_health` | rejected | inactive | none |
| inactive | `deactivate_and_withdraw_collateral` | rejected | inactive | none |
| any | `claim_compensation` | the balance owner | unchanged | pays out and zeroes the caller claimable |

## 2. Liveness: no stuck funds

Every unit of `collateral_pool` has exactly one exit:
- it is credited to a subscriber through a consensus slash, or
- the provider withdraws the remainder through `deactivate_and_withdraw_collateral`,
  after which it sits in `claimable_balances[provider]` and is pulled by
  `claim_compensation`.

The old contract had no provider exit, so a policy that never suffered an outage
trapped its collateral forever (finding F1). The withdraw path removes that.
There is no other terminal state that can hold value.

## 3. Race conditions between competing transitions

The two methods that move value out of `collateral_pool` are
`evaluate_service_health` and `deactivate_and_withdraw_collateral`. Their guards
are mutually exclusive on `active` and the state write is committed in one
transition each:

- evaluate requires `active` and `subscriber_count > 0`; it reads the pool,
  computes the slash, deducts only the distributed amount, and writes the policy
  back once.
- withdraw requires `active` and `sender == provider`; it reads the pool once,
  credits that exact amount to the provider, zeroes the pool, and flips
  `active = False` in the same transition.

A withdraw cannot double-pay: the second call sees `active == False` and reverts
(`test_deactivate_twice_rejected`). A non-provider cannot front-run a provider
withdraw (`test_deactivate_non_provider_rejected`, and on chain the
`nonprovider_withdraw` transaction rolls back). An evaluate after a withdraw
reverts because the policy is inactive (`test_evaluate_inactive_policy_reverts`).

## 4. Accounting invariant

Invariant, enforced in code and tests:

    the only decrease of collateral_pool equals the increase of claimable_balances
    (subscribers on a slash, or the provider on a withdrawal); nothing is minted.

- Slash distribution deducts `per_sub * subscriber_count` (the amount actually
  credited), never the proposed slash, so rounding dust stays in the pool and is
  never invented (`test_evaluate_rounding_dust_conserves_collateral`: 10 proposed
  across 3 subscribers moves exactly 9, pool drops 9, the incident records 9).
- Withdraw credits exactly `policy.collateral_pool` then zeroes it in the same
  step (`test_deactivate_withdraw_returns_collateral_to_provider`, and
  `test_deactivate_partial_after_slash` after a prior slash).
- `claim_compensation` zeroes before returning, so a claim cannot replay
  (`test_claim_compensation_resets_balance`).

## 5. Input validation at every entry point

| Input | Bounds now enforced | Proving test |
|-------|---------------------|--------------|
| `service_name` | 1..64 chars after strip | `test_register_service_name_length_cap` |
| `status_url` | 8..512 chars | `test_register_status_url_scheme_and_length_bounds` |
| `status_url` | must start with `http://` or `https://` | same |
| `status_url` | no whitespace or newline anywhere | `test_register_status_url_whitespace_rejected` |
| `target_uptime_bps` | 0 < x <= 10000 | `test_register_numeric_bounds` |
| `slash_rate_bps` | 0 < x <= 5000 | `test_register_numeric_bounds` |
| `collateral_amount` | 0 < x <= 10^30 | `test_register_numeric_bounds` |
| `summary` | truncated to 300 chars | `test_incident_summary_truncated_to_cap` |
| fetched page | 6000 char window fed to the prompt | enforced in `evaluate_service_health` |

Validation rejects (reverts) rather than clamps, except the documented `summary`
truncation and the fetched-page window, both of which only shorten free text that
is excluded from consensus.

## 6. Consensus safety

- The slash amount is never taken from the LLM. `severity_score` is derived
  deterministically from the canonical `status_code` (`OPERATIONAL` 0, `DEGRADED`
  50, `MAJOR_OUTAGE` 100), and all money math is integer arithmetic on chain.
- `gl.eq_principle.prompt_comparative` makes each validator re-fetch the same
  live page and agree exactly on `status_code`, `severity_score` and
  `is_sla_breached`; free-text summary is intentionally not compared.
- Unreachable or empty pages raise `[TRANSIENT]` so consensus rotates instead of
  persisting a fabricated reading.
- Fetched text and the LLM answer are treated as untrusted: the page is windowed
  and the classification is normalized through `_parse_health`, which rejects any
  value outside the fixed enumeration. A page that injects prompt text cannot
  change the verdict rule, only the (uncompared) summary wording.

Honest limitation: direct-mode tests mock the fetch and the LLM, so they exercise
the leader path only. They do not prove validator agreement. That is covered by
the live `studionet` integration run.

## 7. Findings

| ID | Class | Before | After | Status |
|----|-------|--------|-------|--------|
| F1 | Locked funds | No provider exit; a healthy policy trapped collateral forever | `deactivate_and_withdraw_collateral` returns the remainder to the provider claimable | FIXED, proven by `test_deactivate_withdraw_returns_collateral_to_provider`, `test_deactivate_partial_after_slash` |
| F2 | Griefing / wasted collateral | `evaluate_service_health` slashed pool with zero subscribers (a pure burn) | Evaluation reverts unless `subscriber_count > 0` | FIXED, proven by `test_evaluate_zero_subscribers_reverts` |
| F3 | Input hygiene | `service_name` up to 128, `status_url` up to 2048 with no whitespace rule, `summary` cap 280, `slash_rate` cap 500 but a separate 10000 check, collateral 10^24, page window 4000 | Caps set to 1..64, 8..512 plus no whitespace/newline, 300, a single 0<x<=5000 slash bound, collateral <=10^30, page window 6000 | FIXED, proven by the input-validation tests in section 5 |
| F4 | Authorization | n/a (new path) | Withdraw is provider-only and active-only | FIXED, proven by `test_deactivate_non_provider_rejected`, `test_deactivate_twice_rejected`, `test_deactivate_unknown_policy_reverts` |
| F5 | Race / double action | n/a | Withdraw and evaluate are mutually exclusive on `active`; each commits state in one transition | FIXED, proven by `test_evaluate_inactive_policy_reverts` |
| F6 | Accounting (rounding) | already conserved (deduct only what is credited) | unchanged and re-verified | FIXED, proven by `test_evaluate_rounding_dust_conserves_collateral` |

No findings remain OPEN. The oracle trust boundary (a dishonest provider pointing
`status_url` at a permanently green page) is an economic assumption, not a code
flaw, and is documented in the README. It cannot be closed by validation because
the contract deliberately delegates "what does the page say" to consensus.

## Verification performed in this pass

- `genvm-lint check contracts/sla_uptime_insurance.py`: `ok: true`, 0 errors
  (9 methods: 5 write, 4 view).
- `pytest tests/test_sla_direct.py`: 22 passed, covering every guard above.
- Live `studionet` run and 3-way source integrity: see `evidence/studionet-deploy.json`
  and the README deployment section.
