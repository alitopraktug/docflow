import csv
import io

from openpyxl import load_workbook

from .conftest import drain, register, upload


def test_upload_extracts_digital_pdf_exactly(client, conn, cfg, samples):
    headers = register(client)
    inv, stem = samples[0]
    r = upload(client, headers, stem.with_suffix(".digital.pdf"))
    assert r.status_code == 201
    doc_id = r.json()["document"]["id"]
    drain(conn, cfg)
    d = client.get(f"/api/documents/{doc_id}", headers=headers).json()
    header = {f["name"]: f["value"] for f in d["header"]}
    for name, expected in inv["header"].items():
        assert header[name] == expected, name
    assert len(d["rows"]) == len(inv["lines"])
    assert d["status"] == "ready"
    # Every extracted value points back to where it came from.
    total = next(f for f in d["header"] if f["name"] == "total")
    assert total["page_no"] == 1 and len(total["bbox"]) == 4 and total["raw_text"]


def test_same_file_twice_is_recognised(client, samples):
    headers = register(client)
    _, stem = samples[0]
    first = upload(client, headers, stem.with_suffix(".digital.pdf"))
    second = upload(client, headers, stem.with_suffix(".digital.pdf"))
    assert first.status_code == 201
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert len(client.get("/api/documents", headers=headers).json()) == 1


def test_same_invoice_as_different_file_is_flagged(client, conn, cfg, samples):
    headers = register(client)
    _, stem = samples[0]
    a = upload(client, headers, stem.with_suffix(".digital.pdf")).json()["document"]["id"]
    b = upload(client, headers, stem.with_suffix(".scan.png")).json()["document"]["id"]
    drain(conn, cfg)
    assert client.get(f"/api/documents/{b}", headers=headers).json()["possible_duplicate_of"] == a


def test_users_cannot_see_each_others_documents(client, conn, cfg, samples):
    ali = register(client, "Ali")
    ayse = register(client, "Ayşe")
    _, stem = samples[0]
    doc_id = upload(client, ali, stem.with_suffix(".digital.pdf")).json()["document"]["id"]
    drain(conn, cfg)
    assert client.get("/api/documents", headers=ayse).json() == []
    for path in ("", "/pages/1", "/file", "/export.csv", "/export.xlsx", "/corrections"):
        assert client.get(f"/api/documents/{doc_id}{path}", headers=ayse).status_code == 404
    field_id = client.get(f"/api/documents/{doc_id}", headers=ali).json()["header"][0]["id"]
    assert client.patch(f"/api/documents/{doc_id}/fields/{field_id}", json={"value": "x"}, headers=ayse).status_code == 404
    # The same file uploaded by another user is that user's own, separate document.
    r = upload(client, ayse, stem.with_suffix(".digital.pdf"))
    assert r.status_code == 201 and r.json()["document"]["id"] != doc_id
    assert client.get("/api/documents").status_code == 401


def test_corrections_are_stored_revalidated_and_learned(client, conn, cfg, samples):
    headers = register(client)
    (inv1, stem1), (inv2, stem2) = samples
    doc_id = upload(client, headers, stem1.with_suffix(".digital.pdf")).json()["document"]["id"]
    drain(conn, cfg)
    d = client.get(f"/api/documents/{doc_id}", headers=headers).json()

    # Break a line amount: the document must go back to review.
    amount = d["rows"][0]["cells"]["amount"]
    d = client.patch(f"/api/documents/{doc_id}/fields/{amount['id']}", json={"value": "1,00"}, headers=headers).json()
    assert d["status"] == "needs_review"
    subtotal = next(f for f in d["header"] if f["name"] == "subtotal")
    assert subtotal["status"] == "needs_review"
    assert client.post(f"/api/documents/{doc_id}/approve", headers=headers).status_code == 409

    # Put it back in Turkish number format: consistent again.
    original = inv1["lines"][0]["amount"].replace(".", ",")
    d = client.patch(f"/api/documents/{doc_id}/fields/{amount['id']}", json={"value": original}, headers=headers).json()
    assert d["status"] == "ready"

    # Rename the supplier; the next document from the same supplier gets it automatically.
    supplier = next(f for f in d["header"] if f["name"] == "supplier_name")
    client.patch(f"/api/documents/{doc_id}/fields/{supplier['id']}", json={"value": "Anadolu Kırtasiye"},
                 headers=headers)
    history = client.get(f"/api/documents/{doc_id}/corrections", headers=headers).json()
    assert [h["action"] for h in history] == ["correct", "correct", "correct"]
    assert history[0]["old_value"] == inv1["lines"][0]["amount"] and history[0]["new_value"] == "1.00"

    doc2 = upload(client, headers, stem2.with_suffix(".digital.pdf")).json()["document"]["id"]
    drain(conn, cfg)
    d2 = client.get(f"/api/documents/{doc2}", headers=headers).json()
    supplier2 = next(f for f in d2["header"] if f["name"] == "supplier_name")
    assert supplier2["value"] == "Anadolu Kırtasiye"
    assert "learned_from_correction" in supplier2["issues"]

    assert client.post(f"/api/documents/{doc_id}/approve", headers=headers).json()["status"] == "approved"
    stats = client.get("/api/stats", headers=headers).json()
    assert stats["documents"] == 2 and stats["approved"] == 1 and stats["correction_rate"] > 0


