# AI Working Log

Tool: Claude (agentic session with a Python sandbox and Google Drive access). I wrote my prompts in Portuguese; they are given here in English.

## Prompt 1

> *(Uploaded `Junior_Data_Scientist_Assessment_.docx`, no text.)*

The AI downloaded the three linked files, extracted the rules from the case file and produced a first version: row-by-row script, tests, memo. It also documented 7 judgment calls it had made on its own.

Notes on that version:
- First run had the right flags but a wrong note on CLM-4408. In pandas 3, `Series.map` turns `None` into `NaN`, so `is None` checks silently failed. Fixed.
- Mutation check on the required test: the code was changed to break PO ambiguity by amount, and 3 tests failed, so the test really guards the rule.

## Prompt 2

> "Is this material ready to send? Do you think there is room for my own input? I understand they encourage using AI, but I'd like to have something of my own in it."

The AI said no: the log had no real steering, several business rules had been decided by the AI alone, and the row-by-row loop did not fit the brief's mention of large datasets and Parquet.

## Prompt 3

> "Ask me about everything you consider arbitrary."

The AI asked 12 questions in three rounds. My answers, and where I overrode the AI's first version:

| # | Question | AI's first version | My decision | Changed? | Why (mine) |
|---|---|---|---|---|---|
| 1 | Tolerance compared against | `invoice_amount` | **`paid_amount`**, later **reverted to `invoice_amount`** (see Final validation) | Overrode, then reverted | It is easier to cross-check the claim against the amount that was actually paid. If the difference is within the tolerance, it can be treated as fine. *Reverted because the case states the rule explicitly; the idea survives as a cap on the amount conceded.* |
| 2 | Claim with two problems (out of period and overstated) | Period wins, amount only in the note | **Primary + `secondary_flag`** | Overrode | Better traceability. |
| 3 | Claim more than $1 *below* the reference | Clean, "partial claim" note | **New flag `AMOUNT_MISMATCH`**, sent to review | Overrode | It can be a critical case, so a human review is appropriate. |
| 4 | CLM-4408, `INV-1O6OO` | PO fallback, no OCR repair | **Correct it and show the correction in the output**, but only if it cannot be a different invoice | Overrode | I asked whether `INV-1O6OO` could be a different invoice from `INV-10600`. The AI showed it cannot: Northgate issues the numbers and none contain letters. |
| 5 | When to accept the OCR correction | (new) | **Only if the claim's PO confirms it** (AI's suggestion, accepted) | Kept | My first reason ("the invoice number is the key, so it is the same item") actually argued against the PO check. The AI pointed out that the corrected number is our own guess, not Fernhill's data, and a guess can land on an unrelated real invoice. I asked whether the PO check was manual; it is automatic. I accepted the AI's reasoning: the PO is an independent second confirmation of the guess. |
| 6 | Claim PO differs from ledger PO on an invoice match | Note only | **New flag `REFERENCE_CONFLICT`, match withdrawn, sent to human review** | Overrode (on second thought) | I first kept the AI's version, then changed my mind: for governance and traceability, a claim whose own references contradict each other should be stopped, not passed with a note. |
| 7 | Two claims on the same invoice | Warning in the note | **Sum them and test the total against tolerance** | Overrode | A human review is needed to investigate. |
| 8 | CLM-4403 ($412 vs $380) | Dispute all | **Concede $380, dispute only the $32 excess** | Overrode | Where the items do correspond, the agreed amount can be accepted. |
| 9 | CLM-4404 (ambiguous) | Dispute | Dispute | Kept | **Here I nearly fell into the trap.** My first explanation was "we could discard the invoice that does not correspond", which means dropping INV-10519 because its amount differs and keeping INV-10517. That is exactly the amount-based tie-break the case forbids. The AI flagged the contradiction and I corrected it: an equal amount can be a coincidence or a copy error, and when two invoices are possible only Fernhill can say which one it means. Until it does, the claim is disputed. |
| 10 | CLM-4401 (100% short-ship, fully paid) | Concede after proof of delivery | Same, later extended to CLM-4408 (see Prompt 7) | Kept | The delivery has to be audited: if it was not actually completed, Fernhill should not be charged for it. |
| 11 | Value conceded on cent differences (CLM-4405) | Claimed $300.00 | **Ledger value, $299.97** | Overrode | I was unsure at first and asked to revisit it. Final choice: Northgate never gives back more than it actually received. This still holds after #1 was reverted: the flag is judged on the invoice, the concession is capped at what was paid. |
| 12 | Row-by-row loop vs large data | Left as is | **Refactor to vectorised matching + Parquet/Excel input + equivalence and scale tests** | Overrode | The system has to be built to scale. |

