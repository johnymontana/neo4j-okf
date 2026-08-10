"""parse -> emit -> parse must give back an equivalent model.

This is the property the projection rests on. It is *equivalence*, not byte
equality — `emit.ROUNDTRIP_NOTES` enumerates every place the text legitimately
changes — so the assertions compare the parsed model, and separately check that
a second pass is a fixed point (emit∘parse is idempotent from pass two).

No Neo4j required: the graph is not in this loop.
"""

from pathlib import Path

import pytest

from okf_graph.emit import (EmitCollision, dump_yaml, relative_link,
                            render_bundle, safe_bundle_path)
from okf_graph.parser import (ParsedBundle, ParsedConcept, ParsedLogEntry,
                              parse_bundle)

BUNDLES = Path(__file__).resolve().parents[1] / "bundles"


def _write(files: dict[str, str], root: Path) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _fingerprint(pb: ParsedBundle) -> dict:
    """Everything the graph would be built from, in a comparable shape."""
    return {
        "stats": pb.stats(),
        "concepts": {
            c.uid: (c.type, c.title, c.description, c.resource, c.status,
                    c.stale_after, tuple(c.tags), c.trust_tier, c.runtime,
                    tuple(c.receipt), c.parameters_json, c.generated_at,
                    c.executor_resource, c.attester_resource, c.computation_path,
                    tuple((s.heading, s.text, tuple(s.cites)) for s in c.sections),
                    tuple((s.sid, s.resource, s.title, s.author, s.last_modified,
                           s.usage_count, s.resolves_to_concept) for s in c.sources),
                    tuple((a.id, at) for a, at in c.verified))
            for c in pb.concepts.values()
        },
        "links": sorted((l.source_uid, l.target_uid, l.section, l.text, l.resolved)
                        for l in pb.links),
        "stubs": sorted(pb.stubs),
        "log": sorted((e.date, e.text, tuple(e.references)) for e in pb.log_entries),
        "artifacts": {a["path"]: a.get("text") for a in pb.artifacts.values()},
    }


@pytest.mark.parametrize("name", ["acme_retail", "demo_extras"])
def test_bundle_survives_a_round_trip(name, tmp_path):
    original = parse_bundle(BUNDLES / name, name)
    once = parse_bundle(_write(render_bundle(original), tmp_path / "1" / name), name)
    assert _fingerprint(once) == _fingerprint(original)


@pytest.mark.parametrize("name", ["acme_retail", "demo_extras"])
def test_emit_is_idempotent_from_the_second_pass(name, tmp_path):
    """Text equality is only promised once the first normalization has happened."""
    original = parse_bundle(BUNDLES / name, name)
    first = render_bundle(original)
    second = render_bundle(parse_bundle(_write(first, tmp_path / "1" / name), name))
    assert second == first


def test_the_attester_source_survives_the_round_trip(tmp_path):
    """A bundle carries code, and a projection that drops it is not portable."""
    original = parse_bundle(BUNDLES / "acme_retail", "acme_retail")
    root = _write(render_bundle(original), tmp_path / "acme_retail")
    assert (root / "attesters" / "sql_equality.py").exists()
    assert "def " in (root / "attesters" / "sql_equality.py").read_text()
    # unreferenced files count too — acme's own viewer is linked from nothing
    assert (root / "viz.html").exists()


def test_unknown_frontmatter_keys_are_written_back(tmp_path):
    original = parse_bundle(BUNDLES / "acme_retail", "acme_retail")
    files = render_bundle(original)
    # acme ships a custom `not:` family — SPEC §11 says consumers must not
    # reject it, which means producers must not drop it
    assert "not:" in files["metrics/gross-margin.md"]
    assert "revenue minus product cost only" in files["metrics/gross-margin.md"]


# ---------------------------------------------------------------- unit-level

def test_relative_link_never_produces_a_bare_dot_slash():
    assert relative_link("", "") == ""
    assert relative_link("metrics", "") == ""
    assert relative_link("metrics", "metrics/x.md") == "./x.md"
    assert relative_link("metrics", "policies/y.md") == "../policies/y.md"
    assert relative_link("", "policies/y.md") == "policies/y.md"
    assert relative_link("a/b", "a/c/d.md") == "../c/d.md"


def test_a_root_level_concept_does_not_invent_a_computation_family():
    """`./` is falsy-looking but truthy — it used to mint attester edges."""
    c = ParsedConcept(uid="b:x", id="x", bundle="b", path="x.md", dir="",
                      type="Note", type_label="Note", title="X")
    text = render_bundle(ParsedBundle(name="b", root="", okf_version=None,
                                      concepts={"b:x": c}))["x.md"]
    for absent in ("computation:", "executor:", "attester:"):
        assert absent not in text


@pytest.mark.parametrize("bad", [
    "../escape.md", "/etc/passwd", "a/../../b.md", "", "  ", "a//b.md",
    "nul.md", "C:/windows/x.md",
])
def test_unsafe_bundle_paths_are_refused(bad):
    with pytest.raises(ValueError):
        safe_bundle_path(bad)


def test_two_concepts_claiming_one_path_is_an_error_not_an_overwrite():
    pb = ParsedBundle(name="b", root="", okf_version=None)
    for uid in ("b:one", "b:two"):
        pb.concepts[uid] = ParsedConcept(
            uid=uid, id="dup", bundle="b", path="dup.md", dir="",
            type="Note", type_label="Note", title=uid)
    with pytest.raises(EmitCollision):
        render_bundle(pb)


def test_a_concept_named_log_cannot_shadow_the_reserved_file():
    pb = ParsedBundle(name="b", root="", okf_version=None)
    pb.concepts["b:log"] = ParsedConcept(
        uid="b:log", id="log", bundle="b", path="log.md", dir="",
        type="Note", type_label="Note", title="Log")
    pb.log_entries.append(ParsedLogEntry(
        uid="b:log:0", date="2026-01-01", kind=None, text="**Update** x", order=0))
    with pytest.raises(EmitCollision):
        render_bundle(pb)


def test_dump_yaml_keeps_declared_key_order():
    out = dump_yaml({"type": "Metric", "title": "T", "status": "stable"})
    assert out.splitlines()[0].startswith("type:")
    assert out.splitlines()[1].startswith("title:")
