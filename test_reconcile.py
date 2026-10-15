"""Run with:  pytest -q"""

import random
import re
import time
from pathlib import Path

import pandas as pd
import pytest

from reconcile import (
    AMBIGUOUS_NO_MATCH, AMOUNT_MISMATCH, AMOUNT_OVERSTATED, CLEAN_MATCH, OUT_OF_PERIOD, PHANTOM,
    DATA_ISSUE, PARTIAL_PAYMENT, REFERENCE_CONFLICT, REVIEW, canonical_invoice, canonical_po, load, prepare,
    reconcile,
)

DATA = Path(__file__).parent / "data"
CLAIM_HDR = "claim_id,invoice_ref,po_ref,claim_date,claimed_amount,reason_code\n"
LEDGER_HDR = "invoice_number,po_number,invoice_date,invoice_amount,paid_amount,paid_date\n"


def run(tmp_path, claims_rows: str, ledger_rows: str) -> pd.DataFrame:
    (tmp_path / "c.csv").write_text(CLAIM_HDR + claims_rows)
    (tmp_path / "l.csv").write_text(LEDGER_HDR + ledger_rows)
    return reconcile(*load(tmp_path / "c.csv", tmp_path / "l.csv")).set_index("claim_id")


@pytest.fixture(scope="module")
def register():
    claims, ledger = load(DATA / "fernhill_claims.csv", DATA / "northgate_ledger.csv")
    return reconcile(claims, ledger).set_index("claim_id")


# --- Required test (Deliverable 3) ------------------------------------------
def test_ambiguous_po_5521_is_rejected_not_guessed(register):
    """CLM-4404 has no invoice_ref and PO-5521 sits on two ledger invoices.

    INV-10517 is $915.75, exactly the claimed amount, which makes it the
    tempting pick. The rule forbids resolving ambiguity by amount, so the
    claim must stay unmatched.
    """
    row = register.loc["CLM-4404"]
    assert row["status_flag"] == AMBIGUOUS_NO_MATCH
    assert row["matched_invoice"] == ""
    assert "INV-10517" in row["note"] and "INV-10519" in row["note"]


def test_ambiguity_guard_holds_even_with_a_perfect_amount_match(tmp_path):
    reg = run(tmp_path,
              "T-1,,PO-1,2025-06-01,100.00,SHORTAGE\n",
              "INV-1,PO-1,2025-05-01,100.00,100.00,2025-05-10\n"
              "INV-2,PO-1,2025-05-02,999.00,999.00,2025-05-10\n")
    assert reg.loc["T-1", "status_flag"] == AMBIGUOUS_NO_MATCH
    assert reg.loc["T-1", "matched_invoice"] == ""


# --- Case data: every row ------------------------------------------------------
def test_full_register(register):
    expected = {  # claim: (matched_invoice, status_flag, secondary_flag, method)
        "CLM-4401": ("INV-10432", CLEAN_MATCH, "", "invoice"),
        "CLM-4402": ("0088217", CLEAN_MATCH, "", "invoice"),          # leading zeros
        "CLM-4403": ("INV-10501", AMOUNT_OVERSTATED, "", "invoice"),  # +$32
        "CLM-4404": ("", AMBIGUOUS_NO_MATCH, "", "po"),
        "CLM-4405": ("INV-10611", CLEAN_MATCH, "", "invoice"),        # +$0.03 vs paid
        "CLM-4406": ("INV-09988", OUT_OF_PERIOD, "", "invoice"),      # 2024 invoice
        "CLM-4407": ("", PHANTOM, "", "none"),
        "CLM-4408": ("INV-10600", CLEAN_MATCH, "", "invoice_ocr"),    # O->0, PO confirms
    }
    for cid, exp in expected.items():
        got = tuple(register.loc[cid, ["matched_invoice", "status_flag", "secondary_flag", "method"]])
        assert got == exp, cid


def test_money_split_adds_up(register):
    r = register
    assert (r["concede_c"] + r["dispute_c"] + r["review_c"] == r["claimed_c"]).all()
    assert r.loc["CLM-4403", "concede_c"] == 38000 and r.loc["CLM-4403", "dispute_c"] == 3200
    assert r.loc["CLM-4405", "concede_c"] == 29997  # ledger paid amount, not the claim


