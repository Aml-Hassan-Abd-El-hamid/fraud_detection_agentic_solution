"""AUTO-GENERATED from policy.dsl.json by policy_compiler.py — do not edit by hand.
decide(event, facts) -> (decision, reason_codes, confidence). Rules in precedence order."""


from datetime import datetime


KYC_LIMITS = {"KYC0": 50, "KYC1": 2000, "KYC2": 20000}


DUP_WINDOW = 120


LOW_CONF = 0.6


REQUIRED = {
    "transfer_failed": [
        "customer_id",
        "txn_id"
    ],
    "duplicate_suspected": [
        "customer_id",
        "txn_id"
    ],
    "transfer_completed": [
        "customer_id",
        "txn_id"
    ],
    "pin_locked": [
        "customer_id",
        "txn_id"
    ],
    "kyc_mismatch": [
        "customer_id"
    ],
    "agent_float_low": [
        "agent_id"
    ]
}


def decide(event, facts):
    for rule in (_r11, _r1, _r13, _r12, _r9, _r2, _r10, _r3, _r4, _r5, _r6, _r7, _r8,):
        result = rule(event, facts)
        if result:
            return result
    return ("ESCALATE_FRAUD_OPS", ["No matching policy rule; default escalation"], 0.0)


def _r1(event, facts):
    p = event["payload"]
    if p["fraud_score"] >= 0.9 and (p["new_device"] or p["new_beneficiary"]) and event["amount_kbr"] >= 1000:
        return ("AUTO_BLOCK", ["R1"], 1.0)


def _r2(event, facts):
    p = event["payload"]
    if p["fraud_score"] >= 0.7 or event["amount_kbr"] >= 5000 or p["cross_border"]:
        return ("ESCALATE_FRAUD_OPS", ["R2"], 1.0)


def _r3(event, facts):
    if event["event_type"] != "duplicate_suspected" or not facts["txn"]:
        return None
    this = facts["txn"]
    for t in facts["recent"]:
        if t["status"] == "SUCCESS" and t["payer_id"] == this["payer_id"] and t["payee_id"] == this["payee_id"] and t["amount_kbr"] == this["amount_kbr"] and 0 <= _seconds_before(event, t) <= DUP_WINDOW:
            return ("AUTO_RESOLVE", ["R3"], 1.0)


def _r4(event, facts):
    if event["payload"].get("failure_reason") != "INSUFFICIENT_FUNDS" or not facts["customer"]:
        return None
    if facts["customer"]["available_balance_kbr"] < event["amount_kbr"]:
        return ("AUTO_RESOLVE", ["R4"], 1.0)
    return ("ESCALATE_RECONCILIATION", ["R4"], 1.0)


def _r5(event, facts):
    p = event["payload"]
    if p.get("failure_reason") in ('TIMEOUT', 'UNKNOWN') and event["amount_kbr"] < 5000 and p["fraud_score"] < 0.7:
        return ("ESCALATE_TECH_OPS", ["R5"], 1.0)


def _r6(event, facts):
    if event["payload"].get("failure_reason") == "BENEFICIARY_NOT_FOUND":
        return ("AUTO_RESOLVE", ["R6"], 1.0)


def _r7(event, facts):
    if event["payload"].get("failure_reason") == "LIMIT_EXCEEDED":
        return ("AUTO_RESOLVE", ["R7"], 1.0)


def _r8(event, facts):
    a = facts["agent"]
    if not a:
        return None
    if a["float_kbr"] == 0 and a["pending_cashout_queue"] >= 5:
        return ("ESCALATE_AGENT_NETWORK", ["R8"], 1.0)
    if 0 < a["float_kbr"] < a["float_warning_kbr"]:
        return ("AUTO_NOTIFY", ["R8"], 1.0)


def _r9(event, facts):
    if event["event_type"] == "kyc_mismatch":
        return ("ESCALATE_COMPLIANCE", ["R9"], 1.0)


def _r10(event, facts):
    if event["event_type"] == "pin_locked":
        if event["amount_kbr"] < 2000:
            return ("AUTO_RESOLVE", ["R10"], 1.0)
        return ("ESCALATE_FRAUD_OPS", ["R10"], 1.0)


def _r11(event, facts):
    if facts["sanctioned"]:
        return ("AUTO_BLOCK", ["R11"], 1.0)


def _r12(event, facts):
    claims_success = event["event_type"] == "transfer_completed" or event["payload"].get("failure_reason") == "SUCCESS"
    if claims_success and (facts["txn"] is None or facts["txn"]["status"] != "SUCCESS"):
        return ("ESCALATE_RECONCILIATION", ["R12"], 1.0)


def _r13(event, facts):
    needed = REQUIRED.get(event["event_type"], [])
    for field in needed:
        if not event.get(field):
            return ("ESCALATE_DATA_QUALITY", ["R13"], 1.0)
    if "customer_id" in needed and facts["customer"] is None:
        return ("ESCALATE_DATA_QUALITY", ["R13"], 1.0)
    if "txn_id" in needed and facts["txn"] is None:
        return ("ESCALATE_DATA_QUALITY", ["R13"], 1.0)


def _time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _seconds_before(event, txn):
    return (_time(event["received_at"]) - _time(txn["timestamp_utc"])).total_seconds()
