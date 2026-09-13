"""
RiverPay Exception Agent — hand-written runtime.

Flow (per event):  gather_evidence -> decide -> build messages -> validate -> collect
Then write output/decisions.json and print a short report.

The decision itself lives in policy.decide(). Everything here is plumbing: reading the
CSVs (tools), gathering evidence deterministically, and phrasing the result.

Run:  python src/agent.py
"""

import csv
import json
from collections import Counter
from pathlib import Path

from policy import decide, KYC_LIMITS

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "output" / "decisions.json"

DECISIONS = {
    "AUTO_RESOLVE", "AUTO_NOTIFY", "AUTO_BLOCK",
    "ESCALATE_FRAUD_OPS", "ESCALATE_COMPLIANCE", "ESCALATE_TECH_OPS",
    "ESCALATE_RECONCILIATION", "ESCALATE_AGENT_NETWORK", "ESCALATE_DATA_QUALITY",
}

NEXT_ACTION = {
    "AUTO_RESOLVE": "close with customer notification; no money movement.",
    "AUTO_NOTIFY": "notify agent and territory manager.",
    "AUTO_BLOCK": "wallet frozen; open compliance/fraud case.",
    "ESCALATE_FRAUD_OPS": "route to fraud ops for review.",
    "ESCALATE_COMPLIANCE": "route to compliance/KYC; do not auto-approve identity.",
    "ESCALATE_TECH_OPS": "route to tech ops; recommend manual retry — do NOT auto-retry.",
    "ESCALATE_RECONCILIATION": "ledger and event disagree; reconcile before any action.",
    "ESCALATE_AGENT_NETWORK": "route to agent network team.",
    "ESCALATE_DATA_QUALITY": "missing/invalid identifiers; fix data before processing.",
}


