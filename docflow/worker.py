"""Background job runner with lease-based crash recovery.

Workers claim jobs with an atomic UPDATE ... RETURNING, so several worker
processes can share one database. A claimed job carries a lease that the
worker renews while it makes progress; if the worker crashes (or the machine
restarts) the lease simply expires and the job becomes claimable again. The
pipeline resumes from its last checkpoint. After ``max_attempts`` failed
attempts the job is marked failed and the user can retry it manually.

Run standalone with ``python -m docflow.worker``.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import traceback

from . import db
from .config import Settings, settings as default_settings
from .pipeline import FaultInjector, LeaseLost, run_job

log = logging.getLogger("docflow.worker")


def claim_job(conn, worker_id: str, lease_seconds: int, max_attempts: int) -> str | None:
    now = db.now()
    with db.transaction(conn):
        # Jobs that ran out of attempts while their lease was abandoned are failed for good.
        conn.execute(
            "UPDATE jobs SET status = 'failed', lease_owner = NULL, updated_at = ?,"
            " error = COALESCE(error, 'İşlem tekrar tekrar yarıda kaldı')"
            " WHERE status = 'running' AND lease_expires_at < ? AND attempts >= ?",
            (now, now, max_attempts),
        )
        conn.execute(
            "UPDATE documents SET status = 'failed' WHERE id IN (SELECT document_id FROM jobs WHERE status = 'failed')"
            " AND status = 'processing'"
        )
        row = conn.execute(
            "UPDATE jobs SET status = 'running', lease_owner = ?, lease_expires_at = ?, attempts = attempts + 1,"
            " updated_at = ? WHERE id = ("
            "   SELECT id FROM jobs WHERE status = 'queued' OR (status = 'running' AND lease_expires_at < ?)"
            "   ORDER BY created_at LIMIT 1"
            " ) RETURNING id",
            (worker_id, now + lease_seconds, now, now),
        ).fetchone()
    return row["id"] if row else None


def renew_lease(conn, job_id: str, worker_id: str, lease_seconds: int) -> None:
    cur = conn.execute(
        "UPDATE jobs SET lease_expires_at = ? WHERE id = ? AND lease_owner = ?",
        (db.now() + lease_seconds, job_id, worker_id),
    )
    if cur.rowcount == 0:
        raise LeaseLost(job_id)


def fail_attempt(conn, job_id: str, worker_id: str, error: str, max_attempts: int) -> None:
    with db.transaction(conn):
        job = conn.execute("SELECT attempts, lease_owner, document_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None or job["lease_owner"] != worker_id:
            return
        final = job["attempts"] >= max_attempts
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?,"
            " message = ? WHERE id = ?",
            (
                "failed" if final else "queued",
                error[-2000:],
                db.now(),
                "Başarısız" if final else "Hata oluştu, kaldığı yerden tekrar denenecek",
                job_id,
            ),
        )
        if final:
            conn.execute("UPDATE documents SET status = 'failed' WHERE id = ?", (job["document_id"],))


def process_one(conn, worker_id: str, cfg: Settings, fault: FaultInjector | None = None) -> bool:
    """Claim and run a single job. Returns False when the queue is empty."""
    job_id = claim_job(conn, worker_id, cfg.lease_seconds, cfg.max_attempts)
    if job_id is None:
        return False
    try:
        kwargs = {"fault": fault} if fault else {}
        run_job(conn, job_id, worker_id, cfg, lambda: renew_lease(conn, job_id, worker_id, cfg.lease_seconds), **kwargs)
    except LeaseLost:
        log.warning("lease for %s lost; another worker owns it now", job_id)
    except Exception:
        log.exception("job %s failed", job_id)
        fail_attempt(conn, job_id, worker_id, traceback.format_exc(), cfg.max_attempts)
    return True


class Worker:
    def __init__(self, cfg: Settings | None = None, poll_interval: float = 0.5):
        self.cfg = cfg or default_settings
        self.poll_interval = poll_interval
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_forever(self) -> None:
        conn = db.connect(self.cfg.db_path)
        log.info("worker %s started", self.worker_id)
        while not self._stop.is_set():
            try:
                busy = process_one(conn, self.worker_id, self.cfg)
            except Exception:  # pragma: no cover - keep the loop alive
                log.exception("worker loop error")
                busy = False
            if not busy:
                self._stop.wait(self.poll_interval)
        conn.close()

    def start_in_thread(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="docflow-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)


def main() -> None:  # pragma: no cover - CLI
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_db(default_settings.db_path)
    Worker().run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
