# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""SLAUptimeInsurance: a standalone SLA uptime insurance primitive.

Monitors a provider's public status endpoint, reaches GenLayer consensus on the
observed service health through the equivalence principle, and settles the
outcome on chain: it slashes the provider collateral, credits protection payouts
to registered subscribers, and appends an immutable incident record.

Design boundary
---------------
- External source owns: the raw status page content. It is never trusted
  directly; validators independently re-fetch and re-classify it.
- Nondeterministic block owns: the web fetch and the LLM classification of that
  fetched text into a canonical health snapshot.
- Deterministic contract owns: every state transition (check counters, slash
  accounting, subscriber payout distribution, incident storage). The nondet
  result only feeds a small, validated decision surface (status_code and
  is_sla_breached); all money math is deterministic integer arithmetic.

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
# cannot approach the u256 ceiling.
BASIS_POINTS_MAX = 10000
SEVERITY_WEIGHT_MAX = 100
MAX_SERVICE_NAME_LEN = 128
MAX_STATUS_URL_LEN = 2048
MAX_COLLATERAL = 10**24
MAX_SLASH_RATE_BPS = 500  # a single policy may slash at most 5% of collateral per incident


@allow_storage
@dataclass
class SLAPolicy:
    policy_id: u256
    provider: Address
    service_name: str
    status_url: str
    target_uptime_bps: u256
    collateral_pool: u256
    slash_rate_bps: u256
    total_checks: u256
    failed_checks: u256
    active: bool


@allow_storage
@dataclass
class IncidentRecord:
    incident_id: u256
    policy_id: u256
    status_code: str
    severity_score: u256
    slashed_amount: u256
    summary: str


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


def _severity_for(status_code: str) -> u256:
    """Derive the deterministic severity weight from the canonical status code."""
    if status_code == "OPERATIONAL":
        return 0
    if status_code == "DEGRADED":
        return 50
    return 100  # MAJOR_OUTAGE


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise gl.vm.UserError(f"{ERROR_LLM} Unrecognized is_sla_breached: {value!r}")