What the refactor produced: joins on canonical keys instead of `iterrows`; integer cents instead of floats; a randomised equivalence test against a naive row-by-row reference (3 seeds, 3,000 claims each); a scale test (300k claims, about 7 s). The AI's first vectorised run took 14 s; profiling showed the time went into string joins for ambiguity candidates and note formatting, both reduced.

**Smaller choices the AI made on its own during the refactor (reviewed, I agree):**
- When several claims together overstate one invoice, they go to `REVIEW` instead of splitting the concession between them.
- On a clean match, concede the *lower* of claim and payment (never more than was asked).
- Malformed amounts (non-numeric, more than 2 decimals) stop the run with an error instead of being coerced.

## Prompt 4

> "Ask me for my explanation on the 12 questions, but describe each scenario in more detail and avoid technical jargon."

My reasoning for each decision is in the "Why (mine)" column above.

## Prompt 5

> "Looking at the raw data I see empty fields, non-standard codes, and one thing caught my attention: some invoices were paid before the claim and others after it. Is that natural or a contradiction?"

The AI answered that payment and claim are separate processes (the retailer usually pays and later deducts from a future remittance), so the order alone is not a contradiction, and the case rules do not use the payment date. It also pointed out that CLM-4408 has the same profile as CLM-4401 (full-invoice short-ship on an invoice already paid) but did not get the same proof-of-delivery caveat in the memo.

## Prompt 6

> "Put it as item 1. It may be a common dynamic; we would probably need more data to state anything."

## Prompt 7

> "Proof of delivery for both."

CLM-4408 has the same profile as CLM-4401, so the memo now asks for proof of delivery on both. Flags and totals are unchanged.

## Prompt 8

> "Review everything to confirm it is all tied together."

