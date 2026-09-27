"""HTTP API and static web UI.

Every document route resolves the document through ``_owned_doc``, which
filters by the caller's user id, so one user can never read, edit or even
learn the existence of another user's document (it is a plain 404).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, export, review
from .config import Settings, settings as default_settings
from .extract import FIELD_LABELS_TR, HEADER_FIELDS, LINE_COLUMNS
from .worker import Worker

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_TYPES = {
    b"%PDF": ("application/pdf", ".pdf"),
    b"\x89PNG": ("image/png", ".png"),
    b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
    b"II*\x00": ("image/tiff", ".tif"),
    b"MM\x00*": ("image/tiff", ".tif"),
    b"RIFF": ("image/webp", ".webp"),
}


class RegisterIn(BaseModel):
    name: str


class FieldUpdate(BaseModel):
    value: str | None = None


class RowIn(BaseModel):
    description: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None


def create_app(cfg: Settings | None = None, start_worker: bool | None = None) -> FastAPI:
    cfg = cfg or default_settings
    db.init_db(cfg.db_path)
    run_worker = cfg.embedded_worker if start_worker is None else start_worker

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = Worker(cfg) if run_worker else None
        if worker:
            worker.start_in_thread()
        yield
        if worker:
            worker.stop()

    app = FastAPI(title="DocFlow", version="0.1.0", lifespan=lifespan)
    app.state.cfg = cfg

    def get_conn():
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def current_user(
        conn: sqlite3.Connection = Depends(get_conn),
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ) -> dict:
        raw = token
        if authorization and authorization.lower().startswith("bearer "):
            raw = authorization[7:].strip()
        if not raw:
            raise HTTPException(401, "Giriş gerekli")
        user = conn.execute("SELECT * FROM users WHERE token = ?", (raw,)).fetchone()
        if user is None:
            raise HTTPException(401, "Geçersiz oturum")
        return dict(user)

    def _owned_doc(conn: sqlite3.Connection, user: dict, doc_id: str) -> sqlite3.Row:
        doc = conn.execute("SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"])).fetchone()
        if doc is None:
            raise HTTPException(404, "Belge bulunamadı")
        return doc

    @app.exception_handler(review.ReviewError)
    async def review_error(_: Request, exc: review.ReviewError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    # ------------------------------------------------------------------ auth

    @app.post("/api/auth/register")
    def register(body: RegisterIn, conn: sqlite3.Connection = Depends(get_conn)):
        name = body.name.strip()[:60]
        if not name:
            raise HTTPException(422, "İsim gerekli")
        user = {"id": db.new_id("usr"), "name": name, "token": secrets.token_urlsafe(24), "created_at": db.now()}
        conn.execute("INSERT INTO users (id, name, token, created_at) VALUES (:id, :name, :token, :created_at)", user)
        return user

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)):
        return {"id": user["id"], "name": user["name"]}

    # ------------------------------------------------------------- documents

    @app.post("/api/documents")
    async def upload(
        file: UploadFile = File(...),
        user: dict = Depends(current_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        data = await file.read(cfg.max_upload_mb * 1024 * 1024 + 1)
        if len(data) > cfg.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"Dosya {cfg.max_upload_mb} MB sınırını aşıyor")
        if not data:
            raise HTTPException(422, "Dosya boş")
        kind = next((v for magic, v in ALLOWED_TYPES.items() if data.startswith(magic)), None)
        if kind is None:
            raise HTTPException(415, "Yalnızca PDF, PNG, JPEG, TIFF veya WEBP yüklenebilir")
        content_type, ext = kind
        sha = hashlib.sha256(data).hexdigest()

        existing = conn.execute(
            "SELECT * FROM documents WHERE user_id = ? AND sha256 = ?", (user["id"], sha)
        ).fetchone()
        if existing:
            return JSONResponse(
                {"duplicate": True, "document": _doc_summary(conn, existing)}, status_code=200
            )

        doc_id = db.new_id("doc")
        user_dir = cfg.data_dir / "files" / user["id"]
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / f"{sha}{ext}"
        path.write_bytes(data)
        ts = db.now()
        try:
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO documents (id, user_id, filename, content_type, sha256, size_bytes, storage_path,"
                    " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', ?)",
                    (doc_id, user["id"], (file.filename or "belge")[:200], content_type, sha, len(data), str(path), ts),
                )
                conn.execute(
                    "INSERT INTO jobs (id, document_id, status, stage, message, created_at, updated_at)"
                    " VALUES (?, ?, 'queued', 'rasterize', 'Sırada', ?, ?)",
                    (db.new_id("job"), doc_id, ts, ts),
                )
        except sqlite3.IntegrityError:
            # Two identical uploads raced; the other one won.
            existing = conn.execute(
                "SELECT * FROM documents WHERE user_id = ? AND sha256 = ?", (user["id"], sha)
            ).fetchone()
            return JSONResponse({"duplicate": True, "document": _doc_summary(conn, existing)}, status_code=200)
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return JSONResponse({"duplicate": False, "document": _doc_summary(conn, doc)}, status_code=201)

    @app.get("/api/documents")
    def list_documents(user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        docs = conn.execute(
            "SELECT * FROM documents WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
        ).fetchall()
        return [_doc_summary(conn, d) for d in docs]

    @app.get("/api/documents/{doc_id}")
    def get_document(doc_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        doc = _owned_doc(conn, user, doc_id)
        fields = [
            db.row_to_dict(f)
            for f in conn.execute(
                "SELECT * FROM fields WHERE document_id = ? AND deleted = 0 ORDER BY kind, row_index", (doc_id,)
            )
        ]
        header = sorted((f for f in fields if f["kind"] == "header"), key=lambda f: HEADER_FIELDS.index(f["name"]))
        rows: dict[int, dict] = {}
        for f in fields:
            if f["kind"] == "line":
                rows.setdefault(f["row_index"], {"row_index": f["row_index"], "cells": {}})["cells"][f["name"]] = f
        pages = [
            {"page_no": p["page_no"], "width": p["width"], "height": p["height"]}
            for p in conn.execute(
                "SELECT page_no, width, height FROM pages WHERE document_id = ? ORDER BY page_no", (doc_id,)
            )
        ]
        return {
            **_doc_summary(conn, doc),
            "pages": pages,
            "header": header,
            "rows": [rows[k] for k in sorted(rows)],
            "labels": FIELD_LABELS_TR,
            "line_columns": LINE_COLUMNS,
        }

    @app.get("/api/documents/{doc_id}/pages/{page_no}")
    def page_image(doc_id: str, page_no: int, user: dict = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_conn)):
        _owned_doc(conn, user, doc_id)
        page = conn.execute(
            "SELECT image_path FROM pages WHERE document_id = ? AND page_no = ?", (doc_id, page_no)
        ).fetchone()
        if page is None:
            raise HTTPException(404, "Sayfa bulunamadı")
        return FileResponse(page["image_path"], media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/documents/{doc_id}/file")
    def original_file(doc_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        doc = _owned_doc(conn, user, doc_id)
        return FileResponse(doc["storage_path"], media_type=doc["content_type"], filename=doc["filename"])

    @app.delete("/api/documents/{doc_id}", status_code=204)
    def delete_document(doc_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        doc = _owned_doc(conn, user, doc_id)
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        Path(doc["storage_path"]).unlink(missing_ok=True)
        shutil.rmtree(cfg.data_dir / "pages" / doc_id, ignore_errors=True)
        return Response(status_code=204)

    @app.post("/api/documents/{doc_id}/retry")
    def retry(doc_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        doc = _owned_doc(conn, user, doc_id)
        with db.transaction(conn):
            cur = conn.execute(
                "UPDATE jobs SET status = 'queued', attempts = 0, error = NULL, message = 'Kaldığı yerden yeniden sırada',"
                " updated_at = ? WHERE document_id = ? AND status = 'failed'",
                (db.now(), doc_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(409, "Yalnızca başarısız işler yeniden başlatılabilir")
            conn.execute("UPDATE documents SET status = 'processing' WHERE id = ?", (doc["id"],))
        return _doc_summary(conn, conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone())

    # ---------------------------------------------------------------- review

    @app.patch("/api/documents/{doc_id}/fields/{field_id}")
    def update_field(doc_id: str, field_id: str, body: FieldUpdate, user: dict = Depends(current_user),
                     conn: sqlite3.Connection = Depends(get_conn)):
        _owned_doc(conn, user, doc_id)
        review.correct_field(conn, user["id"], doc_id, field_id, body.value)
        return get_document(doc_id, user, conn)

    @app.post("/api/documents/{doc_id}/fields/{field_id}/confirm")
    def confirm_field(doc_id: str, field_id: str, user: dict = Depends(current_user),
                      conn: sqlite3.Connection = Depends(get_conn)):
        _owned_doc(conn, user, doc_id)
        review.confirm_field(conn, user["id"], doc_id, field_id)
        return get_document(doc_id, user, conn)

    @app.post("/api/documents/{doc_id}/rows")
    def add_row(doc_id: str, body: RowIn, user: dict = Depends(current_user),
                conn: sqlite3.Connection = Depends(get_conn)):
        _owned_doc(conn, user, doc_id)
        review.add_row(conn, user["id"], doc_id, body.model_dump())
        return get_document(doc_id, user, conn)

    @app.delete("/api/documents/{doc_id}/rows/{row_index}")
    def delete_row(doc_id: str, row_index: int, user: dict = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_conn)):
        _owned_doc(conn, user, doc_id)
        review.delete_row(conn, user["id"], doc_id, row_index)
        return get_document(doc_id, user, conn)

    @app.post("/api/documents/{doc_id}/approve")
    def approve(doc_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        _owned_doc(conn, user, doc_id)
        try:
            review.approve(conn, doc_id)
        except review.ReviewError as exc:
            raise HTTPException(409, str(exc)) from exc
        return get_document(doc_id, user, conn)

    @app.get("/api/documents/{doc_id}/corrections")
    def corrections(doc_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        _owned_doc(conn, user, doc_id)
        return [
            dict(r)
            for r in conn.execute(
                "SELECT field_name, action, old_value, new_value, raw_text, confidence, created_at"
                " FROM corrections WHERE document_id = ? ORDER BY created_at",
                (doc_id,),
            )
        ]

    @app.get("/api/documents/{doc_id}/export.csv")
    def export_csv(doc_id: str, sep: str = Query(default=",", pattern="^[,;]$"), user: dict = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_conn)):
        doc = _owned_doc(conn, user, doc_id)
        return Response(
            export.to_csv(conn, doc_id, sep),
            media_type="text/csv; charset=utf-8",
            headers=_attachment(f"{Path(doc['filename']).stem}.csv"),
        )

    @app.get("/api/documents/{doc_id}/export.xlsx")
    def export_xlsx(doc_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        doc = _owned_doc(conn, user, doc_id)
        return Response(
            export.to_xlsx(conn, doc_id),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers=_attachment(f"{Path(doc['filename']).stem}.xlsx"),
        )

    # ----------------------------------------------------------------- stats

    @app.get("/api/stats")
    def stats(user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        return user_stats(conn, user["id"])

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def _attachment(filename: str) -> dict:
    """Content-Disposition that survives non-ASCII names ('şubat faturası.xlsx').

    HTTP headers are latin-1, so the plain ``filename`` gets an ASCII fallback and
    the real name goes into RFC 5987 ``filename*``.
    """
    fallback = re.sub(r'[^\x20-\x7e]|["\\]', "_", filename).strip() or "docflow"
    return {"Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"}


def _doc_summary(conn: sqlite3.Connection, doc: sqlite3.Row) -> dict:
    job = db.row_to_dict(
        conn.execute(
            "SELECT status, stage, progress, message, attempts, error, metrics FROM jobs"
            " WHERE document_id = ? ORDER BY created_at DESC LIMIT 1",
            (doc["id"],),
        ).fetchone()
    )
    counts = conn.execute(
        "SELECT COUNT(*) AS total, SUM(status = 'needs_review') AS pending, SUM(initial_status = 'needs_review')"
        " AS flagged, SUM(status = 'corrected') AS corrected FROM fields WHERE document_id = ? AND deleted = 0",
        (doc["id"],),
    ).fetchone()
    d = dict(doc)
    d.pop("storage_path", None)
    return {
        **d,
        "job": job,
        "field_counts": {k: counts[k] or 0 for k in ("total", "pending", "flagged", "corrected")},
    }


def user_stats(conn: sqlite3.Connection, user_id: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS fields, SUM(f.initial_status = 'needs_review') AS flagged,"
        " SUM(f.initial_status IN ('auto_accepted', 'needs_review') AND f.status = 'corrected'"
        "     AND COALESCE(f.value, '') != COALESCE(f.extracted_value, '')) AS corrected"
        " FROM fields f JOIN documents d ON d.id = f.document_id WHERE d.user_id = ? AND f.initial_status != 'added'",
        (user_id,),
    ).fetchone()
    docs = conn.execute(
        "SELECT COUNT(*) AS n, AVG(processing_ms) AS avg_ms, SUM(status = 'approved') AS approved"
        " FROM documents WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    fields = row["fields"] or 0
    return {
        "documents": docs["n"],
        "approved": docs["approved"] or 0,
        "avg_processing_ms": round(docs["avg_ms"] or 0),
        "fields": fields,
        "review_rate": round((row["flagged"] or 0) / fields, 4) if fields else 0,
        "correction_rate": round((row["corrected"] or 0) / fields, 4) if fields else 0,
    }

