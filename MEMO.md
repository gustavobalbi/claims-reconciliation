# Findings Memo: Fernhill Mart Deduction Claims

**To:** Northgate Outdoor Supply, Accounts Receivable
**Re:** Reconciliation of 8 Fernhill deduction claims ($6,357.65) against Northgate's own invoice ledger
**Basis:** Only Northgate's ledger counts as evidence. A claim that the ledger cannot confirm is not given the benefit of the doubt. Amounts are compared with what Northgate was actually paid, within a $1.00 tolerance.

## Bottom line

Of $6,357.65 claimed, Northgate should **concede $4,004.87** and **dispute $2,352.78**. Of the amount conceded, $2,682.40 (two claims) should only be paid once Fernhill provides proof of delivery. Three claims ($2,320.75) are rejected outright: one predates the 2025 audit period, one cannot be tied to a single invoice, and one refers to an invoice and PO that do not exist. One claim is overstated and is conceded only up to what Northgate was paid.

## Results by flag

| Flag | Meaning | Claims | Claimed | Concede | Dispute |
|---|---|---:|---:|---:|---:|
| CLEAN_MATCH | Real 2025 invoice, amount within $1.00 of payment | 4 | $3,624.90 | $3,624.87 | $0.03 |
| AMOUNT_OVERSTATED | Real invoice, claim exceeds payment by more than $1.00 | 1 | $412.00 | $380.00 | $32.00 |
| OUT_OF_PERIOD | Real invoice, dated outside 1 Jan to 31 Dec 2025 | 1 | $875.00 | | $875.00 |
| AMBIGUOUS_NO_MATCH | PO covers more than one invoice; cannot tell which | 1 | $915.75 | | $915.75 |
| PHANTOM | Neither invoice nor PO exists in our ledger | 1 | $530.00 | | $530.00 |
| **Total** | | **8** | **$6,357.65** | **$4,004.87** | **$2,352.78** |

## Claim by claim

**Dispute**

- **CLM-4406, $875.00.** The claim was filed in January 2025, but it refers to invoice INV-09988 from 2 November 2024. The audit window follows the invoice date, so this claim is out of scope.
- **CLM-4404, $915.75.** No invoice number was given, and PO-5521 covers two invoices (INV-10517 for $915.75 and INV-10519 for $210.00). One of them happens to equal the claimed amount, but a matching number is not proof. Reject and ask Fernhill to name the invoice.
- **CLM-4407, $530.00.** Neither INV-99981 nor PO-5599 exists in our records. Reject unless Fernhill can produce supporting documents.

**Concede in part**

- **CLM-4403, concede $380.00, dispute $32.00.** Fernhill claims $412.00 on invoice INV-10501, which was invoiced and paid at $380.00. A shortage cannot be worth more than the goods themselves, so the $32.00 excess is disputed.

**Concede**

- **CLM-4402, $642.50.** Fernhill wrote "88217" for our "0088217"; same invoice, formatting difference only.
- **CLM-4405, concede $299.97.** Fernhill claims $300.00; we were paid $299.97. The 3 cents are within tolerance, but we concede what the ledger shows, not what the claim says.
- **CLM-4401 ($1,180.00) and CLM-4408 ($1,502.40), after proof of delivery.** Both claim the *entire* invoice was short shipped, and in both cases Fernhill had already paid the invoice in full before filing the claim. The reconciliation proves the invoices exist, not that the shortages happened. Requesting proof of delivery for both before paying is cheap insurance.
- **CLM-4408** also arrived with a garbled invoice number ("INV-1O6OO", letters instead of zeros). Read as digits it is INV-10600, and the claim's PO-5530 and amount both confirm it. Ask Fernhill to correct the reference on their side.

## Other observations

- INV-10611 was paid at $299.97 against a $299.99 invoice, a $0.02 short payment unrelated to these claims.
- Two ledger invoices (INV-10650, INV-10701) have no claims against them; nothing to action.
- The process also sends to a person any claim it cannot judge safely: a claim well below the invoice; two claims on the same invoice that together exceed it; an invoice number and PO that point to different invoices; an invoice that was only partly paid (the deduction may already have been taken); and data problems such as a repeated claim ID, an unreadable amount, a missing date, or a claim dated before its invoice (whether that last one is an error depends on Northgate's business rules). None of the 8 claims triggered these checks.