def _parse_health(raw, service_name: str) -> dict:
    """Validate the LLM classification into a strict canonical snapshot.

    Accepts an already-decoded dict or a raw JSON string. Never trusts the
    leader's shape: every decision field is normalised and bounded here.
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
    severity_score = _severity_for(status_code)

    breach_val = data.get("is_sla_breached")
    if breach_val is None:
        breach_val = data.get("sla_breached", data.get("breached", status_code != "OPERATIONAL"))
    is_sla_breached = _coerce_bool(breach_val)

    summary_val = data.get("summary")
    summary = str(summary_val).strip()[:280] if summary_val is not None else service_name

    return {
        "status_code": status_code,
        "severity_score": severity_score,
        "is_sla_breached": is_sla_breached,
        "summary": summary,
    }


def _snapshot_for_eq(snapshot: dict) -> dict:
    """Deterministic fields compared for consensus; summary excluded (free text)."""
    return {
        "status_code": snapshot["status_code"],
        "severity_score": snapshot["severity_score"],
        "is_sla_breached": snapshot["is_sla_breached"],
    }


class SLAUptimeInsurance(gl.Contract):
    next_policy_id: u256
    next_incident_id: u256
    policies: TreeMap[u256, SLAPolicy]
    incidents: TreeMap[u256, IncidentRecord]
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
        slash_rate_bps: u256,
    ) -> u256:
        # Exact validator consensus on inputs that later gate fund movement.
        name = str(service_name).strip()
        if len(name) == 0 or len(name) > MAX_SERVICE_NAME_LEN:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} service_name must be 1..{MAX_SERVICE_NAME_LEN} chars"
            )

        url = str(status_url).strip()
        if len(url) == 0 or len(url) > MAX_STATUS_URL_LEN:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} status_url must be 1..{MAX_STATUS_URL_LEN} chars"
            )
        if not (url.startswith("http://") or url.startswith("https://")):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} status_url must be http(s)")

        if target_uptime_bps <= 0 or target_uptime_bps > BASIS_POINTS_MAX:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} target_uptime_bps must be in 1..{BASIS_POINTS_MAX}"
            )
        if slash_rate_bps <= 0 or slash_rate_bps > BASIS_POINTS_MAX:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} slash_rate_bps must be in 1..{BASIS_POINTS_MAX}"
            )
        # Floor and ceiling on the collateral so slash products stay far below the
        # u256 ceiling and a slash rate cannot be used to nuke a whole pool.
        if collateral_amount <= 0 or collateral_amount > MAX_COLLATERAL:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} collateral_amount must be in 1..{MAX_COLLATERAL}"
            )
        if slash_rate_bps > MAX_SLASH_RATE_BPS:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} slash_rate_bps exceeds {MAX_SLASH_RATE_BPS} bps per-incident cap"
            )

        policy_id = self.next_policy_id
        self.policies[policy_id] = SLAPolicy(
            policy_id=policy_id,
            provider=gl.message.sender_address,
            service_name=name,
            status_url=url,
            target_uptime_bps=target_uptime_bps,
            collateral_pool=collateral_amount,
            slash_rate_bps=slash_rate_bps,
            total_checks=0,
            failed_checks=0,
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
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown policy {policy_id}")

        policy = self.policies[policy_id]
        if not policy.active:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Policy {policy_id} is inactive")

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
                "You are a deterministic SLA monitor. Classify the health of the "
                f"service '{service_name}' strictly from the status page text below. "
                "Do not use outside knowledge. Return a JSON object with exactly "
                'these keys: "status_code" (one of "OPERATIONAL", "DEGRADED", '
                '"MAJOR_OUTAGE"), "severity_score" (one of 0, 50, 100), '
                '"is_sla_breached" (true or false), "summary" (a short plain-text '
                "explanation citing the evidence you saw). "
                "OPERATIONAL means fully up, DEGRADED means partial or impaired "
                "service, MAJOR_OUTAGE means unavailable or down.\n\n"
                f"STATUS PAGE TEXT:\n{str(page)[:4000]}"
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
                "status page. They must agree exactly on status_code, severity_score "
                "and is_sla_breached. The summary wording may differ; ignore it. "
                "If either answer is missing any decision field or uses a value "
                "outside the allowed enumerations, they do NOT agree."
            ),
        )

        status_code = snapshot["status_code"]
        severity_score = snapshot["severity_score"]

        policy.total_checks = policy.total_checks + 1

        slashed_amount = 0
        if status_code == "DEGRADED" or status_code == "MAJOR_OUTAGE":
            policy.failed_checks = policy.failed_checks + 1

            # Deterministic slash: collateral * slash_rate_bps / 10000, then
            # scaled by the severity weight (50% for DEGRADED, 100% for outage).
            base_slash = (policy.collateral_pool * policy.slash_rate_bps) // BASIS_POINTS_MAX
            proposed_slash = (base_slash * severity_score) // SEVERITY_WEIGHT_MAX
            if proposed_slash > policy.collateral_pool:
                proposed_slash = policy.collateral_pool

            subs = self.policy_subscribers.get_or_insert_default(policy_id)
            subscriber_count = len(subs)
            if subscriber_count > 0 and proposed_slash > 0:
                per_sub = proposed_slash // subscriber_count
                if per_sub > 0:
                    # Only the amount actually credited leaves the pool, so the
                    # collateral decrease always equals the claimable increase.
                    moved = per_sub * subscriber_count
                    for addr in subs:
                        current = self.claimable_balances.get(addr, 0)
                        self.claimable_balances[addr] = current + per_sub
                    policy.collateral_pool = policy.collateral_pool - moved
                    slashed_amount = moved
                # else: proposed slash is smaller than the subscriber count, so it
                # cannot be split. Nothing moves and slashed_amount stays 0, which
                # keeps the incident record honest (never overstates the penalty).
            elif subscriber_count == 0 and proposed_slash > 0:
                # Penalty forfeit: a confirmed outage with no subscriber to pay
                # still reduces the provider's collateral and accrues to no one.
                policy.collateral_pool = policy.collateral_pool - proposed_slash
                slashed_amount = proposed_slash

        incident_id = self.next_incident_id
        self.incidents[incident_id] = IncidentRecord(
            incident_id=incident_id,
            policy_id=policy_id,
            status_code=status_code,
            severity_score=severity_score,
            slashed_amount=slashed_amount,
            summary=str(snapshot.get("summary", ""))[:280],
        )
        self.next_incident_id = incident_id + 1
        self.policies[policy_id] = policy

        return status_code

    @gl.public.write
    def claim_compensation(self) -> u256:
        sender = gl.message.sender_address
        amount = self.claimable_balances.get(sender, 0)
        if amount == 0:
            return 0
        # Reset before returning so a claim cannot be replayed.
        self.claimable_balances[sender] = 0
        return amount

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
            "slash_rate_bps": int(p.slash_rate_bps),
            "total_checks": int(p.total_checks),
            "failed_checks": int(p.failed_checks),
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
            "severity_score": int(rec.severity_score),
            "slashed_amount": int(rec.slashed_amount),
            "summary": rec.summary,
        }

    @gl.public.view
    def get_compliance_metrics(self, policy_id: u256) -> dict:
        if policy_id not in self.policies:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown policy {policy_id}")
        p = self.policies[policy_id]
        total = int(p.total_checks)
        failed = int(p.failed_checks)
        if total == 0:
            uptime_bps = BASIS_POINTS_MAX
        else:
            uptime_bps = ((total - failed) * BASIS_POINTS_MAX) // total
        target = int(p.target_uptime_bps)
        return {
            "policy_id": int(p.policy_id),
            "service_name": p.service_name,
            "total_checks": total,
            "failed_checks": failed,
            "uptime_bps": uptime_bps,
            "target_uptime_bps": target,
            "compliant": uptime_bps >= target,
            "breach_margin_bps": uptime_bps - target,
        }

    @gl.public.view
    def get_claimable_balance(self, account: str) -> u256:
        return self.claimable_balances.get(Address(account), 0)
