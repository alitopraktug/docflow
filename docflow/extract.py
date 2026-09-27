"""Rule-based extraction for purchase documents (invoices / purchase orders).

The extractor never returns a bare value: each candidate carries the raw
text OCR produced, the word-level confidence, the page and bounding box it
came from, and a list of issues found while parsing. Deciding whether a value
can be trusted happens later, in :mod:`docflow.validate`.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from decimal import Decimal, InvalidOperation

from .ocr import Line, group_lines, union_bbox

HEADER_FIELDS = [
    "supplier_name",
    "supplier_tax_id",
    "invoice_number",
    "invoice_date",
    "due_date",
    "po_number",
    "currency",
    "subtotal",
    "tax_rate",
    "tax_amount",
    "total",
]
LINE_COLUMNS = ["description", "quantity", "unit_price", "amount"]

FIELD_LABELS_TR = {
    "supplier_name": "Satıcı",
    "supplier_tax_id": "Vergi No (VKN)",
    "invoice_number": "Belge No",
    "invoice_date": "Belge Tarihi",
    "due_date": "Vade Tarihi",
    "po_number": "Sipariş No",
    "currency": "Para Birimi",
    "subtotal": "Ara Toplam",
    "tax_rate": "KDV Oranı (%)",
    "tax_amount": "KDV Tutarı",
    "total": "Genel Toplam",
    "description": "Açıklama",
    "quantity": "Miktar",
    "unit_price": "Birim Fiyat",
    "amount": "Tutar",
}

# Synonyms are matched against ASCII-folded, lower-cased text. Order matters:
# more specific labels first.
LABELS: dict[str, list[str]] = {
    "supplier_name": ["satici", "tedarikci", "supplier", "vendor", "seller"],
    "supplier_tax_id": ["vergi kimlik no", "vergi no", "vkn", "tax id", "tax no"],
    "invoice_number": ["fatura no", "belge no", "invoice number", "invoice no"],
    "invoice_date": ["fatura tarihi", "belge tarihi", "invoice date", "tarih", "date"],
    "due_date": ["son odeme tarihi", "vade tarihi", "due date"],
    "po_number": ["siparis no", "purchase order no", "po number", "po no"],
    "currency": ["para birimi", "currency"],
    "subtotal": ["ara toplam", "sub total", "subtotal"],
    "tax_amount": ["kdv", "vat", "tax"],
    "total": ["genel toplam", "odenecek tutar", "grand total", "toplam", "total"],
}
FIELD_TYPES = {
    "supplier_name": "text",
    "supplier_tax_id": "tax_id",
    "invoice_number": "code",
    "invoice_date": "date",
    "due_date": "date",
    "po_number": "code",
    "currency": "currency",
    "subtotal": "money",
    "tax_amount": "money",
    "total": "money",
    "tax_rate": "percent",
    "description": "text",
    "quantity": "number",
    "unit_price": "money",
    "amount": "money",
}
TABLE_HEADER_WORDS = {
    "aciklama", "urun", "hizmet", "description", "item",
    "miktar", "adet", "qty", "quantity",
    "birim", "fiyat", "unit", "price",
    "tutar", "amount",
}
TABLE_END_LABELS = ["ara toplam", "subtotal", "sub total", "genel toplam", "grand total", "kdv", "vat", "toplam", "total"]

# Characters Tesseract commonly confuses with digits.
DIGIT_CONFUSIONS = {"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "|": "1", "i": "1",
                    "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "G": "6", "g": "9"}
CURRENCY_TOKENS = {"tl": "TRY", "try": "TRY", "₺": "TRY", "usd": "USD", "$": "USD", "eur": "EUR", "€": "EUR"}

SEGMENT_GAP = 0.035  # horizontal gap (fraction of page width) that separates label/value columns


def fold(text: str) -> str:
    """Lower-case and strip Turkish/Latin diacritics: 'Satıcı' -> 'satici'."""
    text = text.replace("ı", "i").replace("İ", "i").replace("I", "i")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def _clean_token(text: str) -> str:
    return re.sub(r"[:;]+$", "", fold(text)).strip("()[]")


@dataclass
class Candidate:
    name: str
    kind: str = "header"
    row_index: int | None = None
    value: str | None = None
    raw_text: str | None = None
    ocr_conf: float = 0.0
    page: int | None = None
    bbox: list[float] | None = None
    issues: list[str] = field(default_factory=list)
    words: list[dict] = field(default_factory=list, repr=False)

    @classmethod
    def from_words(cls, name: str, words: list[dict], value: str | None, issues: list[str], **kw) -> "Candidate":
        if FIELD_TYPES.get(name) == "text" and any(_ambiguous(w) for w in words):
            issues = [*issues, "ambiguous_characters"]
        return cls(
            name=name,
            value=value,
            raw_text=" ".join(w["text"] for w in words),
            ocr_conf=min(w["conf"] for w in words) / 100.0,
            page=words[0]["page"],
            bbox=union_bbox(words),
            issues=list(issues),
            words=list(words),
            **kw,
        )


def _ambiguous(word: dict) -> bool:
    """A product code like '8SA' vs '85A': digits next to look-alike letters, read without
    high confidence. Free text has no checksum or arithmetic to catch this, so ask a human."""
    text = word["text"]
    return (
        word["conf"] < 96
        and any(c.isdigit() for c in text)
        and any(c in DIGIT_CONFUSIONS and not c.isdigit() for c in text)
    )


# --------------------------------------------------------------------------
# Value parsers: return (normalised value | None, issues)
# --------------------------------------------------------------------------


def _fix_digit_confusions(token: str) -> tuple[str, bool]:
    fixed = "".join(DIGIT_CONFUSIONS.get(c, c) for c in token)
    return fixed, fixed != token


def parse_money(text: str) -> tuple[str | None, list[str]]:
    issues: list[str] = []
    raw = text.strip()
    for sym in ("₺", "$", "€"):
        raw = raw.replace(sym, "")
    raw = re.sub(r"(?i)\b(tl|try|usd|eur)\b", "", raw).replace(" ", "").strip()
    if not raw:
        return None, ["not_a_number"]
    # Only repair confusions when the token is otherwise numeric-looking.
    if sum(c.isdigit() for c in raw) >= max(1, len(raw) // 2):
        raw, changed = _fix_digit_confusions(raw)
        if changed:
            issues.append("ocr_char_substitution")
    if raw.startswith("-"):
        issues.append("negative_value")
        raw = raw[1:]
    if not re.fullmatch(r"[\d.,]+", raw) or not any(c.isdigit() for c in raw):
        return None, ["not_a_number"]
    m = re.search(r"[.,](\d{1,2})$", raw)
    if m:
        integer_part, decimals = raw[: m.start()], m.group(1).ljust(2, "0")
        if len(m.group(1)) == 1:
            issues.append("malformed_number")
    else:
        integer_part, decimals = raw, "00"
    groups = re.split(r"[.,]", integer_part)
    if len(groups) > 1 and (any(len(g) != 3 for g in groups[1:]) or not groups[0] or len(groups[0]) > 3):
        issues.append("malformed_number")
    digits = "".join(groups)
    if not digits:
        return None, ["not_a_number"]
    try:
        value = Decimal(f"{int(digits)}.{decimals}")
    except (InvalidOperation, ValueError):
        return None, ["not_a_number"]
    return f"{value:.2f}", issues


def parse_number(text: str) -> tuple[str | None, list[str]]:
    value, issues = parse_money(text)
    if value is None:
        return None, issues
    d = Decimal(value)
    return (str(int(d)) if d == int(d) else str(d.normalize())), issues


DATE_PATTERNS = [
    (re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})"), ("d", "m", "y")),
    (re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})"), ("y", "m", "d")),
]


def parse_date(text: str) -> tuple[str | None, list[str]]:
    issues: list[str] = []
    fixed, changed = _fix_digit_confusions(text.replace(" ", ""))
    if changed:
        issues.append("ocr_char_substitution")
    for pattern, order in DATE_PATTERNS:
        m = pattern.search(fixed)
        if m:
            parts = dict(zip(order, (int(g) for g in m.groups())))
            try:
                return dt.date(parts["y"], parts["m"], parts["d"]).isoformat(), issues
            except ValueError:
                return None, issues + ["invalid_date"]
    return None, ["not_a_date"]


def vkn_is_valid(vkn: str) -> bool:
    """Turkish tax id (VKN) checksum. A single misread digit breaks it."""
    if not re.fullmatch(r"\d{10}", vkn):
        return False
    total = 0
    for i in range(9):
        tmp = (int(vkn[i]) + (9 - i)) % 10
        tmp2 = (tmp * 2 ** (9 - i)) % 9
        if tmp != 0 and tmp2 == 0:
            tmp2 = 9
        total += tmp2
    return (10 - total % 10) % 10 == int(vkn[9])


def parse_tax_id(text: str) -> tuple[str | None, list[str]]:
    fixed, changed = _fix_digit_confusions(text.replace(" ", ""))
    issues = ["ocr_char_substitution"] if changed else []
    m = re.search(r"\d{10,11}", fixed)
    if not m:
        return None, ["not_a_tax_id"]
    value = m.group(0)
    if len(value) == 10 and not vkn_is_valid(value):
        issues.append("checksum_failed")
    return value, issues


def parse_code(text: str) -> tuple[str | None, list[str]]:
    token = text.strip().split(" ")[0].strip(":#")
    if not re.search(r"\d", token):
        return None, ["not_a_code"]
    # Document numbers use letters, digits and a few separators. Anything else
    # ('PO-1665$') is almost certainly a misread character.
    return token, ([] if re.fullmatch(r"[A-Za-z0-9./_-]+", token) else ["unexpected_character"])


def parse_text(text: str) -> tuple[str | None, list[str]]:
    value = text.strip(" :")
    return (value or None), ([] if value else ["empty"])


def parse_currency(text: str) -> tuple[str | None, list[str]]:
    for tok in re.split(r"\s+", text.strip()):
        code = CURRENCY_TOKENS.get(fold(tok).strip("().:"))
        if code:
            return code, []
    return None, ["unknown_currency"]


PARSERS = {
    "money": parse_money,
    "number": parse_number,
    "date": parse_date,
    "tax_id": parse_tax_id,
    "code": parse_code,
    "text": parse_text,
    "currency": parse_currency,
}


# --------------------------------------------------------------------------
# Layout helpers
# --------------------------------------------------------------------------


def split_segments(line: Line) -> list[list[dict]]:
    """Split a visual line into column segments at large horizontal gaps."""
    segments: list[list[dict]] = [[line.words[0]]]
    for prev, word in zip(line.words, line.words[1:]):
        if word["bbox"][0] - prev["bbox"][2] > SEGMENT_GAP:
            segments.append([])
        segments[-1].append(word)
    return segments


def _label_match(words: list[dict], synonym: str) -> int:
    """Return how many leading words of ``words`` form ``synonym`` (0 = no match).

    Fuzzy, so 'Fatura N0:' or 'Satici' still match despite OCR noise.
    """
    lead = 0
    while lead < len(words) and not any(ch.isalnum() for ch in words[lead]["text"]):
        lead += 1  # skip OCR debris such as '-' or '|' before a label
    words = words[lead:]
    n = len(synonym.split())
    if len(words) < n:
        return 0
    head = " ".join(_clean_token(w["text"]) for w in words[:n])
    if head == synonym or (len(synonym) >= 5 and SequenceMatcher(None, head, synonym).ratio() >= 0.84):
        return lead + n
    return 0


def _find_value(
    lines: list[Line], li: int, si: int, segments: list[list[dict]], rest: list[dict], ftype: str
) -> tuple[str | None, list[dict], list[str]]:
    """Look for a parseable value right of the label, then in the next segments, then below."""
    parser = PARSERS[ftype]
    candidates: list[list[dict]] = []
    if rest:
        candidates.append(rest)
    candidates.extend(segments[si + 1 : si + 2])
    if li + 1 < len(lines):
        label_box = union_bbox(segments[si])
        below = lines[li + 1]
        for seg in split_segments(below):
            box = union_bbox(seg)
            if box[0] < label_box[2] + 0.1 and box[2] > label_box[0] and box[1] - label_box[3] < 0.03:
                candidates.append(seg)
                break
    last_issues: list[str] = ["not_found"]
    for words in candidates:
        words = [w for w in words if w["text"].strip(":") != ""]
        if not words:
            continue
        if ftype in ("money", "number"):
            words = _numeric_tail(words)
            if not words:
                continue
        value, issues = parser(" ".join(w["text"] for w in words))
        if value is not None:
            return value, words, issues
        last_issues = issues
    return None, [], last_issues


def _looks_numeric(text: str) -> bool:
    t = text.strip("₺$€")
    if fold(t) in CURRENCY_TOKENS:
        return False
    digits = sum(c.isdigit() for c in t)
    return digits > 0 and digits >= (len(t) - t.count(".") - t.count(",")) * 0.5


def _numeric_tail(words: list[dict]) -> list[dict]:
    """Keep the trailing run of numeric-looking words (drops '(%20)', 'TL', etc.)."""
    words = [w for w in words if fold(w["text"]).strip("().") not in CURRENCY_TOKENS and "%" not in w["text"]]
    tail: list[dict] = []
    for w in reversed(words):
        if _looks_numeric(w["text"]):
            tail.insert(0, w)
        elif tail:
            break
    return tail


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


def extract(words: list[dict]) -> list[Candidate]:
    lines = group_lines(words)
    table_start, table_end = _find_table(lines)
    header_lines = [ln for i, ln in enumerate(lines) if not (table_start is not None and table_start <= i < table_end)]

    results: dict[str, Candidate] = {}
    for name, synonyms in LABELS.items():
        cand = _extract_labeled(lines, header_lines, name, synonyms)
        if cand:
            results[name] = cand

    _extract_tax_rate(lines, results)
    if "currency" not in results:
        _infer_currency(lines, results)

    out = [results.get(name) or Candidate(name=name, issues=["not_found"]) for name in HEADER_FIELDS]
    if table_start is not None:
        out.extend(_extract_line_items(lines[table_start + 1 : table_end]))
    return out


def _extract_labeled(lines: list[Line], header_lines: list[Line], name: str, synonyms: list[str]) -> Candidate | None:
    ftype = FIELD_TYPES[name]
    header_ids = {id(ln) for ln in header_lines}
    unreadable: Candidate | None = None
    for synonym in synonyms:
        for li, line in enumerate(lines):
            if id(line) not in header_ids:
                continue
            segments = split_segments(line)
            for si, seg in enumerate(segments):
                n = _label_match(seg, synonym)
                if not n:
                    continue
                if name == "total" and _label_match(seg, "ara toplam"):
                    continue
                value, words, issues = _find_value(lines, li, si, segments, seg[n:], ftype)
                if value is None:
                    unreadable = unreadable or Candidate.from_words(name, seg[:n], None, ["value_unreadable"])
                    continue
                return Candidate.from_words(name, words, value, issues)
    # The label is on the page but no value next to it could be parsed: point the
    # reviewer at the label instead of pretending the field does not exist.
    return unreadable


def _extract_tax_rate(lines: list[Line], results: dict[str, Candidate]) -> None:
    anchor = results.get("tax_amount")
    for line in lines:
        if anchor and line.page == anchor.page and not (line.bbox[1] <= (anchor.bbox[1] + anchor.bbox[3]) / 2 <= line.bbox[3]):
            continue
        if not any(fold(x["text"]).startswith(("kdv", "vat", "tax")) for x in line.words):
            continue
        for w in line.words:
            m = re.search(r"%\s*(\d{1,2})\b|\b(\d{1,2})\s*%", w["text"])
            if m:
                results["tax_rate"] = Candidate.from_words("tax_rate", [w], m.group(1) or m.group(2), [])
                return
            # Tesseract often reads '%' as '9o', '90' or '96': '(%20)' -> '(9020)'.
            m = re.fullmatch(r"\((?:9[06o]?|o/o|°/o)(\d{1,2})\)", w["text"])
            if m:
                results["tax_rate"] = Candidate.from_words("tax_rate", [w], m.group(1), ["ocr_char_substitution"])
                return


def _infer_currency(lines: list[Line], results: dict[str, Candidate]) -> None:
    """No explicit currency label: look at standalone currency tokens (e.g. '1.250,00 TL')."""
    found: list[tuple[str, dict]] = []
    for line in lines:
        for w in line.words:
            token = w["text"]
            code = CURRENCY_TOKENS.get(fold(token).strip("().:,"))
            if code is None:
                for sym, c in (("₺", "TRY"), ("€", "EUR"), ("$", "USD")):
                    # A symbol counts only when attached to an amount ('$1,250.00'),
                    # not when it is debris inside a code ('PO-1665$').
                    if sym in token and re.fullmatch(r"[₺$€]?[\d.,]+[₺$€]?", token):
                        code = c
            if code:
                found.append((code, w))
    if not found:
        return
    codes = {code for code, _ in found}
    code, word = max(found, key=lambda f: sum(1 for c, _ in found if c == f[0]))
    issues = [] if len(codes) == 1 else ["conflicting_currency"]
    results["currency"] = Candidate.from_words("currency", [word], code, issues)


def _find_table(lines: list[Line]) -> tuple[int | None, int]:
    for i, line in enumerate(lines):
        tokens = {_clean_token(w["text"]) for w in line.words}
        if len(tokens & TABLE_HEADER_WORDS) >= 3:
            end = len(lines)
            for j in range(i + 1, len(lines)):
                segs = split_segments(lines[j])
                if any(_label_match(seg, lab) for seg in segs for lab in TABLE_END_LABELS):
                    end = j
                    break
            return i, end
    return None, len(lines)


def _merge_numeric_fragments(words: list[dict]) -> list[dict]:
    """OCR sometimes splits '1.250,00' into '1.250,' + '00'; stitch them back."""
    merged: list[dict] = []
    for w in words:
        if merged:
            prev = merged[-1]
            gap = w["bbox"][0] - prev["bbox"][2]
            if gap < 0.006 and _looks_numeric(prev["text"]) and _looks_numeric(w["text"]) and (
                prev["text"][-1] in ".," or w["text"][0] in ".,"
            ):
                merged[-1] = {
                    **prev,
                    "text": prev["text"] + w["text"],
                    "conf": min(prev["conf"], w["conf"]),
                    "bbox": union_bbox([prev, w]),
                }
                continue
        merged.append(w)
    return merged


def _extract_line_items(rows: list[Line]) -> list[Candidate]:
    items: list[dict[str, Candidate]] = []
    for line in rows:
        words = _merge_numeric_fragments(
            [w for w in line.words if fold(w["text"]).strip("().") not in CURRENCY_TOKENS]
        )
        numeric: list[dict] = []
        for w in reversed(words):
            if _looks_numeric(w["text"]) and len(numeric) < 3:
                numeric.insert(0, w)
            else:
                break
        desc_words = words[: len(words) - len(numeric)]
        while desc_words and not any(ch.isalnum() for ch in desc_words[-1]["text"]):
            desc_words.pop()  # trailing OCR debris like '.' or '|'
        while desc_words and not any(ch.isalnum() for ch in desc_words[0]["text"]):
            desc_words.pop(0)
        if len(numeric) < 2:
            # A wrapped description line continues the previous row.
            if items and desc_words:
                prev = items[-1]["description"]
                all_words = [*prev.words, *desc_words]
                cand = Candidate.from_words("description", all_words, " ".join(w["text"] for w in all_words), [],
                                            kind="line", row_index=prev.row_index)
                items[-1]["description"] = cand
            continue
        row_index = len(items)
        row: dict[str, Candidate] = {}
        cols = ["quantity", "unit_price", "amount"][-len(numeric):]
        for col, w in zip(cols, numeric):
            value, issues = PARSERS[FIELD_TYPES[col]](w["text"])
            row[col] = Candidate.from_words(col, [w], value, issues if value else issues + ["not_found"],
                                            kind="line", row_index=row_index)
        for col in ["quantity", "unit_price", "amount"]:
            row.setdefault(col, Candidate(name=col, kind="line", row_index=row_index, issues=["not_found"]))
        if desc_words:
            cand = Candidate.from_words("description", desc_words, " ".join(w["text"] for w in desc_words), [],
                                        kind="line", row_index=row_index)
        else:
            cand = Candidate(name="description", kind="line", row_index=row_index, issues=["not_found"])
        row["description"] = cand
        items.append(row)
    return [row[col] for row in items for col in LINE_COLUMNS]
