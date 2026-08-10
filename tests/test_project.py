"""Projection tests — these need a live Neo4j (docker compose up -d).

Skipped automatically when no database answers, so `uv run pytest -q` stays
green on a laptop with nothing running. Run them with the stack up to check the
full loop: parse -> ingest -> project -> emit -> parse.
"""

from pathlib import Path

import pytest

from okf_graph.emit import render_bundle
from okf_graph.parser import parse_bundle
from okf_graph.project import (ProjectionSpec, project, project_bundle,
                               write_dir, write_tar, write_zip)

BUNDLES = Path(__file__).resolve().parents[1] / "bundles"
BUNDLE = "test_acme_projection"        # its own name, so a real demo DB is untouched


@pytest.fixture(scope="module")
def writer():
    from dotenv import load_dotenv
    load_dotenv()
    import neo4j

    from okf_graph.ingest import GraphWriter, get_driver

    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception as exc:                       # noqa: BLE001 - any failure = skip
        pytest.skip(f"no Neo4j available: {type(exc).__name__}: {exc}")

    w = GraphWriter(driver)
    w.reset(BUNDLE)
    w.ingest(parse_bundle(BUNDLES / "acme_retail", BUNDLE))
    yield w
    w.reset(BUNDLE)
    driver.close()


def _fingerprint(pb):
    return {
        "stats": pb.stats(),
        "concepts": {
            c.id: (c.type, c.title, c.status, c.stale_after, c.trust_tier,
                   tuple((s.heading, s.text) for s in c.sections),
                   tuple((s.sid, s.resource, s.title, s.author, s.last_modified)
                         for s in c.sources),
                   tuple((a.id, at) for a, at in c.verified))
            for c in pb.concepts.values()
        },
        "links": sorted((l.source_uid, l.target_uid, l.section) for l in pb.links),
        "artifacts": {a["path"]: a.get("text") for a in pb.artifacts.values()},
    }


def test_the_full_loop_is_lossless(writer, tmp_path):
    """parse -> ingest -> project -> emit -> parse returns the same model."""
    original = parse_bundle(BUNDLES / "acme_retail", BUNDLE)
    result = project(writer, ProjectionSpec(bundle=BUNDLE))
    again = parse_bundle(write_dir(result.files, tmp_path / BUNDLE), BUNDLE)
    assert _fingerprint(again) == _fingerprint(original)


def test_the_projection_matches_a_pure_emit(writer, tmp_path):
    """Going through the graph must not differ from serializing the parse."""
    direct = render_bundle(parse_bundle(BUNDLES / "acme_retail", BUNDLE))
    via_graph = project(writer, ProjectionSpec(bundle=BUNDLE)).files
    via_graph.pop(".okf/projection.json", None)
    assert via_graph == direct


def test_governance_filters_select_what_is_servable(writer):
    """The serve/no-serve verdict, as a bundle rather than a report."""
    pb, stats = project_bundle(writer, ProjectionSpec(
        bundle=BUNDLE, statuses=["stable"], min_trust="human-reviewed"))
    ids = {c.id for c in pb.concepts.values()}
    assert "metrics/gross-margin" in ids
    assert "metrics/gross-margin-legacy" not in ids      # deprecated
    assert stats["selected"] == len(pb.concepts)


def test_the_consumer_contract_is_never_left_dangling(writer):
    """A filter must not cut the executor out from under a computation (§10.5).

    `skills/run-on-bq` is unverified, so `min_trust=human-reviewed` drops it —
    but two Attested Computations name it as their executor, and a bundle that
    ships them without it asserts a contract nobody can follow.
    """
    pb, _ = project_bundle(writer, ProjectionSpec(
        bundle=BUNDLE, min_trust="human-reviewed"))
    ids = {c.id for c in pb.concepts.values()}
    assert "computations/gross-margin-period" in ids
    assert "skills/run-on-bq" in ids
    assert "attesters/sql_equality.py" in {a["path"] for a in pb.artifacts.values()}


def test_a_seed_neighbourhood_is_a_context_pack(writer):
    pb, _ = project_bundle(writer, ProjectionSpec(
        bundle=BUNDLE, seed="metrics/gross-margin", hops=1, name="pack"))
    ids = {c.id for c in pb.concepts.values()}
    assert "metrics/gross-margin" in ids
    assert "computations/gross-margin-period" in ids       # one link hop
    assert "tables/orders" not in ids                      # two hops away
    assert all(c.bundle == "pack" for c in pb.concepts.values())


def test_excluded_link_targets_are_reported_not_hidden(writer):
    result = project(writer, ProjectionSpec(bundle=BUNDLE, statuses=["stable"]))
    dangling = result.manifest["dangling_links"]
    assert any(d["to"] == "metrics/gross-margin-legacy" for d in dangling)
    assert all(d["reason"] for d in dangling)


def test_per_citation_source_metadata_survives_deduplication(writer):
    """Source nodes dedupe by resource; the declared title must still be per-edge."""
    pb, _ = project_bundle(writer, ProjectionSpec(bundle=BUNDLE))
    citing = [c for c in pb.concepts.values()
              if any(s.resource == "policies/revenue-recognition.md"
                     for s in c.sources)]
    assert len(citing) >= 2
    for c in citing:
        src = next(s for s in c.sources
                   if s.resource == "policies/revenue-recognition.md")
        assert src.title == "Revenue Recognition Policy (FY2026)"
        assert src.author == "human:jsmith@acme"


def test_archives_carry_the_whole_bundle_under_one_root(writer, tmp_path):
    import tarfile
    import zipfile

    files = project(writer, ProjectionSpec(bundle=BUNDLE)).files
    tar = write_tar(files, tmp_path / "b.tar.gz", arcroot="b")
    zip_ = write_zip(files, tmp_path / "b.zip", arcroot="b")
    with tarfile.open(tar) as t:
        names = t.getnames()
    with zipfile.ZipFile(zip_) as z:
        assert sorted(names) == sorted(z.namelist())
    assert all(n.startswith("b/") for n in names)
    assert "b/metrics/gross-margin.md" in names
