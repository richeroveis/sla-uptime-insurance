# SLAUptimeInsurance

A production-grade, standalone GenLayer Intelligent Contract primitive for
decentralized SLA (Service Level Agreement) uptime insurance. It monitors a
service provider's public status endpoint, reaches validator consensus on the
observed service health, and automatically settles the outcome on chain: it
slashes provider collateral, credits compensation to protected subscribers, and
writes an immutable incident record.

- Contract: [`contracts/sla_uptime_insurance.py`](contracts/sla_uptime_insurance.py)
- Network: GenLayer `studionet` (chain id `61999`)
- Deployed address: `0xD938694c9C745d9816650B944a32D3c45a95E710`
- Runner: `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`

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

- A provider registers a service and locks a **collateral pool**.
- Anyone can **subscribe** to that policy as a protected consumer.
- The contract **evaluates** the provider's live status endpoint. Because a
  status page is external, subjective, and rendered by non-deterministic systems,
  the verdict is decided through GenLayer's equivalence principle so independent
  validators agree on what really happened.
- When an outage is confirmed, the contract deterministically **slashes** the
  provider collateral and distributes compensation to subscribers.
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

## 2. Consensus & Equivalence Design

The hardest part of this contract is the boundary between the deterministic
on-chain world and the non-deterministic real world. Getting that boundary right
is what makes the payouts defensible rather than gameable.

### The equivalence problem

Two APIs cannot be blindly trusted here:

1. `gl.nondet.web.render(status_url, mode="text")` returns live HTML/text that
   can change between the leader's read and a validator's read.
2. `gl.nondet.exec_prompt(...)` classifies that text with an LLM, whose output is
   inherently non-deterministic in wording.

`strict_eq` on the raw LLM answer would fail consensus every time (different
free-text), and a leader-only or format-only validator would let a single
malicious node decide whether a provider "had an outage" and drain its
collateral. Both are unacceptable for anything that touches funds.

### The chosen principle: comparative verification of a canonical decision

`evaluate_service_health` reads `status_url` and `service_name` from storage
*before* entering the non-deterministic block, so no contract state is touched
inside it. Inside, a zero-argument closure:

1. fetches the live status page,
2. prompts the LLM to classify it into a canonical snapshot
   `{"status_code", "severity_score", "is_sla_breached", "summary"}`,
3. normalizes that snapshot through `_parse_health`, which snaps the status to one
   of the three allowed enumerations and **derives `severity_score` from
   `status_code` deterministically** (OPERATIONAL -> 0, DEGRADED -> 50,
   MAJOR_OUTAGE -> 100). Free-text `summary` is deliberately excluded from the
   compared fields.

The snapshot is then run through `gl.eq_principle.prompt_comparative`, whose
principle requires each validator to re-fetch the same page, re-run the same
classification, and agree exactly on `status_code`, `severity_score` and
`is_sla_breached`. That means:

- Every validator independently verifies the live status page rather than
  trusting the leader's formatting.
- A leader cannot win by producing a well-formed but false verdict; a dissenting
  validator that reads "OPERATIONAL" while the leader claims "MAJOR_OUTAGE" votes
  `False` and forces rotation.
- Unreachable pages raise a `[TRANSIENT]` error (empty body or fetch failure) so
  consensus rotates instead of persisting a fabricated healthy reading.

Only a small, validated decision surface (the status code) crosses the
determinism boundary. Every unit of collateral moved is the product of pure
integer arithmetic on chain, which is what the adversarial fund-safety rules
demand.

### Error classification

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
| `slash_rate_bps` | `u256` | Penalty rate per confirmed incident (basis points) |
| `total_checks` | `u256` | Number of evaluations run |
| `failed_checks` | `u256` | Evaluations that observed DEGRADED or MAJOR_OUTAGE |
| `active` | `bool` | Whether the policy is open for evaluation |

### `IncidentRecord` (`@allow_storage @dataclass`)

| Field | Type | Meaning |
|-------|------|---------|
| `incident_id` | `u256` | Unique incident identifier |
| `policy_id` | `u256` | Parent policy |
| `status_code` | `str` | `OPERATIONAL`, `DEGRADED`, or `MAJOR_OUTAGE` |
| `severity_score` | `u256` | `0`, `50`, or `100` (derived from status) |
| `slashed_amount` | `u256` | Collateral units slashed for this incident |
| `summary` | `str` | Truncated evidence summary from the classification |

### Contract storage layout