# ---- Step 1: load the 5 systems of record once ----
def _read_csv(name):
    with open(BASE / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def load_data():
    customers = {}
    for r in _read_csv("customers.csv"):
        r["available_balance_kbr"] = int(r["available_balance_kbr"])
        customers[r["customer_id"]] = r

    ledger = _read_csv("ledger.csv")
    for r in ledger:
        r["amount_kbr"] = int(r["amount_kbr"])
    ledger_by_txn = {r["txn_id"]: r for r in ledger}

    agents = {}
    for r in _read_csv("agents.csv"):
        for k in ("float_kbr", "float_warning_kbr", "pending_cashout_queue"):
            r[k] = int(r[k])
        agents[r["agent_id"]] = r

    sanction_ids, sanction_names = set(), set()
    for r in _read_csv("sanctions.csv"):
        if r["customer_id"]:
            sanction_ids.add(r["customer_id"])
        if r["full_name"]:
            sanction_names.add(r["full_name"].lower())

    events = json.loads((BASE / "events.json").read_text(encoding="utf-8"))
    return {
        "customers": customers, "ledger": ledger, "ledger_by_txn": ledger_by_txn,
        "agents": agents, "sanction_ids": sanction_ids, "sanction_names": sanction_names,
        "events": events,
    }


# ---- Step 2: tools (each records itself in `used`) ----
def lookup_customer(store, cid, used):
    used.append("lookup_customer")
    return store["customers"].get(cid)

def lookup_transaction(store, txn_id, used):
    used.append("lookup_transaction")
    return store["ledger_by_txn"].get(txn_id)

def lookup_recent_transfers(store, payer_id, used):
    used.append("lookup_recent_transfers")
    return [t for t in store["ledger"] if t["payer_id"] == payer_id]

def lookup_agent(store, agent_id, used):
    used.append("lookup_agent")
    return store["agents"].get(agent_id)

def check_sanctions(store, cid, name, used):
    used.append("check_sanctions")
    return cid in store["sanction_ids"] or (bool(name) and name.lower() in store["sanction_names"])

def get_policy(used):
    used.append("get_policy")
    return "ops_policy R1-R13 v1"


# ---- Step 2b: deterministic evidence gathering (the event cannot shrink this) ----
def gather_evidence(event, store, used):
    cid = event.get("customer_id")
    txn_id = event.get("txn_id")
    agent_id = event.get("agent_id")
    get_policy(used)
    customer = lookup_customer(store, cid, used) if cid else None
    return {
        "customer": customer,
        "txn": lookup_transaction(store, txn_id, used) if txn_id else None,
        "agent": lookup_agent(store, agent_id, used) if agent_id else None,
        "recent": lookup_recent_transfers(store, cid, used) if cid else [],
        "sanctioned": check_sanctions(store, cid, customer["full_name"] if customer else None, used)
        if cid else False,
    }


# ---- Step 4: messages ----
def build_customer_message(decision, codes, event, facts):
    code = codes[0]
    if code == "R7" and facts["customer"]:
        limit = KYC_LIMITS[facts["customer"]["kyc_tier"]]
        return f"This transfer is above your current daily limit of {limit} KBR. No money left your wallet."
    if code == "R4" and decision == "AUTO_RESOLVE":
        return "You don't have enough balance for this transfer. No money left your wallet."
    if code == "R6":
        return "We couldn't find that recipient, so no money left your wallet. Please check the details and try again."
    if code == "R10" and decision == "AUTO_RESOLVE":
        return "For your security your PIN is locked. Reset it in-app or dial *123*5#."
    if decision == "AUTO_BLOCK":
        return "Your account is temporarily under review. We'll contact you shortly."
    if decision == "AUTO_NOTIFY":
        return "N/A — agent notification, no customer contact."
    if decision.startswith("ESCALATE"):
        return "We're reviewing your recent transaction and will update you shortly."
    return "We've reviewed your recent transaction. No money left your wallet."

def build_internal_case_note(decision, codes, event, facts, used):
    parts = [f"{decision} ({'/'.join(codes)}).",
             f"event={event['event_id']} type={event['event_type']} amount={event['amount_kbr']}."]
    c = facts["customer"]
    if c:
        parts.append(f"customer={c['customer_id']} tier={c['kyc_tier']} "
                     f"balance={c['available_balance_kbr']} wallet={c['wallet_status']}.")
    t = facts["txn"]
    if t:
        parts.append(f"ledger={t['txn_id']} status={t['status']} "
                     f"{t['payer_id']}->{t['payee_id']} {t['amount_kbr']}.")
    a = facts["agent"]
    if a:
        parts.append(f"agent={a['agent_id']} float={a['float_kbr']} "
                     f"queue={a['pending_cashout_queue']} mgr={a['territory_manager']}.")
    if facts["sanctioned"]:
        parts.append("SANCTIONS MATCH.")
    parts.append("next: " + NEXT_ACTION.get(decision, "review."))
    parts.append("tools: " + ", ".join(used) + ".")
    return " ".join(parts)


# ---- Step 5: assemble + validate ----
def build_decision_object(event, decision, codes, confidence, used, facts):
    return {
        "event_id": event["event_id"],
        "decision": decision,
        "reason_codes": codes,
        "customer_message": build_customer_message(decision, codes, event, facts),
        "internal_case_note": build_internal_case_note(decision, codes, event, facts, used),
        "tools_used": used,
        "confidence": confidence,
        "requires_human": decision not in ("AUTO_RESOLVE", "AUTO_NOTIFY"),
    }

def validate(obj):
    assert obj["decision"] in DECISIONS, f"bad decision: {obj['decision']}"
    assert obj["reason_codes"], "need at least one reason code"
    assert 0 <= obj["confidence"] <= 1, "confidence out of range"
    msg = obj["customer_message"].lower()
    assert "sanction" not in msg, "customer message leaked 'sanctions'"
    assert "fraud score" not in msg, "customer message leaked 'fraud score'"


# ---- Step 7: report ----
def print_report(decisions):
    counts = Counter(d["decision"] for d in decisions)
    print(f"\n{len(decisions)} events processed:")
    for decision, n in sorted(counts.items()):
        print(f"  {n:2}  {decision}")
    blocks = [d["event_id"] for d in decisions if d["decision"] == "AUTO_BLOCK"]
    print(f"AUTO_BLOCK: {blocks or 'none'}")


# ---- Orchestrator ----
def main():
    store = load_data()
    decisions = []
    for event in store["events"]:
        used = []
        facts = gather_evidence(event, store, used)
        decision, codes, confidence = decide(event, facts)
        decisions.append(build_decision_object(event, decision, codes, confidence, used, facts))

    for obj in decisions:
        validate(obj)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(decisions, indent=2), encoding="utf-8")
    print_report(decisions)


if __name__ == "__main__":
    main()
