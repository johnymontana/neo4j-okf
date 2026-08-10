"""Documents -> an OKF bundle the parser accepts.

Everything here runs on the `heuristic` extractor: deterministic, no API key,
no network. The point is that the *pipeline* is testable end to end — the LLM
is a swappable stage, not a prerequisite for knowing the output is well-formed.
"""

from pathlib import Path

import pytest

from okf_graph.documents import load_paths
from okf_graph.parser import parse_bundle
from okf_graph.project import write_dir
from okf_graph.wiki import (WikiSpec, build_wiki, infer_type, propose_links,
                            safe_slug)

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "acme_intranet"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    docs = load_paths([CORPUS])
    build = build_wiki(docs, WikiSpec(name="acme_wiki", extractor="heuristic"))
    root = write_dir(build.files, tmp_path_factory.mktemp("wiki") / "acme_wiki")
    return build, parse_bundle(root, "acme_wiki"), root


def test_the_authored_bundle_parses_cleanly(built):
    _, pb, _ = built
    assert pb.warnings == []
    assert pb.okf_version == "0.2"
    # one extracted concept per document, plus a references/ mirror per document
    assert len(pb.concepts) == 10


def test_every_generated_concept_is_unverified_and_draft(built):
    """The demo's whole argument: machine-authored knowledge starts at the bottom."""
    _, pb, _ = built
    authored = [c for c in pb.concepts.values() if not c.id.startswith("references/")]
    assert authored
    for c in authored:
        assert c.trust_tier == "unverified", c.id      # SPEC §5.3, no `verified` key
        assert c.status == "draft"                     # §5.4
        assert c.generated_by is not None              # §5.2
        assert c.generated_by.kind == "process"


def test_claims_carry_footnote_level_provenance(built):
    """Sections cite the document they came from, not just the file (§5.1)."""
    _, pb, _ = built
    gm = pb.concepts["acme_wiki:metrics/gross-margin"]
    assert [s.sid for s in gm.sources] == ["gross-margin"]
    cited = [s for s in gm.sections if s.cites]
    assert cited, "no section carried a [^sid] citation"
    assert all(c in {s.uid for s in gm.sources} for sec in cited for c in sec.cites)


def test_sources_resolve_to_mirrored_references(built):
    """§6.3: mirroring the corpus makes provenance a traversal, not a string."""
    _, pb, _ = built
    gm = pb.concepts["acme_wiki:metrics/gross-margin"]
    src = gm.sources[0]
    assert src.resource == "references/gross-margin.md"
    assert src.resolves_to_concept == "acme_wiki:references/gross-margin"
    mirror = pb.concepts[src.resolves_to_concept]
    assert mirror.type == "Reference"
    assert mirror.resource.endswith("wiki-gross-margin.html")   # the original


def test_cross_references_become_real_links(built):
    """Prose mentions of other concepts have to survive as markdown."""
    _, pb, _ = built
    edges = {(l.source_uid.split(":", 1)[1], l.target_id) for l in pb.links}
    assert ("metrics/gross-margin", "policies/fy2026-cost-allocation-memo") in edges
    assert all(not l.raw.startswith("/") for l in pb.links), \
        "links must be file-relative so they render on GitHub"


def test_unwritten_targets_become_a_queryable_backlog(built):
    """SPEC §6.1: a link to knowledge nobody wrote is legal, and useful."""
    _, pb, _ = built
    backlog = {s.id for s in pb.stubs.values()}
    assert "dashboards/margin-daily-dashboard" in backlog
    assert "tables/customer-orders-table" in backlog
    # sentence-initial determiners are not proper nouns
    assert not any("this-" in s for s in backlog)


def test_the_wiki_contradicts_the_curated_bundle_and_says_so(built):
    """The corpus's wiki page states the retired formula. It must stay a draft."""
    _, pb, _ = built
    gm = pb.concepts["acme_wiki:metrics/gross-margin"]
    body = "\n".join(s.text for s in gm.sections)
    assert "revenue minus product cost" in body.lower()
    assert gm.trust_tier == "unverified" and gm.status == "draft"


# ---------------------------------------------------------------- unit-level

def test_slugs_avoid_reserved_names_and_collisions():
    taken: set[str] = set()
    assert safe_slug("index", "x", taken) == "index-concept"
    assert safe_slug("Revenue", "x", taken) == "revenue"
    assert safe_slug("revenue", "x", taken) == "revenue-2"     # case-insensitive
    assert safe_slug("REVENUE", "x", taken) == "revenue-3"


def test_type_inference_is_evidence_based():
    assert infer_type("Runbook: margin drop", "step 1 ... escalate") == "Runbook"
    assert infer_type("Data Dictionary", "one row per order, primary key") == "Table"
    assert infer_type("FY2026 Memo", "this policy is effective 2026 and supersedes") \
        == "Policy"


def test_link_proposals_skip_sentence_starts():
    assert propose_links("See the Margin Daily dashboard for details.") == \
        ["Margin Daily dashboard"]
    assert propose_links("This standard is reviewed annually.") == []
    assert propose_links("Follow the Margin Drop Triage runbook.") == \
        ["Margin Drop Triage runbook"]


def test_build_refuses_to_silently_lose_a_concept(monkeypatch):
    """The re-parse self-check is the guard against the whole silent-loss class."""
    import okf_graph.wiki as wiki

    docs = load_paths([CORPUS])[:2]
    # `log.md` is an OKF reserved filename: the file gets written, and the
    # parser then skips it — the concept disappears with no error anywhere.
    monkeypatch.setattr(wiki, "safe_slug", lambda raw, fb, taken: "log")
    with pytest.raises(RuntimeError, match="survive a re-parse"):
        build_wiki(docs, WikiSpec(name="w", extractor="heuristic",
                                  mirror_sources=False))
