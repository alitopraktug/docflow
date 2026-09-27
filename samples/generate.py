"""Generate synthetic purchase invoices at several quality levels, with ground truth.

    python samples/generate.py --count 12 --out samples/out

For each invoice this writes:
  <id>.digital.pdf  - born-digital PDF with a text layer
  <id>.scan.png     - 200 dpi scan with light noise
  <id>.photo.jpg    - phone photo: 130 dpi, slight rotation, blur, uneven light, JPEG
  <id>.poor.jpg     - bad photo: 100 dpi, more rotation and blur, heavy JPEG
  <id>.json         - ground truth
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pymupdf as fitz
import numpy as np
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
]
SUPPLIERS = [
    "Anadolu Kırtasiye Ltd. Şti.", "Boğaziçi Bilişim A.Ş.", "Ege Ofis Malzemeleri", "Karadeniz Teknik Tedarik",
    "Marmara Endüstriyel Ürünler", "Kapadokya Mobilya San.", "Toros Elektronik A.Ş.", "Çukurova Ambalaj Ltd.",
]
BUYERS = ["Yıldız Yazılım A.Ş.", "Mavi Lojistik Ltd.", "Deniz Danışmanlık", "Atlas Mühendislik"]
PRODUCTS = [
    ("A4 Fotokopi Kağıdı 80gr", 90, 180), ("Toner Kartuşu 85A", 450, 1400), ("Ofis Sandalyesi", 1800, 6500),
    ("USB-C Kablo 2m", 60, 250), ("Laptop Standı Alüminyum", 350, 1200), ("Beyaz Tahta Kalemi 12li", 80, 220),
    ("Ergonomik Klavye", 600, 2200), ("Monitör 27 inç", 5200, 11000), ("Dosya Klasörü Geniş", 25, 70),
    ("Kablosuz Mouse", 250, 900), ("Ağ Anahtarı 24 Port", 3200, 9000), ("Harici Disk 2TB", 1900, 4200),
    ("Zımba Makinesi", 120, 400), ("Toplantı Masası", 7000, 18000), ("Etiket Yazıcı", 1500, 4800),
]


def vkn_check_digit(first9: str) -> str:
    total = 0
    for i in range(9):
        tmp = (int(first9[i]) + (9 - i)) % 10
        tmp2 = (tmp * 2 ** (9 - i)) % 9
        if tmp != 0 and tmp2 == 0:
            tmp2 = 9
        total += tmp2
    return str((10 - total % 10) % 10)


def tr_money(d: Decimal) -> str:
    s = f"{d:,.2f}"  # 1,234.56
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def tr_date(d: dt.date) -> str:
    return d.strftime("%d.%m.%Y")


def make_invoice(rng: random.Random, idx: int) -> dict:
    first9 = "".join(str(rng.randint(0, 9)) for _ in range(9))
    inv_date = dt.date(2026, 1, 1) + dt.timedelta(days=rng.randint(0, 240))
    lines = []
    for name, lo, hi in rng.sample(PRODUCTS, rng.randint(3, 7)):
        qty = rng.choice([1, 1, 2, 3, 4, 5, 6, 10, 12, 20, 25, 50])
        price = Decimal(rng.randint(lo * 100, hi * 100)) / 100
        amount = (price * qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
        lines.append({"description": name, "quantity": str(qty), "unit_price": f"{price:.2f}", "amount": f"{amount:.2f}"})
    subtotal = sum(Decimal(line["amount"]) for line in lines)
    rate = rng.choice([20, 20, 20, 10, 1])
    tax = (subtotal * rate / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    has_po = rng.random() < 0.7
    return {
        "id": f"inv{idx:03d}",
        "header": {
            "supplier_name": rng.choice(SUPPLIERS),
            "supplier_tax_id": first9 + vkn_check_digit(first9),
            "invoice_number": f"{rng.choice(['EFA', 'GIB', 'ABC'])}2026{rng.randint(0, 999999999):09d}",
            "invoice_date": inv_date.isoformat(),
            "due_date": (inv_date + dt.timedelta(days=rng.choice([15, 30, 45, 60]))).isoformat(),
            "po_number": f"PO-{rng.randint(10000, 99999)}" if has_po else None,
            "currency": "TRY",
            "subtotal": f"{subtotal:.2f}",
            "tax_rate": str(rate),
            "tax_amount": f"{tax:.2f}",
            "total": f"{subtotal + tax:.2f}",
        },
        "buyer": rng.choice(BUYERS),
        "lines": lines,
    }


def _register_fonts() -> tuple[str, str]:
    for regular, bold in FONT_CANDIDATES:
        if Path(regular).exists():
            pdfmetrics.registerFont(TTFont("DocSans", regular))
            pdfmetrics.registerFont(TTFont("DocSans-Bold", bold if Path(bold).exists() else regular))
            return "DocSans", "DocSans-Bold"
    return "Helvetica", "Helvetica-Bold"


def render_pdf(inv: dict, path: Path, fonts: tuple[str, str]) -> None:
    regular, bold = fonts
    h = inv["header"]
    c = canvas.Canvas(str(path), pagesize=A4)
    W, H = A4
    left, right = 50, 330
    c.setFont(bold, 22)
    c.drawString(left, H - 70, "FATURA")

    c.setFont(bold, 11)
    y = H - 110
    c.drawString(left, y, f"Satıcı: {h['supplier_name']}")
    c.setFont(regular, 10)
    c.drawString(left, y - 16, "Adres: Atatürk Cad. No:12 İstanbul")
    c.drawString(left, y - 32, f"VKN: {h['supplier_tax_id']}")

    c.setFont(regular, 10)
    meta = [
        ("Fatura No:", h["invoice_number"]),
        ("Fatura Tarihi:", tr_date(dt.date.fromisoformat(h["invoice_date"]))),
        ("Vade Tarihi:", tr_date(dt.date.fromisoformat(h["due_date"]))),
    ]
    if h["po_number"]:
        meta.append(("Sipariş No:", h["po_number"]))
    for i, (label, value) in enumerate(meta):
        c.drawString(right, y - 16 * i, f"{label} {value}")

    y -= 90
    c.setFont(bold, 10)
    c.drawString(left, y, "Alıcı:")
    c.setFont(regular, 10)
    c.drawString(left + 40, y, inv["buyer"])

    # Line items table
    y -= 40
    cols = {"description": left, "quantity": 350, "unit_price": 450, "amount": W - 50}
    c.setFont(bold, 10)
    c.drawString(cols["description"], y, "Açıklama")
    c.drawRightString(cols["quantity"], y, "Miktar")
    c.drawRightString(cols["unit_price"], y, "Birim Fiyat")
    c.drawRightString(cols["amount"], y, "Tutar")
    c.line(left, y - 6, W - 50, y - 6)
    c.setFont(regular, 10)
    for line in inv["lines"]:
        y -= 22
        c.drawString(cols["description"], y, line["description"])
        c.drawRightString(cols["quantity"], y, line["quantity"])
        c.drawRightString(cols["unit_price"], y, tr_money(Decimal(line["unit_price"])))
        c.drawRightString(cols["amount"], y, tr_money(Decimal(line["amount"])))
    c.line(left, y - 10, W - 50, y - 10)

    y -= 40
    totals = [
        ("Ara Toplam", h["subtotal"], regular),
        (f"KDV (%{h['tax_rate']})", h["tax_amount"], regular),
        ("Genel Toplam", h["total"], bold),
    ]
    for label, value, font in totals:
        c.setFont(font, 11)
        c.drawString(330, y, label)
        c.drawRightString(W - 50, y, f"{tr_money(Decimal(value))} TL")
        y -= 20

    c.setFont(regular, 8)
    c.drawString(left, 60, "Bu belge DocFlow değerlendirmesi için üretilmiş örnek bir faturadır.")
    c.save()


def _degrade(img: Image.Image, rng: random.Random, rotate: float, blur: float, noise: float, shade: float) -> Image.Image:
    img = img.convert("L")
    if rotate:
        img = img.rotate(rng.uniform(-rotate, rotate), resample=Image.BICUBIC, expand=True, fillcolor=235)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    arr = np.asarray(img).astype(np.float32)
    if shade:
        h, w = arr.shape
        gx = np.linspace(1 - shade, 1, w)[None, :]
        gy = np.linspace(1, 1 - shade / 2, h)[:, None]
        arr = arr * gx * gy
    if noise:
        arr = arr + np.random.default_rng(rng.randint(0, 2**31)).normal(0, noise, arr.shape)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def render_variants(pdf_path: Path, stem: Path, rng: random.Random) -> None:
    with fitz.open(pdf_path) as pdf:
        page = pdf[0]

        def raster(dpi: int) -> Image.Image:
            pix = page.get_pixmap(dpi=dpi)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        _degrade(raster(200), rng, rotate=0.3, blur=0, noise=6, shade=0).save(stem.with_suffix(".scan.png"))
        _degrade(raster(130), rng, rotate=1.2, blur=0.6, noise=10, shade=0.25).save(
            stem.with_suffix(".photo.jpg"), quality=45
        )
        _degrade(raster(100), rng, rotate=2.0, blur=0.9, noise=14, shade=0.35).save(
            stem.with_suffix(".poor.jpg"), quality=25
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    fonts = _register_fonts()
    for i in range(1, args.count + 1):
        inv = make_invoice(rng, i)
        stem = args.out / inv["id"]
        pdf_path = stem.with_suffix(".digital.pdf")
        render_pdf(inv, pdf_path, fonts)
        render_variants(pdf_path, stem, rng)
        stem.with_suffix(".json").write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{args.count} invoices x 4 quality levels -> {args.out}")


if __name__ == "__main__":
    main()
