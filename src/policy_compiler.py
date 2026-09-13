"""
Policy Compiler — turns the written policy into the deterministic decide() engine.

Two stages (mirrors the Policy Compiler box in the README):

  1. NL policy  --(LLM: Gemma / Gemini-flash)-->  policy.dsl.json   [optional, needs API key]
  2. DSL        --(deterministic template)------>  src/policy.py      [always, no LLM]

Then it runs the golden + invariant tests. If they fail and an LLM is available, it feeds the
failing tests back and regenerates the DSL (bounded retries) — the generate -> validate ->
retry-on-error loop borrowed from the web-scraping agent.

Default (offline) run renders the committed DSL and tests it:
    python src/policy_compiler.py
Regenerate the DSL from the written policy with an LLM:
    python src/policy_compiler.py --llm --model gemma-4-31b-it
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import logging
from google import genai   # only needed for --llm / --llm-code
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DSL_PATH = HERE / "policy.dsl.json"
NL_POLICY_PATH = HERE / "ops_policy.md"      # clean NL policy (fallback input for --llm-code)
POLICY_PATH = HERE / "policy.py"
TESTS = ROOT / "tests" / "test_agent.py"
POLICY_TEXT = ROOT / "task.txt"      # source of the written R1–R13 policy
MAX_RETRIES = 3


# ═══════════════════════════════════════════════════════════
# Stage 2: DSL -> Python  (deterministic template, no LLM)
# ═══════════════════════════════════════════════════════════
def render_policy(dsl: dict) -> str:
    c = dsl["constants"]
    r = dsl["rules"]
    fb = dsl["fallback"]
    order = ", ".join("_" + rid.lower() for rid in dsl["precedence"])

    blocks = [
        '"""AUTO-GENERATED from policy.dsl.json by policy_compiler.py — do not edit by hand.\n'
        "decide(event, facts) -> (decision, reason_codes, confidence). Rules in precedence order.\"\"\"",
        "from datetime import datetime",
        f"KYC_LIMITS = {json.dumps(c['KYC_LIMITS'])}",
        f"DUP_WINDOW = {c['DUP_WINDOW']}",
        f"LOW_CONF = {c['LOW_CONF']}",
        f"REQUIRED = {json.dumps(dsl['required'], indent=4)}",
        f'''def decide(event, facts):
    for rule in ({order},):
        result = rule(event, facts)
        if result:
            return result
    return ("{fb['decision']}", ["{fb['reason']}"], {fb['confidence']})''',

        f'''def _r1(event, facts):
    p = event["payload"]
    if p["fraud_score"] >= {r['R1']['fraud_min']} and (p["new_device"] or p["new_beneficiary"]) and event["amount_kbr"] >= {r['R1']['amount_min']}:
        return ("{r['R1']['decision']}", ["R1"], {r['R1']['confidence']})''',

        f'''def _r2(event, facts):
    p = event["payload"]
    if p["fraud_score"] >= {r['R2']['fraud_min']} or event["amount_kbr"] >= {r['R2']['amount_min']} or p["cross_border"]:
        return ("{r['R2']['decision']}", ["R2"], {r['R2']['confidence']})''',

        f'''def _r3(event, facts):
    if event["event_type"] != "duplicate_suspected" or not facts["txn"]:
        return None
    this = facts["txn"]
    for t in facts["recent"]:
        if t["status"] == "SUCCESS" and t["payer_id"] == this["payer_id"] and t["payee_id"] == this["payee_id"] and t["amount_kbr"] == this["amount_kbr"] and 0 <= _seconds_before(event, t) <= DUP_WINDOW:
            return ("{r['R3']['decision']}", ["R3"], {r['R3']['confidence']})''',

        f'''def _r4(event, facts):
    if event["payload"].get("failure_reason") != "{r['R4']['failure']}" or not facts["customer"]:
        return None
    if facts["customer"]["available_balance_kbr"] < event["amount_kbr"]:
        return ("{r['R4']['lt']}", ["R4"], {r['R4']['confidence_lt']})
    return ("{r['R4']['ge']}", ["R4"], {r['R4']['confidence_ge']})''',

        f'''def _r5(event, facts):
    p = event["payload"]
    if p.get("failure_reason") in {tuple(r['R5']['failures'])} and event["amount_kbr"] < {r['R5']['amount_max']} and p["fraud_score"] < {r['R5']['fraud_max']}:
        return ("{r['R5']['decision']}", ["R5"], {r['R5']['confidence']})''',

        f'''def _r6(event, facts):
    if event["payload"].get("failure_reason") == "{r['R6']['failure']}":
        return ("{r['R6']['decision']}", ["R6"], {r['R6']['confidence']})''',

        f'''def _r7(event, facts):
    if event["payload"].get("failure_reason") == "{r['R7']['failure']}":
        return ("{r['R7']['decision']}", ["R7"], {r['R7']['confidence']})''',

        f'''def _r8(event, facts):
    a = facts["agent"]
    if not a:
        return None
    if a["float_kbr"] == 0 and a["pending_cashout_queue"] >= {r['R8']['queue_min']}:
        return ("{r['R8']['network']}", ["R8"], {r['R8']['confidence']})
    if 0 < a["float_kbr"] < a["float_warning_kbr"]:
        return ("{r['R8']['notify']}", ["R8"], {r['R8']['confidence']})''',

        f'''def _r9(event, facts):
    if event["event_type"] == "{r['R9']['event_type']}":
        return ("{r['R9']['decision']}", ["R9"], {r['R9']['confidence']})''',

        f'''def _r10(event, facts):
    if event["event_type"] == "pin_locked":
        if event["amount_kbr"] < {r['R10']['amount_threshold']}:
            return ("{r['R10']['lt']}", ["R10"], {r['R10']['confidence']})
        return ("{r['R10']['ge']}", ["R10"], {r['R10']['confidence']})''',

        f'''def _r11(event, facts):
    if facts["sanctioned"]:
        return ("{r['R11']['decision']}", ["R11"], {r['R11']['confidence']})''',

        f'''def _r12(event, facts):
    claims_success = event["event_type"] == "transfer_completed" or event["payload"].get("failure_reason") == "SUCCESS"
    if claims_success and (facts["txn"] is None or facts["txn"]["status"] != "SUCCESS"):
        return ("{r['R12']['decision']}", ["R12"], {r['R12']['confidence']})''',

        f'''def _r13(event, facts):
    needed = REQUIRED.get(event["event_type"], [])
    for field in needed:
        if not event.get(field):
            return ("{r['R13']['decision']}", ["R13"], {r['R13']['confidence']})
    if "customer_id" in needed and facts["customer"] is None:
        return ("{r['R13']['decision']}", ["R13"], {r['R13']['confidence']})
    if "txn_id" in needed and facts["txn"] is None:
        return ("{r['R13']['decision']}", ["R13"], {r['R13']['confidence']})''',

        '''def _time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _seconds_before(event, txn):
    return (_time(event["received_at"]) - _time(txn["timestamp_utc"])).total_seconds()''',
    ]
    return "\n\n\n".join(blocks) + "\n"


