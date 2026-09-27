"""If processing stops half way, does it restart from where it left off?"""

import pymupdf

from docflow import db, pipeline
from docflow.worker import process_one

from .conftest import register, upload


class Crash(Exception):
    pass


def scanned_pdf(samples, tmp_path):
    """Two-page image-only PDF (no text layer), so every page needs OCR."""
    path = tmp_path / "scanned.pdf"
    with pymupdf.open() as pdf:
        for _, stem in samples:
            img = stem.with_suffix(".scan.png")
            page = pdf.new_page(width=595, height=842)
            page.insert_image(page.rect, filename=str(img))
        pdf.save(path)
    return path


def test_crash_mid_ocr_resumes_without_redoing_finished_pages(client, conn, cfg, samples, tmp_path, monkeypatch):
    headers = register(client)
    doc_id = upload(client, headers, scanned_pdf(samples, tmp_path)).json()["document"]["id"]

    calls = []
    real_ocr = pipeline.ocr_image

    def counting_ocr(path, page_no, lang):
        calls.append(page_no)
        return real_ocr(path, page_no, lang)

    monkeypatch.setattr(pipeline, "ocr_image", counting_ocr)

    def crash_after_first_page(stage, page):
        if stage == "ocr" and page == 2:
            raise Crash("worker killed while committing page 2")

    # Worker A: rasterises, OCRs page 1 (committed), dies on page 2.
    assert process_one(conn, "worker-A", cfg, fault=crash_after_first_page)
    job = conn.execute("SELECT * FROM jobs WHERE document_id = ?", (doc_id,)).fetchone()
    assert job["status"] == "queued" and job["stage"] == "ocr" and job["attempts"] == 1
    done_pages = conn.execute(
        "SELECT page_no FROM pages WHERE document_id = ? AND ocr_words IS NOT NULL", (doc_id,)
    ).fetchall()
    assert [p["page_no"] for p in done_pages] == [1]

    # Worker B picks it up and only OCRs the missing page.
    assert process_one(conn, "worker-B", cfg)
    assert calls == [1, 2, 2]
    job = conn.execute("SELECT * FROM jobs WHERE document_id = ?", (doc_id,)).fetchone()
    assert job["status"] == "done" and job["attempts"] == 2
    detail = client.get(f"/api/documents/{doc_id}", headers=headers).json()
    assert detail["status"] in ("ready", "needs_review")
    assert detail["page_count"] == 2


def test_abandoned_lease_is_reclaimed(client, conn, cfg, samples):
    """A worker that vanished without reporting (kill -9) leaves a running job behind."""
    headers = register(client)
    inv, stem = samples[0]
    doc_id = upload(client, headers, stem.with_suffix(".digital.pdf")).json()["document"]["id"]
    conn.execute(
        "UPDATE jobs SET status = 'running', lease_owner = 'dead-worker', lease_expires_at = ?, attempts = 1",
        (db.now() - 1,),
    )
    assert process_one(conn, "worker-B", cfg)
    job = conn.execute("SELECT status, lease_owner FROM jobs WHERE document_id = ?", (doc_id,)).fetchone()
    assert job["status"] == "done" and job["lease_owner"] is None


def test_live_lease_is_not_stolen(client, conn, cfg, samples):
    headers = register(client)
    _, stem = samples[0]
    upload(client, headers, stem.with_suffix(".digital.pdf"))
    conn.execute("UPDATE jobs SET status = 'running', lease_owner = 'busy-worker', lease_expires_at = ?",
                 (db.now() + 60,))
    assert not process_one(conn, "worker-B", cfg)


def test_repeated_failures_mark_job_failed_and_retry_resumes(client, conn, cfg, samples):
    headers = register(client)
    _, stem = samples[0]
    doc_id = upload(client, headers, stem.with_suffix(".digital.pdf")).json()["document"]["id"]

    def always_fail(stage, page):
        if stage == "extract":
            raise Crash("boom")

    for _ in range(cfg.max_attempts):
        process_one(conn, "w", cfg, fault=always_fail)
    assert client.get(f"/api/documents/{doc_id}", headers=headers).json()["status"] == "failed"

    r = client.post(f"/api/documents/{doc_id}/retry", headers=headers)
    assert r.status_code == 200
    job = conn.execute("SELECT stage FROM jobs WHERE document_id = ?", (doc_id,)).fetchone()
    assert job["stage"] == "extract"  # earlier stages are not repeated
    process_one(conn, "w", cfg)
    assert client.get(f"/api/documents/{doc_id}", headers=headers).json()["status"] == "ready"


def test_worker_that_lost_its_lease_writes_nothing(client, conn, cfg, samples):
    """A worker that stalled past its lease must not overwrite the new owner's work."""
    headers = register(client)
    _, stem = samples[0]
    doc_id = upload(client, headers, stem.with_suffix(".digital.pdf")).json()["document"]["id"]

    def taken_over(stage, page):
        if stage == "extract":
            conn.execute("UPDATE jobs SET lease_owner = 'worker-B' WHERE document_id = ?", (doc_id,))

    assert process_one(conn, "worker-A", cfg, fault=taken_over)
    assert conn.execute("SELECT COUNT(*) FROM fields WHERE document_id = ?", (doc_id,)).fetchone()[0] == 0
    job = conn.execute("SELECT status, lease_owner FROM jobs WHERE document_id = ?", (doc_id,)).fetchone()
    assert job["status"] == "running" and job["lease_owner"] == "worker-B"
