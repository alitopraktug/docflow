"""Measure DocFlow on documents of different quality against ground truth.

    python samples/generate.py --count 12        # once
    python eval/evaluate.py                      # writes eval/results.md

Documents go through the real path: HTTP upload -> job queue -> worker ->
checkpointed pipeline -> database. For each quality tier it reports:

* field accuracy        share of expected values the system extracted exactly
* review rate           share of fields the system sent to a human
* silent error rate     share of fields that were WRONG yet auto-accepted
                        (the number that matters: an error nobody is told about)
* errors caught         share of wrong fields that were flagged for review
* processing time       per document, mean and p95
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from docflow import db  # noqa: E402
from docflow.api import create_app  # noqa: E402
from docflow.config import Settings  # noqa: E402
from docflow.extract import HEADER_FIELDS, LINE_COLUMNS  # noqa: E402
from docflow.worker import process_one  # noqa: E402

TIERS = [
    ("digital", "digital.pdf", "Digital PDF"),
    ("scan", "scan.png", "Scan (200 dpi)"),
    ("photo", "photo.jpg", "Phone photo"),
    ("poor", "poor.jpg", "Poor photo"),
]


def norm(value: str | None) -> str | None:
    return " ".join(value.split()).casefold() if value else None


def score_document(detail: dict, truth: dict) -> list[dict]:
    """One record per expected field: was it right, and what did the system decide?"""
    out = []
    header = {f["name"]: f for f in detail["header"]}
    for name in HEADER_FIELDS:
        f = header.get(name) or {}
        out.append(_record("header", name, f, truth["header"].get(name)))
    rows = detail["rows"]
    for i, expected_row in enumerate(truth["lines"]):
        cells = rows[i]["cells"] if i < len(rows) else {}
        for col in LINE_COLUMNS:
            out.append(_record("line", col, cells.get(col) or {}, expected_row[col]))
    # Extra rows the system invented are errors too.
    for row in rows[len(truth["lines"]):]:
        for col in LINE_COLUMNS:
            f = row["cells"].get(col) or {}
            out.append({"kind": "line", "name": col, "correct": False, "status": f.get("status", "needs_review"),
                        "suggestion_correct": False})
    return out


def _record(kind: str, name: str, f: dict, expected: str | None) -> dict:
    status = f.get("status", "needs_review")
    return {
        "kind": kind,
        "name": name,
        "correct": norm(f.get("value")) == norm(expected),
        "status": status,
        "suggestion_correct": f.get("suggested_value") is not None and norm(f.get("suggested_value")) == norm(expected),
    }


def run(samples: Path, out_md: Path) -> dict:
    truths = sorted(samples.glob("*.json"))
    if not truths:
        raise SystemExit(f"No samples in {samples}. Run: python samples/generate.py")
    tmp = Path(tempfile.mkdtemp(prefix="docflow-eval-"))
    cfg = Settings(data_dir=tmp, embedded_worker=False)
    app = create_app(cfg, start_worker=False)
    results: dict[str, dict] = {}
    with TestClient(app) as client:
        token = client.post("/api/auth/register", json={"name": "eval"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        conn = db.connect(cfg.db_path)
        for tier, suffix, _ in TIERS:
            records, times, docs = [], [], 0
            for truth_path in truths:
                truth = json.loads(truth_path.read_text(encoding="utf-8"))
                path = truth_path.with_name(f"{truth['id']}.{suffix}")
                if not path.exists():
                    continue
                with path.open("rb") as fh:
                    r = client.post("/api/documents", files={"file": (path.name, fh)}, headers=auth)
                doc_id = r.json()["document"]["id"]
                started = time.perf_counter()
                while process_one(conn, "eval-worker", cfg):
                    pass
                times.append((time.perf_counter() - started) * 1000)
                detail = client.get(f"/api/documents/{doc_id}", headers=auth).json()
                records.extend(score_document(detail, truth))
                docs += 1
            results[tier] = summarise(records, times, docs)
        conn.close()
    write_markdown(results, out_md)
    return results


def summarise(records: list[dict], times: list[float], docs: int) -> dict:
    n = len(records) or 1
    wrong = [r for r in records if not r["correct"]]
    flagged = [r for r in records if r["status"] == "needs_review"]
    silent = [r for r in wrong if r["status"] != "needs_review"]
    by_kind = defaultdict(list)
    for r in records:
        by_kind[r["kind"]].append(r["correct"])
    return {
        "documents": docs,
        "fields": len(records),
        "field_accuracy": round(sum(r["correct"] for r in records) / n, 4),
        "header_accuracy": round(sum(by_kind["header"]) / max(len(by_kind["header"]), 1), 4),
        "line_accuracy": round(sum(by_kind["line"]) / max(len(by_kind["line"]), 1), 4),
        "review_rate": round(len(flagged) / n, 4),
        "silent_error_rate": round(len(silent) / n, 4),
        "errors_caught": round(1 - len(silent) / len(wrong), 4) if wrong else 1.0,
        "correct_suggestions": sum(r["suggestion_correct"] for r in records),
        "time_mean_ms": round(statistics.mean(times)) if times else 0,
        "time_p95_ms": round(sorted(times)[max(0, int(len(times) * 0.95) - 1)]) if times else 0,
    }


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def write_markdown(results: dict, path: Path) -> None:
    labels = {t: label for t, _, label in TIERS}
    lines = [
        "# DocFlow evaluation results",
        "",
        "Generated by `python eval/evaluate.py`. Samples are synthetic invoices from `samples/generate.py`;",
        "every invoice is processed at four quality levels.",
        "",
        "| Quality | Docs | Fields | Field accuracy | Sent to review | Silent errors | Errors caught | Mean time | p95 time |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for tier, r in results.items():
        lines.append(
            f"| {labels[tier]} | {r['documents']} | {r['fields']} | {pct(r['field_accuracy'])} | {pct(r['review_rate'])}"
            f" | {pct(r['silent_error_rate'])} | {pct(r['errors_caught'])} | {r['time_mean_ms']} ms | {r['time_p95_ms']} ms |"
        )
    lines += [
        "",
        "- **Field accuracy**: share of expected values extracted exactly (header fields + line-item cells).",
        "- **Sent to review**: share of fields the system was not sure about and left for a person to confirm.",
        "- **Silent errors**: share of fields that were wrong yet auto-accepted. This is the number to keep near zero.",
        "- **Errors caught**: share of wrong fields that ended up on the review list.",
        "- **Time**: processing time per document once it leaves the queue (single worker).",
        "",
        "Per tier: " + "; ".join(
            f"{labels[t]}: header {pct(r['header_accuracy'])}, line items {pct(r['line_accuracy'])},"
            f" correct suggestions {r['correct_suggestions']}"
            for t, r in results.items()
        ),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", type=Path, default=ROOT / "samples" / "out")
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "results.md")
    args = parser.parse_args()
    results = run(args.samples, args.out)
    print(args.out.read_text(encoding="utf-8"))
    args.out.with_suffix(".json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
