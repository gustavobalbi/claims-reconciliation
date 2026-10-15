"""
Northgate vs Fernhill claim reconciliation.

Reads Fernhill's claim file (the retailer's assertion) and Northgate's
independent ledger (ground truth) and writes a register with one row per
claim: claim_id, matched_invoice, status_flag, note (plus detail columns).

Governing principle: the claim file is a hypothesis, never evidence. A claim
only gets credit for what the independent ledger confirms.

Matching is fully vectorised (joins on canonical keys, no per-row loops), so
it scales to large files. Inputs may be CSV, Excel or Parquet. A bad row never
stops the run: it is flagged DATA_ISSUE and sent to review, and the rest of the
batch is processed.

Usage:
    python reconcile.py [claims_file] [ledger_file] [output_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------
AUDIT_START = pd.Timestamp("2025-01-01")
AUDIT_END = pd.Timestamp("2025-12-31")
TOLERANCE_CENTS = 100  # $1.00 around the INVOICE amount, as the case specifies

CLEAN_MATCH = "CLEAN_MATCH"
OUT_OF_PERIOD = "OUT_OF_PERIOD"
AMBIGUOUS_NO_MATCH = "AMBIGUOUS_NO_MATCH"
AMOUNT_OVERSTATED = "AMOUNT_OVERSTATED"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"        # claim more than $1.00 BELOW the paid amount
REFERENCE_CONFLICT = "REFERENCE_CONFLICT"  # invoice found, but the claim's PO points elsewhere
PARTIAL_PAYMENT = "PARTIAL_PAYMENT"        # invoice paid more than $1.00 short: deduction may already be taken
DATA_ISSUE = "DATA_ISSUE"                  # the row cannot be evaluated reliably as it stands
PHANTOM = "PHANTOM"

CONCEDE, CONCEDE_PARTIAL, DISPUTE, REVIEW = "CONCEDE", "CONCEDE_PARTIAL", "DISPUTE", "REVIEW"
REVIEW_FLAGS = {AMOUNT_MISMATCH, REFERENCE_CONFLICT, PARTIAL_PAYMENT, DATA_ISSUE}

# Characters OCR commonly confuses with digits. Only used when the PO confirms.
OCR_MAP = str.maketrans({"O": "0", "I": "1", "L": "1"})
SEP = r"[\s\-_#./]*"  # separators tolerated after a prefix: "INV-", "INV ", "INV#", "PO_" ...

CLAIM_COLS = ["claim_id", "invoice_ref", "po_ref", "claim_date", "claimed_amount", "reason_code"]
LEDGER_COLS = ["invoice_number", "po_number", "invoice_date", "invoice_amount", "paid_amount", "paid_date"]


# ---------------------------------------------------------------------------
# Loading and cleaning
# ---------------------------------------------------------------------------
def read_any(path) -> pd.DataFrame:
    """Read CSV / Excel / Parquet with every column as text.

    Text-only reading is what keeps '0088217' from becoming 88217 and a blank
    invoice_ref from becoming NaN.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str, keep_default_na=False)
    elif ext == ".parquet":
        df = pd.read_parquet(path).astype(str).replace({"None": "", "nan": "", "<NA>": "", "NaT": ""})
    else:
        raise ValueError(f"Unsupported file type: {path.name}")
    return df.fillna("").astype(str).apply(lambda c: c.str.strip())


def _require(df: pd.DataFrame, cols: list[str], name: str) -> None:
    """Missing COLUMNS are a broken file, not a bad row: that one does stop the run."""
    missing = set(cols) - set(df.columns)
    if missing:
        raise ValueError(f"{name} is missing columns: {sorted(missing)}")


