"""Human review: corrections, confirmations, row edits and re-validation.

Every human action is appended to ``corrections`` (an audit log that is never
rewritten). After each edit the document is re-validated: a corrected line
amount can, for example, make the subtotal check pass, which in turn clears
the review flag on the subtotal. Values a person entered or confirmed are
treated as trusted inputs to those checks.
"""

from __future__ import annotations

import json
import sqlite3

from . import db
from .config import settings
from .extract import FIELD_TYPES, LINE_COLUMNS, PARSERS, Candidate
from .pipeline import LEARNABLE_TYPES
from .validate import CROSS_CHECK_ISSUES, validate

HUMAN_STATUSES = {"corrected", "confirmed"}


class ReviewError(ValueError):
    pass


def normalise_input(name: str, value: str | None) -> str | None:
    """Parse what the user typed with the same parser the extractor uses."""
    if value is None or str(value).strip() == "":
        return None
    ftype = FIELD_TYPES.get(name, "text")
    if ftype == "percent":
        cleaned = str(value).strip().strip("%").replace(",", ".")
        try:
            return str(int(float(cleaned))) if float(cleaned) == int(float(cleaned)) else cleaned
        except ValueError as exc:
            raise ReviewError(f"'{value}' geçerli bir oran değil") from exc
    parsed, issues = PARSERS[ftype](str(value))
    if parsed is None:
        raise ReviewError(f"'{value}' bu alan için geçerli değil ({', '.join(issues)})")
    if ftype == "tax_id" and "checksum_failed" in issues:
        raise ReviewError("Vergi numarasının kontrol hanesi tutmuyor")
    return parsed


def correct_field(conn: sqlite3.Connection, user_id: str, doc_id: str, field_id: str, value: str | None) -> None:
    field = _get_field(conn, doc_id, field_id)
    new_value = normalise_input(field["name"], value)
    with db.transaction(conn):
        conn.execute(
            "UPDATE fields SET value = ?, status = 'corrected', suggested_value = NULL, updated_at = ? WHERE id = ?",
            (new_value, db.now(), field_id),
        )
        _log(conn, doc_id, field, user_id, "correct", field["value"], new_value)
        if (
            field["kind"] == "header"
            and FIELD_TYPES.get(field["name"]) in LEARNABLE_TYPES
            and field["raw_text"]
            and new_value is not None
            and new_value != field["extracted_value"]
        ):
            conn.execute(
                "INSERT INTO correction_rules (user_id, field_name, raw_value, value, hits, updated_at)"
                " VALUES (?, ?, ?, ?, 0, ?)"
                " ON CONFLICT (user_id, field_name, raw_value) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (user_id, field["name"], field["raw_text"], new_value, db.now()),
            )
    revalidate(conn, doc_id)


def confirm_field(conn: sqlite3.Connection, user_id: str, doc_id: str, field_id: str) -> None:
    field = _get_field(conn, doc_id, field_id)
    with db.transaction(conn):
        conn.execute("UPDATE fields SET status = 'confirmed', updated_at = ? WHERE id = ?", (db.now(), field_id))
        _log(conn, doc_id, field, user_id, "confirm", field["value"], field["value"])
    revalidate(conn, doc_id)


def add_row(conn: sqlite3.Connection, user_id: str, doc_id: str, values: dict) -> int:
    normalised = {col: normalise_input(col, values.get(col)) for col in LINE_COLUMNS}
    with db.transaction(conn):
        row = conn.execute(
            "SELECT COALESCE(MAX(row_index), -1) + 1 FROM fields WHERE document_id = ? AND kind = 'line'", (doc_id,)
        ).fetchone()
        row_index = row[0]
        for col in LINE_COLUMNS:
            fid = db.new_id("fld")
            conn.execute(
                "INSERT INTO fields (id, document_id, kind, row_index, name, value, confidence, verified, status,"
                " initial_status, issues, updated_at) VALUES (?, ?, 'line', ?, ?, ?, 1, 0, 'corrected', 'added', '[]', ?)",
                (fid, doc_id, row_index, col, normalised[col], db.now()),
            )
            _log(conn, doc_id, {"id": fid, "name": col, "raw_text": None, "confidence": None}, user_id,
                 "add_row", None, normalised[col])
    revalidate(conn, doc_id)
    return row_index


