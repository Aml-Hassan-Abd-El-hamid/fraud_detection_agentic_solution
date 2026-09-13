# RiverPay Ops Policy (R1–R13) — binding

Tiers (daily send limit): KYC0 = 50 KBR, KYC1 = 2,000 KBR, KYC2 = 20,000 KBR.
If the event and the CSV records disagree, the CSV is the source of truth.

- **R1 — Hard fraud block.** `fraud_score >= 0.90` AND (`new_device` OR `new_beneficiary`) AND `amount_kbr >= 1000` → `AUTO_BLOCK`.
- **R2 — Fraud / high-value.** `fraud_score >= 0.70` OR `amount_kbr >= 5000` OR `cross_border` → `ESCALATE_FRAUD_OPS`.
- **R3 — Duplicate of a completed transfer.** Suspected duplicate AND a ledger `SUCCESS` for the same payer, payee, and amount within 120 seconds before the event → `AUTO_RESOLVE`. Do not retry.
- **R4 — Insufficient funds.** Look up `available_balance_kbr`. Balance `<` amount → `AUTO_RESOLVE` (notify, no retry). Balance `>=` amount → stale/wrong event → `ESCALATE_RECONCILIATION`.
- **R5 — Timeout / unknown.** `TIMEOUT` or `UNKNOWN`, amount `< 5000`, `fraud_score < 0.70` → `ESCALATE_TECH_OPS`. Recommend a manual retry only; never execute one.
- **R6 — Beneficiary not found.** → `AUTO_RESOLVE`. Notify sender that no funds moved.
- **R7 — Limit exceeded.** → `AUTO_RESOLVE`. Quote the daily limit for the customer's actual KYC tier from the customer file.
- **R8 — Agent float.** Float `> 0` and below the warning threshold → `AUTO_NOTIFY` (agent + territory manager). Float `= 0` AND `pending_cashout_queue >= 5` → `ESCALATE_AGENT_NETWORK`.
- **R9 — KYC mismatch.** → `ESCALATE_COMPLIANCE`. Never auto-approve identity.
- **R10 — PIN locked.** Amount `< 2000` → `AUTO_RESOLVE` (keep locked, send reset). Amount `>= 2000` → `ESCALATE_FRAUD_OPS`.
- **R11 — Sanctions.** On the watchlist (customer_id or case-insensitive name) → `AUTO_BLOCK`, reason `R11`.
- **R12 — Success claims.** Event says SUCCESS and the ledger status is not `SUCCESS` → `ESCALATE_RECONCILIATION`.
- **R13 — Data quality.** Required `customer_id` / `txn_id` missing or not found → `ESCALATE_DATA_QUALITY`.

**Precedence (highest first):** R11, R1, R13, R12, R9, R2, R10, then R3, R4, R5, R6, R7, R8.
If nothing matches, escalate (fail-closed) with low confidence. Never auto-retry a debit.
