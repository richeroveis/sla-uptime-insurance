# SLAUptimeInsurance

A production-grade, standalone GenLayer Intelligent Contract primitive for
decentralized SLA (Service Level Agreement) uptime insurance. It monitors a
service provider's public status endpoint, reaches validator consensus on the
observed service uptime, and settles the outcome on chain against ONE enforced
SLA-breach rule: it slashes provider collateral only for a true breach, credits
compensation to protected subscribers, and writes an immutable record.

- Contract: [`contracts/sla_uptime_insurance.py`](contracts/sla_uptime_insurance.py)
- Network: GenLayer `studionet` (chain id `61999`)
- Deployed address: `0x397C73Fa3a192372A74BBFe7a063d15B260B7A7D`
- Runner: `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
- Deployed source tag: `studionet-v1` (the hardened `main` HEAD)

This is a pure contract primitive. There is no frontend by design; the value it
delivers is a reusable, composable, consensus-backed enforcement component that
other contracts and protocols can integrate.

---

## 1. Overview & Purpose

Machine-to-machine economies, DePIN networks and API marketplaces all sell
"guaranteed uptime", but almost none of them can *prove* downtime without a
trusted intermediary. A provider claims 99.9% availability, the customer takes
their word for it, and disputes are settled off chain, slowly, by whoever has
the better lawyers.

`SLAUptimeInsurance` turns that promise into an enforceable on-chain contract:

- A provider registers a service, declares a `target_uptime_bps` SLA goal, and
  locks a **collateral pool**.
- Anyone can **subscribe** to that policy as a protected consumer.
- The contract **evaluates** the provider's live status endpoint through
  GenLayer's equivalence principle so independent validators agree on the
  measured uptime. This audit records `verified_uptime_bps` and the resulting
  compliance state; it moves no money.
- **Settlement** applies the single SLA-breach rule: a policy still within its
  target is refunded in full (never slashed); a policy that breached its target
  is slashed by the shortfall and the slashed units are paid to subscribers.
- Subscribers **claim** their accrued compensation.

Why it is a primitive, not an app:
- It holds no real native value. It is an accounting and settlement engine
  (collateral units and claimable units) that an escrow, payment or oracle layer
  can wrap. That keeps the consensus surface small and the fund math auditable.
- It is provider-agnostic: one deployment serves any number of independent
  `SLAPolicy` records.
- Every decision that moves value is backed by a validator-agreed snapshot of
  real-world evidence, not a single caller's assertion.

---

## 2. The single SLA-breach rule

There is exactly one predicate, and it is the only place an uptime is ever
compared against a target:

```
COMPLIANT (is_sla_breached == False)  <=>  an evaluation is recorded and verified_uptime_bps >= target_uptime_bps
BREACHED  (is_sla_breached == True)   <=>  an evaluation is recorded and verified_uptime_bps <  target_uptime_bps
```

`is_sla_breached(policy_id)`, `get_compliance_metrics(policy_id)`, the audit step
`evaluate_service_health`, and both settlement paths `settle_policy` and
`claim_sla_compensation` all derive from this one comparison (the shared pure
helper `_breach_for`). The compensation for a breach is the shortfall-based
formula in section 3. A compliant (or not-yet-evaluated) policy can never be
slashed: settlement refunds 100% of the collateral to the provider, and any
attempt to slash it through `claim_sla_compensation` reverts with
`SLA target met: policy is compliant and cannot be slashed`. There is no owner or
admin override of `target_uptime_bps`.

This replaced an earlier design where the slash was keyed to a status label and a
per-incident `slash_rate_bps`, while compliance was keyed to `target_uptime_bps`,
so the two could disagree. That incoherence is fixed: settlement now uses the
declared `target_uptime_bps` and the resulting compliance state, consistently
with `is_sla_breached`. See `docs/adversarial-review.md` finding F7.

### Consensus & equivalence design

The hardest part is the boundary between the deterministic on-chain world and the
non-deterministic real world:

1. `gl.nondet.web.render(status_url, mode="text")` returns live content that can
   change between the leader's read and a validator's read.
2. `gl.nondet.exec_prompt(...)` classifies that text with an LLM.

`evaluate_service_health` reads `status_url` and `service_name` from storage
*before* entering the non-deterministic block, so no contract state is touched
inside it. Inside, a zero-argument closure fetches the live page and prompts the
LLM to return a canonical snapshot, normalized through `_parse_health`, which
snaps the status to one of the three allowed enumerations and clamps the measured
`verified_uptime_bps` to 0..10000. The snapshot runs through
`gl.eq_principle.prompt_comparative`, whose principle requires each validator to
re-fetch the same page and agree exactly on `status_code` AND `verified_uptime_bps`
(the free-text `summary` is deliberately not compared). A validator that reads a
different status or a materially different uptime votes `False` and forces
rotation, so no single node decides the measurement that later gates a payout.

Critically, the consensus step does NOT move money and does NOT decide the slash.
It only records a bounded measurement; the breach and the entire payout are
computed deterministically, on chain, by `_breach_for` and `_slash_from_shortfall`
from `target_uptime_bps`. Unreachable or empty pages raise `[TRANSIENT]` so
consensus rotates instead of persisting a fabricated reading.

| Prefix | Meaning | Consensus policy |
|--------|---------|------------------|
| `[EXPECTED]` | Deterministic business/validation failure | Must match exactly |
| `[TRANSIENT]` | Network/5xx/fetch failure | Agree only if both transient, else rotate |
| `[LLM_ERROR]` | LLM produced an unusable classification | Always disagree, force rotation |

---

## 3. State Architecture

All persistent state uses strict GenVM types. Python `dict` / `list` are never
stored; `TreeMap` and `DynArray` are.

### `SLAPolicy` (`@allow_storage @dataclass`)

| Field | Type | Meaning |
|-------|------|---------|
| `policy_id` | `u256` | Unique identifier (starts at 1) |
| `provider` | `Address` | Registrant (caller of `register_policy`) |
| `service_name` | `str` | Human label for the monitored service |
| `status_url` | `str` | http(s) endpoint fetched during evaluation |
| `target_uptime_bps` | `u256` | SLA target in basis points (9990 = 99.90%) |
| `collateral_pool` | `u256` | Remaining provider collateral (units) |
| `verified_uptime_bps` | `u256` | Last consensus-measured uptime (bps); 0 before any audit |
| `total_checks` | `u256` | Number of evaluations run |
| `failed_checks` | `u256` | Evaluations whose status was not OPERATIONAL |
| `evaluated` | `bool` | Whether an uptime has been recorded (required before any slash) |
| `sla_breached` | `bool` | Compliance mirror derived from the single predicate |
| `settled` | `bool` | Whether funds have been distributed and the policy closed |
| `active` | `bool` | Whether the policy is open for evaluation/settlement |

### `IncidentRecord` (`@allow_storage @dataclass`)

Written by the audit step (no funds move).

| Field | Type | Meaning |
|-------|------|---------|
| `incident_id` | `u256` | Unique incident identifier |
| `policy_id` | `u256` | Parent policy |
| `status_code` | `str` | `OPERATIONAL`, `DEGRADED`, or `MAJOR_OUTAGE` |
| `verified_uptime_bps` | `u256` | Measured uptime for this audit (bps) |
| `sla_breached` | `bool` | Whether this measurement breached the target |
| `summary` | `str` | Truncated evidence summary from the classification |

### `SettlementRecord` (`@allow_storage @dataclass`)

Written once, by settlement.

| Field | Type | Meaning |
|-------|------|---------|
| `policy_id` | `u256` | The settled policy (also the map key) |
| `outcome` | `str` | `COMPLIANT` or `BREACHED` |
| `target_uptime_bps` | `u256` | Declared target |
| `verified_uptime_bps` | `u256` | Recorded measured uptime |
| `shortfall_bps` | `u256` | `target - verified` when breached, else 0 |
| `slash_amount` | `u256` | Collateral slashed from the provider (0 when compliant) |
| `provider_refund` | `u256` | Collateral returned to the provider |
| `subscriber_count` | `u256` | Insured subscribers the slash was split across |

### Contract storage layout

| Field | Type | Purpose |
|-------|------|---------|
| `next_policy_id` | `u256` | Monotonic policy id counter |
| `next_incident_id` | `u256` | Monotonic incident id counter |
| `policies` | `TreeMap[u256, SLAPolicy]` | Policy records by id |
| `incidents` | `TreeMap[u256, IncidentRecord]` | Audit log by id |
| `settlements` | `TreeMap[u256, SettlementRecord]` | Final settlement, keyed by policy id |
| `policy_subscribers` | `TreeMap[u256, DynArray[Address]]` | Protected subscribers per policy |
| `claimable_balances` | `TreeMap[Address, u256]` | Accrued, unclaimed balance per account |

### Slash & refund math (deterministic, only at settlement)

With a recorded `verified_uptime_bps` and `target_uptime_bps`:

```
if verified_uptime_bps >= target_uptime_bps:        # COMPLIANT
    slash_amount   = 0
    provider_refund = collateral
    subscriber gets 0