The AI re-ran the package from a clean copy of the zip and cross-checked the numbers between the register, the memo, the README and this log. Fixes made:
- The memo's bottom line presented $4,004.87 as conceded without saying that $2,682.40 of it (CLM-4401 and CLM-4408) depends on proof of delivery. Now stated.
- The memo said the overstated claim is conceded "up to the invoice value"; under my rule #1 it is up to what Northgate was paid. Wording fixed (same $380 either way).
- The memo now says that the human-review checks (decisions #3, #6, #7) exist and that none of the 8 claims triggered them.
- Dates read from Excel or Parquet can arrive as "2025-02-14 00:00:00", which the strict date parser rejected. Fixed, with a new test using real Excel date cells.
- README now lists `openpyxl`, needed for Excel input.

## Prompt 9

> "For a few prompts you haven't sent the AI log. Is it up to date?"

It was (the zipped copy matched), but the check showed Prompt 8 had been placed after the verification section. Sections were put back in order.

## Prompt 10

> "Do you think there is any exception left to handle?"

Instead of only reading the code, the AI ran the script against deliberately broken inputs. It found two bugs of its own (a $0.00 claim was flagged for review but recommended "concede"; negative amounts were not validated) and several gaps that were my decisions:

| # | Situation found | What happened before | My decision |
|---|---|---|---|
| 13 | PO written `PO5501` instead of `PO-5501` | Marked PHANTOM and disputed, a false miss | **Normalise POs too**, and accept any separator after `INV` (`INV 10432`) |
| 14 | Same claim ID sent twice | Both conceded; a resubmission would be paid twice | **New flag `DATA_ISSUE`, human review**, rest of the batch continues |
| 15 | Claim dated before its invoice | Accepted normally | `DATA_ISSUE` and review, **with a disclaimer that whether this is an error depends on the business rules** |
| 16 | Ledger invoice with no date | Labelled OUT_OF_PERIOD, as if the date were known | `DATA_ISSUE` and review |
| 17 | Amount written `1,000.00` | Whole run stopped | `DATA_ISSUE` for that row only; not guessed, since separators depend on locale |
| 18 | Unpaid invoice recorded as `0.00` instead of blank | Treated as "paid zero", claim disputed | **Treat as unpaid**, compare with invoice amount |
| 19 | Invoice paid short (e.g. $1,000 invoiced, $600 paid) | Compared with the $600; a deduction already taken could be claimed again unnoticed | **New flag `PARTIAL_PAYMENT`, human review** |

The two bugs were fixed regardless. An empty claims file, which also crashed, now produces an empty register. None of these situations occur in the 8 real claims, so the register and the totals did not change. The randomised equivalence test was extended to generate all of them, and 21 new tests were added (48 in total).

## Prompt 11

> "Ready to send?"

The AI re-checked that the zip matched the latest files and that all tests passed from a clean copy.

## Prompt 12

> "Can you create the repo and push it to GitHub?" (Public, named `claims-reconciliation`, with no mention of the hiring company.)

The AI checked every file for mentions of the company before publishing (none found), added `.gitignore` and `requirements.txt`, and pushed. The first push went out with "Claude" as commit author because of a pre-set git identity; the AI caught it on verification and rewrote the single commit under my name, keeping Claude as co-author.

*(Prompts about drafting the reply email are omitted here: they were not part of the work.)*

## Final validation

> "How do you assess our result?"

I asked the AI for a critical review before submitting. It listed what was strong (all traps handled, required test validated by mutation, honest log, my payment-timing observation leading to the CLM-4408 fix) and four risks, starting with its own mistake:

1. **Rule #1 contradicted the brief.** The case says the tolerance is measured against "the independent invoice amount". The AI had presented this as an open choice when the brief had already settled it, and I had switched it to the paid amount. It changed no flag on the case data, but it was a change to a written rule.
2. **Four flags beyond the five the case defines**, which could read as not following the spec.
3. **My first prompt** was just the uploaded file, close to the "here are two CSVs, reconcile them" the brief warns about.
4. **Short justifications** for several decisions where I accepted the AI's recommendation.

> "OK. Tie everything together and push these adjustments to GitHub. Add this interaction to the AI log as a final validation."

What changed:
- **Rule #1 reverted to the brief:** flags are judged against the invoice amount. My original concern is kept as a separate rule: the amount conceded never exceeds what Northgate was actually paid. On the case data, flags and totals are unchanged (concede $4,004.87, dispute $2,352.78).
- **README:** new section "Relation to the brief", stating that the five case flags follow the written rules exactly and that the extra flags are a review-only safety layer that does not fire on the case data.
- Risks 3 and 4 are recorded here as they are; I will answer for them in the interview.

## My verification against the raw rows

I opened both CSVs and checked them by hand.

1. **Payment timing.** Some invoices were paid before the claim was filed (CLM-4401, 4406, 4408) and others after it, in full (CLM-4402, 4403, 4405). It may be a common dynamic, since deductions are usually taken from later payments, but with only 8 claims I would need more data before stating anything. I did not turn this into a rule.
2. **Empty fields.** CLM-4404 has no invoice reference at all, only the PO, which is what makes it ambiguous.
3. **Non-standard codes.** Invoice numbers come in different formats: `88217` in the claim vs `0088217` in the ledger (CLM-4402), and `INV-1O6OO` with letters instead of zeros (CLM-4408). Both are handled by the normalisation and OCR rules above.