# --- Individual rules -----------------------------------------------------------
@pytest.mark.parametrize("raw,exact,ocr", [
    ("INV-10432", "10432", None), ("0088217", "88217", None), ("88217", "88217", None),
    ("inv-09988", "9988", None), ("INV-1O6OO", None, "10600"), ("INV-1I0L", None, "1101"),
    ("", None, None), ("INV-ABC", None, None), ("INV 10432", "10432", None), ("INV#10432", "10432", None),
    ("inv_00432", "432", None),
])
def test_canonical_invoice(raw, exact, ocr):
    e, o = canonical_invoice(pd.Series([raw]))
    assert (None if pd.isna(e[0]) else e[0]) == exact
    assert (None if pd.isna(o[0]) else o[0]) == ocr


def test_ocr_correction_rejected_when_po_disagrees(tmp_path):
    """'INV-1O6OO' reads as INV-10600, but the claim's PO belongs to another invoice."""
    reg = run(tmp_path,
              "T-1,INV-1O6OO,PO-9,2025-07-01,50.00,X\n",
              "INV-10600,PO-1,2025-06-01,1502.40,1502.40,2025-06-10\n"
              "INV-20000,PO-9,2025-06-01,50.00,50.00,2025-06-10\n")
    assert reg.loc["T-1", "method"] == "po"            # OCR reading ignored
    assert reg.loc["T-1", "matched_invoice"] == "INV-20000"
    assert "not confirmed by PO" in reg.loc["T-1", "note"]


def test_tolerance_is_against_paid_amount(tmp_path):
    """$101 vs invoice $100 would be clean; vs the $99.50 actually paid it is $1.50 over."""
    reg = run(tmp_path,
              "T-1,INV-1,PO-1,2025-07-01,101.00,X\n",
              "INV-1,PO-1,2025-06-01,100.00,99.50,2025-06-10\n")
    assert reg.loc["T-1", "status_flag"] == AMOUNT_OVERSTATED


def test_unpaid_invoice_falls_back_to_invoice_amount(tmp_path):
    reg = run(tmp_path,
              "T-1,INV-1,PO-1,2025-07-01,100.00,X\n",
              "INV-1,PO-1,2025-06-01,100.00,,\n")
    assert reg.loc["T-1", "status_flag"] == CLEAN_MATCH


def test_under_claim_is_amount_mismatch(tmp_path):
    reg = run(tmp_path,
              "T-1,INV-1,PO-1,2025-07-01,40.00,X\n",
              "INV-1,PO-1,2025-06-01,100.00,100.00,2025-06-10\n")
    assert reg.loc["T-1", "status_flag"] == AMOUNT_MISMATCH
    assert reg.loc["T-1", "recommendation"] == REVIEW


def test_out_of_period_keeps_amount_problem_as_secondary(tmp_path):
    reg = run(tmp_path,
              "T-1,INV-1,PO-1,2025-01-15,150.00,X\n",
              "INV-1,PO-1,2024-12-31,100.00,100.00,2025-01-05\n")
    assert reg.loc["T-1", "status_flag"] == OUT_OF_PERIOD
    assert reg.loc["T-1", "secondary_flag"] == AMOUNT_OVERSTATED


def test_audit_window_boundaries_are_inclusive(tmp_path):
    reg = run(tmp_path,
              "A,INV-1,PO-1,2025-02-01,10.00,X\nB,INV-2,PO-2,2026-02-01,10.00,X\n",
              "INV-1,PO-1,2025-01-01,10.00,10.00,2025-01-05\n"
              "INV-2,PO-2,2025-12-31,10.00,10.00,2026-01-05\n")
    assert (reg["status_flag"] == CLEAN_MATCH).all()


def test_claims_on_same_invoice_are_summed(tmp_path):
    """Each claim alone fits inside $100, together they deduct $120."""
    reg = run(tmp_path,
              "A,INV-1,PO-1,2025-07-01,60.00,X\nB,00001,PO-1,2025-08-01,60.00,X\n",
              "INV-1,PO-1,2025-06-01,100.00,100.00,2025-06-10\n")
    assert (reg["status_flag"] == AMOUNT_OVERSTATED).all()
    assert (reg["recommendation"] == REVIEW).all()


def test_po_conflict_on_invoice_match_goes_to_review(tmp_path):
    """Invoice exists and amount is exact, but the claim's PO belongs to another invoice."""
    reg = run(tmp_path,
              "T-1,INV-1,PO-2,2025-07-01,100.00,X\n",
              "INV-1,PO-1,2025-06-01,100.00,100.00,2025-06-10\n"
              "INV-2,PO-2,2025-06-01,100.00,100.00,2025-06-10\n")
    row = reg.loc["T-1"]
    assert row["status_flag"] == REFERENCE_CONFLICT
    assert row["matched_invoice"] == ""
    assert row["recommendation"] == REVIEW and row["review_c"] == 10000