| Field | Type | Purpose |
|-------|------|---------|
| `next_policy_id` | `u256` | Monotonic policy id counter |
| `next_incident_id` | `u256` | Monotonic incident id counter |
| `policies` | `TreeMap[u256, SLAPolicy]` | Policy records by id |
| `incidents` | `TreeMap[u256, IncidentRecord]` | Incident log by id |
| `policy_subscribers` | `TreeMap[u256, DynArray[Address]]` | Protected subscribers per policy |
| `claimable_balances` | `TreeMap[Address, u256]` | Accrued, unclaimed compensation per account |

`TreeMap` gives O(log n) keyed persistence; `DynArray` gives an append-only
indexed list. Nested subscriber lists are created with
`policy_subscribers.get_or_insert_default(policy_id)` (the GenVM-safe way to
allocate an empty `DynArray` value inside a `TreeMap`).

### Slash & payout math (deterministic)

For a DEGRADED or MAJOR_OUTAGE verdict:

```
base_slash = collateral_pool * slash_rate_bps // 10000
slashed    = base_slash * severity_score // 100          # 50% or 100% weight
per_sub    = slashed // subscriber_count                  # equal split
```

`slashed` is capped at `collateral_pool`, `per_sub` is credited to each
subscriber's `claimable_balances`, and only the actually-distributed amount
(`per_sub * subscriber_count`) is deducted from the pool, so rounding dust is
never minted or double-counted. With no subscribers the slash is recorded but not
redistributed.

---

## 4. Public Interface Reference

### Write methods (`@gl.public.write`)

| Method | Parameters | Returns | Notes |
|--------|-----------|---------|-------|
| `register_policy` | `service_name: str, status_url: str, target_uptime_bps: u256, collateral_amount: u256, slash_rate_bps: u256` | `u256` (policy_id) | Validates name/URL scheme, bounds `target<=10000`, `slash_rate_bps in 1..500` (5% per-incident cap), `collateral in 1..1e24` |
| `subscribe_to_policy` | `policy_id: u256` | `None` | Registers caller as a protected subscriber; rejects duplicates and unknown policies |
| `evaluate_service_health` | `policy_id: u256` | `str` (status_code) | Runs the consensus classification, updates counters, slashes and distributes on failure, writes an incident |
| `claim_compensation` | none | `u256` (claimed) | Pays out and resets the caller's claimable balance (reset-before-return prevents replay) |

### View methods (`@gl.public.view`)

| Method | Parameters | Returns |
|--------|-----------|---------|
| `get_policy` | `policy_id: u256` | `dict` with all policy fields plus `subscriber_count` |
| `get_incident` | `incident_id: u256` | `dict` with the incident record |
| `get_compliance_metrics` | `policy_id: u256` | `dict` with `uptime_bps`, `target_uptime_bps`, `compliant`, `breach_margin_bps` |
| `get_claimable_balance` | `account: str` | `u256` unclaimed balance for that address |

`uptime_bps = ((total_checks - failed_checks) * 10000) // total_checks` (100% when
no checks have run yet), compared against `target_uptime_bps` for `compliant`.

### Constructor

`__init__(self)` initializes the id counters to 1 and relies on `TreeMap` /
`DynArray` defaulting to empty. No parameters.

---

## 5. Live Deployment on `studionet`

Deployed and driven on the shared GenLayer `studionet` (gasless, chain id
`61999`). Every transaction below was verified FINALIZED **and** execution
successful by inspecting the leader receipt's execution status (FINALIZED alone
is a lifecycle state, not proof of success).

Machine-readable proof: [`evidence/studionet-deploy.json`](evidence/studionet-deploy.json).

- Contract address: `0xD938694c9C745d9816650B944a32D3c45a95E710`
- Explorer: https://explorer-studio.genlayer.com/address/0xD938694c9C745d9816650B944a32D3c45a95E710
- Deploy tx: `0xfdaab70df0f936d437c167c4d90aa44261ee8db85bd8ea9ed35e64df2eea0865`

| Step | Transaction | Result |
|------|-------------|--------|
| `register_policy` (collateral 1000000, rate 200bps, target 9990) | `0x5e454baa2ee1aa0efd83275ed298cc8fb377f6b325bf86a1fefe26b73db6e001` | `return`, policy_id `1` |
| `subscribe_to_policy(1)` | `0xcf7d5dab95bf5b16935ba6dbd144f9565b27c75f594f73937576fd03d59e5b21` | `return`, subscriber_count 0 -> 1 |
| `register_policy` with invalid `ftp://` URL | `0x3922c07934442f18df5404b020b5b9986bcc923616ca3a15877c05295d312f1a` | `rollback`, `[EXPECTED] status_url must be http(s)` |

