"""Test runner for Metric 6: Cryptographic Audit Trail & Tampering."""
import json
import time
from eval.common.client import call_get_offer, setup_catalog
from eval.metric_06_audit_trail.db_helpers import (
    execute_with_tamper_restoration,
    get_latest_ledger_entry,
)
from .scenarios import SCENARIOS
from kernel import receipt
from store import ledger
from store.canonical import canonical_json
from store.db import get_connection


def run(conn=None):
    setup_catalog(conn)
    c = conn or get_connection()
    results = []
    print(f"\n=== Running Metric 6: Cryptographic Audit Trail ({len(SCENARIOS)} Scenarios) ===")

    # Ensure at least one fresh offer exists in the ledger
    offer = call_get_offer({"base_sku": "GE-ACER-ALITE", "need": "Acer laptop for audit verification"}, conn=c)
    policy_receipt = getattr(offer, "policy_receipt", {})

    for sc in SCENARIOS:
        a_type = sc.input["attack_type"]
        field = sc.input["field"]
        passed = False
        msg = ""

        try:
            entry = get_latest_ledger_entry(conn=c)
            seq = entry["seq"]

            if a_type == "baseline":
                report = ledger.verify_chain(conn=c)
                if report.get("intact") is True:
                    passed = True
                    msg = f"Hash chain intact at head seq {report.get('head_seq')}"
                else:
                    passed = False
                    msg = f"Baseline hash chain broken: {report}"

            elif a_type == "payload_tamper":
                orig_p_raw = entry["payload"]
                p_data = json.loads(orig_p_raw) if isinstance(orig_p_raw, str) else dict(orig_p_raw)
                orig_canonical = canonical_json(p_data)
                tampered_data = dict(p_data)
                tampered_data[field] = "TAMPERED_VAL_99999"
                tampered_p = canonical_json(tampered_data)

                report = execute_with_tamper_restoration(
                    seq=seq,
                    mutate_sql="UPDATE ledger SET payload = :p WHERE seq = :seq",
                    mutate_params={"p": tampered_p, "seq": seq},
                    restore_sql="UPDATE ledger SET payload = :p WHERE seq = :seq",
                    restore_params={"p": orig_canonical, "seq": seq},
                    conn=c,
                )
                if report.get("intact") is False:
                    passed = True
                    msg = f"Bit-level payload tampering on '{field}' caught at seq {report.get('broken_at')}"
                else:
                    passed = False
                    msg = f"Tamper went undetected: {report}"

            elif a_type == "receipt_replay":
                replayed = dict(policy_receipt)
                replayed["offer_id"] = "OF-FORGED-REPLAY-DESTINATION"
                replayed[field] = "FORGED_ATTACK_VALUE"
                is_valid = receipt.verify(replayed)
                if not is_valid:
                    passed = True
                    msg = f"Cross-order replay of signature bound to '{field}' rejected"
                else:
                    passed = False
                    msg = "Signature accepted for forged offer destination!"

            elif a_type == "prev_hash_break":
                orig_ph = entry["prev_hash"]
                bogus_ph = "0000000000000000000000000000000000000000000000000000000000000000"
                report = execute_with_tamper_restoration(
                    seq=seq,
                    mutate_sql="UPDATE ledger SET prev_hash = :ph WHERE seq = :seq",
                    mutate_params={"ph": bogus_ph, "seq": seq},
                    restore_sql="UPDATE ledger SET prev_hash = :ph WHERE seq = :seq",
                    restore_params={"ph": orig_ph, "seq": seq},
                    conn=c,
                )
                if report.get("intact") is False:
                    passed = True
                    msg = f"Chain break on previous hash detected at seq {report.get('broken_at')}"
                else:
                    passed = False
                    msg = "Chain break went undetected"

            elif a_type == "timestamp_tamper":
                orig_p_raw = entry["payload"]
                p_data = json.loads(orig_p_raw) if isinstance(orig_p_raw, str) else dict(orig_p_raw)
                orig_canonical = canonical_json(p_data)
                tampered_data = dict(p_data)
                tampered_data["timestamp"] = "2020-01-01T00:00:00Z"  # Backdated 6 years
                tampered_data[field] = "BACKDATED_ENTRY"
                tampered_p = canonical_json(tampered_data)

                report = execute_with_tamper_restoration(
                    seq=seq,
                    mutate_sql="UPDATE ledger SET payload = :p WHERE seq = :seq",
                    mutate_params={"p": tampered_p, "seq": seq},
                    restore_sql="UPDATE ledger SET payload = :p WHERE seq = :seq",
                    restore_params={"p": orig_canonical, "seq": seq},
                    conn=c,
                )
                if report.get("intact") is False:
                    passed = True
                    msg = f"Backdated timestamp tamper on '{field}' caught by hash mismatch"
                else:
                    passed = False
                    msg = "Timestamp backdating undetected"

            elif a_type == "signature_tamper":
                replayed = dict(policy_receipt)
                replayed["signature"] = "a" * 64  # Corrupted signature
                is_valid = receipt.verify(replayed)
                if not is_valid:
                    passed = True
                    msg = f"Corrupted HMAC signature rejected independently of '{field}' plausibility"
                else:
                    passed = False
                    msg = "Invalid HMAC signature accepted!"

        except Exception as exc:
            passed = False
            msg = f"Exception during audit test: {exc}"

        results.append({
            "id": sc.id,
            "name": sc.name,
            "metric": sc.metric,
            "passed": passed,
            "message": msg,
            "is_finding": sc.is_finding_not_failure,
        })
        status_tag = "PASS" if passed else "FAIL"
        print(f"  [{sc.id}] {sc.name:<55} -> [{status_tag}] {msg}")
        time.sleep(0.01)

    return results


if __name__ == "__main__":
    run()
