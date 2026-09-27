"""Decide which extracted values can be trusted.

OCR confidence alone is not enough: Tesseract can be 95% sure of a wrong
digit. So numbers are cross-checked against each other (qty x price = amount,
sum of lines = subtotal, subtotal x rate = tax, subtotal + tax = total) and
identifiers against their checksums. A value that passes an independent check
is marked ``verified``; a value that fails one, or that is uncertain and
unverified, is sent to human review. Nothing is silently "fixed": when the
arithmetic implies a different value it is stored as ``suggested_value`` and a
person decides.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .extract import Candidate

# Only these may legitimately be absent from a purchase document.
OPTIONAL_FIELDS = {"due_date", "po_number"}

# Issues that always require a human, regardless of confidence or verification.
HARD_ISSUES = {
    "not_found",
    "value_unreadable",
    "not_a_number",
    "not_a_date",
    "not_a_tax_id",
    "not_a_code",
    "invalid_date",
    "malformed_number",
    "checksum_failed",
    "row_arithmetic_mismatch",
    "lines_do_not_sum_to_subtotal",
    "tax_mismatch",
    "total_mismatch",
    "due_before_invoice_date",
    "date_in_future",
    "negative_value",
    "unexpected_character",
    "conflicting_currency",
}
# Issues produced by cross-checks (recomputed on every validation run).
CROSS_CHECK_ISSUES = {
    "row_arithmetic_mismatch",
    "lines_do_not_sum_to_subtotal",
    "tax_mismatch",
    "total_mismatch",
    "due_before_invoice_date",
    "date_in_future",
    "low_confidence",
}
# Issues that an independent verification can outweigh.
SOFT_ISSUES = {"ocr_char_substitution", "low_confidence", "ambiguous_characters"}
# Informational only.
INFO_ISSUES = {"learned_from_correction"}

CENT = Decimal("0.01")
PARSE_PENALTY = {"ocr_char_substitution": 0.6, "malformed_number": 0.5}


@dataclass
class ValidatedField:
    name: str
    kind: str
    row_index: int | None
    value: str | None
    raw_text: str | None
    confidence: float
    ocr_confidence: float
    verified: bool
    status: str
    issues: list[str]
    suggested_value: str | None
    page: int | None
    bbox: list[float] | None


def _dec(c: Candidate | None) -> Decimal | None:
    if c is None or c.value is None:
        return None
    try:
        return Decimal(c.value)
    except Exception:
        return None


def _close(a: Decimal, b: Decimal, tol: Decimal = Decimal("0.011")) -> bool:
    return abs(a - b) <= tol


def _add_issue(c: Candidate, issue: str) -> None:
    if issue not in c.issues:
        c.issues.append(issue)


def validate(candidates: list[Candidate], review_threshold: float, today: dt.date | None = None) -> list[ValidatedField]:
    today = today or dt.date.today()
    header = {c.name: c for c in candidates if c.kind == "header"}
    rows: dict[int, dict[str, Candidate]] = {}
    for c in candidates:
        if c.kind == "line":
            rows.setdefault(c.row_index, {})[c.name] = c

    verified: set[int] = set()
    suggestions: dict[int, str] = {}

    def verify(*cands: Candidate | None) -> None:
        for c in cands:
            if c is not None and c.value is not None:
                verified.add(id(c))

    # 1. Row arithmetic: quantity x unit price = amount.
    row_ok: dict[int, bool] = {}
    for idx, row in rows.items():
        q, p, a = _dec(row.get("quantity")), _dec(row.get("unit_price")), _dec(row.get("amount"))
        if q is None or p is None or a is None:
            row_ok[idx] = False
            continue
        if _close((q * p).quantize(CENT, ROUND_HALF_UP), a):
            row_ok[idx] = True
            verify(row["quantity"], row["unit_price"], row["amount"])
        else:
            row_ok[idx] = False
            for col in ("quantity", "unit_price", "amount"):
                _add_issue(row[col], "row_arithmetic_mismatch")

    # 2. Sum of line amounts = subtotal.
    subtotal, tax, total, rate = (header.get(n) for n in ("subtotal", "tax_amount", "total", "tax_rate"))
    amounts = [_dec(r.get("amount")) for r in rows.values()]
    lines_sum = sum(amounts, Decimal(0)) if rows and all(a is not None for a in amounts) else None
    sub_ok = False
    if lines_sum is not None and _dec(subtotal) is not None:
        if _close(lines_sum, _dec(subtotal), Decimal("0.02")):
            sub_ok = True
            verify(subtotal, *(r.get("amount") for r in rows.values()))
        else:
            _add_issue(subtotal, "lines_do_not_sum_to_subtotal")
            for idx, r in rows.items():
                if not row_ok[idx]:
                    _add_issue(r["amount"], "lines_do_not_sum_to_subtotal")
            if all(row_ok.values()):
                suggestions[id(subtotal)] = f"{lines_sum:.2f}"

    # Rows whose amount was confirmed by the subtotal but whose qty x price failed:
    # the amount is right, so point at quantity / unit price instead.
    for idx, row in rows.items():
        if row_ok[idx] or not sub_ok:
            continue
        q, a = _dec(row.get("quantity")), _dec(row.get("amount"))
        row["amount"].issues = [i for i in row["amount"].issues if i != "row_arithmetic_mismatch"]
        if q and a is not None:
            implied = (a / q).quantize(CENT, ROUND_HALF_UP)
            if _close(implied * q, a):
                suggestions[id(row["unit_price"])] = f"{implied:.2f}"
    for idx, row in rows.items():
        if not row_ok[idx] and not sub_ok:
            q, p = _dec(row.get("quantity")), _dec(row.get("unit_price"))
            if q is not None and p is not None and row.get("amount") is not None:
                suggestions[id(row["amount"])] = f"{(q * p).quantize(CENT, ROUND_HALF_UP):.2f}"

    # 3. Subtotal x rate = tax.
    s, t, tot, r = _dec(subtotal), _dec(tax), _dec(total), _dec(rate)
    if s is not None and t is not None and r is not None:
        expected_tax = (s * r / 100).quantize(CENT, ROUND_HALF_UP)
        if _close(expected_tax, t, Decimal("0.02")):
            verify(tax, rate)
            if sub_ok:
                verify(subtotal)
        else:
            _add_issue(tax, "tax_mismatch")
            if sub_ok:
                suggestions[id(tax)] = f"{expected_tax:.2f}"

    # 4. Subtotal + tax = total.
    if s is not None and t is not None and tot is not None:
        if _close(s + t, tot, Decimal("0.02")):
            verify(total, subtotal, tax)
        else:
            _add_issue(total, "total_mismatch")
            if id(subtotal) in verified and id(tax) in verified:
                suggestions[id(total)] = f"{s + t:.2f}"
                # The mismatch is the total's fault, not the verified inputs'.
            else:
                _add_issue(subtotal, "total_mismatch")
                _add_issue(tax, "total_mismatch")
    # A tax mismatch explained by a verified total (subtotal + tax = total) is not the tax's fault.
    if tax is not None and "tax_mismatch" in tax.issues and id(tax) in verified and id(total) in verified:
        tax.issues.remove("tax_mismatch")
        suggestions.pop(id(tax), None)
        if rate is not None and s is not None and t is not None and s:
            _add_issue(rate, "tax_mismatch")
            suggestions[id(rate)] = f"{(t / s * 100).quantize(Decimal('1'))}"
    if total is not None and "total_mismatch" in total.issues and id(total) in verified:
        total.issues.remove("total_mismatch")

    # 5. Dates.
    inv_date = header.get("invoice_date")
    due = header.get("due_date")
    if inv_date and inv_date.value and due and due.value and due.value < inv_date.value:
        _add_issue(due, "due_before_invoice_date")
    if inv_date and inv_date.value and dt.date.fromisoformat(inv_date.value) > today + dt.timedelta(days=1):
        _add_issue(inv_date, "date_in_future")

    # 6. Tax id checksum passing is itself an independent verification.
    vkn = header.get("supplier_tax_id")
    if vkn and vkn.value and "checksum_failed" not in vkn.issues and len(vkn.value) == 10:
        verify(vkn)

    out: list[ValidatedField] = []
    for c in candidates:
        is_verified = id(c) in verified
        issues = list(c.issues)
        if c.value is None and c.kind == "header" and c.name in OPTIONAL_FIELDS and issues == ["not_found"]:
            # Optional field genuinely absent from many documents: record, don't block.
            out.append(_field(c, 0.0, False, "auto_accepted", [], None))
            continue
        confidence = c.ocr_conf
        for issue in issues:
            confidence *= PARSE_PENALTY.get(issue, 1.0)
        if c.value is None:
            confidence = 0.0
        if is_verified:
            confidence = max(confidence, 0.99)
        elif confidence < review_threshold and c.value is not None:
            issues.append("low_confidence")

        blocking = [i for i in issues if i in HARD_ISSUES or (i in SOFT_ISSUES and not is_verified)]
        status = "needs_review" if blocking else "auto_accepted"
        out.append(_field(c, round(confidence, 4), is_verified, status, issues, suggestions.get(id(c))))
    return out


def _field(c: Candidate, confidence: float, verified: bool, status: str, issues: list[str], suggestion: str | None) -> ValidatedField:
    return ValidatedField(
        name=c.name,
        kind=c.kind,
        row_index=c.row_index,
        value=c.value,
        raw_text=c.raw_text,
        confidence=confidence,
        ocr_confidence=c.ocr_conf,
        verified=verified,
        status=status,
        issues=issues,
        suggested_value=suggestion if suggestion != c.value else None,
        page=c.page,
        bbox=c.bbox,
    )
