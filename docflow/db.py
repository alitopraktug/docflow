"""SQLite persistence layer.

SQLite in WAL mode is enough for a single-node deployment and lets the API
process and one or more worker processes share the same database file.
Every table that holds document data carries ``user_id`` (directly or via
``documents``) and every query in the API is scoped by it.
"""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id                    TEXT PRIMARY KEY,
    user_id               TEXT NOT NULL REFERENCES users(id),
    filename              TEXT NOT NULL,
    content_type          TEXT NOT NULL,
    sha256                TEXT NOT NULL,
    size_bytes            INTEGER NOT NULL,
    storage_path          TEXT NOT NULL,
    status                TEXT NOT NULL,       -- processing | needs_review | ready | approved | failed
    page_count            INTEGER,
    source_kind           TEXT,                -- pdf_text | ocr
    possible_duplicate_of TEXT,                -- same supplier + invoice number, different file
    processing_ms         INTEGER,
    created_at            REAL NOT NULL,
    approved_at           REAL,
    UNIQUE (user_id, sha256)
);

CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    document_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status           TEXT NOT NULL,           -- queued | running | done | failed
    stage            TEXT NOT NULL,           -- next stage to run (checkpoint)
    progress         REAL NOT NULL DEFAULT 0,
    message          TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    lease_owner      TEXT,
    lease_expires_at REAL,
    error            TEXT,
    metrics          TEXT NOT NULL DEFAULT '{}',  -- per-stage durations (ms)
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    finished_at      REAL
);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS pages (
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no      INTEGER NOT NULL,
    image_path   TEXT NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    ocr_words    TEXT,                    -- JSON list, NULL until the page is OCR'd (per-page checkpoint)
    ocr_engine   TEXT,
    ocr_ms       INTEGER,
    PRIMARY KEY (document_id, page_no)
);

-- Header fields and line-item cells share one table so review, correction
-- and provenance work the same way for both.
CREATE TABLE IF NOT EXISTS fields (
    id              TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,        -- header | line
    row_index       INTEGER,              -- line items only
    name            TEXT NOT NULL,
    value           TEXT,                 -- normalised value (e.g. 1234.50, 2026-09-01)
    raw_text        TEXT,                 -- exactly what OCR read
    extracted_value TEXT,                 -- value before any human edit
    confidence      REAL NOT NULL DEFAULT 0,   -- final confidence after cross-checks
    ocr_confidence  REAL,                      -- raw recogniser confidence of the value's words
    verified        INTEGER NOT NULL DEFAULT 0,  -- passed an independent cross-check
    status          TEXT NOT NULL,        -- auto_accepted | needs_review | corrected | confirmed
    initial_status  TEXT NOT NULL,        -- status the pipeline assigned (for metrics)
    issues          TEXT NOT NULL DEFAULT '[]',
    suggested_value TEXT,
    page_no         INTEGER,
    bbox            TEXT,                 -- JSON [x0, y0, x1, y1], normalised 0..1
    deleted         INTEGER NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fields_doc ON fields(document_id, kind, row_index);

CREATE TABLE IF NOT EXISTS corrections (
    id          TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    field_id    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    field_name  TEXT NOT NULL,
    action      TEXT NOT NULL,            -- correct | confirm | add_row | delete_row
    old_value   TEXT,
    new_value   TEXT,
    raw_text    TEXT,
    confidence  REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS corrections_doc ON corrections(document_id);

-- Learned from corrections: "when OCR reads <raw> for <field>, the user means <value>".
CREATE TABLE IF NOT EXISTS correction_rules (
    user_id     TEXT NOT NULL,
    field_name  TEXT NOT NULL,
    raw_value   TEXT NOT NULL,
    value       TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (user_id, field_name, raw_value)
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> float:
    return time.time()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE so concurrent writers serialise instead of failing late."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("issues", "bbox", "metrics"):
        if key in d and isinstance(d[key], str):
            d[key] = json.loads(d[key])
    return d
