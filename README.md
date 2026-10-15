# Northgate vs Fernhill: Claim Reconciliation

| File | Deliverable |
|---|---|
| `reconcile.py` | 1. Reconciliation script (pandas) |
| `output/reconciliation_register.csv` | Register: `claim_id, matched_invoice, status_flag, note` |
| `output/reconciliation_register_detailed.csv` | Same, plus secondary flag, match method, recommendation and concede / dispute / review amounts |
| `MEMO.md` | 2. One-page findings memo |
| `test_reconcile.py` | 3. Automated tests (the PO-5521 ambiguity test is the first one) |
| `AI_LOG.md` | 4. AI working log |

## Run

```bash
pip install -r requirements.txt     # pandas, pytest, pyarrow (Parquet), openpyxl (Excel)
python reconcile.py                   # default: data/*.csv -> output/
python reconcile.py claims.parquet ledger.xlsx out/   # any mix of CSV / Excel / Parquet
pytest -q                             # 48 tests, ~15 s (includes a 300k-claim scale test)
```

## Approach

1. **Load as text.** Every column is read as a string, so leading zeros (`0088217`) survive and a blank `invoice_ref` stays blank. Money is converted to integer cents, so the $1.00 boundary is exact.
2. **Canonical keys** (so formatting drift never causes a false miss):
   - Invoice: strip the `INV` prefix with any separator (`INV-`, `INV `, `INV#`, `INV_`) and leading zeros. A reference that is still not purely digits gets no key, but gets a separate *OCR candidate* (O to 0, I/L to 1).
   - PO: `PO-5501`, `PO5501`, `po 5501` and `5501` all become `5501`.
3. **Match, in order, with joins (no per-row loops):**
   1. Canonical invoice number. If two ledger invoices collapse to the same key: `AMBIGUOUS_NO_MATCH`. If the invoice is found but the claim's PO belongs to a different invoice: `REFERENCE_CONFLICT`, the match is withdrawn and the claim goes to review.
   2. OCR candidate, **only if the claim's PO equals the PO of the corrected invoice**. Otherwise the reading is ignored.
   3. PO fallback, only when exactly one ledger invoice carries the PO. More than one: `AMBIGUOUS_NO_MATCH`, never broken by amount. None: `PHANTOM`.
4. **Data issues.** A row that cannot be evaluated reliably is flagged `DATA_ISSUE` and sent to review; **the rest of the batch keeps running**. Triggers:
   - `claim_id` blank or repeated (possible resubmission, which would otherwise be paid twice);
   - `claimed_amount` blank, unreadable (e.g. `1,000.00`, never guessed because separators depend on locale), zero or negative;
   - `claim_date` blank or invalid;
   - the matched ledger invoice has an invalid amount or a missing/invalid date (it is **not** labelled `OUT_OF_PERIOD`, because its period is unknown);
   - the claim is dated **before** its invoice. *Whether this is really an error depends on business rules* (some businesses raise claims against an order before it is invoiced), so it is sent to review rather than rejected.
   When the row is otherwise evaluable, the flag it would have had is kept in `secondary_flag`.
5. **Flag a match.** Precedence: `DATA_ISSUE` > no match > `OUT_OF_PERIOD` > `PARTIAL_PAYMENT` > amount.
   - Invoice date outside 2025-01-01 to 2025-12-31 (inclusive): `OUT_OF_PERIOD`. Judged on the invoice date, not the claim date.
   - Invoice paid more than $1.00 short (e.g. $1,000 invoiced, $600 paid): `PARTIAL_PAYMENT`. The short payment may already be the deduction, so claiming it again would be a double deduction; a person checks.
   - Otherwise compare the claimed amount with **`invoice_amount`**, as the case specifies. More than $1.00 above: `AMOUNT_OVERSTATED`. More than $1.00 below: `AMOUNT_MISMATCH`. Else `CLEAN_MATCH`.
   - When several claims land on the same invoice, their **sum** is tested against it.
   - The next problem in line is kept in `secondary_flag` (e.g. an out-of-period claim that is also overstated).
6. **Recommendation and money split** (`concede + dispute + review = claimed` on every row):
   - The amount conceded is **capped at what Northgate actually received** (`paid_amount`; the invoice amount if `paid_amount` is blank or 0.00, i.e. unpaid). This does not change any flag, only the money split.
   - `CLEAN_MATCH`: concede the lower of the claim and that cap (cents above it are disputed).
   - `AMOUNT_OVERSTATED`, single claim: concede up to the cap, dispute the excess.
   - Human review: `DATA_ISSUE`, `PARTIAL_PAYMENT`, `AMOUNT_MISMATCH`, `REFERENCE_CONFLICT`, and `AMOUNT_OVERSTATED` from summed claims.
   - `OUT_OF_PERIOD`, `AMBIGUOUS_NO_MATCH`, `PHANTOM`: dispute in full.
   - Only missing **columns** stop the run (the file itself is broken). An empty claims file produces an empty register.

## Relation to the brief

- The five flags defined in the case (`CLEAN_MATCH`, `OUT_OF_PERIOD`, `AMBIGUOUS_NO_MATCH`, `AMOUNT_OVERSTATED`, `PHANTOM`) follow the stated rules exactly: audit window on invoice date, ID canonicalisation, invoice first then PO only when unique, never resolved by amount, $1.00 tolerance on the invoice amount.
- Four extra flags (`AMOUNT_MISMATCH`, `REFERENCE_CONFLICT`, `PARTIAL_PAYMENT`, `DATA_ISSUE`) are a safety layer for production data. They only route a claim to human review when the stated rules cannot decide it safely; they never turn a claim the case rules would dispute into a concession. **None of them fires on the case data**, so the register uses only the five case flags.
- Additions beyond the brief, all documented in `AI_LOG.md`: PO normalisation (same spirit as the invoice rule, so `PO5501` is not a false miss), OCR correction confirmed by PO, summing several claims on one invoice, the cap on the conceded amount, and the recommendation and money split columns.

## Scale

Matching is done with `value_counts` + `map` joins on canonical keys, so it is linear in the number of rows. The only row-wise step is building the human-readable `note` text, which makes no decisions. A test runs 300,000 claims against 100,000 invoices (about 7 s), and an equivalence test checks the vectorised engine against a deliberately naive row-by-row implementation on randomised data with every trap mixed in (format drift, OCR damage, shared POs, partial and zero payments, claims dated before their invoice).

## Assumptions

Decisions I made on rules the case leaves open, with the reasoning, are in `AI_LOG.md`.