@pytest.mark.parametrize("raw,key", [
    ("PO-5501", "5501"), ("PO5501", "5501"), ("po 5501", "5501"), ("5501", "5501"), ("PO-05501", "5501"),
    ("PO_AB-12", "AB12"), ("", None),
])
def test_canonical_po(raw, key):
    k = canonical_po(pd.Series([raw]))[0]
    assert (None if pd.isna(k) else k) == key


def test_po_format_drift_is_not_a_false_phantom(tmp_path):
    reg = run(tmp_path,
              "T-1,,PO5501,2025-07-01,100.00,X\n",
              "INV-1,PO-5501,2025-06-01,100.00,100.00,2025-06-10\n")
    assert reg.loc["T-1", "status_flag"] == CLEAN_MATCH
    assert reg.loc["T-1", "matched_invoice"] == "INV-1"


# --- Data issues: flagged and reviewed, never stop the batch ------------------------
def test_duplicate_claim_id_goes_to_review(tmp_path):
    reg = run(tmp_path,
              "A,INV-1,PO-1,2025-07-01,50.00,X\nA,INV-1,PO-1,2025-07-01,50.00,X\n",
              "INV-1,PO-1,2025-06-01,100.00,100.00,2025-06-10\n").reset_index()
    assert (reg["status_flag"] == DATA_ISSUE).all()
    assert (reg["recommendation"] == REVIEW).all()
    assert reg["note"].str.contains("not unique").all()


def test_claim_dated_before_invoice_is_flagged_with_disclaimer(tmp_path):
    reg = run(tmp_path,
              "T-1,INV-1,PO-1,2025-05-01,100.00,X\n",
              "INV-1,PO-1,2025-06-01,100.00,100.00,2025-06-10\n")
    assert reg.loc["T-1", "status_flag"] == DATA_ISSUE
    assert reg.loc["T-1", "secondary_flag"] == ""             # otherwise clean
    assert "depends on business rules" in reg.loc["T-1", "note"]


def test_missing_ledger_date_is_data_issue_not_out_of_period(tmp_path):
    reg = run(tmp_path,
              "T-1,INV-1,PO-1,2025-07-01,100.00,X\n",
              "INV-1,PO-1,,100.00,100.00,2025-06-10\n")
    assert reg.loc["T-1", "status_flag"] == DATA_ISSUE


def test_invalid_amount_does_not_stop_the_batch(tmp_path):
    reg = run(tmp_path,
              "BAD,INV-1,PO-1,2025-07-01,\"1,000.00\",X\nOK,INV-2,PO-2,2025-07-01,50.00,X\n",
              "INV-1,PO-1,2025-06-01,1000.00,1000.00,2025-06-10\n"
              "INV-2,PO-2,2025-06-01,50.00,50.00,2025-06-10\n")
    assert reg.loc["BAD", "status_flag"] == DATA_ISSUE
    assert reg.loc["OK", "status_flag"] == CLEAN_MATCH


@pytest.mark.parametrize("amount", ["0.00", "-50.00", ""])
def test_zero_negative_or_blank_claim_goes_to_review(tmp_path, amount):
    reg = run(tmp_path,
              f"T-1,INV-1,PO-1,2025-07-01,{amount},X\n",
              "INV-1,PO-1,2025-06-01,100.00,100.00,2025-06-10\n")
    assert reg.loc["T-1", "status_flag"] == DATA_ISSUE
    assert reg.loc["T-1", "recommendation"] == REVIEW     # was CONCEDE before the fix


def test_paid_recorded_as_zero_means_unpaid(tmp_path):
    reg = run(tmp_path,
              "T-1,INV-1,PO-1,2025-07-01,100.00,X\n",
              "INV-1,PO-1,2025-06-01,100.00,0.00,\n")
    assert reg.loc["T-1", "status_flag"] == CLEAN_MATCH


def test_partial_payment_goes_to_review(tmp_path):
    """Invoice $1,000 paid $600: the $400 claim may already have been deducted."""
    reg = run(tmp_path,
              "T-1,INV-1,PO-1,2025-07-01,400.00,SHORTAGE\n",
              "INV-1,PO-1,2025-06-01,1000.00,600.00,2025-06-10\n")
    assert reg.loc["T-1", "status_flag"] == PARTIAL_PAYMENT
    assert reg.loc["T-1", "secondary_flag"] == AMOUNT_MISMATCH
    assert reg.loc["T-1", "recommendation"] == REVIEW