def to_cents(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Money as integer cents (exact, no float drift).

    Returns (cents, invalid): cents is NA for blank or unreadable values;
    invalid marks values that are present but unreadable (e.g. '1,000.00').
    Nothing is guessed: '1,000.00' could be one thousand or one, depending on locale.
    """
    ok = s.str.fullmatch(r"-?\d+(\.\d{1,2})?")
    parts = s.where(ok, "0").str.extract(r"(-?)(\d+)(?:\.(\d{1,2}))?")
    cents = parts[1].astype("int64") * 100 + parts[2].fillna("0").str.ljust(2, "0").astype("int64")
    cents = cents.where(parts[0] != "-", -cents)
    return cents.astype("Int64").where(ok), ~ok & (s != "")


def to_date(s: pd.Series) -> pd.Series:
    # First 10 chars: tolerates "2025-02-14 00:00:00" coming from Excel/Parquet dates.
    return pd.to_datetime(s.str[:10], format="%Y-%m-%d", errors="coerce")


def _strip_zeros(x: pd.Series) -> pd.Series:
    y = x.str.lstrip("0")
    return y.where(y != "", "0")


def canonical_invoice(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Return (exact_key, ocr_key) for a series of invoice references.

    exact_key: 'INV' prefix (with any separator) and leading zeros removed;
               NA if not purely digits.
    ocr_key:   only for refs that failed exact parsing: O->0, I/L->1 applied,
               NA if still not digits. Used later ONLY when the PO agrees.
    """
    body = s.fillna("").str.strip().str.upper().str.replace(rf"^INV{SEP}", "", regex=True)
    is_num = body.str.fullmatch(r"\d+")
    exact = _strip_zeros(body).where(is_num)

    fixed = body.str.translate(OCR_MAP)
    ocr_ok = ~is_num & (body != "") & fixed.str.fullmatch(r"\d+")
    ocr = _strip_zeros(fixed).where(ocr_ok)
    return exact, ocr


def canonical_po(s: pd.Series) -> pd.Series:
    """'PO-5501', 'PO5501', 'po 5501' and '5501' all become '5501'. NA if blank."""
    body = s.fillna("").str.strip().str.upper().str.replace(rf"^PO{SEP}", "", regex=True)
    key = _strip_zeros(body).where(body.str.fullmatch(r"\d+"), body.str.replace(r"[^0-9A-Z]", "", regex=True))
    return key.replace("", pd.NA)


def prepare(claims: pd.DataFrame, ledger: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    _require(claims, CLAIM_COLS, "claims")
    _require(ledger, LEDGER_COLS, "ledger")
    claims = claims.reset_index(drop=True).fillna("").astype(str).copy()
    ledger = ledger.reset_index(drop=True).fillna("").astype(str).copy()

    claims["inv_key"], claims["ocr_key"] = canonical_invoice(claims["invoice_ref"])
    claims["po_key"] = canonical_po(claims["po_ref"])
    claims["claimed_c"], _ = to_cents(claims["claimed_amount"])
    claims["claim_dt"] = to_date(claims["claim_date"])

    ledger["inv_key"], _ = canonical_invoice(ledger["invoice_number"])
    ledger["po_key"] = canonical_po(ledger["po_number"])
    ledger["invoice_c"], _ = to_cents(ledger["invoice_amount"])
    paid_c, paid_bad = to_cents(ledger["paid_amount"])
    # Unpaid invoice: blank OR recorded as 0.00.
    ledger["paid_c"] = paid_c.mask(paid_c == 0)
    # Flags are judged against the INVOICE amount (case rule). The amount conceded
    # is capped at what Northgate actually received (the invoice amount if unpaid).
    ledger["ref_c"] = ledger["invoice_c"]
    ledger["cap_c"] = ledger["paid_c"].fillna(ledger["invoice_c"])
    ledger["inv_dt"] = to_date(ledger["invoice_date"])
    ledger["partial"] = (
        (ledger["invoice_c"] - ledger["paid_c"] > TOLERANCE_CENTS).fillna(False).astype(bool)
    )

    # Ledger rows that cannot support a decision, with the reason.
    problems = [
        (ledger["invoice_c"].isna(), "invoice_amount '" + ledger["invoice_amount"] + "' invalid"),
        (paid_bad, "paid_amount '" + ledger["paid_amount"] + "' invalid"),
        (ledger["inv_dt"].isna(), "invoice_date '" + ledger["invoice_date"] + "' missing or invalid"),
    ]
    ledger["bad_reason"] = _join_reasons(problems, ledger.index)
    ledger["bad"] = ledger["bad_reason"] != ""
    return claims, ledger


def load(claims_path, ledger_path) -> tuple[pd.DataFrame, pd.DataFrame]:
    return prepare(read_any(claims_path), read_any(ledger_path))


def _join_reasons(pairs, index) -> pd.Series:
    """Vectorised '; '.join of the reasons whose condition is True on each row."""
    out = pd.Series("", index=index, dtype=object)
    for cond, text in pairs:
        cond = pd.Series(cond, index=index).fillna(False).astype(bool)
        add = text if isinstance(text, pd.Series) else pd.Series(text, index=index)
        out = out.where(~cond, np.where(out == "", add, out + "; " + add))
    return out


# ---------------------------------------------------------------------------
# Matching (vectorised)
# ---------------------------------------------------------------------------
def _lookup(keys: pd.Series, ledger: pd.DataFrame, col: str) -> tuple[pd.Series, pd.Series]:
    """For each key: number of ledger rows carrying it, and the ledger row id if unique."""
    counts = ledger[col].value_counts()
    n = keys.map(counts).fillna(0).astype(int)
    uniq = ledger.loc[ledger[col].map(counts) == 1, [col]].reset_index(names="lid").set_index(col)["lid"]
    lid = keys.map(uniq).where(n == 1)
    return n, lid


def match(claims: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    """Add lid (ledger row id or NA), method and an unmatched flag per claim."""
    c = claims
    lid = pd.Series(pd.NA, index=c.index, dtype="Int64")
    method = pd.Series("", index=c.index, dtype=object)
    flag = pd.Series("", index=c.index, dtype=object)

    # 1) Invoice number.
    n_inv, lid_inv = _lookup(c["inv_key"], ledger, "inv_key")
    hit = n_inv == 1
    lid[hit], method[hit] = lid_inv[hit], "invoice"
    amb_inv = n_inv > 1
    flag[amb_inv], method[amb_inv] = AMBIGUOUS_NO_MATCH, "invoice"

    # Invoice found, but the claim's own PO belongs to a different invoice.
    # The two references contradict each other, so nothing is confirmed:
    # the match is withdrawn and the claim goes to a human.
    ledger_po = lid_inv.map(ledger["po_key"])
    conflict = hit & c["po_key"].notna() & (ledger_po != c["po_key"]).fillna(False)
    conflict_inv = lid_inv.map(ledger["invoice_number"]).where(conflict, "")
    conflict_po = lid_inv.map(ledger["po_number"]).where(conflict, "")
    lid[conflict], flag[conflict] = pd.NA, REFERENCE_CONFLICT

    # 2) OCR-corrected invoice number, accepted ONLY if the claim's PO agrees.
    open_ = (method == "")
    n_ocr, lid_ocr = _lookup(c["ocr_key"], ledger, "inv_key")
    ocr_po = lid_ocr.map(ledger["po_key"])
    ocr_hit = open_ & (n_ocr == 1) & (ocr_po == c["po_key"]).fillna(False)
    lid[ocr_hit], method[ocr_hit] = lid_ocr[ocr_hit], "invoice_ocr"

    # 3) PO fallback: exactly one candidate, never broken by amount.
    open_ = (method == "")
    n_po, lid_po = _lookup(c["po_key"], ledger, "po_key")
    po_hit = open_ & (n_po == 1)
    lid[po_hit], method[po_hit] = lid_po[po_hit], "po"
    po_amb = open_ & (n_po > 1)
    flag[po_amb], method[po_amb] = AMBIGUOUS_NO_MATCH, "po"
    nothing = open_ & (n_po == 0)
    flag[nothing], method[nothing] = PHANTOM, "none"

    # Candidate lists, so a reviewer sees what the ambiguity actually is.
    # Built only for the keys that are actually ambiguous (string joins are slow).
    label = ledger["invoice_number"] + " ($" + ledger["invoice_amount"] + ")"

    def joined(col: str, keys: pd.Series) -> pd.Series:
        sub = ledger[col].isin(set(keys.dropna()))
        return label[sub].groupby(ledger.loc[sub, col]).agg(", ".join)

    cands = pd.Series("", index=c.index, dtype=object)
    cands[po_amb] = c.loc[po_amb, "po_key"].map(joined("po_key", c.loc[po_amb, "po_key"]))
    cands[amb_inv] = c.loc[amb_inv, "inv_key"].map(joined("inv_key", c.loc[amb_inv, "inv_key"]))

    return c.assign(lid=lid, method=method, unmatched_flag=flag, n_inv=n_inv, n_po=n_po, candidates=cands,
                    conflict_inv=conflict_inv.fillna(""), conflict_po=conflict_po.fillna(""))


# ---------------------------------------------------------------------------
# Flags, recommendation and notes
# ---------------------------------------------------------------------------
def _money(cents) -> str:
    v = int(cents)
    return f"{'-' if v < 0 else ''}${abs(v) / 100:,.2f}"


OUTPUT_COLS = ["claim_id", "matched_invoice", "status_flag", "secondary_flag", "method", "recommendation",
               "claimed_c", "concede_c", "dispute_c", "review_c", "note"]


def reconcile(claims: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    if claims.empty:  # nothing to reconcile: an empty register, not an error
        return pd.DataFrame({c: pd.Series(dtype="int64" if c.endswith("_c") else object) for c in OUTPUT_COLS})
    m = match(claims, ledger)
    L = ledger.add_prefix("l_")
    m = m.join(L, on="lid")
    matched = m["lid"].notna()
    l_bad = matched & m["l_bad"].fillna(False).astype(bool)

    # --- Data issues: the row cannot be evaluated reliably as it stands. ----------
    bad_amount = (m["claimed_c"].isna() | (m["claimed_c"] <= 0)).fillna(True).astype(bool)
    dup_id = m["claim_id"].duplicated(keep=False) | (m["claim_id"] == "")
    early = (matched & ~l_bad & (m["claim_dt"] < m["l_inv_dt"])).fillna(False).astype(bool)
    issue_text = _join_reasons([
        (dup_id, "claim_id '" + m["claim_id"] + "' is not unique (possible resubmission)"),
        (bad_amount, "claimed_amount '" + m["claimed_amount"] + "' is not a valid positive amount"),
        (m["claim_dt"].isna(), "claim_date '" + m["claim_date"] + "' missing or invalid"),
        (l_bad, "ledger invoice " + m["l_invoice_number"].fillna("").astype(str) + ": "
                + m["l_bad_reason"].fillna("").astype(str)),
        (early, "claim dated " + m["claim_date"] + " is before the invoice date "
                + m["l_invoice_date"].fillna("").astype(str) + "; whether this is an error depends on business rules "
                "(e.g. claims raised against an order before it is invoiced)"),
    ], m.index)
    data_issue = issue_text != ""
    m["issue_text"] = issue_text
    m["bad_amount"] = bad_amount

    # --- Amounts: several claims on the same invoice are tested TOGETHER. --------
    valid = matched & ~data_issue
    claimed = m["claimed_c"].where(~bad_amount, 0).fillna(0).astype("int64")
    grp = claimed[valid].groupby(m.loc[valid, "lid"])
    m["group_total_c"] = claimed
    m["group_size"] = 1
    m.loc[valid, "group_total_c"] = grp.transform("sum")
    m.loc[valid, "group_size"] = grp.transform("size")

    diff = m["group_total_c"] - m["l_ref_c"].astype("Int64")
    over = (diff > TOLERANCE_CENTS).fillna(False).to_numpy(bool)
    under = (diff < -TOLERANCE_CENTS).fillna(False).to_numpy(bool)
    amount_flag = pd.Series(np.select([over, under], [AMOUNT_OVERSTATED, AMOUNT_MISMATCH], CLEAN_MATCH),
                            index=m.index)
    in_period = m["l_inv_dt"].between(AUDIT_START, AUDIT_END).fillna(False).astype(bool)
    partial = (matched & m["l_partial"].fillna(False)).astype(bool)

    # Precedence: data issue > no match > out of period > partial payment > amount.
    base = pd.Series(np.select(
        [~matched, ~in_period, partial], [m["unmatched_flag"], OUT_OF_PERIOD, PARTIAL_PAYMENT], amount_flag
    ), index=m.index)
    m["status_flag"] = base.where(~data_issue, DATA_ISSUE)

    # Secondary flag keeps the next problem in line, so nothing is lost.
    evaluable = ~bad_amount & ~l_bad
    m["secondary_flag"] = np.select(
        [data_issue & evaluable & (base != CLEAN_MATCH),
         ~data_issue & matched & (~in_period | partial) & (amount_flag != CLEAN_MATCH),
         ~data_issue & matched & ~in_period & partial],
        [base, amount_flag, PARTIAL_PAYMENT], "",
    )

    # --- Money split: concede + dispute + review == claimed, for every row. -----
    cap = m["l_cap_c"].fillna(0).astype("int64")
    st, single = m["status_flag"], m["group_size"] == 1
    to_review = st.isin(REVIEW_FLAGS) | (st.eq(AMOUNT_OVERSTATED) & ~single)
    concede = np.where(st.eq(CLEAN_MATCH) | (st.eq(AMOUNT_OVERSTATED) & single), np.minimum(claimed, cap), 0)
    m["claimed_c"] = claimed
    m["concede_c"] = concede
    m["review_c"] = np.where(to_review, claimed, 0)
    m["dispute_c"] = claimed - m["concede_c"] - m["review_c"]
    m["recommendation"] = np.select(
        [to_review, st.eq(CLEAN_MATCH), m["concede_c"] > 0], [REVIEW, CONCEDE, CONCEDE_PARTIAL], DISPUTE
    )

    m["matched_invoice"] = m["l_invoice_number"].fillna("")
    note_cols = ["invoice_ref", "po_ref", "claim_date", "inv_key", "ocr_key", "po_key", "method",
                 "status_flag", "n_inv", "n_po", "candidates", "conflict_inv", "conflict_po", "lid",
                 "claimed_c", "group_size", "group_total_c", "issue_text", "bad_amount", "l_bad",
                 "l_invoice_number", "l_invoice_date", "l_po_key", "l_po_number",
                 "l_ref_c", "l_invoice_c", "l_paid_c", "l_partial"]
    m["note"] = [_note(r) for r in m[note_cols].itertuples(index=False)]
    return m


def _note(r) -> str:
    """Human-readable explanation. Pure formatting, no decisions are made here."""
    parts: list[str] = []
    ref = r.invoice_ref

    if r.issue_text:
        parts.append(f"{r.issue_text}; sent to human review")
    if r.method == "invoice" and r.status_flag != AMBIGUOUS_NO_MATCH and pd.notna(r.lid) \
            and ref != r.l_invoice_number:
        parts.append(f"invoice_ref '{ref}' matched ledger '{r.l_invoice_number}' after normalisation")
    if r.status_flag == REFERENCE_CONFLICT:
        parts.append(
            f"invoice_ref '{ref}' is ledger invoice {r.conflict_inv}, which belongs to {r.conflict_po}, "
            f"but the claim cites {r.po_ref}; conflicting references, sent to human review"
        )
    if r.method == "invoice_ocr":
        parts.append(
            f"invoice_ref '{ref}' corrected to '{r.l_invoice_number}' (OCR letters read as digits), "
            f"confirmed by {r.po_ref}"
        )
    if r.method in ("po", "none") or (r.method == "invoice" and pd.isna(r.lid)
                                      and r.status_flag != REFERENCE_CONFLICT):
        if ref == "":
            parts.append("no invoice_ref on claim")
        elif pd.isna(r.inv_key):
            fix = "" if pd.isna(r.ocr_key) else " (OCR reading not confirmed by PO)"
            parts.append(f"invoice_ref '{ref}' is malformed{fix}")
        elif r.n_inv > 1:
            parts.append(f"invoice_ref '{ref}' maps to {r.n_inv} ledger invoices [{r.candidates}]")
        else:
            parts.append(f"invoice_ref '{ref}' not found in ledger")

    if r.method == "po":
        if r.n_po > 1:
            parts.append(f"{r.po_ref} is on {r.n_po} ledger invoices [{r.candidates}]; not resolved by amount")
        else:
            via = "" if r.po_ref == r.l_po_number else f" (ledger '{r.l_po_number}')"
            parts.append(f"matched via unique {r.po_ref}{via}")
    if r.method == "none":
        parts.append(f"{r.po_ref or 'no PO'} not found in ledger")

    if pd.notna(r.lid) and not r.l_bad and not r.bad_amount:
        if r.status_flag == OUT_OF_PERIOD:
            parts.append(f"invoice dated {r.l_invoice_date} is outside the 2025 audit window "
                         f"(claim filed {r.claim_date})")
        if r.l_partial:
            parts.append(f"invoice {_money(r.l_invoice_c)} but only {_money(r.l_paid_c)} paid; "
                         f"part of the deduction may already have been taken")
        txt = f"claimed {_money(r.claimed_c)} vs invoice {_money(r.l_ref_c)}"
        if r.group_size > 1:
            txt += f"; {r.group_size} claims on this invoice total {_money(r.group_total_c)}"
        d = int(r.group_total_c) - int(r.l_ref_c)
        if d == 0:
            txt += "; exact"
        elif abs(d) <= TOLERANCE_CENTS:
            txt += f"; {'+' if d > 0 else '-'}{_money(abs(d))} within $1.00 tolerance"
        elif d > 0:
            txt += f"; over by {_money(d)}"
        else:
            txt += f"; under by {_money(-d)}"
        if pd.isna(r.l_paid_c):
            txt += "; invoice unpaid"
        elif r.l_paid_c != r.l_invoice_c and not r.l_partial:
            txt += f"; paid {_money(r.l_paid_c)}, so at most {_money(r.l_paid_c)} can be conceded"
        parts.append(txt)

    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
FLAG_ORDER = [CLEAN_MATCH, AMOUNT_OVERSTATED, AMOUNT_MISMATCH, PARTIAL_PAYMENT, OUT_OF_PERIOD,
              REFERENCE_CONFLICT, AMBIGUOUS_NO_MATCH, PHANTOM, DATA_ISSUE]


def summarise(reg: pd.DataFrame) -> pd.DataFrame:
    out = (
        reg.groupby("status_flag")
        .agg(claims=("claim_id", "size"), claimed=("claimed_c", "sum"),
             concede=("concede_c", "sum"), dispute=("dispute_c", "sum"), review=("review_c", "sum"))
        .reindex([f for f in FLAG_ORDER if f in set(reg["status_flag"])])
    )
    out.loc["TOTAL"] = out.sum()
    return out.astype("int64")


def main(argv: list[str]) -> None:
    here = Path(__file__).parent
    claims_path = Path(argv[1]) if len(argv) > 1 else here / "data" / "fernhill_claims.csv"
    ledger_path = Path(argv[2]) if len(argv) > 2 else here / "data" / "northgate_ledger.csv"
    out_dir = Path(argv[3]) if len(argv) > 3 else here / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    reg = reconcile(*load(claims_path, ledger_path))

    reg[["claim_id", "matched_invoice", "status_flag", "note"]].to_csv(
        out_dir / "reconciliation_register.csv", index=False
    )
    detail = reg[["claim_id", "matched_invoice", "status_flag", "secondary_flag", "method",
                  "recommendation", "claimed_c", "concede_c", "dispute_c", "review_c", "note"]].copy()
    for col in ["claimed_c", "concede_c", "dispute_c", "review_c"]:
        detail[col.replace("_c", "_amount")] = detail.pop(col).map(lambda v: f"{v / 100:.2f}")
    detail.rename(columns={"method": "match_method"}).to_csv(
        out_dir / "reconciliation_register_detailed.csv", index=False
    )

    summary = summarise(reg)
    (summary.assign(**{c: summary[c] / 100 for c in ["claimed", "concede", "dispute", "review"]})
            .to_csv(out_dir / "summary_by_flag.csv", float_format="%.2f"))

    pd.set_option("display.max_colwidth", None, "display.width", 250)
    print(reg[["claim_id", "matched_invoice", "status_flag", "secondary_flag", "method",
               "recommendation", "note"]].to_string(index=False))
    print()
    print(summary.assign(**{c: summary[c].map(_money) for c in ["claimed", "concede", "dispute", "review"]})
                 .to_string())


if __name__ == "__main__":
    main(sys.argv)
