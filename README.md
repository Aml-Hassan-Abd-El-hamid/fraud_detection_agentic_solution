# RiverPay Exception Agent — Plan

For each event in `events.json`: read it → look up the real records → apply policy R1–R13 →
write one JSON decision to `output/decisions.json`.

**Golden rule:** if the event and a CSV disagree, the **CSV wins**. Never move money,
auto-retry, auto-approve KYC, or clear a sanctions hit.

## Design

There are two designs here. The **production target** is what a real 24/7 system should look
like. **What I built now** is the 5-hour version. They are the same shape — the production extras
are a human sign-off and a saved history of rulebooks, not a different design.

### What I built now

```
BUILD (make the engine)  - primary: AI writes the rulebook (data), a template writes the code
  task.txt  (the written policy, in words)
      |   Gemma 4 31B  - the AI turns the words into a rulebook (DSL)
      v
  policy.dsl.json  (the rulebook, as plain data)
      |   a small template  - plain code turns the data into code (no AI)
      v
  policy.py  (the "decide" step)
      |   run the tests (19 known cases + safety checks)
      v
  pass -> keep it    |    fail -> put back the last good engine

RUN (handle the events)
  events.json
      v
  gather evidence   - always look up customer / ledger / agent / sanctions
      v
  decide            - policy.py: plain rules, same input -> same answer
      v
  write message     - fixed templates for now (no AI here yet)
      v
  check output      - no false "success", never say "sanctions" to a customer
      v
  output/decisions.json  + a short report
```

**Three ways to build the engine** — the primary path is the middle one (`--llm`, AI writes the
DSL, a template writes the code). The third is an experiment (see "What I would do differently"):

| Command | Who writes the code | Absorbs new rule *shapes*? | Safety |
|---|---|---|---|
| `policy_compiler.py` | template | ❌ values only | deterministic |
| `policy_compiler.py --llm` | **AI writes DSL, template writes code** | ❌ values only | **low risk (AI writes data)** |
| `policy_compiler.py --llm-code` | AI writes `policy.py` directly | ✅ any shape | AST guard + tests + restore (experimental) |

Primary = **`--llm`**: the AI only writes the DSL (reviewable data), and a deterministic template
turns it into the code — so the code always faithfully matches the reviewed rulebook. The `--llm-code`
mode (AI writes the code directly) is an experiment; frontier models kept missing edge cases, so
the guardrail rejected those builds and the deterministic engine ships.

Not built yet (these are the production extras): no human sign-off before a new rulebook goes
live (the tests are the only gate), no saved history of old rulebooks (just one file), and the
messages are fixed templates instead of AI-written.

### Target design (production)

The full picture. Build time adds a human approval and a saved history of every rulebook; run
time adds an AI that writes the customer and staff messages.

Two phases. **Build time**: an LLM compiles the written policy into a DSL/config (and, optionally,
the engine code directly) plus tests; an AST guard + tests gate it and a human approves before it
ships. **Runtime**: a fully deterministic path gathers evidence and decides; an LLM only writes the
message prose at the end.

