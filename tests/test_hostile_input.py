"""Regression tests from the adversarial review: hostile-but-conformant input
must never crash parse_bundle (SPEC §11 — tolerate, don't reject), and parsed
values destined for Cypher date()/datetime() must be validated or None."""

from okf_graph.parser import parse_bundle


def _bundle(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_garbage_datetimes_become_none(tmp_path):
    _bundle(tmp_path, {"a.md": (
        "---\ntype: Metric\ntimestamp: May 2026\n"
        "generated: { by: agent/1, at: last Tuesday }\n"
        "verified: { by: human:x, at: not-a-date }\n---\nBody.\n")})
    a = parse_bundle(tmp_path, "syn").concepts["syn:a"]
    assert a.generated_at is None
    assert a.verified[0][1] is None          # actor kept, garbage date dropped
    assert a.trust_tier == "human-reviewed"


def test_calendar_invalid_dates_become_none(tmp_path):
    _bundle(tmp_path, {
        "a.md": ("---\ntype: Metric\nstale_after: '2026-13-45'\n"
                 "sources:\n  - {id: s, resource: 'https://x', last_modified: '2026-99-99'}\n"
                 "---\nBody.\n"),
        "log.md": "# Log\n\n## 2026-13-01\n* **Update**: bad month.\n",
    })
    pb = parse_bundle(tmp_path, "syn")
    a = pb.concepts["syn:a"]
    assert a.stale_after is None
    assert a.sources[0].last_modified is None
    assert all(e.date for e in pb.log_entries) or not pb.log_entries


def test_yaml_dates_in_parameters_and_keys(tmp_path):
    _bundle(tmp_path, {"c.md": (
        "---\ntype: Attested Computation\nruntime: bigquery\n"
        "parameters:\n  - { name: start, type: date, required: true, default: 2026-01-01 }\n"
        "2026-01-01: launch day\n---\n# Computation\n```sql\nSELECT 1\n```\n")})
    c = parse_bundle(tmp_path, "syn").concepts["syn:c"]
    assert "2026-01-01" in c.parameters_json
    assert "2026-01-01" in (c.extra_frontmatter or "")


def test_non_string_resources_tolerated(tmp_path):
    _bundle(tmp_path, {"c.md": (
        "---\ntype: Attested Computation\nruntime: bigquery\n"
        "executor: { resource: 123 }\nattester: { resource: [a.py, b.py] }\n"
        "computation: { nested: oops }\nresource: { url: x }\ndescription: [a, b]\n"
        "---\nBody.\n")})
    c = parse_bundle(tmp_path, "syn").concepts["syn:c"]
    assert c.executor_resource is None and c.attester_resource is None
    assert c.computation_path is None
    assert isinstance(c.resource, str) and isinstance(c.description, str)


def test_string_tags_do_not_explode_per_char(tmp_path):
    _bundle(tmp_path, {"a.md": "---\ntype: Note\ntags: alpha, beta gamma\n---\nB.\n"})
    a = parse_bundle(tmp_path, "syn").concepts["syn:a"]
    assert a.tags == ["alpha", "beta gamma"]


def test_bom_hidden_dirs_and_protocol_relative(tmp_path):
    _bundle(tmp_path, {
        "a.md": "﻿---\ntype: BomNote\ntitle: Bommed\n---\nSee [x](//cdn.example.com/x.md).\n",
        ".github/PULL_REQUEST_TEMPLATE.md": "---\ntype: Junk\n---\nnope\n",
    })
    pb = parse_bundle(tmp_path, "syn")
    assert pb.concepts["syn:a"].type == "BomNote"        # BOM did not break frontmatter
    assert not any(".github" in uid for uid in pb.concepts)
    assert not pb.stubs                                   # //-links are external


def test_nested_fences(tmp_path):
    body = ("---\ntype: Note\n---\n# Real\n\n"
            "````markdown\n```sql\nSELECT 1\n```\n# not a heading\n````\ntail\n")
    _bundle(tmp_path, {"a.md": body})
    a = parse_bundle(tmp_path, "syn").concepts["syn:a"]
    assert [s.heading for s in a.sections] == ["Real"]


def test_duplicate_links_deduped_in_stats(tmp_path):
    _bundle(tmp_path, {
        "a.md": "---\ntype: Note\n---\n# S\n[one](/b.md) and again [one](/b.md).\n",
        "b.md": "---\ntype: Note\n---\nB.\n",
    })
    pb = parse_bundle(tmp_path, "syn")
    assert pb.stats()["links"] == 1          # matches what MERGE/CREATE will write


def test_log_backtick_references(tmp_path):
    _bundle(tmp_path, {
        "m.md": "---\ntype: Metric\n---\nB.\n",
        "log.md": "# L\n\n## 2026-01-05\n* **Update**: re-generated `m.md` overnight.\n",
    })
    pb = parse_bundle(tmp_path, "syn")
    assert pb.log_entries[0].references == ["syn:m"]
