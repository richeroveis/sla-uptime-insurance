# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""SLAUptimeInsurance: a standalone SLA uptime insurance primitive.

Monitors a provider's public status endpoint, reaches GenLayer consensus on the
observed service uptime through the equivalence principle, and settles the
outcome on chain against ONE enforced SLA-breach rule.

The single SLA-breach rule
--------------------------
Every decision that can move value flows through exactly one predicate, defined
against the policy's declared ``target_uptime_bps``:

- ``COMPLIANT`` (``is_sla_breached == False``) when an evaluation has recorded
  ``verified_uptime_bps >= target_uptime_bps``.
- ``BREACHED`` (``is_sla_breached == True``) ONLY when a recorded evaluation has
  ``verified_uptime_bps < target_uptime_bps``.

``is_sla_breached``, ``get_compliance_metrics``, the audit step
(``evaluate_service_health``) and the settlement paths (``settle_policy`` and
``claim_sla_compensation``) all derive from this one comparison via the shared
pure helper ``_breach_for``. A compliant policy can never be slashed: settlement
returns the full collateral to the provider, and any attempt to slash it through
the compensation path reverts with an explicit message.

Design boundary
---------------
- External source owns: the raw status page content. It is never trusted
  directly; validators independently re-fetch and re-classify it.
- Nondeterministic block owns: the web fetch and the LLM classification of that
  fetched text into a canonical, bounded health snapshot (status enum + an
  uptime measurement clamped to 0..10000 bps).
- Deterministic contract owns: every state transition (the breach predicate,
  slash accounting, subscriber payout distribution, incident/settlement storage).
  The nondet result only feeds a small, validated decision surface; all money
  math is deterministic integer arithmetic keyed to ``target_uptime_bps``.