```
                         ┌──────────────────────┐
                         │   Policy Owner       │
                         │  ops_policy.md       │
                         │  + expected examples │
                         └──────────┬───────────┘
                                    ▼
                         ┌──────────────────────┐
                         │   Policy Compiler    │  ◄── LLM (build time, offline)
                         │        LLM           │
                         │ NL Policy → DSL/config│
                         │  → guarded engine code│
                         │ + generate test cases │
                         └──────────┬───────────┘
                                    ▼
                         ┌──────────────────────┐
                         │   Policy Validation  │
                         │ Golden + Regression   │
                         │ Edge cases            │
                         │ Precedence checks     │
                         │ Invariants (safety)   │
                         │ AST guard (gen code)  │
                         └──────────┬───────────┘
                              PASS  │  FAIL
                    ┌───────────────┴────────────┐
                    ▼                            ▼
             Human Approval                   Reject
                    │
                    ▼
             ┌──────────────┐
             │ Policy Store │
             │ Versioned    │
             └──────┬───────┘
                    │ active policy (DSL/config or guarded engine code)
                    ▼
══════════════════════════════════════════════════════════════════
                    RUNTIME  (deterministic path — no LLM decides)
══════════════════════════════════════════════════════════════════

 events.json
     │
     ▼
┌───────────────┐
│ Event Ingestor│
└───────┬───────┘
        ▼
┌───────────────────────────────┐
│ Evidence Gatherer             │  ◄── DETERMINISTIC router (no LLM)
│ event identifiers → tool set  │      the event CANNOT shrink this set
│ (mandatory: sanctions +       │      missing required record → R13 fail-closed
│  customer/ledger/agent)       │
└──────────┬────────────────────┘
           │ mandatory tool calls
           ▼
 ┌──────────────────────────────────────────┐
 │              Tool Layer                   │
 │ lookup_customer()   check_sanctions()     │
 │ lookup_transaction()  lookup_agent()      │
 │ lookup_recent_transfers()  get_policy()   │
 └───────────────────┬──────────────────────┘
                     ▼
          ┌─────────────────────┐
          │ Systems of Record   │
          │ customers.csv       │
          │ ledger.csv          │
          │ agents.csv          │
          │ sanctions.csv       │
          └──────────┬──────────┘
                     │ complete evidence (facts)
                     ▼
          ┌─────────────────────┐
          │ Deterministic       │  ◄── decides. same input → same output
          │ Policy Engine       │
          │ Active Policy DSL   │
          │ R11→R1→R13→R12→R9→   │
          │ R2→R10→ …           │
          │ no match → FAIL-CLOSED (ESCALATE)
          └──────────┬──────────┘
                     ▼
              ┌──────────────┐
              │ Decision     │
              │ AUTO_*       │
              │ ESCALATE_*   │
              └──────┬───────┘
                     ▼
          ┌─────────────────────┐
          │ Response Generator  │  ◄── LLM (runtime, narrator ONLY)
          │       LLM           │      writes words, never the verdict
          │ customer_message    │
          │ internal_case_note  │
          └──────────┬──────────┘
                     ▼
          ┌─────────────────────┐
          │ Output Validator    │  ◄── hard gate
          │ JSON Schema         │      + safety: no false success,
          │ Safety checks       │        no "sanctions" leak, enum only
          └──────────┬──────────┘
                     ▼
              decisions.json
```

- **Evidence Gatherer** is a deterministic router: identifiers present → mandatory tool set.
  The event can never shrink it (e.g. sanctions always runs when a customer is present).
- **Policy engine** decides from complete evidence. Deterministic = auditable. No match → fail-closed escalate.
- **LLM lives in exactly 2 safe places:** Policy Compiler (build time, behind tests + human
  approval) and Response Generator (runtime, prose only, output-validated). Nothing that touches
  the decision is an LLM.

Rule precedence (first match wins): **R11 → R1 → R13 → R12 → R9 → R2 → R10 → rest.**

## Design choices

1. **Rules decide, the AI only helps write.** The decision (block, notify, escalate, close) comes
   from plain rules that give the same answer every time. The AI never decides who gets blocked.
   *Rejected:* letting the AI read a case and decide. *Why:* money mistakes are hard to undo, so
   we need the same answer every time and a reason we can show an auditor.

2. **Trust the records, not the message.** For every event we look up the real customer and
   ledger files; we never believe the event on its own. *Rejected:* acting on what the event
   says. *Why:* some events are stale or wrong — e.g. E002 says "not enough money" but the
   balance is actually fine, so the right move is to reconcile, not to close it.

3. **When unsure, ask a person.** Anything the rules don't clearly cover goes to a human, never
   auto-handled. *Rejected:* guessing. *Why:* a person spending five minutes is far cheaper than
   a wrong wallet freeze.

## ToDo

- [x] **Step 1 — Load data** (`load_data`): read 5 files into memory once.
- [x] **Step 2 — Tools**: `lookup_customer`, `lookup_transaction`, `lookup_recent_transfers`,
  `lookup_agent`, `check_sanctions`, `get_policy`. Each records itself in `tools_used`.