def write_policy(dsl: dict):
    POLICY_PATH.write_text(render_policy(dsl), encoding="utf-8")
    print(f"rendered {POLICY_PATH.relative_to(ROOT)} from DSL")


def run_tests():
    """Run the golden + invariant suite in a fresh interpreter (picks up new policy.py)."""
    proc = subprocess.run([sys.executable, str(TESTS)], capture_output=True, text=True)
    return proc.returncode == 0, proc.stdout + proc.stderr


# ═══════════════════════════════════════════════════════════
# Stage 1: NL policy -> DSL  (LLM, optional — Gemma / Gemini-flash)
# ═══════════════════════════════════════════════════════════
_DSL_SCHEMA_HINT = """The DSL is one JSON object. Use these EXACT keys (fill values from the policy):
  constants  {"KYC_LIMITS": {"KYC0":int,"KYC1":int,"KYC2":int}, "DUP_WINDOW":int, "LOW_CONF":float}
  required   {event_type: [identifier fields that must be present]}
  precedence [rule ids, highest priority first]
  fallback   {"decision":enum, "reason":str, "confidence":float}  (used when no rule matches; must be an ESCALATE_*)
  rules — each rule id maps to an object with EXACTLY these keys:
    R1  {"fraud_min":float, "amount_min":int, "decision":enum, "confidence":float}
    R2  {"fraud_min":float, "amount_min":int, "decision":enum, "confidence":float}
    R3  {"decision":enum, "confidence":float}
    R4  {"failure":str, "lt":enum, "ge":enum, "confidence_lt":float, "confidence_ge":float}
    R5  {"failures":[str], "amount_max":int, "fraud_max":float, "decision":enum, "confidence":float}
    R6  {"failure":str, "decision":enum, "confidence":float}
    R7  {"failure":str, "decision":enum, "confidence":float}
    R8  {"queue_min":int, "network":enum, "notify":enum, "confidence":float}
    R9  {"event_type":str, "decision":enum, "confidence":float}
    R10 {"amount_threshold":int, "lt":enum, "ge":enum, "confidence":float}
    R11 {"decision":enum, "confidence":float}
    R12 {"decision":enum, "confidence":float}
    R13 {"decision":enum, "confidence":float}
  enum (decision) is one of: AUTO_RESOLVE, AUTO_NOTIFY, AUTO_BLOCK, ESCALATE_FRAUD_OPS,
  ESCALATE_COMPLIANCE, ESCALATE_TECH_OPS, ESCALATE_RECONCILIATION, ESCALATE_AGENT_NETWORK, ESCALATE_DATA_QUALITY.
Output ONLY the JSON object, no prose."""


