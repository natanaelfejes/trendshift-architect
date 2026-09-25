"""Offline smoke test. Run: python tests/test_scraper.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scraper import BlockDetected, is_block_page, parse_snapshots_dir, parse_view_html, read_html_file

ROOT = Path(__file__).resolve().parent.parent


def test_parses_sample_site():
    sample = next((ROOT / "sample_sites").glob("Weekly*.mhtml"))
    entries = parse_view_html(read_html_file(sample))
    assert len(entries) > 0, "expected at least one repo parsed from the sample site"
    assert all("/" in e["name"] for e in entries)


def test_block_page_raises():
    fixture_dir = Path(__file__).resolve().parent / "fixtures"
    try:
        parse_snapshots_dir(fixture_dir)
        raise AssertionError("expected BlockDetected on a Cloudflare challenge page")
    except BlockDetected:
        pass


if __name__ == "__main__":
    test_parses_sample_site()
    test_block_page_raises()
    print("OK")
