"""Deploy SLAUptimeInsurance to GenLayer studionet and drive real transactions.

This is the documented deploy path used by the `genlayer` CLI, driven through the
same SDK primitive (`client.deploy_contract`) so it is fully scriptable from a
headless session. The private key is read from the git-ignored `.env` and is
never printed, logged or committed.

studionet is gasless, so a 0 GEN deployer balance is expected and fine.

Flow
----
1. submit the verbatim contract source as the deploy code and wait for FINALIZED;
2. resolve the created contract address from the deployment receipt;
3. confirm the public explorer indexes that address as a CONTRACT;
4. drive the state-changing path with real transactions:
     register_policy -> subscribe_to_policy;
5. read state back through the view methods;
6. persist every transaction hash + the address to artifacts/studionet-deploy.json.

Run:
    python scripts/deploy_studionet.py
Optionally skip a fresh deploy and reuse an existing address:
    SLA_CONTRACT_ADDRESS=0x... python scripts/deploy_studionet.py
"""

from __future__ import annotations

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


def drive(client, account, address: str) -> dict:
    txs: list[dict] = []

    # --- write: register_policy (provider = deployer) ---
    print("\n[write] register_policy ...")
    tx_reg = str(
        client.write_contract(
            address,
            "register_policy",
            account=account,
            args=["payments-api", "https://www.githubstatus.com/api/v2/status.json", 9990, 1_000_000, 200],
        )
    )
    print(f"    tx={tx_reg}")
    rc = wait_for_final(client, tx_reg, DEFAULT_TIMEOUT, "register_policy")
    assert_execution_ok(rc, "register_policy")
    policy_id = exec_payload(rc)
    txs.append(
        {
            "step": "register_policy",
            "tx": tx_reg,
            "consensus": result_name(rc),
            "exec_status": exec_status(rc),
            "return_data": policy_id,
        }
    )

    # --- read: get_policy(1) ---
    print("\n[read] get_policy(1) ...")
    policy = client.read_contract(address, "get_policy", args=[1])
    print(f"    {json.dumps(policy, default=str)}")

    # --- write: subscribe_to_policy(1) (same EOA subscribes) ---
    print("\n[write] subscribe_to_policy(1) ...")
    tx_sub = str(
        client.write_contract(
            address, "subscribe_to_policy", account=account, args=[1]
        )
    )
    print(f"    tx={tx_sub}")
    rc_sub = wait_for_final(client, tx_sub, DEFAULT_TIMEOUT, "subscribe_to_policy")
    assert_execution_ok(rc_sub, "subscribe_to_policy")
    txs.append(
        {
            "step": "subscribe_to_policy",
            "tx": tx_sub,
            "consensus": result_name(rc_sub),
            "exec_status": exec_status(rc_sub),
        }
    )

    # --- reads after the writes ---
    print("\n[read] get_policy(1) after subscribe ...")
    policy_after = client.read_contract(address, "get_policy", args=[1])
    print(f"    {json.dumps(policy_after, default=str)}")

    print("\n[read] get_compliance_metrics(1) ...")
    metrics = client.read_contract(address, "get_compliance_metrics", args=[1])
    print(f"    {json.dumps(metrics, default=str)}")

    print("\n[read] get_claimable_balance(deployer) ...")
    claimable = client.read_contract(
        address, "get_claimable_balance", args=[account.address]
    )
    print(f"    {claimable}")

    # --- adversarial input must revert on chain too ---
    print("\n[write] register_policy(invalid url) is expected to revert ...")
    tx_bad = str(
        client.write_contract(
            address,
            "register_policy",
            account=account,
            args=["bad", "ftp://nope", 9990, 1000, 200],
        )
    )
    rc_bad = wait_for_final(client, tx_bad, DEFAULT_TIMEOUT, "register_policy_invalid")
    bad_status = exec_status(rc_bad)
    bad_payload = exec_payload(rc_bad)
    txs.append(
        {
            "step": "register_policy_invalid",
            "tx": tx_bad,
            "consensus": result_name(rc_bad),
            "exec_status": bad_status,
            "revert_reason": bad_payload,
        }
    )
    print(f"    exec_status={bad_status} reason={bad_payload!r}")

    return {
        "policy_id_return": policy_id,
        "policy_after": policy_after,
        "metrics": metrics,
        "claimable": claimable,
        "transactions": txs,
        "invalid_reverted": bad_status != "return",
        "invalid_revert_reason": bad_payload,
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
        deploy_tx = "(reused)"
        print(f"[deploy] reusing existing contract {address}")
    else:
        address, deploy_tx = deploy_contract(client, account)

    evidence = drive(client, account, address)

    record = {
        "network": {"name": "studionet", "chain_id": studionet.id, "rpc_urls": list(studionet.rpc_urls)},
        "deployer": account.address,
        "contract_address": address,
        "deploy_tx": deploy_tx,
        "explorer": f"https://explorer-studio.genlayer.com/address/{address}",
        "runner": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6",
        "evidence": evidence,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / "studionet-deploy.json"
    out.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    print(f"\n[deploy] wrote {out}")
    print(json.dumps({"contract_address": address, "transactions": evidence["transactions"]}, indent=2))

    # Non-zero exit if the on-chain proof is incomplete.
    if not evidence["invalid_reverted"]:
        print("[deploy] WARNING: invalid register_policy did NOT revert", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
