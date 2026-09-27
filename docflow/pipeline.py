"""Checkpointed document processing pipeline.

A job moves through ``rasterize -> ocr -> extract -> done``. The name of the
*next* stage is stored on the job, and every stage writes its output and
advances that pointer in the same transaction. OCR, the slow part, is
checkpointed per page. If the worker dies at any point, the job's lease
expires, another worker claims it and continues from the last committed
checkpoint: finished pages are not OCR'd again, and no half-written results
are ever visible.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Callable

from . import db
from .config import Settings
from .extract import FIELD_TYPES, Candidate, extract
from .ocr import available_langs, deskew_page, ocr_image, rasterize
from .validate import validate

STAGES = ["rasterize", "ocr", "extract", "done"]
LEARNABLE_TYPES = {"text", "code", "tax_id", "currency"}

Heartbeat = Callable[[], None]
FaultInjector = Callable[[str, int | None], None]


class LeaseLost(Exception):
    """Another worker took over this job; stop without writing anything."""


def _noop_fault(stage: str, page: int | None) -> None:  # pragma: no cover - trivial
    return None


def run_job(
    conn: sqlite3.Connection,
    job_id: str,
    worker_id: str,
    settings: Settings,
    heartbeat: Heartbeat,
    fault: FaultInjector = _noop_fault,
) -> None:
    while True:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job["lease_owner"] != worker_id:
            raise LeaseLost(job_id)
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (job["document_id"],)).fetchone()
        stage = job["stage"]
        if stage == "done":
            return
        started = time.perf_counter()
        if stage == "rasterize":
            _stage_rasterize(conn, job, doc, settings, fault, worker_id)
        elif stage == "ocr":
            _stage_ocr(conn, job, doc, settings, heartbeat, fault, worker_id)
        elif stage == "extract":
            _stage_extract(conn, job, doc, settings, fault, worker_id)
        else:  # pragma: no cover - corrupt row
            raise ValueError(f"unknown stage {stage!r}")
        _record_metric(conn, job_id, stage, (time.perf_counter() - started) * 1000)
        if stage == "extract":
            # Processing time = sum of stage work (queue wait excluded).
            metrics = json.loads(conn.execute("SELECT metrics FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])
            conn.execute(
                "UPDATE documents SET processing_ms = ? WHERE id = ?",
                (round(sum(metrics.values())), doc["id"]),
            )
            return  # the extract stage finished the job and released the lease
        heartbeat()


def _assert_owner(conn: sqlite3.Connection, job_id: str, worker_id: str) -> None:
    """Called inside each stage's write transaction: a worker whose lease was taken
    over (it stalled past the lease) must not write anything."""
    owner = conn.execute("SELECT lease_owner FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    if owner != worker_id:
        raise LeaseLost(job_id)


def _advance(conn: sqlite3.Connection, job_id: str, next_stage: str, progress: float, message: str) -> None:
    conn.execute(
        "UPDATE jobs SET stage = ?, progress = ?, message = ?, updated_at = ? WHERE id = ?",
        (next_stage, progress, message, db.now(), job_id),
    )


def _record_metric(conn: sqlite3.Connection, job_id: str, stage: str, ms: float) -> None:
    row = conn.execute("SELECT metrics FROM jobs WHERE id = ?", (job_id,)).fetchone()
    metrics = json.loads(row["metrics"])
    metrics[f"{stage}_ms"] = round(metrics.get(f"{stage}_ms", 0) + ms, 1)
    conn.execute("UPDATE jobs SET metrics = ? WHERE id = ?", (json.dumps(metrics), job_id))


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def _stage_rasterize(conn, job, doc, settings: Settings, fault: FaultInjector, worker_id: str) -> None:
    out_dir = settings.data_dir / "pages" / doc["id"]
    pages = rasterize(Path(doc["storage_path"]), doc["content_type"], out_dir, settings.render_dpi)
    fault("rasterize", None)
    source_kind = "pdf_text" if pages and all(p.text_words for p in pages) else "ocr"
    with db.transaction(conn):
        _assert_owner(conn, job["id"], worker_id)
        conn.execute("DELETE FROM pages WHERE document_id = ?", (doc["id"],))
        for p in pages:
            conn.execute(
                "INSERT INTO pages (document_id, page_no, image_path, width, height, ocr_words, ocr_engine, ocr_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc["id"], p.page_no, str(p.image_path), p.width, p.height,
                    json.dumps(p.text_words) if p.text_words else None,
                    "pdf_text" if p.text_words else None,
                    0 if p.text_words else None,
                ),
            )
        conn.execute(
            "UPDATE documents SET page_count = ?, source_kind = ? WHERE id = ?",
            (len(pages), source_kind, doc["id"]),
        )
        _advance(conn, job["id"], "ocr", 0.1, f"{len(pages)} sayfa hazırlandı")


def _stage_ocr(conn, job, doc, settings: Settings, heartbeat: Heartbeat, fault: FaultInjector, worker_id: str) -> None:
    lang = available_langs(settings.ocr_lang)
    pages = conn.execute(
        "SELECT page_no, image_path, ocr_words IS NOT NULL AS done FROM pages WHERE document_id = ? ORDER BY page_no",
        (doc["id"],),
    ).fetchall()
    total = len(pages) or 1
    for page in pages:
        if page["done"]:
            continue  # checkpoint: already recognised before a crash/restart
        started = time.perf_counter()
        deskew_page(Path(page["image_path"]))  # idempotent: a straight page measures ~0 degrees
        words = ocr_image(Path(page["image_path"]), page["page_no"], lang)
        ms = int((time.perf_counter() - started) * 1000)
        fault("ocr", page["page_no"])
        done = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE document_id = ? AND ocr_words IS NOT NULL", (doc["id"],)
        ).fetchone()[0] + 1
        with db.transaction(conn):
            _assert_owner(conn, job["id"], worker_id)
            conn.execute(
                "UPDATE pages SET ocr_words = ?, ocr_engine = ?, ocr_ms = ? WHERE document_id = ? AND page_no = ?",
                (json.dumps(words), f"tesseract:{lang}", ms, doc["id"], page["page_no"]),
            )
            conn.execute(
                "UPDATE jobs SET progress = ?, message = ?, updated_at = ? WHERE id = ?",
                (0.1 + 0.8 * done / total, f"OCR: sayfa {done}/{total}", db.now(), job["id"]),
            )
        heartbeat()
    with db.transaction(conn):
        _assert_owner(conn, job["id"], worker_id)
        _advance(conn, job["id"], "extract", 0.9, "Metin tanındı, alanlar çıkarılıyor")


def _stage_extract(conn, job, doc, settings: Settings, fault: FaultInjector, worker_id: str) -> None:
    words: list[dict] = []
    for row in conn.execute(
        "SELECT ocr_words FROM pages WHERE document_id = ? ORDER BY page_no", (doc["id"],)
    ):
        words.extend(json.loads(row["ocr_words"]))
    candidates = extract(words) if words else []
    _apply_learned_corrections(conn, doc["user_id"], candidates)
    fields = validate(candidates, settings.review_threshold)
    fault("extract", None)

    needs_review = any(f.status == "needs_review" for f in fields) or not fields
    header = {f.name: f.value for f in fields if f.kind == "header"}
    duplicate_of = _find_content_duplicate(conn, doc, header)
    ts = db.now()
    with db.transaction(conn):
        _assert_owner(conn, job["id"], worker_id)
        # Re-running extract is idempotent: replace machine output wholesale.
        conn.execute("DELETE FROM fields WHERE document_id = ?", (doc["id"],))
        for f in fields:
            conn.execute(
                "INSERT INTO fields (id, document_id, kind, row_index, name, value, raw_text, extracted_value,"
                " confidence, ocr_confidence, verified, status, initial_status, issues, suggested_value, page_no, bbox, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    db.new_id("fld"), doc["id"], f.kind, f.row_index, f.name, f.value, f.raw_text, f.value,
                    f.confidence, f.ocr_confidence, int(f.verified), f.status, f.status, json.dumps(f.issues), f.suggested_value,
                    f.page, json.dumps(f.bbox) if f.bbox else None, ts,
                ),
            )
        conn.execute(
            "UPDATE documents SET status = ?, possible_duplicate_of = ? WHERE id = ?",
            ("needs_review" if needs_review else "ready", duplicate_of, doc["id"]),
        )
        conn.execute(
            "UPDATE jobs SET stage = 'done', status = 'done', progress = 1, message = 'Tamamlandı',"
            " lease_owner = NULL, lease_expires_at = NULL, finished_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, job["id"]),
        )


def _apply_learned_corrections(conn, user_id: str, candidates: list[Candidate]) -> None:
    """Reuse a user's earlier fix when OCR produces the exact same reading again."""
    for c in candidates:
        if c.kind != "header" or FIELD_TYPES.get(c.name) not in LEARNABLE_TYPES or not c.raw_text:
            continue
        rule = conn.execute(
            "SELECT value FROM correction_rules WHERE user_id = ? AND field_name = ? AND raw_value = ?",
            (user_id, c.name, c.raw_text),
        ).fetchone()
        if rule:
            c.value = rule["value"]
            c.ocr_conf = 1.0
            c.issues = ["learned_from_correction"]
            conn.execute(
                "UPDATE correction_rules SET hits = hits + 1, updated_at = ?"
                " WHERE user_id = ? AND field_name = ? AND raw_value = ?",
                (db.now(), user_id, c.name, c.raw_text),
            )


def _find_content_duplicate(conn, doc, header: dict) -> str | None:
    """Different file bytes, same business document (e.g. a re-scan or photo of a PDF)."""
    number = header.get("invoice_number")
    if not number:
        return None
    rows = conn.execute(
        "SELECT d.id FROM documents d JOIN fields f ON f.document_id = d.id AND f.kind = 'header'"
        " AND f.name = 'invoice_number' AND f.value = ?"
        " WHERE d.user_id = ? AND d.id != ? ORDER BY d.created_at",
        (number, doc["user_id"], doc["id"]),
    ).fetchall()
    for row in rows:
        other = {
            r["name"]: r["value"]
            for r in conn.execute(
                "SELECT name, value FROM fields WHERE document_id = ? AND kind = 'header'", (row["id"],)
            )
        }
        same_supplier = (
            not header.get("supplier_tax_id")
            or not other.get("supplier_tax_id")
            or header["supplier_tax_id"] == other["supplier_tax_id"]
        )
        if same_supplier:
            return row["id"]
    return None
