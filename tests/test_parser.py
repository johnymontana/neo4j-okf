"""Parser smoke tests — run with `uv run pytest -q`. No Neo4j required."""

from pathlib import Path

import pytest

from okf_graph.parser import parse_bundle, split_sections, type_to_label

ACME = Path(__file__).resolve().parents[1] / "bundles" / "acme_retail"


@pytest.fixture(scope="module")
def acme():
    return parse_bundle(ACME, "acme_retail")


def test_acme_counts(acme):
    stats = acme.stats()
    assert stats["concepts"] == 9
    assert stats["stub_concepts"] == 0          # every acme link resolves
    assert stats["links"] >= 14
    assert stats["log_entries"] == 4
    # every non-markdown file, referenced or not: the attester + OKF's own viewer
    assert stats["artifacts"] == 2
    assert {a["path"] for a in acme.artifacts.values()} == {
        "attesters/sql_equality.py", "viz.html"}


def test_trust_tiers(acme):
    gm = acme.concepts["acme_retail:metrics/gross-margin"]
    assert gm.trust_tier == "human-reviewed"
    assert gm.status == "stable"
    skill = acme.concepts["acme_retail:skills/run-on-bq"]
    assert skill.trust_tier == "unverified"
    legacy = acme.concepts["acme_retail:metrics/gross-margin-legacy"]
    assert legacy.status == "deprecated"


def test_type_labels(acme):
    assert acme.concepts["acme_retail:tables/orders"].type_label == "BigQueryTable"
    assert type_to_label("Attested Computation") == "AttestedComputation"
    assert type_to_label("weird/type!! name") == "WeirdTypeName"
    assert type_to_label("") == "Concept"


def test_attested_computation_wiring(acme):
    comp = acme.concepts["acme_retail:computations/gross-margin-period"]
    assert comp.runtime == "bigquery"
    assert comp.receipt == ["job_id", "executed_sql", "result"]
    # executor resolved to the Skill concept uid (root-relative fallback)
    assert comp.executor_resource == "acme_retail:skills/run-on-bq"
    # attester resolved to a code artifact
    assert comp.attester_resource == "acme_retail:art:attesters/sql_equality.py"
    assert comp.attester_resource in acme.artifacts


def test_sources_and_citations(acme):
    gm = acme.concepts["acme_retail:metrics/gross-margin"]
    by_sid = {s.sid: s for s in gm.sources}
    assert by_sid["margin-standard"].resolves_to_concept == \
        "acme_retail:policies/margin-standard"
    # the Definition section cites margin-standard via footnote
    definition = next(s for s in gm.sections if s.heading == "Definition")
    assert by_sid["margin-standard"].uid in definition.cites


def test_unknown_frontmatter_preserved(acme):
    gm = acme.concepts["acme_retail:metrics/gross-margin"]
    assert gm.extra_frontmatter and "not" in gm.extra_frontmatter


# ---------------------------------------------------------------- synthetic

def _write_bundle(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_v01_timestamp_fallback_and_bare_verified(tmp_path):
    _write_bundle(tmp_path, {
        "a.md": "---\ntype: Metric\ntimestamp: '2025-01-02T03:04:05+00:00'\n"
                "verified: {by: human:x, at: 2025-02-01T00:00:00Z}\n---\nBody.\n",
    })
    pb = parse_bundle(tmp_path, "syn")
    a = pb.concepts["syn:a"]
    assert a.generated_at is not None and a.generated_at.startswith("2025-01-02")
    assert len(a.verified) == 1                # bare mapping == 1-element list
    assert a.trust_tier == "human-reviewed"


def test_broken_link_becomes_stub(tmp_path):
    _write_bundle(tmp_path, {
        "a.md": "---\ntype: Note\n---\nSee [future](/plans/future.md).\n",
    })
    pb = parse_bundle(tmp_path, "syn")
    assert "syn:plans/future" in pb.stubs
    assert pb.links[0].resolved is False


def test_fenced_heading_is_not_a_section():
    body = "intro\n\n# Real\n\n```sql\n# not a heading\nSELECT 1;\n```\ntail\n"
    headings = [h for h, _, _ in split_sections(body)]
    assert headings == ["_preamble", "Real"]


def test_footnote_definition_alone_is_not_a_citation(tmp_path):
    _write_bundle(tmp_path, {
        "a.md": ("---\ntype: Note\nsources:\n  - {id: s1, resource: 'https://x'}\n---\n"
                 "# One\nA claim.[^s1]\n\n# Two\n[^s1]: the definition text only\n"),
    })
    pb = parse_bundle(tmp_path, "syn")
    a = pb.concepts["syn:a"]
    one = next(s for s in a.sections if s.heading == "One")
    two = next(s for s in a.sections if s.heading == "Two")
    assert one.cites and not two.cites