else:                                                # BREACHED
    shortfall_bps  = target_uptime_bps - verified_uptime_bps
    slash_amount   = min(max(collateral * shortfall_bps // 10000, 1), collateral)
    provider_refund = collateral - slash_amount
    per_sub        = slash_amount // subscriber_count   # remainder to earliest subs
```

The whole slash leaves the pool and is credited to subscribers (any indivisible
remainder goes to the earliest subscribers, so nothing is minted or lost), the
provider keeps `provider_refund`, and `slash_amount + provider_refund ==
collateral` exactly. A confirmed breach always costs at least one unit, which is
what makes `is_sla_breached` and `slash_amount > 0` logically identical. Both
settlement paths run through this one code path, so they can never disagree.

---

## 4. Public Interface Reference

### Write methods (`@gl.public.write`)

| Method | Parameters | Returns | Notes |
|--------|-----------|---------|-------|
| `register_policy` | `service_name: str, status_url: str, target_uptime_bps: u256, collateral_amount: u256` | `u256` (policy_id) | Rejects unless `service_name` is 1..64 chars, `status_url` is 8..512 chars and http(s) with no whitespace/newline, `target_uptime_bps` in 1..10000, `0 < collateral_amount <= 1e30` |
| `subscribe_to_policy` | `policy_id: u256` | `None` | Registers caller as a protected subscriber; rejects duplicates and unknown policies |
| `evaluate_service_health` | `policy_id: u256` | `str` (status_code) | AUDIT ONLY, moves no money. Requires active, unsettled, `subscriber_count > 0`. Records `verified_uptime_bps`, sets `sla_breached`, writes an incident |
| `settle_policy` | `policy_id: u256` | `u256` (slashed) | Provider or subscriber only. COMPLIANT -> 0 slash + full refund; BREACHED -> shortfall slash to subscribers + remainder refund. Closes the policy |
| `claim_sla_compensation` | `policy_id: u256` | `u256` (slashed) | Subscriber only. Reverts `has not been evaluated` before any audit; reverts `SLA target met: policy is compliant and cannot be slashed` on a compliant policy; otherwise applies the breach slash |
| `claim_compensation` | none | `u256` (claimed) | Pays out and resets the caller's claimable balance (reset-before-return prevents replay) |
| `deactivate_and_withdraw_collateral` | `policy_id: u256` | `u256` (withdrawn) | Provider-only. Returns remaining pool for a compliant or un-audited policy; REVERTS on a confirmed breach (settle first), so a provider cannot dodge a slash |

### View methods (`@gl.public.view`)

| Method | Parameters | Returns |
|--------|-----------|---------|
| `is_sla_breached` | `policy_id: u256` | `bool` from the single predicate |
| `get_policy` | `policy_id: u256` | `dict` with all policy fields plus `subscriber_count` |
| `get_incident` | `incident_id: u256` | `dict` with the audit record |
| `get_settlement` | `policy_id: u256` | `dict` with the settlement breakdown |
| `get_compliance_metrics` | `policy_id: u256` | `dict` with `verified_uptime_bps`, `target_uptime_bps`, `compliant`, `sla_breached`, `shortfall_bps` |
| `get_claimable_balance` | `account: str` | `u256` unclaimed balance for that address |

### Constructor

`__init__(self)` initializes the id counters to 1 and relies on `TreeMap` /
`DynArray` defaulting to empty. No parameters.

---

## 5. Live Deployment on `studionet`

Deployed and driven on the shared GenLayer `studionet` (gasless, chain id
`61999`). Every transaction below was verified FINALIZED **and** execution
successful (or reverted with the exact expected reason) by inspecting the leader
receipt's execution status; FINALIZED alone is a lifecycle state, not proof.

Machine-readable proof: [`evidence/studionet-deploy.json`](evidence/studionet-deploy.json).

- Contract address: `0x397C73Fa3a192372A74BBFe7a063d15B260B7A7D`
- Explorer: https://explorer-studio.genlayer.com/address/0x397C73Fa3a192372A74BBFe7a063d15B260B7A7D
- Deploy tx: `0xa2772b7e2fb0de9935c0da4a761f17e68bfeb99da02b1906da16cacbe5fb5389`
- Provider (deployer) account: `0x154a452b0755EE812014C1405B15D905BA06f9c7`; a
  separate ephemeral account is the subscriber, so the two roles are distinct.

The deploy driver seeds the settlement rule on chain three ways:

| Case | What it proves on chain | Result |
|------|-------------------------|--------|
| `guard` (deterministic, no web/LLM) | An un-audited policy is never slashed and the guards bind | `claim_sla_compensation` reverts `Policy 4 has not been evaluated...`; outsider `settle_policy` reverts `Only the provider or a subscriber may settle policy 4`; provider `settle_policy` returns `0` slashed, settlement `COMPLIANT`, `provider_refund 1000000` |
| `compliant` (live consensus audit, target 9000) | A real audited, within-target policy is not slashed | `evaluate_service_health` returned `OPERATIONAL`, `verified_uptime_bps 10000`, `is_sla_breached false`; `claim_sla_compensation` reverted `SLA target met: policy is compliant and cannot be slashed`; `settle_policy` returned `0` slashed, full refund |
| `breach` (live consensus audit, strict target 10000) | The predicate is applied even at a 100.00% goal | The same healthy GitHub status page measured `OPERATIONAL` (uptime 10000), which genuinely MEETS a 10000 target, so it is correctly COMPLIANT and slashed 0 |
| adversarial input | Input validation still reverts on chain | `register_policy("bad", "ftp://nope", ...)` reverts `status_url must be http(s)` |

Honest note on the breach case: the on-chain audit can only slash a provider when
the *live* service actually misses its target. The monitored public endpoint
(GitHub status) was fully operational during the run, so even a strict 100.00%
target measured as COMPLIANT and was correctly refunded with zero slash. A true
breach slash cannot be manufactured against a genuinely healthy service, so the
breach path (and the exact shortfall formula, subscriber split, and
compliance-conservation invariant) is proven by the deterministic direct tests
`test_true_sla_breach_below_target_is_slashed_and_compensated` and
`test_breach_compensation_path_slashes_and_multi_subscriber_distribution`, and
the predicate equivalence by `test_is_sla_breached_and_settlement_use_identical_target_uptime_predicate`.
What *is* proven on chain is the part the steward asked for: a within-target
policy is never slashed, and the settlement path applies the declared
`target_uptime_bps` consistently with `is_sla_breached`.

Deployment integrity: the code returned by `gen_getContractCode` for the address
above decodes to source that is byte-for-byte identical to
`contracts/sla_uptime_insurance.py`. Local file and on-chain code both hash to
`sha256 e292d1e81f5f0b3f2d534c80e80ee8669d48b84f726b7a4103881559c7a6ba26`
(31789 bytes, `match_local_chain: true`), and
the committed git blob matches the same bytes because `.gitattributes` pins
`* text=auto eol=lf`. The third way (git raw blob) is verified at push time.

---

## 6. Testing & Reproduction Guide

### Environment

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # genlayer-py, genlayer-test, genvm-linter, pytest
cp .env.example .env              # then fill in GENLAYER_PRIVATE_KEY
```

`.env` is git-ignored and must never be committed. Never print key material.

### Lint / validate the contract

```bash
genvm-lint check contracts/sla_uptime_insurance.py
```

Reports Lint passed, Validation passed, 13 methods (7 write, 6 view), 0 errors.

### Run the fast direct-mode tests (no network)

```bash
pytest                          # runs the direct suite; integration is deselected
pytest tests/test_sla_direct.py -v
```

30 direct tests cover: policy registration and read-back; every input-validation
revert; subscriber registration plus duplicate prevention; the audit step recording
compliance without moving money; the griefing/inactive guards; the three steward
requirements (`test_policy_within_sla_target_is_not_slashed`,
`test_true_sla_breach_below_target_is_slashed_and_compensated`,
`test_is_sla_breached_and_settlement_use_identical_target_uptime_predicate`); the
boundary case where a compliant policy cannot be slashed through the compensation
path; multi-subscriber distribution and collateral conservation; settlement races
and the stakeholder/authorization guards; and `claim_compensation` reset semantics.
Web render and LLM classification are mocked; the consensus validator path is
exercised on `studionet`.

### Deploy and drive on `studionet`

```bash
python scripts/deploy_studionet.py
```

Deploys the contract, then seeds the `guard` (deterministic), `compliant` and
`breach` cases through the real settlement and live consensus-audit paths, attempts
the on-chain reverts (compensation on an un-audited policy, outsider settlement, a
compliant slash, and a malformed registration), verifies that the deployed
`gen_getContractCode` bytes hash to the same sha256 as the local file, and writes
the full proof to `evidence/studionet-deploy.json`. To re-drive against an existing
deployment without a fresh deploy:

```bash
SLA_CONTRACT_ADDRESS=0x... SLA_DEPLOY_TX=0x... python scripts/deploy_studionet.py
```

### Run the integration test against `studionet`

```bash
pytest -m integration tests/test_sla_integration.py -v -s
```

It reuses the exact deploy driver, so a green run and the collected evidence come
from identical code. It accepts `SLA_CONTRACT_ADDRESS`/`SLA_DEPLOY_TX` to run
against an existing contract. `studionet` is rate-limited per IP and finality can
be slow, so run these serially and allow generous timeouts (the driver waits for
receipts).

### Repository layout

```
contracts/sla_uptime_insurance.py   the primitive
tests/test_sla_direct.py            fast direct-mode tests
tests/test_sla_integration.py       live studionet integration test
tests/conftest.py                   makes the direct harness run on Windows (no-op elsewhere)
scripts/deploy_studionet.py         deploy + seed both outcomes + integrity + evidence
evidence/studionet-deploy.json      machine-readable on-chain proof
docs/adversarial-review.md          adversarial security review, findings and proofs
gltest.config.yaml                  network config (no secrets)
requirements.txt                    pinned toolchain
.gitattributes                      pins LF line endings for byte-exact integrity
.env.example                        template; real .env is git-ignored
```

---

## Security Model (summary)

- No private keys, secrets or `.env` values are ever stored on chain, in logs, or
  in this repository.
- One predicate (`_breach_for`) decides compliance and every fund movement, so a
  within-target policy can never be slashed and a true breach always is.
- Every transition that moves value depends on validator-agreed external
  evidence, not a caller's claim, and each validator re-verifies the source.
- All inputs that later feed fund math carry explicit floors and ceilings
  (`target_uptime_bps` in 1..10000, `0 < collateral_amount <= 1e30`, `service_name`
  1..64 chars, `status_url` 8..512 chars, http(s) and free of whitespace or
  newlines); the consensus uptime measurement is clamped to 0..10000.
- Settlement is restricted to the provider or an insured subscriber; the
  compensation path is subscriber-only and reverts on a compliant policy; the
  provider cannot withdraw out of a confirmed breach.
- Slash is capped by the collateral pool, the whole slash is distributed (no dust
  minted or lost), `slash_amount + provider_refund == collateral`, and claims reset
  the balance before returning so a claim cannot be replayed.

The full adversarial walk-through (state machine, races, invariant, findings F1-F9
and the test that closes each one) lives in
[`docs/adversarial-review.md`](docs/adversarial-review.md).

### Known limitations & trust assumptions (stated honestly)

- Oracle trust boundary: the verdict is only as good as the registered
  `status_url`. A dishonest provider could point it at a page that always renders
  "OPERATIONAL". Mitigation is economic, not cryptographic: subscribers choose
  reputable, independent status endpoints, and every validator re-fetches the page
  itself. The contract does not claim to be injection-proof against a provider who
  fully controls a misleading page.
- Measurement precision: `verified_uptime_bps` is the single numeric decision field
  crossing the consensus boundary. It is clamped and must be agreed exactly
  (disagreement rotates the leader); it is never used until recorded, and the payout
  is pure on-chain integer arithmetic from it and `target_uptime_bps`.
- Direct-mode tests mock the fetch and the LLM, so they exercise the leader path
  and the deterministic money math; they do not prove validator agreement. The live
  `studionet` run covers the validator path.
- A breach slash is a live-market property: it only happens when the real audited
  service actually misses its target, so it cannot be forced against a healthy
  endpoint for a demo. The breach logic is therefore also fixed by direct tests.
- Collateral is internal accounting (the contract holds no native value). The
  provider refund/withdraw path moves the pool into a claimable balance rather than
  transferring native GEN, so nothing real is locked in this primitive; a wrapping
  escrow can settle the claimable ledger against real funds.