def test_invalid_input_is_rejected(client, conn, cfg, samples):
    headers = register(client)
    _, stem = samples[0]
    doc_id = upload(client, headers, stem.with_suffix(".digital.pdf")).json()["document"]["id"]
    drain(conn, cfg)
    d = client.get(f"/api/documents/{doc_id}", headers=headers).json()
    date = next(f for f in d["header"] if f["name"] == "invoice_date")
    vkn = next(f for f in d["header"] if f["name"] == "supplier_tax_id")
    assert client.patch(f"/api/documents/{doc_id}/fields/{date['id']}", json={"value": "31.02.2026"},
                        headers=headers).status_code == 422
    assert client.patch(f"/api/documents/{doc_id}/fields/{vkn['id']}", json={"value": "1234567891"},
                        headers=headers).status_code == 422
    r = client.post("/api/documents", files={"file": ("x.txt", b"hello")}, headers=headers)
    assert r.status_code == 415


def test_rows_can_be_added_and_deleted(client, conn, cfg, samples):
    headers = register(client)
    inv, stem = samples[0]
    doc_id = upload(client, headers, stem.with_suffix(".digital.pdf")).json()["document"]["id"]
    drain(conn, cfg)
    d = client.post(f"/api/documents/{doc_id}/rows", json={"description": "Kargo", "quantity": "1",
                                                           "unit_price": "0", "amount": "0"}, headers=headers).json()
    assert len(d["rows"]) == len(inv["lines"]) + 1
    d = client.delete(f"/api/documents/{doc_id}/rows/{d['rows'][-1]['row_index']}", headers=headers).json()
    assert len(d["rows"]) == len(inv["lines"])
    assert d["status"] == "ready"


def test_export_csv_and_xlsx(client, conn, cfg, samples):
    headers = register(client)
    inv, stem = samples[0]
    doc_id = upload(client, headers, stem.with_suffix(".digital.pdf")).json()["document"]["id"]
    drain(conn, cfg)
    text = client.get(f"/api/documents/{doc_id}/export.csv?sep=;", headers=headers).content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    assert len(rows) == len(inv["lines"])
    assert rows[0]["invoice_number"] == inv["header"]["invoice_number"]
    assert rows[0]["amount"] == inv["lines"][0]["amount"]
    assert rows[0]["needs_review"] == "no"

    wb = load_workbook(io.BytesIO(client.get(f"/api/documents/{doc_id}/export.xlsx", headers=headers).content))
    assert wb.sheetnames == ["Belge", "Kalemler", "Düzeltmeler"]
    assert wb["Kalemler"].max_row == len(inv["lines"]) + 1


def test_turkish_file_names_download_correctly(client, conn, cfg, samples):
    headers = register(client)
    _, stem = samples[0]
    with stem.with_suffix(".digital.pdf").open("rb") as fh:
        doc_id = client.post("/api/documents", files={"file": ("şubat faturası.pdf", fh)},
                             headers=headers).json()["document"]["id"]
    drain(conn, cfg)
    for ext in ("csv", "xlsx"):
        r = client.get(f"/api/documents/{doc_id}/export.{ext}", headers=headers)
        assert r.status_code == 200
        disposition = r.headers["content-disposition"]
        assert f"filename*=UTF-8''%C5%9Fubat%20faturas%C4%B1.{ext}" in disposition
        assert disposition.isascii()


def test_document_cannot_be_approved_before_processing(client, conn, cfg, samples):
    headers = register(client)
    _, stem = samples[0]
    doc_id = upload(client, headers, stem.with_suffix(".digital.pdf")).json()["document"]["id"]
    r = client.post(f"/api/documents/{doc_id}/approve", headers=headers)
    assert r.status_code == 409 and "işleniyor" in r.json()["detail"]
    drain(conn, cfg)
    assert client.post(f"/api/documents/{doc_id}/approve", headers=headers).json()["status"] == "approved"


def test_delete_removes_document_and_files(client, conn, cfg, samples):
    headers = register(client)
    _, stem = samples[0]
    doc_id = upload(client, headers, stem.with_suffix(".digital.pdf")).json()["document"]["id"]
    drain(conn, cfg)
    assert (cfg.data_dir / "pages" / doc_id).exists()
    assert client.delete(f"/api/documents/{doc_id}", headers=headers).status_code == 204
    assert not (cfg.data_dir / "pages" / doc_id).exists()
    assert client.get(f"/api/documents/{doc_id}", headers=headers).status_code == 404
    # Deleting frees the hash: the same file can be uploaded again as a new document.
    assert upload(client, headers, stem.with_suffix(".digital.pdf")).status_code == 201
