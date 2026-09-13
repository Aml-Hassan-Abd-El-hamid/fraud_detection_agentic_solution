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
      |   Gemma 4 31B  - the AI turns the words into a rulebook
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

**Three ways to build the engine** — the primary path is the middle one (`--llm`: the AI writes the
rulebook, a template writes the code). The third is an experiment (see "What I would do differently"):

| Command | Who writes the code | Handles a brand-new rule? | Safety |
|---|---|---|---|
| `policy_compiler.py` | a template | ❌ only value changes | fixed, same every time |
| `policy_compiler.py --llm` | **AI writes the rulebook, template writes code** | ❌ only value changes | **low risk (AI writes data, not code)** |
| `policy_compiler.py --llm-code` | AI writes `policy.py` directly | ✅ any new rule | safety scan + tests + auto-restore (experiment) |

Primary = **`--llm`**: the AI only writes the rulebook (data you can read and check), and a plain
template turns it into the code — so the code always matches the rulebook you approved. The
`--llm-code` mode (AI writes the code itself) is an experiment; the AI models kept missing an edge
case, so the checks rejected those builds and the fixed engine ships.

Not built yet (these are the production extras): no human sign-off before a new rulebook goes
live (the tests are the only gate), no saved history of old rulebooks (just one file), and the
messages are fixed templates instead of AI-written.

### Target design (production)

The full picture: the same idea as above, plus three things a real 24/7 system needs — a person
signs off on a new rulebook, every version is saved so you can roll back, and an AI writes the
customer and staff messages at run time.

```
BUILD (make the rulebook)  - a person owns the rules; the AI helps; checks + a person approve
  ops_policy.md  (the written rules + example answers)
      |   the AI reads the rules
      v
  rulebook (data)  - and, if you want, the AI can write the decision code too
      |   automatic checks: known-answer tests, re-checks of old cases, tricky edge
      |   cases, rule-order checks, safety rules, and a scan of any AI-written code
      v
  a person approves    ->  pass: save it with a version number (so you can roll back)
                           fail: reject, keep the last good rulebook

RUN (handle each event)  - fixed rules decide; the AI never decides
  events.json
      v
  gather evidence   - always look up customer / ledger / agent / sanctions
                      (the event can't skip a lookup; a missing record -> send to a person)
      v
  decide            - fixed rules in a set order; same input -> same answer
                      (no rule fits -> send to a person, never auto-act)
      v
  write message     - the AI writes the wording only, never the decision
      v
  final safety check- right shape; never a false "success"; never say "sanctions"
                      to a customer; only allowed decisions
      v
  decisions.json
```

- **Gather evidence** always runs the needed lookups; the event can't shrink the set (the
  sanctions check always runs when there's a customer).
- **Decide** works only from the full evidence, the same way every time; anything the rules don't
  clearly cover goes to a person.
- The **AI is used in only two safe spots**: writing the rulebook at build time (behind the checks
  and a person) and writing the message wording at run time (behind the final safety check). It
  never makes the decision.

The rules are checked in a fixed order, strictest first (sanctions and fraud blocks before the rest).

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
- [x] **Step 3 — Decision rules** (`decide`): rules `_r1`–`_r13` checked in a set order (in `policy.py`).
- [x] **Step 4 — Messages**: `build_customer_message` (no jargon), `build_internal_case_note` (rule IDs + next action).
- [x] **Step 5 — Output**: build the decision, check its shape, write `output/decisions.json`.
- [x] **Step 6 — Check traps** (below) — all 19 pinned by `tests/test_agent.py`.
- [x] **Step 7 — Report**: counts by decision + AUTO_BLOCK ids.
- [x] **Step 8 — Rulebook builder**: rulebook → `policy.py` (plain template) + optional AI stage (rules → rulebook) that retries until the tests pass.
- [ ] **Step 9 — AI message writer**: optional Gemini/Gemma writer for the messages, checked before it ships.

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
python src/policy_compiler.py             # 3. rebuild the fixed engine from the saved rulebook
python src/policy_compiler.py --llm       # 4. PRIMARY build: AI writes the rulebook; a template writes the code
```

What you should see:

- **1** prints 19 events and `AUTO_BLOCK: ['E005', 'E015']`.
- **2** prints `8/8 passed`.
- **3** prints `rendered src/policy.py from DSL` then `8/8 passed` (no AI, offline).
- **4 (primary)** needs `GOOGLE_API_KEY` in `.env` and `pip install google-genai`. The AI writes the
  rulebook, a template turns it into `policy.py`, and the tests grade it; if it can't pass, the last good
  engine is restored. (`--llm-code`, the AI-writes-code experiment, also exists — see below.)
- Then open `output/decisions.json` and read the trap rows (E002, E009, E015, E018) to see the
  right calls in plain sight.

Files: `src/policy.dsl.json` (the rulebook, as data), `src/policy.py` (the decide step, generated),
`src/policy_compiler.py` (builds + tests the engine), `src/agent.py` (looks things up + runs everything),
`tests/test_agent.py`. Python 3, standard library only; the AI build step is the one optional extra.

## What I would do differently

The current build is the 5-hour version. To reach the production design above I would:

- **Let the AI write the `decide` code directly** (the `--llm-code` experiment), not just the
  rulebook, so a brand-new rule needs no template change — kept safe by the code safety scan +
  tests + auto-restore. In live runs the AI models kept missing an edge case, so I'd invest in
  better prompting/examples and a stronger sandbox before trusting it.
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
- **AI assistance:** GitHub Copilot helped write the plan and code; Gemma 4 31B turns the written
  rules into the rulebook (checked by the tests).
- **Not done:** no AI writing the messages yet (fixed templates), no human sign-off step, no
  saved rulebook history, no real bank connection. All are in "what I would do differently".
