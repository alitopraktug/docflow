import pytest

from docflow.extract import parse_code, parse_date, parse_money, parse_tax_id, vkn_is_valid


@pytest.mark.parametrize(
    "text, value, issues",
    [
        ("1.234,56", "1234.56", []),
        ("1,234.56", "1234.56", []),
        ("1234,5", "1234.50", ["malformed_number"]),
        ("149.712,10 TL", "149712.10", []),
        ("₺ 99,90", "99.90", []),
        ("1.2O4,56", "1204.56", ["ocr_char_substitution"]),
        ("12.34,56", "1234.56", ["malformed_number"]),
        ("abc", None, ["not_a_number"]),
    ],
)
def test_parse_money(text, value, issues):
    assert parse_money(text) == (value, issues)


def test_parse_date():
    assert parse_date("01.06.2026") == ("2026-06-01", [])
    assert parse_date("2026-06-01") == ("2026-06-01", [])
    assert parse_date("31.02.2026") == (None, ["invalid_date"])
    assert parse_date("O1.06.2026") == ("2026-06-01", ["ocr_char_substitution"])


def test_vkn_checksum_catches_single_digit_misread():
    assert vkn_is_valid("1043321815")
    assert not vkn_is_valid("1043321816")
    assert parse_tax_id("1043321818") == ("1043321818", ["checksum_failed"])


def test_codes_with_odd_characters_are_flagged():
    assert parse_code("PO-16658") == ("PO-16658", [])
    assert parse_code("PO-1665$") == ("PO-1665$", ["unexpected_character"])