def build_dsl_prompt(policy_text: str, prev_dsl: str = "", failures: str = "") -> str:
    if not failures:
        return f"""<start_of_turn>user
You are a policy compiler. Convert the written operations policy (rules R1–R13) into a JSON DSL
that a deterministic engine can execute. Trust the numbers and precedence exactly as written.

{_DSL_SCHEMA_HINT}

WRITTEN POLICY:
{policy_text}

Output only the JSON DSL.
<end_of_turn>
<start_of_turn>model
"""
    return f"""<start_of_turn>user
Your previous DSL failed the policy tests. Fix it.

{_DSL_SCHEMA_HINT}

WRITTEN POLICY:
{policy_text}

PREVIOUS DSL:
{prev_dsl}

FAILING TESTS (event_id, got vs expected):
{failures}

Output only the corrected JSON DSL.
<end_of_turn>
<start_of_turn>model
"""


def extract_json(text: str) -> dict:
    fenced = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    raw = fenced[-1] if fenced else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)


def _load_env(path: Path):
    """Minimal .env reader (KEY = value per line) so we don't need python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def generate_dsl(prompt: str, model_name: str) -> str:
    return _llm_text(prompt, model_name)


def _llm_text(prompt: str, model_name: str) -> str:
  
    logging.getLogger("google_genai").setLevel(logging.ERROR)   # silence the AFC notice
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    for _ in range(MAX_RETRIES):          # transient 5xx from the server -> retry the same prompt
        try:
            return client.models.generate_content(model=model_name, contents=prompt).text
        except Exception as e:
            if not any(code in str(e) for code in ("500", "503", "INTERNAL", "UNAVAILABLE")):
                raise
            last = e
    raise last


def compile_with_llm(model_name: str):
    _load_env(ROOT / ".env")
    policy_text = POLICY_TEXT.read_text(encoding="utf-8")
    prev, failures = "", ""
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n[LLM attempt {attempt}] generating DSL with {model_name}...")
        try:
            raw = generate_dsl(build_dsl_prompt(policy_text, prev, failures), model_name)
            dsl = extract_json(raw)
            write_policy(dsl)           # render errors (bad schema) also feed the retry
            ok, out = run_tests()
        except Exception as e:
            print(f"  error: {e}")
            prev = json.dumps(dsl, indent=2) if "dsl" in dir() else ""
            failures = f"error: {e}"
            continue
        print(out)
        if ok:
            DSL_PATH.write_text(json.dumps(dsl, indent=2), encoding="utf-8")
            print("DSL passed all tests and was saved.")
            return True
        prev, failures = json.dumps(dsl, indent=2), out
    print("LLM could not produce a passing DSL within the retry budget.")
    write_policy(json.loads(DSL_PATH.read_text(encoding="utf-8")))   # restore known-good engine
    print("restored policy.py from the committed DSL.")
    return False


# ═══════════════════════════════════════════════════════════
# OPTION 2: LLM writes policy.py DIRECTLY (guarded + tested)
# Trade-off: absorbs brand-new rule shapes the fixed template can't, but the AI now
# writes logic that touches money -> we gate it with an AST guard + the test suite.
# ═══════════════════════════════════════════════════════════
_CODE_CONTRACT = '''You get a POLICY (either a JSON DSL or written rules) and this code interface.
Write ONE Python module that implements the policy as:  def decide(event, facts):  returning
(decision, reason_codes, confidence).

HARD constraints:
- Import ONLY datetime. No other imports. No open/exec/eval/__import__/os/sys/subprocess/network.
- Pure function: read event and facts, return the tuple. No printing, no file or network access.

How to read the DSL:
  constants -> KYC_LIMITS (daily send limit by tier), DUP_WINDOW (R3 seconds before the event),
               LOW_CONF (confidence floor).
  required  -> event_type: identifiers that must be present AND found in facts, else R13 data-quality.
  precedence-> rule ids in order; try them first-match-wins.
  rules     -> per-rule thresholds, decision(s), reason id, confidence.
  fallback  -> use EXACTLY when no rule matches (its confidence is below LOW_CONF).

event keys: event_id, event_type, customer_id, txn_id, agent_id, amount_kbr, received_at,
  payload{failure_reason, fraud_score, new_device, new_beneficiary, cross_border, channel, notes}.
facts keys (already gathered for you):
  customer -> None or {customer_id, full_name, kyc_tier, wallet_status, available_balance_kbr, country}
  txn      -> None or {txn_id, timestamp_utc, payer_id, payee_id, amount_kbr, status, failure_code}
  recent   -> list of the payer's ledger rows (same keys as txn) for the R3 duplicate check
  agent    -> None or {agent_id, float_kbr, float_warning_kbr, pending_cashout_queue, territory_manager}
  sanctioned -> bool

decision must be one of the DSL's decision values (AUTO_* / ESCALATE_*). reason_codes is a list with
at least one rule id like ["R4"]. Never return an AUTO_* when no rule matches (use the fallback).
Also define at MODULE level (other code imports them): KYC_LIMITS and LOW_CONF from the DSL constants.

Output ONLY the Python code, no prose.'''

_ALLOWED_MODULES = {"datetime"}
_FORBIDDEN_NAMES = {"exec", "eval", "open", "__import__", "compile",
                    "globals", "locals", "input", "getattr", "setattr", "delattr", "vars"}


def guard_code(src: str):
    """Static safety gate: parse the LLM code and reject anything that could reach outside
    a pure decision (imports besides datetime, I/O, exec/eval, dunder access)."""
    tree = ast.parse(src)                       # also proves it is valid Python
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in _ALLOWED_MODULES:
                    raise ValueError(f"forbidden import: {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in _ALLOWED_MODULES:
                raise ValueError(f"forbidden import from: {node.module}")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise ValueError(f"forbidden name: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError(f"forbidden dunder attribute: {node.attr}")
    if "def decide(" not in src:
        raise ValueError("no decide(event, facts) function found")


def extract_code(text: str) -> str:
    fenced = re.findall(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        return fenced[-1].strip()
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l.startswith(("import ", "from ", "def ", '"""')):
            return "\n".join(lines[i:]).strip()
    return text.strip()


