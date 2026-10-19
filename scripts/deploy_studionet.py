"""Deploy SLAUptimeInsurance to GenLayer studionet and drive real transactions.

This is the documented deploy path, driven through the SDK primitive
(`client.deploy_contract`) so it is fully scriptable from a headless session. The
private key is read from the git-ignored `.env` and is never printed, logged or
committed.

studionet is gasless, so a 0 GEN deployer balance is expected and fine.

Flow
----
1. submit the verbatim contract source as the deploy code and wait for FINALIZED;
2. resolve the created contract address from the deployment receipt;
3. confirm the public explorer indexes that address as a CONTRACT;
4. seed BOTH settlement outcomes on-chain through the real consensus audit:
     * a COMPLIANT policy (measured uptime at/above its declared target) that,
       once settled, is slashed ZERO and fully refunds the provider, and whose
       subscriber compensation path reverts on-chain;
     * a policy declared at a strict 100.00% target so any real measured
       shortfall makes it a BREACH that is slashed by the shortfall formula and
       compensates the subscriber;
5. read state back through the view methods;
6. verify 3-way source integrity (local file == on-chain code bytes);
7. persist every transaction hash + the address to evidence/studionet-deploy.json.

Run:
    python scripts/deploy_studionet.py
Optionally skip a fresh deploy and reuse an existing address:
    SLA_CONTRACT_ADDRESS=0x... python scripts/deploy_studionet.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from genlayer_py import create_account, create_client, studionet
from genlayer_py.types.transactions import TransactionStatus

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "sla_uptime_insurance.py"
EVIDENCE = ROOT / "evidence"

DEFAULT_TIMEOUT = int(os.environ.get("GENLAYER_DEPLOY_TIMEOUT", "900"))

# Collateral locked by each demo provider policy, kept identical to the value the
# integration test reasons about so a green run and the evidence agree.
DEMO_COLLATERAL = 1_000_000
# A real, always-reachable public status page that the consensus audit re-fetches.
DEMO_STATUS_URL = "https://www.githubstatus.com/api/v2/status.json"
# Case 1: a realistic target a healthy service comfortably meets (compliant).
DEMO_TARGET_COMPLIANT = 9000
# Case 2: a strict 100.00% target, so any real measured shortfall is a true breach.
DEMO_TARGET_BREACH = 10000


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_chain_code(client, address: str) -> bytes:
    # gen_getContractCode returns the deployed source as base64. Decoding it
    # gives the exact bytes the chain runs, which is what integrity is checked
    # against. Raises if the RPC returns an error object.
    resp = client.w3.provider.make_request("gen_getContractCode", [address])
    if "error" in resp:
        raise SystemExit(f"gen_getContractCode failed for {address}: {resp['error']}")
    result = resp.get("result")
    if not isinstance(result, str):
        raise SystemExit(f"gen_getContractCode returned no code string for {address}")
    return base64.b64decode(result)


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def status_name(tx: dict) -> str:
    name = tx.get("status_name")
    if name:
        return str(name)
    by_number = {int(s.value): s.name for s in TransactionStatus if str(s.value).isdigit()}
    return by_number.get(tx.get("status"), str(tx.get("status")))


def result_name(tx: dict) -> str:
    return str(tx.get("result_name") or tx.get("result") or "").upper()


def _leader_result(receipt: dict) -> dict:
    cd = receipt.get("consensus_data") or {}
    lr = cd.get("leader_receipt") or []
    if lr and isinstance(lr[0], dict):
        return lr[0].get("result") or {}
    return {}


def exec_status(receipt: dict) -> str:
    # 'return' = applied; 'rollback'/'error' = reverted, no state change.
    return str(_leader_result(receipt).get("status", "")).lower()


def exec_payload(receipt: dict):
    p = _leader_result(receipt).get("payload")
    if isinstance(p, dict):
        return p.get("readable", p)
    return p


def wait_for_final(client, tx_hash: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            tx = client.get_transaction(tx_hash)
        except Exception as exc:  # not indexed yet
            print(f"  [wait] {label}: not readable yet ({type(exc).__name__})")
            time.sleep(4)
            continue
        name = status_name(tx)
        if name != last:
            print(f"  [wait] {label}: {name} (result={result_name(tx)})")
            last = name
        if name == "FINALIZED":
            return tx
        if name in {"CANCELED", "ERROR", "INVALID"}:
            raise SystemExit(f"{label} settled in terminal lifecycle state {name}: {tx}")
        time.sleep(4)
    raise SystemExit(f"{label} did not finalize within {timeout}s (last {last})")


def assert_execution_ok(receipt: dict, label: str) -> None:
    # FINALIZED + MAJORITY_AGREE are lifecycle/consensus states, NOT proof the
    # code applied. Gate every read on the leader execution status.
    st = exec_status(receipt)
    if st != "return":
        raise SystemExit(
            f"{label} finalized but leader execution status was {st!r}; "
            f"payload={exec_payload(receipt)!r}"
        )


def retry_read(fn, label: str, attempts: int = 12, delay: int = 6):
    # studionet reads can drop the SSL connection mid-call (transient
    # SSLEOFError). Reads are pure and idempotent, so retry the same call rather
    # than tearing down an otherwise-good deployment run.
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"  [retry] {label}: read attempt {i + 1} failed ({type(exc).__name__}); sleeping {delay}s")
            time.sleep(delay)
    raise SystemExit(f"{label} failed after {attempts} read attempts: {last}")


def explorer_type(address: str) -> str:
    import urllib.error
    import urllib.request

    url = f"https://explorer-studio.genlayer.com/api/address/{address}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "sla-uptime-insurance-deploy"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return (data.get("type") or "").upper()
    except urllib.error.HTTPError as exc:
        return f"http-{exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}"


def deploy_contract(client, account) -> tuple[str, str]:
    source = CONTRACT.read_text(encoding="utf-8")
    print(f"[deploy] submitting {CONTRACT.name} ({len(source)} chars) ...")
    tx_hash = str(client.deploy_contract(code=source, account=account))
    print(f"[deploy] tx={tx_hash}")
    receipt = wait_for_final(client, tx_hash, DEFAULT_TIMEOUT, "deploy SLAUptimeInsurance")
    assert_execution_ok(receipt, "deploy")
    address = str(receipt.get("recipient") or receipt.get("to_address") or "")
    if not address:
        raise SystemExit(f"deploy finalized but no contract address on {tx_hash}")

    kind = ""
    for _ in range(12):
        kind = explorer_type(address)
        if kind == "CONTRACT":
            break
        time.sleep(5)
    print(f"[deploy] contract address = {address} (explorer type={kind!r})")
    print(f"[deploy] explorer: https://explorer-studio.genlayer.com/address/{address}")
    if kind != "CONTRACT":
        print("[deploy] WARNING: explorer does not yet index this as a CONTRACT", file=sys.stderr)
    return address, tx_hash


def _submit(client, account, address, method, args, label, txs):
    # Submit one write, wait for FINALIZED, and record the leader execution result
    # (without hard-failing) so an adversarial/reverted path is captured too.
    print(f"\n[write] {method} ({label}) ...")
    tx_hash = str(client.write_contract(address, method, account=account, args=args))
    print(f"    tx={tx_hash}")
    receipt = wait_for_final(client, tx_hash, DEFAULT_TIMEOUT, f"{method}:{label}")
    st = exec_status(receipt)
    payload = exec_payload(receipt)
    rec = {
        "step": f"{method}:{label}",
        "method": method,
        "args": args,
        "tx": tx_hash,
        "consensus": result_name(receipt),
        "exec_status": st,
        "finalized": status_name(receipt) == "FINALIZED",
    }
    if st == "return":
        rec["return_data"] = payload
    else:
        rec["revert_reason"] = payload
    txs.append(rec)
    print(f"    exec_status={st} payload={payload!r}")
    return receipt, st, payload


def _seed_case(client, provider, subscriber, address, name, target, txs) -> dict:
    """Drive one policy through the full on-chain lifecycle:
    register -> subscribe -> evaluate (live consensus audit) -> (compliant guard)
    -> settle, reading back the enforced SLA-breach outcome.
    """
    print(f"\n===== seeding CASE '{name}' with target_uptime_bps={target} =====")

    _, st, pid_payload = _submit(
        client, provider, address, "register_policy",
        [f"svc-{name}", DEMO_STATUS_URL, target, DEMO_COLLATERAL], f"{name}:register", txs,
    )
    if st != "return":
        raise SystemExit(f"register_policy for case '{name}' did not apply on chain: {pid_payload!r}")
    pid = int(pid_payload)

    # A distinct subscriber account signs up so provider and subscriber are separate.
    _submit(client, subscriber, address, "subscribe_to_policy", [pid], f"{name}:subscribe", txs)

    # The live consensus audit: validators re-fetch DEMO_STATUS_URL and agree on a
    # bounded measured uptime; compliance is then derived from target vs measured.
    # This step is best-effort: it depends on a live web render + LLM reaching
    # validator consensus, so a transient/rotation failure is captured honestly
    # rather than aborting the whole run.
    audit_note = None
    status_code = None
    try:
        rc_eval, st_eval, status_code = _submit(
            client, provider, address, "evaluate_service_health", [pid], f"{name}:evaluate", txs,
        )
        evaluated = st_eval == "return"
        if not evaluated:
            audit_note = f"live audit reverted on chain: {status_code!r}"
    except SystemExit as exc:  # wait_for_final timed out on the nondeterministic audit
        evaluated = False
        status_code = None
        audit_note = f"live audit did not finalize in time: {exc}"

    # Read the authoritative on-chain state produced by the audit.
    metrics = retry_read(
        lambda: client.read_contract(address, "get_compliance_metrics", args=[pid]),
        f"{name}:metrics",
    )
    is_breached = retry_read(
        lambda: client.read_contract(address, "is_sla_breached", args=[pid]),
        f"{name}:is_sla_breached",
    )
    print(f"    metrics={json.dumps(metrics, default=str)}")
    print(f"    is_sla_breached={is_breached}")

    # On a COMPLIANT policy the subscriber compensation path must revert on-chain.
    claim_guard_reverted = None
    claim_guard_reason = None
    if evaluated and not is_breached:
        _, st_claim, claim_payload = _submit(
            client, subscriber, address, "claim_sla_compensation", [pid], f"{name}:claim-guard", txs,
        )
        claim_guard_reverted = st_claim != "return"
        claim_guard_reason = claim_payload

    # Settle against the single predicate: compliant -> 0 slash/full refund;
    # breach -> shortfall-formula slash credited to subscribers.
    _, st_settle, slash_payload = _submit(
        client, provider, address, "settle_policy", [pid], f"{name}:settle", txs,
    )
    settlement = None
    if st_settle == "return":
        settlement = retry_read(
            lambda: client.read_contract(address, "get_settlement", args=[pid]),
            f"{name}:settlement",
        )
        print(f"    settlement={json.dumps(settlement, default=str)}")

    provider_balance = retry_read(
        lambda: client.read_contract(address, "get_claimable_balance", args=[provider.address]),
        f"{name}:provider_balance",
    )

    return {
        "case": name,
        "policy_id": pid,
        "declared_target_uptime_bps": target,
        "audit_status_code": status_code if evaluated else None,
        "evaluated_on_chain": evaluated,
        "metrics": metrics,
        "is_sla_breached": is_breached,
        "subscriber_compensation_guard_reverted": claim_guard_reverted,
        "subscriber_compensation_guard_reason": claim_guard_reason,
        "settle_exec_status": st_settle,
        "slashed_amount": int(slash_payload) if st_settle == "return" and slash_payload is not None else None,
        "settlement": settlement,
        "provider_claimable_after_settle": provider_balance,
        "audit_note": audit_note,
    }


def _deterministic_guard_case(client, provider, subscriber, address, txs) -> dict:
    """A settlement/predicate demo that needs NO web render and NO LLM call, so it
    finalizes reliably even when the live audit is slow. It proves on chain that an
    un-audited policy is never slashed, that the compensation path requires an
    evaluation, and that an outsider cannot force a settlement.
    """
    print("\n===== seeding CASE 'guard' (deterministic, no web/LLM) =====")
    target = 9900
    _, st_reg, pid_payload = _submit(
        client, provider, address, "register_policy",
        ["svc-guard", DEMO_STATUS_URL, target, DEMO_COLLATERAL], "guard:register", txs,
    )
    pid = int(pid_payload)
    _submit(client, subscriber, address, "subscribe_to_policy", [pid], "guard:subscribe", txs)

    # A subscriber compensation claim on a policy with no recorded evaluation must
    # revert (the predicate is not yet decidable), so nothing can be slashed.
    _, st_claim, claim_payload = _submit(
        client, subscriber, address, "claim_sla_compensation", [pid], "guard:claim-unevaluated", txs,
    )
    claim_reverted = st_claim != "return"

    # An outsider (neither provider nor subscriber) must not be able to settle.
    outsider = create_account()
    _, st_out, out_payload = _submit(
        client, outsider, address, "settle_policy", [pid], "guard:outsider-settle", txs,
    )
    outsider_reverted = st_out != "return"

    # The provider settling an unevaluated policy takes the COMPLIANT path: zero
    # slash and a full refund of the pool, with no web/LLM dependency.
    _, st_settle, slash_payload = _submit(
        client, provider, address, "settle_policy", [pid], "guard:settle-unevaluated", txs,
    )
    settlement = None
    if st_settle == "return":
        settlement = retry_read(
            lambda: client.read_contract(address, "get_settlement", args=[pid]), "guard:settlement",
        )
    provider_balance = retry_read(
        lambda: client.read_contract(address, "get_claimable_balance", args=[provider.address]),
        "guard:provider_balance",
    )
    return {
        "case": "guard",
        "policy_id": pid,
        "declared_target_uptime_bps": target,
        "claim_requires_evaluation_reverted": claim_reverted,
        "claim_reason": claim_payload,
        "outsider_settle_reverted": outsider_reverted,
        "outsider_settle_reason": out_payload,
        "settle_exec_status": st_settle,
        "slashed_amount": int(slash_payload) if st_settle == "return" and slash_payload is not None else None,
        "settlement": settlement,
        "provider_claimable_after_settle": provider_balance,
    }


def drive(client, account, address: str) -> dict:
    txs: list[dict] = []
    # A second, ephemeral signer plays the subscriber role so provider and
    # subscriber are distinct accounts. studionet is gasless, so it needs no
    # balance. Its private key is random and discarded after the run.
    subscriber = create_account()

    # 1. Deterministic guard case: always runs, no web/LLM, proves the enforced
    #    predicate and settlement rules on chain.
    guard = _deterministic_guard_case(client, account, subscriber, address, txs)

    # 2 & 3. Best-effort live consensus audits for a compliant and a strict target.
    #    Wrapped so a slow/rotating LLM audit cannot wipe out the rest of the run.
    def _safe_seed(name, target):
        try:
            return _seed_case(client, account, subscriber, address, name, target, txs)
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            return {"case": name, "error": f"{type(exc).__name__}: {exc}"}

    compliant = _safe_seed("compliant", DEMO_TARGET_COMPLIANT)
    breach = _safe_seed("breach", DEMO_TARGET_BREACH)

    # Adversarial input must revert on chain too (proof the guards still bind).
    _, st_bad, bad_payload = _submit(
        client, account, address, "register_policy",
        ["bad", "ftp://nope", 9990, 1000], "invalid-register", txs,
    )

    finalized = [t for t in txs if t.get("finalized")]
    return {
        "subscriber": subscriber.address,
        "guard_policy": guard,
        "compliant_policy": compliant,
        "breach_policy": breach,
        "invalid_reverted": st_bad != "return",
        "invalid_revert_reason": bad_payload,
        "transactions": txs,
        "finalized_count": len(finalized),
    }


def main() -> int:
    load_dotenv()
    private_key = os.environ.get("GENLAYER_PRIVATE_KEY")
    if not private_key:
        print("[deploy] GENLAYER_PRIVATE_KEY is not set", file=sys.stderr)
        return 2

    account = create_account(private_key)
    client = create_client(studionet, account=account)
    print(f"[deploy] chainId={studionet.id} deployer={account.address}")

    reused = os.environ.get("SLA_CONTRACT_ADDRESS")
    if reused:
        address = reused
        deploy_tx = os.environ.get("SLA_DEPLOY_TX", "(reused)")
        print(f"[deploy] reusing existing contract {address} (deploy_tx={deploy_tx})")
    else:
        address, deploy_tx = deploy_contract(client, account)

    evidence = drive(client, account, address)

    # 3-way integrity starts here: the bytes we submit (read_text normalizes to
    # LF, which .gitattributes pins for the repo) versus the bytes the chain
    # actually runs, pulled back through gen_getContractCode.
    local_bytes = CONTRACT.read_text(encoding="utf-8").encode("utf-8")
    chain_bytes = fetch_chain_code(client, address)
    integrity = {
        "sha256_local": sha256_hex(local_bytes),
        "sha256_chain": sha256_hex(chain_bytes),
        "local_bytes": len(local_bytes),
        "chain_bytes": len(chain_bytes),
        "match_local_chain": local_bytes == chain_bytes,
    }
    if not integrity["match_local_chain"]:
        print("[deploy] ERROR: on-chain code does not match the local source", file=sys.stderr)
    print("[deploy] sha256 local  = " + integrity["sha256_local"])
    print("[deploy] sha256 chain  = " + integrity["sha256_chain"])

    record = {
        "network": {"name": "studionet", "chain_id": studionet.id, "rpc_urls": list(studionet.rpc_urls)},
        "deployer": account.address,
        "contract_address": address,
        "deploy_tx": deploy_tx,
        "explorer": f"https://explorer-studio.genlayer.com/address/{address}",
        "runner": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6",
        "integrity": integrity,
        "evidence": evidence,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / "studionet-deploy.json"
    out.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    print(f"\n[deploy] wrote {out}")
    summ = compliant_summary(evidence)
    summary = {
        "contract_address": address,
        "compliant": {"is_sla_breached": summ["compliant_breached"], "slashed": summ["compliant_slash"]},
        "breach": {"is_sla_breached": summ["breach_breached"], "slashed": summ["breach_slash"]},
    }
    print(json.dumps(summary, indent=2))

    if not integrity["match_local_chain"]:
        return 1
    return 0


def compliant_summary(evidence: dict) -> dict:
    c = evidence.get("compliant_policy", {})
    b = evidence.get("breach_policy", {})
    g = evidence.get("guard_policy", {})
    return {
        "guard_slash": g.get("slashed_amount"),
        "guard_claim_reverted": g.get("claim_requires_evaluation_reverted"),
        "guard_outsider_reverted": g.get("outsider_settle_reverted"),
        "compliant_breached": c.get("is_sla_breached"),
        "compliant_slash": c.get("slashed_amount"),
        "breach_breached": b.get("is_sla_breached"),
        "breach_slash": b.get("slashed_amount"),
    }


if __name__ == "__main__":
    sys.exit(main())
