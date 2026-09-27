"""CSV and Excel export of a reviewed document."""

from __future__ import annotations

import csv
import io
import sqlite3

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .extract import FIELD_LABELS_TR, HEADER_FIELDS, LINE_COLUMNS

STATUS_TR = {
    "auto_accepted": "Otomatik kabul",
    "needs_review": "Kontrol gerekli",
    "corrected": "Düzeltildi",
    "confirmed": "Onaylandı",
}
REVIEW_FILL = PatternFill("solid", fgColor="FFF2CC")
CORRECTED_FILL = PatternFill("solid", fgColor="DDEBF7")


def load(conn: sqlite3.Connection, doc_id: str) -> tuple[dict, list[dict]]:
    fields = conn.execute(
        "SELECT * FROM fields WHERE document_id = ? AND deleted = 0 ORDER BY kind, row_index", (doc_id,)
    ).fetchall()
    header = {f["name"]: dict(f) for f in fields if f["kind"] == "header"}
    rows: dict[int, dict] = {}
    for f in fields:
        if f["kind"] == "line":
            rows.setdefault(f["row_index"], {})[f["name"]] = dict(f)
    return header, [rows[k] for k in sorted(rows)]


def to_csv(conn: sqlite3.Connection, doc_id: str, delimiter: str = ",") -> bytes:
    """One row per line item with the document header repeated (import-friendly)."""
    header, rows = load(conn, doc_id)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter)
    writer.writerow(HEADER_FIELDS + LINE_COLUMNS + ["needs_review"])
    head_values = [(header.get(n) or {}).get("value") or "" for n in HEADER_FIELDS]
    head_pending = any((header.get(n) or {}).get("status") == "needs_review" for n in HEADER_FIELDS)
    for row in rows or [{}]:
        line_values = [(row.get(c) or {}).get("value") or "" for c in LINE_COLUMNS]
        pending = head_pending or any((row.get(c) or {}).get("status") == "needs_review" for c in LINE_COLUMNS)
        writer.writerow(head_values + line_values + ["yes" if pending else "no"])
    # UTF-8 BOM so Excel opens Turkish characters correctly.
    return ("﻿" + out.getvalue()).encode("utf-8")


def _num(value: str | None, name: str):
    if value is None:
        return None
    if name in ("subtotal", "tax_amount", "total", "unit_price", "amount", "quantity", "tax_rate"):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def to_xlsx(conn: sqlite3.Connection, doc_id: str) -> bytes:
    header, rows = load(conn, doc_id)
    wb = Workbook()
    bold = Font(bold=True)

    ws = wb.active
    ws.title = "Belge"
    ws.append(["Alan", "Değer", "Durum", "Güven", "Belgede okunan", "Sayfa"])
    for name in HEADER_FIELDS:
        f = header.get(name) or {}
        ws.append([
            FIELD_LABELS_TR[name], _num(f.get("value"), name), STATUS_TR.get(f.get("status"), ""),
            f.get("confidence"), f.get("raw_text"), f.get("page_no"),
        ])
        _style(ws, ws.max_row, f.get("status"), cols=range(1, 7))

    ws2 = wb.create_sheet("Kalemler")
    ws2.append([FIELD_LABELS_TR[c] for c in LINE_COLUMNS])
    for row in rows:
        ws2.append([_num((row.get(c) or {}).get("value"), c) for c in LINE_COLUMNS])
        for ci, c in enumerate(LINE_COLUMNS, start=1):
            _style(ws2, ws2.max_row, (row.get(c) or {}).get("status"), cols=[ci])

    ws3 = wb.create_sheet("Düzeltmeler")
    ws3.append(["Zaman", "Alan", "İşlem", "Önceki", "Yeni", "OCR okuması"])
    for c in conn.execute(
        "SELECT datetime(created_at, 'unixepoch') AS ts, field_name, action, old_value, new_value, raw_text"
        " FROM corrections WHERE document_id = ? ORDER BY created_at",
        (doc_id,),
    ):
        ws3.append(list(c))

    for sheet in (ws, ws2, ws3):
        for cell in sheet[1]:
            cell.font = bold
        for i, col in enumerate(sheet.columns, start=1):
            width = max(len(str(c.value or "")) for c in col)
            sheet.column_dimensions[get_column_letter(i)].width = min(max(12, width + 2), 60)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _style(ws, row: int, status: str | None, cols) -> None:
    fill = REVIEW_FILL if status == "needs_review" else CORRECTED_FILL if status == "corrected" else None
    if fill:
        for c in cols:
            ws.cell(row=row, column=c).fill = fill