def build_code_prompt(policy: str, prev_code: str = "", failures: str = "") -> str:
    if not failures:
        return (f"<start_of_turn>user\n{_CODE_CONTRACT}\n\nPOLICY:\n{policy}\n"
                f"<end_of_turn>\n<start_of_turn>model\n")
    return (f"<start_of_turn>user\nYour previous policy.py failed. Fix it.\n\n{_CODE_CONTRACT}\n\n"
            f"POLICY:\n{policy}\n\nPREVIOUS CODE:\n{prev_code}\n\n"
            f"FAILING TESTS (event_id, got vs expected):\n{failures}\n"
            f"<end_of_turn>\n<start_of_turn>model\n")


def _try_code_source(label: str, policy: str, model_name: str) -> bool:
    """One policy source through the retry loop: generate -> guard -> test. True if it passes."""
    prev, failures = "", ""
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"[LLM-code {label} attempt {attempt}] the AI writes policy.py with {model_name}...")
        try:
            code = extract_code(_llm_text(build_code_prompt(policy, prev, failures), model_name))
            guard_code(code)                     # AST safety gate BEFORE we run anything
            POLICY_PATH.write_text(code + "\n", encoding="utf-8")
            ok, out = run_tests()                # subprocess isolation + correctness gate
        except Exception as e:
            print(f"  rejected: {e}")
            prev = code if "code" in dir() else ""
            failures = f"error: {e}"
            continue
        print(out)
        if ok:
            return True
        prev, failures = code, out
    return False


def compile_with_llm_code(model_name: str):
    _load_env(ROOT / ".env")
    # Try the clean DSL first; if it can't pass, fall back to the written NL policy.
    for label, path in (("DSL", DSL_PATH), ("NL", NL_POLICY_PATH)):
        print(f"\n=== building from {label} policy ({path.name}) ===")
        if _try_code_source(label, path.read_text(encoding="utf-8"), model_name):
            print(f"AI-written engine (from {label}) passed the guard and all tests.")
            print("note: run `python src/policy_compiler.py` to restore the deterministic engine.")
            return True
        print(f"{label} source could not pass within the retry budget.")
    write_policy(json.loads(DSL_PATH.read_text(encoding="utf-8")))   # restore known-good engine
    print("restored policy.py from the committed DSL.")
    return False


# ═══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="AI writes the DSL from task.txt; template makes the code")
    ap.add_argument("--llm-code", action="store_true", help="AI writes policy.py DIRECTLY (guarded + tested)")
    ap.add_argument("--model", default="gemma-4-31b-it", help="LLM model name")
    args = ap.parse_args()

    if args.llm_code:
        ok = compile_with_llm_code(args.model)
    elif args.llm:
        ok = compile_with_llm(args.model)
    else:
        write_policy(json.loads(DSL_PATH.read_text(encoding="utf-8")))
        ok, out = run_tests()
        print(out)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