def test_empty_claims_file(tmp_path):
    reg = run(tmp_path, "", "INV-1,PO-1,2025-06-01,100.00,100.00,2025-06-10\n")
    assert len(reg) == 0


def test_ledger_ids_colliding_after_normalisation_are_ambiguous(tmp_path):
    reg = run(tmp_path,
              "T-1,88217,PO-1,2025-07-01,10.00,X\n",
              "0088217,PO-1,2025-06-01,10.00,10.00,2025-06-10\n"
              "INV-88217,PO-2,2025-06-01,10.00,10.00,2025-06-10\n")
    assert reg.loc["T-1", "status_flag"] == AMBIGUOUS_NO_MATCH


# --- Equivalence + scale ----------------------------------------------------------
def _reference(claims: list[dict], ledger: list[dict]) -> dict:
    """Deliberately naive row-by-row implementation of the same rules."""
    sep = r"[\s\-_#./]*"

    def canon(x):
        b = re.sub(r"^INV" + sep, "", x.strip().upper())
        return (b.lstrip("0") or "0") if b.isdigit() else None

    def pcanon(x):
        b = re.sub(r"^PO" + sep, "", x.strip().upper())
        k = (b.lstrip("0") or "0") if b.isdigit() else re.sub(r"[^0-9A-Z]", "", b)
        return k or None

    def ocr(x):
        b = re.sub(r"^INV" + sep, "", x.strip().upper())
        if not b or b.isdigit():
            return None
        f = b.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
        return (f.lstrip("0") or "0") if f.isdigit() else None

    by_inv, by_po = {}, {}
    for i, l in enumerate(ledger):
        by_inv.setdefault(canon(l["invoice_number"]), []).append(i)
        by_po.setdefault(pcanon(l["po_number"]), []).append(i)

    hits = {}
    for c in claims:
        k, po = canon(c["invoice_ref"]), pcanon(c["po_ref"])
        cand = by_inv.get(k, []) if k else []
        o = ocr(c["invoice_ref"]) if not k else None
        ocand = by_inv.get(o, []) if o else []
        if len(cand) == 1 and po and pcanon(ledger[cand[0]]["po_number"]) != po:
            hits[c["claim_id"]] = REFERENCE_CONFLICT
        elif len(cand) == 1:
            hits[c["claim_id"]] = cand[0]
        elif len(cand) > 1:
            hits[c["claim_id"]] = AMBIGUOUS_NO_MATCH
        elif len(ocand) == 1 and pcanon(ledger[ocand[0]]["po_number"]) == po:
            hits[c["claim_id"]] = ocand[0]
        else:
            p = by_po.get(po, []) if po else []
            hits[c["claim_id"]] = p[0] if len(p) == 1 else (AMBIGUOUS_NO_MATCH if p else PHANTOM)

    cents = lambda s: round(float(s) * 100)
    early = {c["claim_id"]: isinstance(hits[c["claim_id"]], int)
             and c["claim_date"] < ledger[hits[c["claim_id"]]]["invoice_date"] for c in claims}
    totals = {}
    for c in claims:
        h = hits[c["claim_id"]]
        if isinstance(h, int) and not early[c["claim_id"]]:
            totals[h] = totals.get(h, 0) + cents(c["claimed_amount"])

    out = {}
    for c in claims:
        h = hits[c["claim_id"]]
        if early[c["claim_id"]]:
            out[c["claim_id"]] = (ledger[h]["invoice_number"], DATA_ISSUE)
            continue
        if not isinstance(h, int):
            out[c["claim_id"]] = ("", h)
            continue
        l = ledger[h]
        paid = cents(l["paid_amount"]) if l["paid_amount"] else 0
        inv = cents(l["invoice_amount"])
        ref = paid if paid else inv
        d = totals[h] - ref
        if not ("2025-01-01" <= l["invoice_date"] <= "2025-12-31"):
            flag = OUT_OF_PERIOD
        elif paid and inv - paid > 100:
            flag = PARTIAL_PAYMENT
        elif d > 100:
            flag = AMOUNT_OVERSTATED
        elif d < -100:
            flag = AMOUNT_MISMATCH
        else:
            flag = CLEAN_MATCH
        out[c["claim_id"]] = (l["invoice_number"], flag)
    return out


