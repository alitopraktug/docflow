"""Page rasterisation and word-level text recognition.

Every recognised word keeps its page, a normalised bounding box and a
confidence, so each extracted value can later be traced back to the exact
pixels it came from. Digital PDFs use their embedded text layer (no OCR
guessing); scans and photos go through Tesseract.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz
import numpy as np
import pytesseract
from PIL import Image, ImageFilter, ImageOps

PDF_TEXT_CONFIDENCE = 99.0
MIN_TEXT_LAYER_CHARS = 40


@dataclass
class RenderedPage:
    page_no: int
    image_path: Path
    width: int
    height: int
    text_words: list[dict] | None  # words from the PDF text layer, if any


def rasterize(file_path: Path, content_type: str, out_dir: Path, dpi: int) -> list[RenderedPage]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if content_type == "application/pdf" or file_path.suffix.lower() == ".pdf":
        return _rasterize_pdf(file_path, out_dir, dpi)
    return [_normalise_image(file_path, out_dir)]


def _rasterize_pdf(file_path: Path, out_dir: Path, dpi: int) -> list[RenderedPage]:
    pages: list[RenderedPage] = []
    with fitz.open(file_path) as pdf:
        for index, page in enumerate(pdf, start=1):
            pix = page.get_pixmap(dpi=dpi)
            image_path = out_dir / f"page-{index}.png"
            pix.save(image_path)
            words = _pdf_text_words(page, index)
            pages.append(RenderedPage(index, image_path, pix.width, pix.height, words))
    return pages


def _pdf_text_words(page: "fitz.Page", page_no: int) -> list[dict] | None:
    w, h = page.rect.width, page.rect.height
    raw = page.get_text("words")
    if sum(len(item[4]) for item in raw) < MIN_TEXT_LAYER_CHARS:
        return None  # scanned PDF: no usable text layer, fall back to OCR
    return [
        {
            "text": text,
            "conf": PDF_TEXT_CONFIDENCE,
            "page": page_no,
            "bbox": [x0 / w, y0 / h, x1 / w, y1 / h],
        }
        for x0, y0, x1, y1, text, *_ in raw
        if text.strip()
    ]


def _normalise_image(file_path: Path, out_dir: Path) -> RenderedPage:
    with Image.open(file_path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        image_path = out_dir / "page-1.png"
        img.save(image_path)
        return RenderedPage(1, image_path, img.width, img.height, None)


def flatten_illumination(gray: Image.Image) -> Image.Image:
    """Divide by a blurred background estimate so shadows and uneven phone lighting
    do not push whole regions below Tesseract's global threshold."""
    arr = np.asarray(gray, dtype=np.float32)
    background = np.asarray(gray.filter(ImageFilter.GaussianBlur(radius=max(gray.width // 40, 10))), dtype=np.float32)
    flat = np.clip(arr / np.maximum(background, 1) * 255, 0, 255).astype(np.uint8)
    return Image.fromarray(flat)


def estimate_skew(gray: Image.Image, max_angle: float = 4.0) -> float:
    """Projection-profile deskew: text lines are sharpest when perfectly horizontal."""
    small = gray.copy()
    small.thumbnail((900, 900))
    ink = Image.fromarray(((np.asarray(small) < 160) * 255).astype(np.uint8))

    def score(angle: float) -> float:
        rows = np.asarray(ink.rotate(angle, resample=Image.BILINEAR), dtype=np.float32).sum(axis=1)
        return float(np.sum(np.diff(rows) ** 2))

    coarse = max(np.arange(-max_angle, max_angle + 0.01, 0.25), key=score)
    fine = max(np.arange(coarse - 0.25, coarse + 0.26, 0.05), key=score)
    return round(float(fine), 2)


def preprocess(img: Image.Image) -> Image.Image:
    """Cheap clean-up that helps Tesseract on phone photos and low-DPI scans."""
    gray = flatten_illumination(ImageOps.grayscale(img))
    if gray.width < 1800:
        scale = 2000 / gray.width
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.LANCZOS)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    return gray.filter(ImageFilter.MedianFilter(3))


def deskew_page(image_path: Path) -> float:
    """Straighten the stored page image in place, so OCR boxes and the image the
    reviewer sees share one coordinate system. Returns the applied angle."""
    with Image.open(image_path) as img:
        img.load()
    angle = estimate_skew(flatten_illumination(ImageOps.grayscale(img)))
    if abs(angle) >= 0.1:
        img.rotate(angle, resample=Image.BICUBIC, fillcolor="white").save(image_path)
    return angle


def ocr_image(image_path: Path, page_no: int, lang: str) -> list[dict]:
    with Image.open(image_path) as img:
        prepared = preprocess(img)
    w, h = prepared.size
    data = pytesseract.image_to_data(
        prepared, lang=lang, config="--psm 4", output_type=pytesseract.Output.DICT
    )
    words = []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue
        x, y, bw, bh = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append(
            {
                "text": text,
                "conf": conf,
                "page": page_no,
                "bbox": [x / w, y / h, (x + bw) / w, (y + bh) / h],
            }
        )
    return words


def available_langs(requested: str) -> str:
    """Use only the Tesseract languages that are actually installed."""
    try:
        installed = set(pytesseract.get_languages(config=""))
    except Exception:  # pragma: no cover - tesseract missing
        return "eng"
    langs = [lang for lang in requested.split("+") if lang in installed]
    return "+".join(langs) or "eng"


# --------------------------------------------------------------------------
# Line reconstruction
# --------------------------------------------------------------------------


@dataclass
class Line:
    page: int
    words: list[dict]

    @property
    def text(self) -> str:
        return " ".join(w["text"] for w in self.words)

    @property
    def bbox(self) -> list[float]:
        return union_bbox(self.words)

    @property
    def y_center(self) -> float:
        b = self.bbox
        return (b[1] + b[3]) / 2


def union_bbox(words: list[dict]) -> list[float]:
    return [
        min(w["bbox"][0] for w in words),
        min(w["bbox"][1] for w in words),
        max(w["bbox"][2] for w in words),
        max(w["bbox"][3] for w in words),
    ]


def group_lines(words: list[dict]) -> list[Line]:
    """Cluster words into visual lines by vertical overlap.

    Tesseract's own block/line numbering splits table columns into separate
    blocks; rebuilding lines geometrically keeps a table row together.
    """
    lines: list[Line] = []
    for page in sorted({w["page"] for w in words}):
        page_words = [w for w in words if w["page"] == page]
        if not page_words:
            continue
        heights = [w["bbox"][3] - w["bbox"][1] for w in page_words]
        tolerance = statistics.median(heights) * 0.55
        page_words.sort(key=lambda w: ((w["bbox"][1] + w["bbox"][3]) / 2, w["bbox"][0]))
        current: list[dict] = []
        current_y = None
        for word in page_words:
            yc = (word["bbox"][1] + word["bbox"][3]) / 2
            if current and abs(yc - current_y) > tolerance:
                lines.append(Line(page, sorted(current, key=lambda w: w["bbox"][0])))
                current = []
            current.append(word)
            current_y = yc if current_y is None or len(current) == 1 else (
                current_y * (len(current) - 1) + yc
            ) / len(current)
        if current:
            lines.append(Line(page, sorted(current, key=lambda w: w["bbox"][0])))
    return lines