- [x] **Step 3 — Policy engine** (`decide`): rules `_r1`–`_r13` in precedence order (in `policy.py`).
- [x] **Step 4 — Messages**: `build_customer_message` (no jargon), `build_internal_case_note` (rule IDs + next action).
- [x] **Step 5 — Output**: build object, validate schema, write `output/decisions.json`.
- [x] **Step 6 — Check traps** (below) — all 19 pinned by `tests/test_agent.py`.
- [x] **Step 7 — Report**: counts by decision + AUTO_BLOCK ids.
- [x] **Step 8 — Policy Compiler**: DSL → `policy.py` (deterministic) + optional LLM stage (NL → DSL) with a test-driven retry loop.
- [ ] **Step 9 — LLM narrator**: optional Gemini-flash/Gemma message writer, output-validated.

## Traps to verify

| Event | Expected | Why |
|---|---|---|
| E002 | ESCALATE_RECONCILIATION | balance 1800 ≥ 500 → not really insufficient (R4 branch 2) |
| E004 | ESCALATE_TECH_OPS | different payee + TIMEOUT → fake duplicate (R5) |
| E009 | AUTO_RESOLVE, quote 50 | master says C004=KYC0, not KYC1 (R7) |
| E015 | AUTO_BLOCK | C005 on sanctions (R11); customer sees "under review" only |
| E018 | ESCALATE_RECONCILIATION | event "completed" but ledger FAILED (R12) |
| E005 | AUTO_BLOCK | fraud 0.93 + new device + new beneficiary + ≥1000 (R1) |
| E016 | ESCALATE_TECH_OPS | TIMEOUT, small, low fraud (R5); recommend retry, never run it |

## How to test the whole system

Four commands, run from the project folder:

```
python src/agent.py                       # 1. run it: writes output/decisions.json + a summary
python tests/test_agent.py                # 2. check it: 19 known cases + safety rules (no extra install)
python src/policy_compiler.py             # 3. rebuild the deterministic engine from the saved rulebook
python src/policy_compiler.py --llm       # 4. PRIMARY build: AI writes the DSL; a template writes the code
```

What you should see:

- **1** prints 19 events and `AUTO_BLOCK: ['E005', 'E015']`.
- **2** prints `8/8 passed`.
- **3** prints `rendered src/policy.py from DSL` then `8/8 passed` (no AI, offline).
- **4 (primary)** needs `GOOGLE_API_KEY` in `.env` and `pip install google-genai`. The AI writes the
  DSL, a template turns it into `policy.py`, and the tests grade it; if it can't pass, the last good
  engine is restored. (`--llm-code`, the AI-writes-code experiment, also exists — see below.)
- Then open `output/decisions.json` and read the trap rows (E002, E009, E015, E018) to see the
  right calls in plain sight.

Files: `src/policy.dsl.json` (the rulebook, as data), `src/policy.py` (the decide step, generated),
`src/policy_compiler.py` (builds + tests the engine), `src/agent.py` (looks things up + runs everything),
`tests/test_agent.py`. Python 3, standard library only; the AI build step is the one optional extra.

## What I would do differently

The current build is the 5-hour version. To reach the production design above I would:

- **Let the AI write the `decide` code directly** (the `--llm-code` experiment), not just the DSL,
  so a brand-new rule *shape* needs no template change — kept safe by the AST guard + tests + restore.
  In live runs frontier models kept missing an edge case, so I'd invest in better prompting/examples
  and a hardened sandbox before trusting it.
- **Add a human sign-off** before a new rulebook goes live. Today the tests are the only gate;
  production should also have a person approve the change.
- **Keep a history of rulebooks** so we can roll back to yesterday's version in seconds. Today
  it is just one file.
- **Let the AI write the customer and staff messages** (nicer wording), with the same safety
  check catching anything unsafe. Today the messages are fixed templates.
- **Generate the tests too, not just the rules**, and have a person review them, so the policy
  and its checks are written together.
- **Connect to the real bank systems** instead of reading CSV files — behind the same lookup
  functions, so nothing else has to change.
- **Send low-confidence cases to people automatically** (anything under 0.6), as an extra net.

## Notes

- **Why not n8n:** no prior n8n experience + a setup problem (Node version clash) would have
  eaten the time budget. The task allows Python if I explain why, so I used plain Python.
- **AI assistance:** GitHub Copilot helped write the plan and code; Gemma 4 31B writes the decide
  engine from the written policy (guarded by an AST check + the tests).
- **Not done:** no AI writing the messages yet (fixed templates), no human sign-off step, no
  saved rulebook history, no real bank connection. All are in "what I would do differently".
