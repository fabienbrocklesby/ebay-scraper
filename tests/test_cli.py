import click
import pytest

from scraper.cli import _parse_store_lines


def test_parse_store_lines_mixed_niches():
    text = """
# comment line ignored
https://www.ebay.com.au/str/storeone, watches

https://www.ebay.com.au/str/storetwo
https://www.ebay.com/str/storethree,car-parts
"""
    out = _parse_store_lines(text, default_niche="default")
    assert len(out) == 3
    assert out[0][1] == "watches"      # line's own niche wins
    assert out[1][1] == "default"      # falls back to --niche default
    assert out[2][1] == "car-parts"
    assert all(u.startswith("http") for u, _ in out)


def test_parse_store_lines_requires_a_niche():
    with pytest.raises(click.ClickException):
        _parse_store_lines("https://www.ebay.com.au/str/x", default_niche=None)
