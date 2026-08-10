"""Document acquisition: normalization, and the safety rules for fetching.

No network is touched. The fetch tests exercise `_check_host`, which is the
gate every request passes through, on both the original URL and each redirect.
"""

from pathlib import Path

import pytest

from okf_graph.documents import (Document, FetchPolicy, FetchError,
                                 _check_host, body_without_title,
                                 html_to_markdown, load_paths, slugify)

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "acme_intranet"


@pytest.fixture(scope="module")
def docs():
    return {d.slug: d for d in load_paths([CORPUS])}


def test_the_sample_corpus_loads_in_every_format(docs):
    assert len(docs) == 5
    media = {d.media_type for d in docs.values()}
    assert media == {"text/html", "text/markdown", "text/plain"}


def test_html_boilerplate_is_stripped_and_headings_survive(docs):
    page = docs["gross-margin"]
    assert page.title == "Gross Margin"          # the <h1>, not the site-branded <title>
    assert "Acme Analytics Wiki ·" not in page.text        # footer gone
    assert "/wiki/oncall" not in page.text                 # nav gone
    assert "## How we calculate it" in page.text
    assert "gross_margin(period) = revenue(period) - product_cost(period)" in page.text


def test_html_metadata_becomes_okf_provenance(docs):
    page = docs["gross-margin"]
    assert page.author == "human:dpark@acme"     # <meta name=author>
    assert page.last_modified == "2025-11-04"    # article:modified_time, not file mtime
    assert page.sha256 and page.retrieved_at


def test_html_tables_become_pipe_tables(docs):
    table = docs["data-dictionary-acme-sales-orders"].text
    assert "| Column | Type | Notes |" in table
    assert "| order_id | STRING | Primary key. |" in table


def test_plain_text_underlined_headings_are_recovered(docs):
    """A README's `====` underline is real structure; losing it flattens the doc."""
    readme = docs["acme-data-warehouse-readme"]
    assert readme.title == "ACME DATA WAREHOUSE - README"
    assert "## Customer Orders" in readme.text
    assert "## Freshness and gotchas" in readme.text


def test_heading_levels_are_normalized_so_sections_exist():
    """The parser splits on `# ` only, so an h2-structured page must be shifted."""
    _, md, _ = html_to_markdown(
        "<html><body><h2>Alpha</h2><p>a</p><h2>Beta</h2><p>b</p></body></html>")
    assert md.count("\n# ") + md.startswith("# ") >= 2
    assert "## " not in md


def test_body_without_title_promotes_the_documents_own_sections(docs):
    body = body_without_title(docs["gross-margin"])
    assert not body.startswith("# Gross Margin")
    assert "# How we calculate it" in body
    assert "## How we calculate it" not in body


def test_slugify_is_path_safe():
    assert slugify("Data Dictionary: acme.sales.orders") == "data-dictionary-acme-sales-orders"
    assert slugify("../../etc/passwd") == "etc-passwd"
    assert slugify("") == "document"


# ---------------------------------------------------------------- fetch gate

@pytest.mark.parametrize("url,reason", [
    ("http://169.254.169.254/latest/meta-data/", "cloud metadata endpoint"),
    ("http://127.0.0.1/", "loopback"),
    ("http://[::ffff:127.0.0.1]/", "IPv4-mapped IPv6 loopback"),
    ("http://localhost:8888/api/sessions", "non-standard port"),
    ("https://user:secret@example.com/", "credentials in URL"),
    ("file:///etc/passwd", "non-http scheme"),
    ("ftp://example.com/x", "non-http scheme"),
])
def test_fetch_refuses_dangerous_urls(url, reason):
    with pytest.raises(FetchError):
        _check_host(url, FetchPolicy())


def test_a_public_url_passes_the_gate():
    _check_host("https://example.com/handbook", FetchPolicy())


def test_private_hosts_can_be_opted_into():
    _check_host("http://127.0.0.1:8888/", FetchPolicy(allow_private_hosts=True))
