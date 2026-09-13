"""
Tests for the RiverPay Exception Agent.

  1. GOLDEN     — every event -> expected decision + rule (pins the traps).
  2. INVARIANTS — must hold for ALL events (safety net for cases nobody listed).
  3. TOOL UNIT  — the lookups, in isolation.

Run:  python -m pytest tests/ -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import agent


def _all():
    """Run the pipeline once; return {event_id: (obj, facts, event)}."""
    store = agent.load_data()
    out = {}
    for event in store["events"]:
        used = []
        facts = agent.gather_evidence(event, store, used)
        decision, codes, conf = agent.decide(event, facts)
        obj = agent.build_decision_object(event, decision, codes, conf, used, facts)
        out[event["event_id"]] = (obj, facts, event)
    return out


# ---- 1. Golden: expected decision + rule for all 19 events ----
GOLDEN = {
    "E001": ("AUTO_RESOLVE", "R4"),
    "E002": ("ESCALATE_RECONCILIATION", "R4"),   # balance 1800 >= 500 -> stale
    "E003": ("AUTO_RESOLVE", "R3"),              # real duplicate of a SUCCESS
    "E004": ("ESCALATE_TECH_OPS", "R5"),         # fake duplicate (different payee)
    "E005": ("AUTO_BLOCK", "R1"),
    "E006": ("ESCALATE_FRAUD_OPS", "R2"),
    "E007": ("ESCALATE_FRAUD_OPS", "R2"),        # 8000 >= 5000
    "E008": ("AUTO_RESOLVE", "R6"),
    "E009": ("AUTO_RESOLVE", "R7"),              # C004 is KYC0
    "E010": ("AUTO_NOTIFY", "R8"),
    "E011": ("ESCALATE_AGENT_NETWORK", "R8"),
    "E012": ("ESCALATE_COMPLIANCE", "R9"),
    "E013": ("AUTO_RESOLVE", "R10"),
    "E014": ("ESCALATE_FRAUD_OPS", "R10"),       # 3000 >= 2000
    "E015": ("AUTO_BLOCK", "R11"),               # C005 on sanctions
    "E016": ("ESCALATE_TECH_OPS", "R5"),
    "E017": ("ESCALATE_DATA_QUALITY", "R13"),    # txn_id missing
    "E018": ("ESCALATE_RECONCILIATION", "R12"),  # event says done, ledger FAILED
    "E019": ("ESCALATE_FRAUD_OPS", "R2"),        # cross-border
}

def test_golden_decisions():
    res = _all()
    for eid, (decision, code) in GOLDEN.items():
        obj = res[eid][0]
        assert obj["decision"] == decision, f"{eid}: got {obj['decision']}, expected {decision}"
        assert code in obj["reason_codes"], f"{eid}: got {obj['reason_codes']}, expected {code}"


# ---- 2. Invariants: hold for every event ----
def test_never_claims_false_success():
    for eid, (obj, facts, _e) in _all().items():
        msg = obj["customer_message"].lower()
        if any(w in msg for w in ("success", "succeeded", "sent", "completed")):
            assert facts["txn"] and facts["txn"]["status"] == "SUCCESS", eid

def test_customer_message_never_leaks_sanctions():
    for _eid, (obj, _f, _e) in _all().items():
        assert "sanction" not in obj["customer_message"].lower()

def test_mandatory_tools_ran():
    for eid, (obj, _f, event) in _all().items():
        if event.get("customer_id"):
            assert "check_sanctions" in obj["tools_used"], eid
            assert "lookup_customer" in obj["tools_used"], eid

def test_unmatched_event_fails_closed():
    event = {
        "event_id": "X", "event_type": "weird", "customer_id": None,
        "txn_id": None, "agent_id": None, "amount_kbr": 10,
        "received_at": "2026-08-20T09:00:00Z",
        "payload": {"failure_reason": None, "fraud_score": 0.0, "new_device": False,
                    "new_beneficiary": False, "cross_border": False},
    }
    facts = {"customer": None, "txn": None, "agent": None, "recent": [], "sanctioned": False}
    decision, _codes, conf = agent.decide(event, facts)
    assert decision.startswith("ESCALATE") and conf < agent.decide.__globals__["LOW_CONF"]


# ---- 3. Tool unit tests ----
def test_lookup_customer_reads_master_not_event():
    store = agent.load_data()
    c = agent.lookup_customer(store, "C004", [])
    assert c["kyc_tier"] == "KYC0" and c["available_balance_kbr"] == 12

def test_check_sanctions_matches_id_and_name():
    store = agent.load_data()
    assert agent.check_sanctions(store, "C005", None, [])
    assert agent.check_sanctions(store, "CXXX", "viktor malenkov", [])
    assert not agent.check_sanctions(store, "C001", "Ama Boateng", [])

def test_duplicate_window_true_and_false():
    res = _all()
    assert res["E003"][0]["decision"] == "AUTO_RESOLVE"       # matches recent SUCCESS
    assert res["E004"][0]["decision"] == "ESCALATE_TECH_OPS"  # different payee -> not a dup


# Runs under pytest, or standalone: `python tests/test_agent.py` (no pytest needed).
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