Read-back through the view interface after the writes:

- `get_policy(1)`: provider `0x154a...f9c7`, `collateral_pool 1000000`, `slash_rate_bps 200`, `subscriber_count 1`, `total_checks 0`, `active true`.
- `get_compliance_metrics(1)`: `uptime_bps 10000`, `target_uptime_bps 9990`, `compliant true`.
- `get_claimable_balance(deployer)`: `0` (no incident has fired yet).

The adversarial-input revert observed on chain matches the direct-mode revert
exactly, which is the point: consensus agrees on the failure path too.

Deployment integrity: the `contract_code` served by the public explorer for the
address above is byte-for-byte identical to `contracts/sla_uptime_insurance.py`
as committed here. Both hash to
`sha256 7deea4488e76703a0de440659e37fadc64e520aba5e8c58fe3679ecbeafaeaa9`, so the
submitted source is exactly what runs on `studionet`.

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

### Run the fast direct-mode tests (no network)

```bash
pytest                          # runs the direct suite; integration is deselected
pytest tests/test_sla_direct.py -v
```

10 direct tests cover: policy registration and read-back, every input-validation
revert, subscriber registration plus duplicate prevention, `OPERATIONAL`
(0 slash), `DEGRADED` (50% weight) and `MAJOR_OUTAGE` (100% weight) evaluations,
multi-subscriber payout distribution, the zero-subscriber edge case, and
`claim_compensation` reset semantics. Web render and LLM classification are
mocked; the consensus validator path is exercised on `studionet`.

### Deploy and drive on `studionet`

```bash
python scripts/deploy_studionet.py
```

Deploys the contract, submits `register_policy` and `subscribe_to_policy`, reads
state back through the view methods, attempts one adversarial `register_policy`
that must revert, and writes the full proof to `evidence/studionet-deploy.json`.

### Run the integration test against `studionet`

```bash
pytest -m integration tests/test_sla_integration.py -v -s
```

It reuses the exact deploy driver, so a green run and the collected evidence come
from identical code. `studionet` is rate-limited per IP, so run these serially and
wait for receipts (the driver already does).

### Repository layout

```
contracts/sla_uptime_insurance.py   the primitive
tests/test_sla_direct.py            fast direct-mode tests
tests/test_sla_integration.py       live studionet integration test
scripts/deploy_studionet.py         deploy + drive + evidence collection
evidence/studionet-deploy.json      machine-readable on-chain proof
gltest.config.yaml                  network config (no secrets)
requirements.txt                    pinned toolchain
.env.example                        template; real .env is git-ignored
```

---

## Security Model (summary)

- No private keys, secrets or `.env` values are ever stored on chain, in logs, or
  in this repository.
- Every transition that moves value depends on validator-agreed external
  evidence, not a caller's claim, and each validator re-verifies the source.
- All inputs that later feed fund math carry explicit floors and ceilings
  (uptime bps, slash rate, collateral magnitude, URL/name lengths).
- Slash is capped by the collateral pool, distribution only deducts what it
  actually credits (no rounding mint), and claims reset the balance before
  returning so a claim cannot be replayed.

### Known limitations & trust assumptions (stated honestly)

- Oracle trust boundary: the verdict is only as good as the registered
  `status_url`. A dishonest provider could point it at a page that always renders
  "OPERATIONAL". Mitigation is economic, not cryptographic: subscribers choose
  reputable, independent status endpoints, and every validator re-fetches the page
  itself. The contract does not, and cannot claim to be, injection-proof against a
  provider who fully controls a misleading page.
- `evaluate_service_health` is permissionless. Any account can trigger a check. A
  false slash still requires validator consensus on a real outage, so funds are
  safe, but an attacker could inflate `total_checks` with genuine OPERATIONAL
  readings. A deployment that cares about this should gate evaluation to the
  provider and its subscribers.
- Confirmed outage with zero subscribers forfeits the slash: it lowers
  `collateral_pool` and accrues to nobody, so it is a penalty burn rather than a
  transfer. This is intentional (an outage is penalized even mid-membership-change)
  and is covered by a dedicated test.
- There is no provider refund/withdraw path in this primitive. Collateral is
  internal accounting (the contract holds no native value), so nothing real is
  locked; a wrapping escrow is expected to supply redemption on top of the
  slashing ledger this contract maintains.