def delete_row(conn: sqlite3.Connection, user_id: str, doc_id: str, row_index: int) -> None:
    with db.transaction(conn):
        cells = conn.execute(
            "SELECT * FROM fields WHERE document_id = ? AND kind = 'line' AND row_index = ? AND deleted = 0",
            (doc_id, row_index),
        ).fetchall()
        if not cells:
            raise ReviewError("Satır bulunamadı")
        conn.execute(
            "UPDATE fields SET deleted = 1, updated_at = ? WHERE document_id = ? AND kind = 'line' AND row_index = ?",
            (db.now(), doc_id, row_index),
        )
        for cell in cells:
            _log(conn, doc_id, cell, user_id, "delete_row", cell["value"], None)
    revalidate(conn, doc_id)


def approve(conn: sqlite3.Connection, doc_id: str) -> None:
    pending = conn.execute(
        "SELECT COUNT(*) FROM fields WHERE document_id = ? AND deleted = 0 AND status = 'needs_review'", (doc_id,)
    ).fetchone()[0]
    if pending:
        raise ReviewError(f"{pending} alan hâlâ kontrol bekliyor")
    conn.execute("UPDATE documents SET status = 'approved', approved_at = ? WHERE id = ?", (db.now(), doc_id))


def revalidate(conn: sqlite3.Connection, doc_id: str) -> None:
    rows = conn.execute(
        "SELECT * FROM fields WHERE document_id = ? AND deleted = 0 ORDER BY kind, row_index", (doc_id,)
    ).fetchall()
    candidates: list[tuple[sqlite3.Row, Candidate]] = []
    for r in rows:
        human = r["status"] in HUMAN_STATUSES
        parse_issues = [] if human else [i for i in json.loads(r["issues"]) if i not in CROSS_CHECK_ISSUES]
        candidates.append(
            (
                r,
                Candidate(
                    name=r["name"],
                    kind=r["kind"],
                    row_index=r["row_index"],
                    value=r["value"],
                    raw_text=r["raw_text"],
                    ocr_conf=1.0 if human or r["ocr_confidence"] is None else r["ocr_confidence"],
                    page=r["page_no"],
                    bbox=json.loads(r["bbox"]) if r["bbox"] else None,
                    issues=parse_issues,
                ),
            )
        )
    results = validate([c for _, c in candidates], settings.review_threshold)
    with db.transaction(conn):
        for (row, _), res in zip(candidates, results):
            human = row["status"] in HUMAN_STATUSES
            status = row["status"] if human else res.status
            conn.execute(
                "UPDATE fields SET status = ?, issues = ?, verified = ?, suggested_value = ?, confidence = ?"
                " WHERE id = ?",
                (
                    status,
                    json.dumps(res.issues),
                    int(res.verified),
                    None if human else res.suggested_value,
                    row["confidence"] if human else res.confidence,
                    row["id"],
                ),
            )
        doc = conn.execute("SELECT status FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] in ("needs_review", "ready", "approved"):
            pending = any(r.status == "needs_review" and row["status"] not in HUMAN_STATUSES
                          for (row, _), r in zip(candidates, results))
            new_status = "needs_review" if pending else ("approved" if doc["status"] == "approved" else "ready")
            conn.execute("UPDATE documents SET status = ? WHERE id = ?", (new_status, doc_id))


def _get_field(conn: sqlite3.Connection, doc_id: str, field_id: str) -> sqlite3.Row:
    field = conn.execute(
        "SELECT * FROM fields WHERE id = ? AND document_id = ? AND deleted = 0", (field_id, doc_id)
    ).fetchone()
    if field is None:
        raise ReviewError("Alan bulunamadı")
    return field


def _log(conn, doc_id: str, field, user_id: str, action: str, old, new) -> None:
    conn.execute(
        "INSERT INTO corrections (id, document_id, field_id, user_id, field_name, action, old_value, new_value,"
        " raw_text, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            db.new_id("cor"), doc_id, field["id"], user_id, field["name"], action, old, new,
            field["raw_text"], field["confidence"], db.now(),
        ),
    )
