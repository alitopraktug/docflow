"""Runtime configuration, read from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DOCFLOW_DATA_DIR", "data")))
    # Fields whose confidence is below this go to human review unless they are
    # independently verified (e.g. by arithmetic cross-checks).
    review_threshold: float = float(os.environ.get("DOCFLOW_REVIEW_THRESHOLD", "0.90"))
    ocr_lang: str = os.environ.get("DOCFLOW_OCR_LANG", "tur+eng")
    render_dpi: int = int(os.environ.get("DOCFLOW_RENDER_DPI", "200"))
    # A job lease that is not renewed within this many seconds is considered
    # abandoned (worker crashed) and can be picked up by another worker.
    lease_seconds: int = int(os.environ.get("DOCFLOW_LEASE_SECONDS", "30"))
    max_attempts: int = int(os.environ.get("DOCFLOW_MAX_ATTEMPTS", "3"))
    embedded_worker: bool = os.environ.get("DOCFLOW_EMBEDDED_WORKER", "1") == "1"
    max_upload_mb: int = int(os.environ.get("DOCFLOW_MAX_UPLOAD_MB", "20"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "docflow.sqlite3"


settings = Settings()