def _random_case(n_ledger: int, n_claims: int, seed: int):
    rnd = random.Random(seed)
    fmt = [lambda n: f"INV-{n:05d}", lambda n: f"{n:07d}", lambda n: str(n)]
    ledger = []
    for i in range(n_ledger):
        num = rnd.randint(1, n_ledger * 3)                  # collisions happen on purpose
        amt = rnd.randint(1000, 500000)
        paid = rnd.choice([amt, amt, amt - rnd.randint(1, 300), None, 0])
        ledger.append({
            "invoice_number": rnd.choice(fmt)(num),
            "po_number": f"PO-{rnd.randint(1, n_ledger):05d}",  # shared POs happen too
            "invoice_date": f"{rnd.choice([2024, 2025, 2025, 2025, 2026])}-{rnd.randint(1, 12):02d}-15",
            "invoice_amount": f"{amt / 100:.2f}",
            "paid_amount": "" if paid is None else f"{paid / 100:.2f}",
            "paid_date": "",
        })
    claims = []
    for j in range(n_claims):
        l = rnd.choice(ledger)
        num = int(re.sub(r"\D", "", l["invoice_number"]))
        kind = rnd.random()
        if kind < 0.5:
            ref = rnd.choice(fmt)(num)
        elif kind < 0.6:
            ref = f"INV-{num:05d}".replace("0", "O")          # OCR damage
        elif kind < 0.7:
            ref = ""
        else:
            ref = f"INV-{rnd.randint(1, n_ledger * 6):05d}"   # may or may not exist
        po_num = int(l["po_number"][3:]) if rnd.random() < 0.85 else rnd.randint(1, n_ledger * 2)
        po = rnd.choice([f"PO-{po_num:05d}", f"PO{po_num}", f"{po_num}", f"po {po_num:05d}"])  # format drift
        ref = rnd.choice([ref, ref, ref.replace("INV-", "INV ")])
        base = float((l["paid_amount"] if l["paid_amount"] not in ("", "0.00") else "") or l["invoice_amount"]) * 100
        claims.append({
            "claim_id": f"C-{j}", "invoice_ref": ref, "po_ref": po,
            "claim_date": f"{rnd.choice([2025, 2025, 2026])}-{rnd.randint(1, 12):02d}-10",
            "claimed_amount": f"{(base + rnd.choice([0, 0, 50, -50, 150, -150, 5000])) / 100:.2f}",
            "reason_code": "X",
        })
    return claims, ledger


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_vectorised_equals_row_by_row_reference(seed):
    claims, ledger = _random_case(n_ledger=800, n_claims=3000, seed=seed)
    reg = reconcile(*prepare(pd.DataFrame(claims), pd.DataFrame(ledger))).set_index("claim_id")
    ref = _reference(claims, ledger)
    got = dict(zip(reg.index, zip(reg["matched_invoice"], reg["status_flag"])))
    diffs = [(k, got[k], ref[k]) for k in ref if got[k] != ref[k]]
    assert not diffs, diffs[:5]
    assert len(set(reg["status_flag"])) >= 8  # the random data really exercises the rules


def test_scales_to_large_input():
    claims, ledger = _random_case(n_ledger=100_000, n_claims=300_000, seed=7)
    c, l = pd.DataFrame(claims), pd.DataFrame(ledger)
    t = time.perf_counter()
    reg = reconcile(*prepare(c, l))
    elapsed = time.perf_counter() - t
    assert len(reg) == 300_000
    assert elapsed < 20, f"took {elapsed:.1f}s"


def test_reads_parquet(tmp_path):
    pd.read_csv(DATA / "fernhill_claims.csv", dtype=str).to_parquet(tmp_path / "c.parquet")
    pd.read_csv(DATA / "northgate_ledger.csv", dtype=str).to_parquet(tmp_path / "l.parquet")
    reg = reconcile(*load(tmp_path / "c.parquet", tmp_path / "l.parquet")).set_index("claim_id")
    assert reg.loc["CLM-4402", "matched_invoice"] == "0088217"  # leading zeros survived
    assert reg.loc["CLM-4404", "status_flag"] == AMBIGUOUS_NO_MATCH


def test_reads_excel_with_real_date_cells(tmp_path):
    """Excel stores dates as datetimes; they must still parse and keep the period rule."""
    led = pd.read_csv(DATA / "northgate_ledger.csv", dtype=str)
    led["invoice_date"] = pd.to_datetime(led["invoice_date"])  # real date cells
    led.to_excel(tmp_path / "l.xlsx", index=False)
    pd.read_csv(DATA / "fernhill_claims.csv", dtype=str).to_excel(tmp_path / "c.xlsx", index=False)
    reg = reconcile(*load(tmp_path / "c.xlsx", tmp_path / "l.xlsx")).set_index("claim_id")
    assert reg.loc["CLM-4406", "status_flag"] == OUT_OF_PERIOD
    assert reg.loc["CLM-4401", "status_flag"] == CLEAN_MATCH
