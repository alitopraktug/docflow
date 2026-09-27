import importlib.util
import random
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docflow import db  # noqa: E402
from docflow.api import create_app  # noqa: E402
from docflow.config import Settings  # noqa: E402
from docflow.worker import process_one  # noqa: E402


def _load_generator():
    spec = importlib.util.spec_from_file_location("sample_generator", ROOT / "samples" / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    """Two invoices from the same supplier, rendered at every quality level."""
    out = tmp_path_factory.mktemp("samples")
    rng = random.Random(7)
    fonts = gen._register_fonts()
    invoices = []
    for i in (1, 2):
        inv = gen.make_invoice(rng, i)
        inv["header"]["supplier_name"] = "Anadolu Kırtasiye Ltd. Şti."
        stem = out / inv["id"]
        gen.render_pdf(inv, stem.with_suffix(".digital.pdf"), fonts)
        gen.render_variants(stem.with_suffix(".digital.pdf"), stem, rng)
        invoices.append((inv, stem))
    return invoices


@pytest.fixture
def cfg(tmp_path):
    return Settings(data_dir=tmp_path / "data", embedded_worker=False, lease_seconds=30)


@pytest.fixture
def client(cfg):
    app = create_app(cfg, start_worker=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def conn(cfg, client):
    c = db.connect(cfg.db_path)
    yield c
    c.close()


def register(client, name="Ali"):
    token = client.post("/api/auth/register", json={"name": name}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def upload(client, headers, path: Path):
    with path.open("rb") as fh:
        return client.post("/api/documents", files={"file": (path.name, fh)}, headers=headers)


def drain(conn, cfg, worker="test-worker"):
    while process_one(conn, worker, cfg):
        pass