The value model is internal accounting (collateral units and claimable units),
not native GEN transfers, so the primitive composes safely with an
off-chain settlement or escrow layer without holding real funds here.
"""

import json
from dataclasses import dataclass

from genlayer import *

# Equivalence principle / error classification tags. Validators use these to
# decide whether a leader failure should force rotation or match exactly.
ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_TRANSIENT = "[TRANSIENT]"
ERROR_LLM = "[LLM_ERROR]"

# Input bounds. Everything that feeds integer money math is bounded so products
# cannot approach the u256 ceiling, and every free-text input carries a hard
# length cap so a caller can neither bloat storage nor the LLM prompt.
BASIS_POINTS_MAX = 10000
MIN_STATUS_URL_LEN = 8
MAX_SERVICE_NAME_LEN = 64
MAX_STATUS_URL_LEN = 512
MAX_SUMMARY_LEN = 300
MAX_WEB_WINDOW = 6000  # safe character window of the fetched page fed to the LLM
MAX_COLLATERAL = 10**30

# Canonical uptime samples used only when the classification does not carry an
# explicit measurement; they keep the decision surface bounded and deterministic.
UPTIME_OPERATIONAL_BPS = 10000
UPTIME_DEGRADED_BPS = 5000
UPTIME_MAJOR_OUTAGE_BPS = 0


@allow_storage
@dataclass
class SLAPolicy:
    policy_id: u256
    provider: Address
    service_name: str
    status_url: str
    target_uptime_bps: u256
    collateral_pool: u256
    verified_uptime_bps: u256
    total_checks: u256
    failed_checks: u256
    evaluated: bool
    sla_breached: bool
    settled: bool
    active: bool


@allow_storage
@dataclass
class IncidentRecord:
    incident_id: u256
    policy_id: u256
    status_code: str
    verified_uptime_bps: u256
    sla_breached: bool
    summary: str


@allow_storage
@dataclass
class SettlementRecord:
    policy_id: u256
    outcome: str  # "COMPLIANT" or "BREACHED"
    target_uptime_bps: u256
    verified_uptime_bps: u256
    shortfall_bps: u256
    slash_amount: u256
    provider_refund: u256
    subscriber_count: u256


def _normalize_status(raw_status: str) -> str:
    """Map an LLM status string onto the canonical enum, casing/spacing safe."""
    token = str(raw_status).strip().upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "OPERATIONAL": "OPERATIONAL",
        "OK": "OPERATIONAL",
        "UP": "OPERATIONAL",
        "HEALTHY": "OPERATIONAL",
        "NORMAL": "OPERATIONAL",
        "DEGRADED": "DEGRADED",
        "PARTIAL": "DEGRADED",
        "PARTIAL_OUTAGE": "DEGRADED",
        "MAJOR_OUTAGE": "MAJOR_OUTAGE",
        "OUTAGE": "MAJOR_OUTAGE",
        "DOWN": "MAJOR_OUTAGE",
        "MAJOROUTAGE": "MAJOR_OUTAGE",
    }
    if token not in aliases:
        raise gl.vm.UserError(f"{ERROR_LLM} Unrecognized status_code: {raw_status!r}")
    return aliases[token]


def _uptime_for_status(status_code: str) -> u256:
    """Deterministic uptime sample implied by a canonical status, used as a
    fallback when the classification carries no explicit measurement."""
    if status_code == "OPERATIONAL":
        return UPTIME_OPERATIONAL_BPS
    if status_code == "DEGRADED":
        return UPTIME_DEGRADED_BPS
    return UPTIME_MAJOR_OUTAGE_BPS  # MAJOR_OUTAGE


def _coerce_uptime(value) -> u256:
    """Coerce a reported uptime to a bounded integer in 0..10000 bps.

    Never trusts the leader's shape or range: an unusable value falls back to the
    canonical sample for the status, and an out-of-range value is clamped so a
    hostile or sloppy classification can neither fake 100% nor exceed the ceiling.
    """
    try:
        n = int(str(value).strip())
    except (ValueError, TypeError):
        return -1  # sentinel meaning "unusable", caller substitutes
    if n < 0:
        return 0
    if n > BASIS_POINTS_MAX:
        return BASIS_POINTS_MAX
    return n


def _parse_health(raw, service_name: str) -> dict:
    """Validate the LLM classification into a strict canonical snapshot.

    Accepts an already-decoded dict or a raw JSON string. Every decision field is
    normalised and bounded here; the free-text summary is capped.
    """
    data = raw
    if isinstance(data, (str, bytes)):
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last == -1 or last <= first:
            raise gl.vm.UserError(f"{ERROR_LLM} No JSON object in LLM response")
        data = json.loads(text[first : last + 1])

    if not isinstance(data, dict):
        raise gl.vm.UserError(f"{ERROR_LLM} LLM response is not an object: {type(data)}")

    status_val = data.get("status_code")
    if status_val is None:
        for alt in ("status", "state", "health", "service_status"):
            if alt in data:
                status_val = data[alt]
                break
    if status_val is None:
        raise gl.vm.UserError(f"{ERROR_LLM} Missing status_code. Keys: {list(data.keys())}")

    status_code = _normalize_status(status_val)

    # Authoritative uptime measurement: prefer an explicit reported value, clamp
    # it to the basis-points range, and only fall back to the status-implied sample
    # when the reported value is missing or unusable.
    reported = data.get("verified_uptime_bps", data.get("uptime_bps"))
    if reported is None:
        for alt in ("uptime_percent", "uptime", "observed_uptime_bps"):
            if alt in data:
                reported = data[alt]
                break
    verified_uptime_bps = _coerce_uptime(reported) if reported is not None else -1
    if verified_uptime_bps == -1:
        verified_uptime_bps = _uptime_for_status(status_code)

    summary_val = data.get("summary")
    summary = str(summary_val).strip()[:MAX_SUMMARY_LEN] if summary_val is not None else service_name

    return {
        "status_code": status_code,
        "verified_uptime_bps": verified_uptime_bps,
        "summary": summary,
    }


def _snapshot_for_eq(snapshot: dict) -> dict:
    """Deterministic fields compared for consensus; summary excluded (free text)."""
    return {
        "status_code": snapshot["status_code"],
        "verified_uptime_bps": snapshot["verified_uptime_bps"],
    }


def _breach_for(evaluated: bool, verified_uptime_bps, target_uptime_bps) -> tuple:
    """THE single SLA-breach predicate, used everywhere value is decided.

    Returns ``(is_breached, shortfall_bps)``:
    - No value moves unless an evaluation has actually been recorded.
    - A policy at or above its declared target is COMPLIANT: shortfall 0, and the
      caller must not slash anything.
    - Only a measured uptime below target is a BREACH; ``shortfall_bps`` is the
      deficit against the declared target, which ``_slash_from_shortfall`` turns
      into a deterministic, collateral-conserving penalty.

    This is the one and only place that compares an uptime to ``target_uptime_bps``;
    ``is_sla_breached``, the audit step and both settlement paths all call it, so
    compliance and slashing can never disagree.
    """
    if not evaluated:
        return (False, 0)
    if verified_uptime_bps >= target_uptime_bps:
        return (False, 0)
    shortfall = target_uptime_bps - verified_uptime_bps
    return (True, shortfall)


def _slash_from_shortfall(collateral, shortfall_bps) -> u256:
    """Deterministic slash for a confirmed breach, conserving the pool.

    slash = collateral * shortfall_bps / 10000, floored at one unit (a confirmed
    breach always costs the provider) and capped at the collateral pool so it can
    never overdraw. ``provider_refund = collateral - slash`` therefore always
    conserves exactly the pool.
    """
    if shortfall_bps <= 0:
        return 0
    slash = (collateral * shortfall_bps) // BASIS_POINTS_MAX
    if slash == 0:
        slash = 1  # a real breach must slash non-zero, keeping the predicate honest
    if slash > collateral:
        slash = collateral
    return slash


class SLAUptimeInsurance(gl.Contract):
    next_policy_id: u256
    next_incident_id: u256
    policies: TreeMap[u256, SLAPolicy]
    incidents: TreeMap[u256, IncidentRecord]
    settlements: TreeMap[u256, SettlementRecord]
    policy_subscribers: TreeMap[u256, DynArray[Address]]
    claimable_balances: TreeMap[Address, u256]

    def __init__(self):
        self.next_policy_id = 1
        self.next_incident_id = 1

    @gl.public.write
    def register_policy(
        self,
        service_name: str,
        status_url: str,
        target_uptime_bps: u256,
        collateral_amount: u256,
    ) -> u256:
        # Exact validator consensus on inputs that later gate fund movement.
        name = str(service_name).strip()
        if len(name) == 0 or len(name) > MAX_SERVICE_NAME_LEN:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} service_name must be 1..{MAX_SERVICE_NAME_LEN} chars"
            )

        url = str(status_url).strip()
        if len(url) < MIN_STATUS_URL_LEN or len(url) > MAX_STATUS_URL_LEN:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} status_url must be {MIN_STATUS_URL_LEN}..{MAX_STATUS_URL_LEN} chars"
            )
        if not (url.startswith("http://") or url.startswith("https://")):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} status_url must be http(s)")
        # A URL is a single token: any embedded whitespace or newline is either a
        # malformed value or a smuggling attempt into the later fetch/prompt path.
        for ch in url:
            if ch.isspace():
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} status_url must not contain whitespace or newlines"
                )

        if target_uptime_bps <= 0 or target_uptime_bps > BASIS_POINTS_MAX:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} target_uptime_bps must be in 1..{BASIS_POINTS_MAX}"
            )
        # Floor and ceiling on the collateral so slash products stay far below the
        # u256 ceiling.
        if collateral_amount <= 0 or collateral_amount > MAX_COLLATERAL:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} collateral_amount must be in 1..{MAX_COLLATERAL}"
            )

        policy_id = self.next_policy_id
        self.policies[policy_id] = SLAPolicy(
            policy_id=policy_id,
            provider=gl.message.sender_address,
            service_name=name,
            status_url=url,
            target_uptime_bps=target_uptime_bps,
            collateral_pool=collateral_amount,
            verified_uptime_bps=0,
            total_checks=0,
            failed_checks=0,
            evaluated=False,
            sla_breached=False,
            settled=False,
            active=True,
        )
        # Create the (empty) subscriber list slot for this policy in storage.
        self.policy_subscribers.get_or_insert_default(policy_id)
        self.next_policy_id = policy_id + 1
        return policy_id

    @gl.public.write
    def subscribe_to_policy(self, policy_id: u256) -> None:
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown policy {policy_id}")

        sender = gl.message.sender_address
        subs = self.policy_subscribers.get_or_insert_default(policy_id)
        for existing in subs:
            if existing == sender:
                raise gl.vm.UserError(f"{ERROR_EXPECTED} Already subscribed {policy_id}")

        subs.append(sender)

    @gl.public.write
    def evaluate_service_health(self, policy_id: u256) -> str:
        # AUDIT step only: it records the consensus-verified uptime and derives the
        # compliance state, but it NEVER moves funds. All value transfer happens in
        # the settlement paths, so the breach predicate is applied in exactly one
        # place for money math.
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown policy {policy_id}")

        policy = self.policies[policy_id]
        if not policy.active:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Policy {policy_id} is inactive")
        if policy.settled:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Policy {policy_id} is already settled")

        # Griefing / wasted-collateral guard: never run a health check on a policy
        # that protects nobody. With zero subscribers there is no insured party.
        subs = self.policy_subscribers.get_or_insert_default(policy_id)
        if len(subs) == 0:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Policy {policy_id} has no subscribers to protect"
            )

        # Read every storage value used by the nondet block BEFORE entering it.
        # The nondet function must not touch contract state.
        status_url = policy.status_url
        service_name = policy.service_name

        def analyze():
            try:
                page = gl.nondet.web.render(status_url, mode="text")
            except Exception as exc:
                # Treat an unreachable status page as transient so validators
                # rotate rather than persist a fabricated healthy reading.
                raise gl.vm.UserError(f"{ERROR_TRANSIENT} status page fetch failed: {exc}")
            if page is None or len(str(page).strip()) == 0:
                raise gl.vm.UserError(f"{ERROR_TRANSIENT} status page returned empty body")

            prompt = (
                "You are a deterministic SLA monitor. Estimate the uptime of the "
                f"service '{service_name}' strictly from the status page text below. "
                "Do not use outside knowledge. Return a JSON object with exactly "
                'these keys: "status_code" (one of "OPERATIONAL", "DEGRADED", '
                '"MAJOR_OUTAGE"), "verified_uptime_bps" (an integer between 0 and '
                "10000 representing the measured uptime in basis points, e.g. 9990 "
                "for 99.90%), and \"summary\" (a short plain-text explanation citing "
                "the evidence you saw). OPERATIONAL means fully up, DEGRADED means "
                "partial or impaired service, MAJOR_OUTAGE means unavailable or down.\n\n"
                f"STATUS PAGE TEXT:\n{str(page)[:MAX_WEB_WINDOW]}"
            )
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            return _parse_health(raw, service_name)

        # Comparative equivalence: each validator re-fetches the SAME live page,
        # re-runs the SAME classification, and must agree on the deterministic
        # decision fields. The free-text summary is intentionally not compared.
        snapshot = gl.eq_principle.prompt_comparative(
            analyze,
            principle=(
                "The two answers describe the same service's health from the same "
                "status page. They must agree exactly on status_code and on "
                "verified_uptime_bps (the measured uptime in basis points). The "
                "summary wording may differ; ignore it. If either answer is missing "
                "a decision field or uses a value outside the allowed ranges, they "
                "do NOT agree."
            ),
        )

        status_code = snapshot["status_code"]
        verified_uptime_bps = snapshot["verified_uptime_bps"]

        # The compliance state is derived from the ONE predicate, never from the
        # raw status label.
        is_breached, _shortfall = _breach_for(True, verified_uptime_bps, policy.target_uptime_bps)

        policy.total_checks = policy.total_checks + 1
        policy.verified_uptime_bps = verified_uptime_bps
        policy.evaluated = True
        policy.sla_breached = is_breached
        if status_code != "OPERATIONAL":
            policy.failed_checks = policy.failed_checks + 1

        incident_id = self.next_incident_id
        self.incidents[incident_id] = IncidentRecord(
            incident_id=incident_id,
            policy_id=policy_id,
            status_code=status_code,
            verified_uptime_bps=verified_uptime_bps,
            sla_breached=is_breached,
            summary=str(snapshot.get("summary", ""))[:MAX_SUMMARY_LEN],
        )
        self.next_incident_id = incident_id + 1
        self.policies[policy_id] = policy

        return status_code

    @gl.public.write
    def settle_policy(self, policy_id: u256) -> u256:
        """Settle a policy against the single SLA-breach predicate.

        - COMPLIANT (uptime at/above target): nothing is slashed. The whole
          collateral pool is refunded to the provider and 0 goes to subscribers.
        - BREACHED (uptime below target): the provider is slashed by the shortfall
          formula and the slashed units are distributed to subscribers; the
          remaining collateral is refunded to the provider. Total conserved.

        Returns the amount slashed from the provider.
        """
        policy = self._require_settleable(policy_id)
        self._require_stakeholder(policy, policy_id)
        subs = self.policy_subscribers.get_or_insert_default(policy_id)
        collateral = policy.collateral_pool

        is_breached, shortfall = _breach_for(
            policy.evaluated, policy.verified_uptime_bps, policy.target_uptime_bps
        )

        if not is_breached:
            # Compliant path: zero slash, 100% of collateral back to the provider.
            self._credit(policy.provider, collateral)
            slash_amount = 0
            provider_refund = collateral
            outcome = "COMPLIANT"
        else:
            slash_amount = _slash_from_shortfall(collateral, shortfall)
            provider_refund = collateral - slash_amount
            self._distribute(subs, slash_amount)
            if provider_refund > 0:
                self._credit(policy.provider, provider_refund)
            outcome = "BREACHED"

        # Finalize: the pool is fully drained into claimable balances (subscribers +
        # provider), so the collateral decreases by exactly slash_amount +
        # provider_refund and nothing is minted or trapped.
        policy.collateral_pool = 0
        policy.sla_breached = is_breached
        policy.settled = True
        policy.active = False
        self.policies[policy_id] = policy

        self.settlements[policy_id] = SettlementRecord(
            policy_id=policy_id,
            outcome=outcome,
            target_uptime_bps=policy.target_uptime_bps,
            verified_uptime_bps=policy.verified_uptime_bps,
            shortfall_bps=shortfall,
            slash_amount=slash_amount,
            provider_refund=provider_refund,
            subscriber_count=len(subs),
        )
        return slash_amount

    @gl.public.write
    def claim_sla_compensation(self, policy_id: u256) -> u256:
        """Subscriber-initiated breach claim. It enforces the same predicate and
        refuses to slash a policy that is still within its SLA target.

        Reverts unless an evaluation has been recorded, and reverts on a compliant
        policy with the explicit guard message, so a compliant policy can never be
        slashed through the compensation path.
        """
        policy = self._require_settleable(policy_id)
        if not policy.evaluated:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Policy {policy_id} has not been evaluated; settlement needs a recorded uptime"
            )

        # Compensation belongs to insured subscribers only.
        if not self._is_subscriber(policy_id, gl.message.sender_address):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only a subscriber may claim compensation on policy {policy_id}"
            )

        is_breached, _shortfall = _breach_for(
            policy.evaluated, policy.verified_uptime_bps, policy.target_uptime_bps
        )
        if not is_breached:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} SLA target met: policy is compliant and cannot be slashed"
            )

        # Confirmed breach: same settlement math as settle_policy, single source.
        return self._apply_breach_settlement(policy_id)

    @gl.public.write
    def claim_compensation(self) -> u256:
        sender = gl.message.sender_address
        amount = self.claimable_balances.get(sender, 0)
        if amount == 0:
            return 0
        # Reset before returning so a claim cannot be replayed.
        self.claimable_balances[sender] = 0
        return amount

    @gl.public.write
    def deactivate_and_withdraw_collateral(self, policy_id: u256) -> u256:
        # Provider exit path for an UNSETTLED policy. It never slashes anyone; it
        # only returns the provider's own remaining collateral, so it cannot be used
        # to slash a compliant policy or bypass the breach predicate.
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown policy {policy_id}")

        policy = self.policies[policy_id]
        sender = gl.message.sender_address
        if sender != policy.provider:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only the provider may withdraw policy {policy_id}"
            )
        if not policy.active:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Policy {policy_id} is inactive")
        if policy.settled:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Policy {policy_id} is already settled")

        # A provider must not be able to withdraw out of a confirmed breach to dodge
        # the slash its subscribers are owed. The only exit for a breached policy is
        # settlement, which applies the single predicate before any refund.
        is_breached, _sf = _breach_for(
            policy.evaluated, policy.verified_uptime_bps, policy.target_uptime_bps
        )
        if is_breached:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Policy {policy_id} is in confirmed SLA breach; settle it before withdrawing"
            )

        amount = policy.collateral_pool
        if amount > 0:
            # Credit first, then zero the pool, so the accounting invariant
            # (collateral decrease == claimable increase) always holds.
            self._credit(sender, amount)
            policy.collateral_pool = 0
        policy.active = False
        self.policies[policy_id] = policy
        return amount

    # --- internal helpers (not exposed on the ABI, no storage reads outside) ---

    def _require_settleable(self, policy_id: u256) -> SLAPolicy:
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown policy {policy_id}")
        policy = self.policies[policy_id]
        if policy.settled:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Policy {policy_id} is already settled")
        if not policy.active:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Policy {policy_id} is inactive")
        return policy

    def _is_subscriber(self, policy_id: u256, addr: Address) -> bool:
        subs = self.policy_subscribers.get_or_insert_default(policy_id)
        for existing in subs:
            if existing == addr:
                return True
        return False

    def _require_stakeholder(self, policy: SLAPolicy, policy_id: u256) -> None:
        # Only the provider or an insured subscriber may drive a settlement, so an
        # outsider cannot force-close a policy to grief either side.
        sender = gl.message.sender_address
        if sender != policy.provider and not self._is_subscriber(policy_id, sender):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only the provider or a subscriber may settle policy {policy_id}"
            )

    def _apply_breach_settlement(self, policy_id: u256) -> u256:
        policy = self._require_settleable(policy_id)
        subs = self.policy_subscribers.get_or_insert_default(policy_id)
        if len(subs) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Policy {policy_id} has no subscribers to protect")

        collateral = policy.collateral_pool
        is_breached, shortfall = _breach_for(
            policy.evaluated, policy.verified_uptime_bps, policy.target_uptime_bps
        )
        slash_amount = _slash_from_shortfall(collateral, shortfall)
        provider_refund = collateral - slash_amount
        self._distribute(subs, slash_amount)
        if provider_refund > 0:
            self._credit(policy.provider, provider_refund)

        policy.collateral_pool = 0
        policy.sla_breached = is_breached
        policy.settled = True
        policy.active = False
        self.policies[policy_id] = policy

        self.settlements[policy_id] = SettlementRecord(
            policy_id=policy_id,
            outcome="BREACHED",
            target_uptime_bps=policy.target_uptime_bps,
            verified_uptime_bps=policy.verified_uptime_bps,
            shortfall_bps=shortfall,
            slash_amount=slash_amount,
            provider_refund=provider_refund,
            subscriber_count=len(subs),
        )
        return slash_amount

    def _credit(self, account: Address, amount) -> None:
        if amount <= 0:
            return
        current = self.claimable_balances.get(account, 0)
        self.claimable_balances[account] = current + amount

    def _distribute(self, subs: DynArray[Address], slash_amount) -> None:
        """Distribute exactly ``slash_amount`` across subscribers.

        The whole slashed amount leaves the pool and is credited (any indivisible
        remainder goes to the earliest subscribers), so the collateral decrease
        always equals the total claimable increase and no dust is minted or lost.
        """
        n = len(subs)
        if slash_amount <= 0 or n == 0:
            return
        base = slash_amount // n
        rem = slash_amount - base * n
        given = 0
        for addr in subs:
            share = base
            if given < rem:
                share = base + 1
            given = given + 1
            self._credit(addr, share)

    # --- views ---

    @gl.public.view
    def is_sla_breached(self, policy_id: u256) -> bool:
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown policy {policy_id}")
        p = self.policies[policy_id]
        # The SAME predicate the settlement paths use.
        is_breached, _s = _breach_for(
            p.evaluated, p.verified_uptime_bps, p.target_uptime_bps
        )
        return is_breached

    @gl.public.view
    def get_policy(self, policy_id: u256) -> dict:
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown policy {policy_id}")
        p = self.policies[policy_id]
        return {
            "policy_id": int(p.policy_id),
            "provider": str(p.provider),
            "service_name": p.service_name,
            "status_url": p.status_url,
            "target_uptime_bps": int(p.target_uptime_bps),
            "collateral_pool": int(p.collateral_pool),
            "verified_uptime_bps": int(p.verified_uptime_bps),
            "total_checks": int(p.total_checks),
            "failed_checks": int(p.failed_checks),
            "evaluated": p.evaluated,
            "sla_breached": p.sla_breached,
            "settled": p.settled,
            "active": p.active,
            "subscriber_count": len(self.policy_subscribers.get_or_insert_default(policy_id)),
        }

    @gl.public.view
    def get_incident(self, incident_id: u256) -> dict:
        if incident_id not in self.incidents:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown incident {incident_id}")
        rec = self.incidents[incident_id]
        return {
            "incident_id": int(rec.incident_id),
            "policy_id": int(rec.policy_id),
            "status_code": rec.status_code,
            "verified_uptime_bps": int(rec.verified_uptime_bps),
            "sla_breached": rec.sla_breached,
            "summary": rec.summary,
        }

    @gl.public.view
    def get_settlement(self, policy_id: u256) -> dict:
        if policy_id not in self.settlements:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No settlement for policy {policy_id}")
        s = self.settlements[policy_id]
        return {
            "policy_id": int(s.policy_id),
            "outcome": s.outcome,
            "target_uptime_bps": int(s.target_uptime_bps),
            "verified_uptime_bps": int(s.verified_uptime_bps),
            "shortfall_bps": int(s.shortfall_bps),
            "slash_amount": int(s.slash_amount),
            "provider_refund": int(s.provider_refund),
            "subscriber_count": int(s.subscriber_count),
        }

    @gl.public.view
    def get_compliance_metrics(self, policy_id: u256) -> dict:
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown policy {policy_id}")
        p = self.policies[policy_id]
        target = int(p.target_uptime_bps)
        uptime = int(p.verified_uptime_bps) if p.evaluated else BASIS_POINTS_MAX
        is_breached, shortfall = _breach_for(
            p.evaluated, p.verified_uptime_bps, p.target_uptime_bps
        )
        return {
            "policy_id": int(p.policy_id),
            "service_name": p.service_name,
            "total_checks": int(p.total_checks),
            "failed_checks": int(p.failed_checks),
            "evaluated": p.evaluated,
            "verified_uptime_bps": uptime,
            "target_uptime_bps": target,
            "compliant": not is_breached,
            "sla_breached": is_breached,
            "shortfall_bps": shortfall,
        }

    @gl.public.view
    def get_claimable_balance(self, account: str) -> u256:
        return self.claimable_balances.get(Address(account), 0)
