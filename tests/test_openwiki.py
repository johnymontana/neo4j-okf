"""OpenWiki adapter tests: reserved files, grounding extraction, the
`openwiki:` extension promotion, and run metadata — against a synthetic
wiki and the vendored real one (bundles/openwiki_self)."""

import json
from pathlib import Path

import pytest

from okf_graph.openwiki import (
    OpenWikiBundle,
    _path_mention,
    load_changes_json,
    parse_openwiki,
)

VENDORED = Path(__file__).resolve().parent.parent / "bundles" / "openwiki_self"


@pytest.fixture()
def synthetic_wiki(tmp_path: Path) -> Path:
    (tmp_path / "index.md").write_text('---\nokf_version: "0.1"\n---\n\n# Files\n')
    (tmp_path / "INSTRUCTIONS.md").write_text("User brief. Never a concept.\n")
    (tmp_path / "_plan.md").write_text("transient impact plan\n")
    (tmp_path / ".last-update.json").write_text(json.dumps({
        "updatedAt": "2026-08-06T08:57:36.644Z", "command": "update",
        "gitHead": "630eb9ec3fa22a4bed2d347fc3ea3a6a3bd22abc",
        "model": "test-model", "status": "complete", "language": "en",
    }))
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "index.md").write_text("# Files\n")
    (tmp_path / "agent" / "runtime.md").write_text(
        "---\n"
        "type: Technical documentation\n"
        "title: Agent runtime\n"
        "openwiki:\n"
        "  roles: [architecture]\n"
        "  source_paths: [src/agent/index.ts]\n"
        "  symbols: [createOpenWikiAgentGraph]\n"
        "  test_paths: [test/agent.test.ts]\n"
        "  invariants: [Non-chat runs never write outside openwiki/.]\n"
        "---\n\n"
        "The runtime lives in `src/agent/index.ts` and `src/agent/prompts/`.\n"
        "Config comes from `package.json`; see [workflow](workflow.md).\n\n"
        "```bash\n"
        "cat src/agent/never-counted-in-fences.ts\n"
        "```\n"
    )
    (tmp_path / "agent" / "workflow.md").write_text(
        "---\ntype: Reference\n---\n\nSee `src/agent/index.ts` again.\n")
    return tmp_path


def test_reserved_files_are_not_concepts(synthetic_wiki):
    owb = parse_openwiki(synthetic_wiki, "syn")
    ids = set(owb.pb.concepts)
    assert "syn:INSTRUCTIONS" not in ids
    assert "syn:_plan" not in ids
    assert not any(i.endswith(":index") or i.endswith("/index") for i in ids)
    assert ids == {"syn:agent/runtime", "syn:agent/workflow"}


def test_prose_and_declared_groundings_merge(synthetic_wiki):
    owb = parse_openwiki(synthetic_wiki, "syn")
    by_key = {(g.concept_uid, g.path): g for g in owb.groundings}
    g = by_key[("syn:agent/runtime", "src/agent/index.ts")]
    assert g.declared is True          # frontmatter source_paths
    assert g.mentions == 1             # prose mention counted once
    assert g.kind == "file"
    assert by_key[("syn:agent/runtime", "src/agent/prompts")].kind == "dir"
    assert by_key[("syn:agent/runtime", "package.json")].kind == "file"
    test_g = by_key[("syn:agent/runtime", "test/agent.test.ts")]
    assert test_g.kind == "test" and test_g.declared
    # fenced code never grounds
    assert ("syn:agent/runtime", "src/agent/never-counted-in-fences.ts") not in by_key


def test_extension_promotion_and_run_meta(synthetic_wiki):
    owb = parse_openwiki(synthetic_wiki, "syn")
    ext = {e.concept_uid: e for e in owb.extensions}["syn:agent/runtime"]
    assert ext.roles == ["architecture"]
    assert ext.symbols == ["createOpenWikiAgentGraph"]
    assert ext.invariants == ["Non-chat runs never write outside openwiki/."]
    assert owb.run is not None
    assert owb.run.git_head.startswith("630eb9ec")
    assert owb.run.status == "complete"


def test_wiki_internal_links_still_work(synthetic_wiki):
    owb = parse_openwiki(synthetic_wiki, "syn")
    links = {(l.source_uid, l.target_uid) for l in owb.pb.links}
    assert ("syn:agent/runtime", "syn:agent/workflow") in links


@pytest.mark.parametrize("raw,expected", [
    ("src/agent/index.ts", ("src/agent/index.ts", "file")),
    ("src/telemetry/", ("src/telemetry", "dir")),
    ("src/telemetry", ("src/telemetry", "dir")),
    ("src/agent/prompts/*.ts", ("src/agent/prompts", "dir")),
    ("src/cli.tsx#L3102", ("src/cli.tsx", "file")),
    ("package.json", ("package.json", "file")),
    ("AGENTS.md", ("AGENTS.md", "file")),
    ("npm install -g openwiki", None),
    ("https://example.com/a/b", None),
    ("--update", None),
    ("openwiki", None),
])
def test_path_mention(raw, expected):
    assert _path_mention(raw) == expected


# --- the vendored real wiki -------------------------------------------------

@pytest.mark.skipif(not VENDORED.exists(), reason="vendored bundle missing")
def test_vendored_bundle_parses():
    owb = parse_openwiki(VENDORED, "openwiki_self")
    assert isinstance(owb, OpenWikiBundle)
    stats = owb.stats()
    # 15 md files = 7 substantive pages + 7 index.md + INSTRUCTIONS.md
    assert stats["concepts"] == 7
    assert stats["links"] >= 10
    assert stats["groundings"] >= 150       # 390 raw mentions, deduped per page
    assert owb.run is not None and owb.run.git_head
    # INSTRUCTIONS.md excluded; every concept has the OKF-required type
    assert "openwiki_self:INSTRUCTIONS" not in owb.pb.concepts
    assert all(c.type for c in owb.pb.concepts.values())


@pytest.mark.skipif(not VENDORED.exists(), reason="vendored bundle missing")
def test_vendored_sidecars_align():
    changes = load_changes_json(VENDORED / ".git-changes.sample.json")
    owb = parse_openwiki(VENDORED, "openwiki_self")
    assert changes["since"] == owb.run.git_head
    repo_files = json.loads((VENDORED / ".repo-files.sample.json").read_text())["files"]
    grounded = {g.path for g in owb.groundings if g.kind == "file"}
    # most file-level groundings point at real repo files (drift stays small)
    real = grounded & set(repo_files)
    assert len(real) / max(len(grounded), 1) > 0.6
