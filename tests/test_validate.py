"""The core engineering question: is a confidently misread digit accepted as truth?"""

from docflow.extract import Candidate
from docflow.validate import validate


def invoice(amount_row1="200.00", ocr_conf=0.97):
    def h(name, value):
        return Candidate(name=name, value=value, raw_text=value, ocr_conf=ocr_conf, page=1, bbox=[0, 0, 1, 1])

    def cell(row, name, value):
        return Candidate(name=name, kind="line", row_index=row, value=value, raw_text=value,
                         ocr_conf=ocr_conf, page=1, bbox=[0, 0, 1, 1])

    return [
        h("supplier_name", "Anadolu Kırtasiye"),
        h("supplier_tax_id", "1043321815"),
        h("invoice_number", "EFA2026000000123"),
        h("invoice_date", "2026-06-01"),
        h("due_date", "2026-07-01"),
        Candidate(name="po_number", issues=["not_found"]),
        h("currency", "TRY"),
        h("subtotal", "500.00"),
        h("tax_rate", "20"),
        h("tax_amount", "100.00"),
        h("total", "600.00"),
        cell(0, "description", "Kağıt"), cell(0, "quantity", "2"), cell(0, "unit_price", "100.00"),
        cell(0, "amount", amount_row1),
        cell(1, "description", "Toner"), cell(1, "quantity", "1"), cell(1, "unit_price", "300.00"),
        cell(1, "amount", "300.00"),
    ]


def by_key(fields):
    return {(f.kind, f.row_index, f.name): f for f in fields}


def test_consistent_invoice_is_auto_accepted_and_verified():
    fields = by_key(validate(invoice(), review_threshold=0.9))
    assert all(f.status == "auto_accepted" for f in fields.values())
    for key in [("header", None, "subtotal"), ("header", None, "total"), ("line", 0, "amount")]:
        assert fields[key].verified


def test_confident_wrong_digit_is_not_silently_accepted():
    # OCR is 97% sure, but read 200.00 as 280.00. Arithmetic exposes it.
    fields = by_key(validate(invoice(amount_row1="280.00", ocr_conf=0.97), review_threshold=0.9))
    wrong = fields[("line", 0, "amount")]
    assert wrong.status == "needs_review"
    assert "row_arithmetic_mismatch" in wrong.issues
    assert wrong.suggested_value == "200.00"  # proposed, never applied automatically
    assert wrong.value == "280.00"
    assert fields[("header", None, "subtotal")].status == "needs_review"


def test_low_confidence_value_is_accepted_only_when_independently_verified():
    fields = by_key(validate(invoice(ocr_conf=0.6), review_threshold=0.9))
    assert fields[("header", None, "total")].status == "auto_accepted"  # verified by arithmetic
    assert fields[("header", None, "total")].verified
    assert fields[("header", None, "supplier_name")].status == "needs_review"  # nothing can verify a name
    assert "low_confidence" in fields[("header", None, "supplier_name")].issues


def test_misread_total_gets_a_suggestion_from_verified_parts():
    cands = invoice()
    next(c for c in cands if c.name == "total").value = "608.00"
    fields = by_key(validate(cands, review_threshold=0.9))
    total = fields[("header", None, "total")]
    assert total.status == "needs_review"
    assert total.suggested_value == "600.00"
    assert fields[("header", None, "tax_amount")].status == "auto_accepted"


def test_missing_required_field_goes_to_review_but_missing_optional_does_not():
    cands = [c for c in invoice() if c.name != "invoice_number"]
    cands.append(Candidate(name="invoice_number", issues=["not_found"]))
    fields = by_key(validate(cands, review_threshold=0.9))
    assert fields[("header", None, "invoice_number")].status == "needs_review"
    assert fields[("header", None, "po_number")].status == "auto_accepted"
