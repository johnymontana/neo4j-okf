"""openwiki_graph_mcp — the retrieval MCP OpenWiki never shipped.

A stdio MCP server over an ingested OpenWiki bundle (see `okf-graph
ingest-wiki`). OpenWiki's own eval harness (evals/deepswe/ upstream) probes
for an `openwiki-retrieval-mcp` exposing `search` and `change_surface`;
nothing in their source implements it. This server does, backed by the Neo4j
context graph instead of agentic file search — so every answer carries the
structure a grep cannot see: which code a page documents, what links to it,
and whether it is stale against the wiki's own git watermark.

Run:            uv run okf-graph-mcp
Register:       claude mcp add openwiki-graph -- uv --directory /path/to/neo4j-okf run okf-graph-mcp

Environment (.env is honored): NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD,
NEO4J_DATABASE, plus optional OPENWIKI_BUNDLE (default bundle name) and
OPENWIKI_REPO_ROOT (documented repo checkout, enables live `git diff`).

All tools are read-only.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

# First-run "property/relationship type does not exist" notices are expected
# on optional structure (invariants, symbols); keep them off stderr.
logging.getLogger("neo4j").setLevel(logging.ERROR)
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

import neo4j
from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from . import openwiki
from .ingest import FULLTEXT_INDEX, GraphWriter, get_driver

mcp = MCPServer(
    "openwiki_graph_mcp",
    instructions=(
        "Graph-backed retrieval over OpenWiki-generated wikis ingested into "
        "Neo4j. Start with wiki_list_bundles, search with wiki_search, and use "
        "wiki_change_surface before editing code to see which documented "
        "behavior a change touches. All tools are read-only."
    ),
)

_READ_ONLY = dict(read_only_hint=True, destructive_hint=False,
                  idempotent_hint=True, open_world_hint=False)

_STATE: dict = {}


def _writer() -> GraphWriter:
    if "writer" not in _STATE:
        load_dotenv()
        driver = get_driver()
        driver.verify_connectivity()
        _STATE["driver"] = driver
        _STATE["writer"] = GraphWriter(driver)
    return _STATE["writer"]


def _bundles(w: GraphWriter) -> list[dict]:
    return [dict(r) for r in w.run(
        """MATCH (b:Bundle)
           OPTIONAL MATCH (b)<-[:PRODUCED]-(r:WikiRun)
           WITH b, r ORDER BY r.updated_at DESC
           WITH b, collect(r)[0] AS run
           RETURN b.name AS name, b.okf_version AS okf_version,
                  run.git_head AS git_head, toString(run.updated_at) AS updated_at,
                  COUNT { (c:Concept {bundle: b.name}) WHERE NOT coalesce(c.stub, false) }
                      AS pages""").records]


def _resolve_bundle(w: GraphWriter, bundle: Optional[str]) -> str:
    """Explicit param > OPENWIKI_BUNDLE env > the only bundle in the DB."""
    if bundle:
        return bundle
    env = os.getenv("OPENWIKI_BUNDLE")
    if env:
        return env
    names = [b["name"] for b in _bundles(w)]
    if len(names) == 1:
        return names[0]
    raise ValueError(
        f"multiple bundles in the graph ({', '.join(sorted(names)) or 'none'}); "
        "pass bundle=<name>, set OPENWIKI_BUNDLE, or ingest one with "
        "`okf-graph ingest-wiki <wiki_dir>`")


def _snippet(text: str, limit: int = 380) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _err(e: Exception) -> str:
    if isinstance(e, neo4j.exceptions.ServiceUnavailable):
        return ("Error: cannot reach Neo4j. Start it (`docker compose up -d`) "
                "and check NEO4J_URI in .env.")
    if isinstance(e, neo4j.exceptions.AuthError):
        return "Error: Neo4j authentication failed. Check NEO4J_USERNAME/NEO4J_PASSWORD in .env."
    if isinstance(e, ValueError):
        return f"Error: {e}"
    return f"Error: {type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

class SearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., min_length=2, max_length=300,
                       description="Search terms, e.g. 'update no-op check' or 'telemetry error taxonomy'")
    bundle: Optional[str] = Field(default=None, description="Bundle name; omit when only one wiki is ingested")
    limit: int = Field(default=5, ge=1, le=20, description="Max section hits")


class PageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    page_id: str = Field(..., min_length=1, max_length=300,
                         description="Page id = wiki path without .md, e.g. 'agent/workflow'")
    bundle: Optional[str] = Field(default=None, description="Bundle name; omit when only one wiki is ingested")
    include_body: bool = Field(default=False,
                               description="True returns full section texts; default returns headings + first lines")


class ChangeSurfaceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    paths: list[str] = Field(..., min_length=1, max_length=500,
                             description="Changed repo-relative paths, e.g. from `git diff --name-only`")
    bundle: Optional[str] = Field(default=None, description="Bundle name; omit when only one wiki is ingested")


class StalePagesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    bundle: Optional[str] = Field(default=None, description="Bundle name; omit when only one wiki is ingested")
    changed_paths: Optional[list[str]] = Field(
        default=None, max_length=500,
        description="Changed paths since the wiki's gitHead. Omit to derive them live from "
                    "`git diff` in repo_root (or OPENWIKI_REPO_ROOT).")
    repo_root: Optional[str] = Field(default=None,
                                     description="Checkout of the documented repo, for live git diff")


class CoverageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    bundle: Optional[str] = Field(default=None, description="Bundle name; omit when only one wiki is ingested")
    path_prefix: str = Field(default="src/", max_length=200,
                             description="Only report files under this prefix, e.g. 'src/'")
    repo_root: Optional[str] = Field(default=None,
                                     description="Checkout for live `git ls-files`; falls back to the "
                                                 "bundle's vendored .repo-files.sample.json")


# --------------------------------------------------------------------------
# Helpers shared by tools
# --------------------------------------------------------------------------

def _current_files(w: GraphWriter, bundle: str, repo_root: Optional[str]) -> list[str]:
    root = repo_root or os.getenv("OPENWIKI_REPO_ROOT")
    if root:
        return openwiki.git_ls_files(root)
    # Bundle.root persists only the ingest directory's *basename* (absolute
    # paths are machine-local state and deliberately kept out of the graph),
    # so probe the conventional locations relative to the server's cwd for a
    # vendored .repo-files.sample.json sidecar.
    rec = w.run("MATCH (b:Bundle {name: $b}) RETURN b.root AS root", b=bundle).records
    basename = (rec[0]["root"] if rec and rec[0]["root"] else bundle)
    for cand in (Path(basename), Path("bundles") / basename):
        sidecar = cand / ".repo-files.sample.json"
        if sidecar.exists():
            return json.loads(sidecar.read_text())["files"]
    raise ValueError("no repo file list available — pass repo_root (a checkout of the "
                     "documented repo) or set OPENWIKI_REPO_ROOT")


def _changed_paths(w: GraphWriter, bundle: str, paths: Optional[list[str]],
                   repo_root: Optional[str]) -> tuple[list[str], str]:
    if paths:
        return paths, "provided by caller"
    root = repo_root or os.getenv("OPENWIKI_REPO_ROOT")
    if not root:
        raise ValueError("no changed_paths given and no repo_root/OPENWIKI_REPO_ROOT to "
                         "run `git diff` in — pass one of them")
    rec = w.run(
        """MATCH (r:WikiRun)-[:PRODUCED]->(:Bundle {name: $b})
           RETURN r.git_head AS head ORDER BY r.updated_at DESC LIMIT 1""",
        b=bundle).records
    if not rec or not rec[0]["head"]:
        raise ValueError(f"bundle '{bundle}' has no WikiRun git_head — re-ingest a wiki "
                         "that contains .last-update.json")
    head = rec[0]["head"]
    return (openwiki.git_changed_files(root, head),
            f"git diff {head[:8]}..HEAD in {root}")


def _grounding_lines(w: GraphWriter, bundle: str, page_id: str, top: int = 6) -> list[str]:
    recs = w.run(
        """MATCH (c:Concept {bundle: $b, id: $id})-[g:GROUNDED_IN]->(f:SourceFile)
           RETURN f.path AS path, f.kind AS kind, g.mentions AS mentions,
                  g.declared AS declared
           ORDER BY g.mentions DESC LIMIT $top""",
        b=bundle, id=page_id, top=top).records
    return [f"{r['path']}{'/' if r['kind'] == 'dir' else ''} (×{r['mentions']}"
            f"{', declared' if r['declared'] else ''})" for r in recs]


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool(
    name="wiki_list_bundles",
    annotations=ToolAnnotations(title="List ingested wikis", **_READ_ONLY),
)
def wiki_list_bundles() -> str:
    """List every wiki bundle in the graph with page count and git watermark.

    Start here when unsure what is ingested or which bundle name to pass.

    Returns:
        str: markdown table of bundles — name, okf_version, pages, the
        gitHead the wiki last documented, and when it was updated. Empty-state
        message with the ingest command when the graph has no bundles.
    """
    try:
        rows = _bundles(_writer())
        if not rows:
            return ("No bundles ingested yet. Run: "
                    "`okf-graph ingest-wiki <path-to-openwiki-dir> --embed`")
        out = ["| bundle | okf | pages | gitHead | updated |", "|---|---|---|---|---|"]
        for b in rows:
            out.append(f"| {b['name']} | {b['okf_version'] or '?'} | {b['pages']} "
                       f"| {(b['git_head'] or '')[:8] or '—'} | {b['updated_at'] or '—'} |")
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001 — every failure must reach the agent
        return _err(e)


@mcp.tool(
    name="wiki_search",
    annotations=ToolAnnotations(title="Search the wiki", **_READ_ONLY),
)
def wiki_search(params: SearchInput) -> str:
    """Full-text search over wiki sections; every hit carries its graph context.

    Prefer this over reading wiki files: a hit returns the owning page, the
    matched section snippet, the source files the page documents
    (GROUNDED_IN), and its link neighborhood — the context a plain grep
    cannot provide. Uses the Lucene fulltext index when present, falling
    back to a substring scan.

    Args:
        params (SearchInput): query (str), bundle (Optional[str]),
            limit (int, default 5, max 20).

    Returns:
        str: markdown — one block per hit: page id, section heading, score,
        snippet, `grounded in:` paths, `links:` out/in page ids. Or a
        "no hits" line, or "Error: …" with a remedy.
    """
    try:
        w = _writer()
        b = _resolve_bundle(w, params.bundle)
        try:
            recs = w.run(
                f"""CALL db.index.fulltext.queryNodes('{FULLTEXT_INDEX}', $q)
                    YIELD node, score
                    WHERE node.bundle = $b
                    RETURN node.uid AS uid, node.heading AS heading,
                           node.text AS text, score
                    LIMIT $k""",
                q=params.query, b=b, k=params.limit).records
        except neo4j.exceptions.ClientError:
            recs = w.run(
                """MATCH (s:Section {bundle: $b})
                   WHERE toLower(s.text) CONTAINS toLower($q)
                      OR toLower(s.heading) CONTAINS toLower($q)
                   RETURN s.uid AS uid, s.heading AS heading, s.text AS text,
                          1.0 AS score
                   LIMIT $k""",
                q=params.query, b=b, k=params.limit).records
        if not recs:
            return (f"No sections in bundle '{b}' match '{params.query}'. "
                    "Try broader terms, or wiki_get_page if you know the page id.")
        out = []
        for r in recs:
            page_id = r["uid"].split(":", 1)[1].split("#", 1)[0]
            block = [f"## {page_id} — §{r['heading']}  (score {r['score']:.2f})",
                     _snippet(r["text"])]
            grounds = _grounding_lines(w, b, page_id)
            if grounds:
                block.append("grounded in: " + "; ".join(grounds))
            links = w.run(
                """MATCH (c:Concept {bundle: $b, id: $id})
                   RETURN [(c)-[:LINKS_TO]->(t) WHERE NOT coalesce(t.stub,false) | t.id][..5] AS out,
                          [(c)<-[:LINKS_TO]-(f) | f.id][..5] AS inp""",
                b=b, id=page_id).records[0]
            if links["out"] or links["inp"]:
                block.append(f"links: out → {', '.join(links['out']) or '—'} · "
                             f"in ← {', '.join(links['inp']) or '—'}")
            out.append("\n".join(block))
        return "\n\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="wiki_get_page",
    annotations=ToolAnnotations(title="Get a wiki page with its graph context", **_READ_ONLY),
)
def wiki_get_page(params: PageInput) -> str:
    """Fetch one wiki page: metadata, sections, links both ways, groundings.

    Args:
        params (PageInput): page_id (str, wiki path sans .md — see
            wiki_list_bundles/wiki_search for ids), bundle (Optional[str]),
            include_body (bool — False returns headings with first lines,
            True returns full section texts).

    Returns:
        str: markdown — title/type/description/tags, `grounded in` paths with
        mention counts, invariants and documented symbols when the wiki
        declares them, links out (with the section that asserted each),
        backlinks, then the sections. "Error: page not found …" lists close
        ids when the id is wrong.
    """
    try:
        w = _writer()
        b = _resolve_bundle(w, params.bundle)
        recs = w.run(
            """MATCH (c:Concept {bundle: $b, id: $id})
               RETURN c.title AS title, c.type AS type, c.description AS description,
                      c.tags AS tags, coalesce(c.stub, false) AS stub""",
            b=b, id=params.page_id).records
        if not recs:
            frag = params.page_id.split("/")[-1]
            dirname = params.page_id.rsplit("/", 1)[0] if "/" in params.page_id else ""
            near = w.run(
                """MATCH (c:Concept {bundle: $b}) WHERE NOT coalesce(c.stub, false)
                   AND (c.id CONTAINS $frag OR ($dir <> '' AND c.id STARTS WITH $dir + '/'))
                   RETURN c.id AS id LIMIT 6""",
                b=b, frag=frag, dir=dirname).records
            hint = ("; similar ids: " + ", ".join(r["id"] for r in near)) if near else ""
            return f"Error: page '{params.page_id}' not found in bundle '{b}'{hint}."
        page = recs[0]
        if page["stub"]:
            return (f"'{params.page_id}' is a stub — linked from other pages but not "
                    "yet written (OpenWiki §6.1 not-yet-written knowledge).")
        out = [f"# {page['title']}  ({params.page_id})",
               f"type: {page['type']}" + (f" · tags: {', '.join(page['tags'])}" if page["tags"] else "")]
        if page["description"]:
            out.append(page["description"])
        grounds = _grounding_lines(w, b, params.page_id, top=10)
        if grounds:
            out.append("\ngrounded in: " + "; ".join(grounds))
        extras = w.run(
            """MATCH (c:Concept {bundle: $b, id: $id})
               RETURN [(c)-[:HAS_INVARIANT]->(i) | i.text] AS invariants,
                      [(c)-[:DOCUMENTS]->(s) | s.name] AS symbols,
                      [(c)-[:VALIDATED_BY]->(t) | t.path] AS tests""",
            b=b, id=params.page_id).records[0]
        for label, vals in (("invariants", extras["invariants"]),
                            ("documents symbols", extras["symbols"]),
                            ("validated by", extras["tests"])):
            if vals:
                out.append(f"{label}: " + "; ".join(vals))
        links = w.run(
            """MATCH (c:Concept {bundle: $b, id: $id})
               OPTIONAL MATCH (c)-[l:LINKS_TO]->(t:Concept)
               WITH c, collect({to: t.id, section: l.section}) AS outs
               OPTIONAL MATCH (c)<-[:LINKS_TO]-(f:Concept)
               RETURN outs, collect(DISTINCT f.id) AS ins""",
            b=b, id=params.page_id).records[0]
        outs = [o for o in links["outs"] if o["to"]]
        if outs:
            out.append("links out: " + "; ".join(f"{o['to']} (§{o['section']})" for o in outs))
        if links["ins"]:
            out.append("linked from: " + ", ".join(links["ins"]))
        secs = w.run(
            """MATCH (:Concept {bundle: $b, id: $id})-[:HAS_SECTION]->(s:Section)
               RETURN s.heading AS heading, s.text AS text ORDER BY s.order""",
            b=b, id=params.page_id).records
        out.append("")
        for s in secs:
            if params.include_body:
                out.append(f"## {s['heading']}\n{s['text']}")
            else:
                out.append(f"## {s['heading']}\n{_snippet(s['text'], 200)}")
        if not params.include_body:
            out.append("\n(include_body=true for full section texts)")
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="wiki_change_surface",
    annotations=ToolAnnotations(title="Changed files → affected wiki pages", **_READ_ONLY),
)
def wiki_change_surface(params: ChangeSurfaceInput) -> str:
    """Map changed repo files to the wiki pages grounded in them — the impact
    plan OpenWiki derives and deletes on every update run, as one query.

    Use before editing code (which documented behavior am I touching?) or
    before trusting a page (was its subject just changed?). Ranked by how
    hard each page leans on the changed files; each page also lists the
    downstream pages that link to it.

    Args:
        params (ChangeSurfaceInput): paths (list[str], repo-relative, e.g.
            from `git diff --name-only`), bundle (Optional[str]).

    Returns:
        str: markdown table — page, weight (grounding mentions hit),
        changed files matched, downstream pages. Or "no wiki page is
        grounded in any of these paths".
    """
    try:
        w = _writer()
        b = _resolve_bundle(w, params.bundle)
        recs = w.run(openwiki.WIKI_IMPACT_QUERY, paths=params.paths, bundle=b).records
        if not recs:
            return (f"No wiki page in '{b}' is grounded in any of the {len(params.paths)} "
                    "given paths — this change is undocumented territory "
                    "(see wiki_coverage_gaps).")
        out = [f"{len(params.paths)} changed path(s) → {len(recs)} affected page(s)", "",
               "| page | weight | changed files hit | downstream pages |", "|---|---|---|---|"]
        for r in recs:
            out.append(f"| {r['page']} | {r['weight']} | "
                       f"{', '.join(r['changed_files'][:6])} | "
                       f"{', '.join(r['downstream_pages'][:5]) or '—'} |")
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="wiki_stale_pages",
    annotations=ToolAnnotations(title="Per-page staleness verdicts", **_READ_ONLY),
)
def wiki_stale_pages(params: StalePagesInput) -> str:
    """Judge each wiki page against the wiki's own git watermark.

    OpenWiki records one gitHead for the whole wiki; this tool turns that
    into per-page verdicts: a page is suspect iff it is grounded in a file
    changed since that commit.

    Args:
        params (StalePagesInput): bundle (Optional[str]); changed_paths
            (Optional[list[str]]) — omit to run `git diff <gitHead>..HEAD`
            live in repo_root or $OPENWIKI_REPO_ROOT.

    Returns:
        str: markdown table — page, verdict ('re-verify …' or 'current as
        of <sha>'), changed files hit, grounding weight — plus a header
        naming the diff source. "Error: …" with a remedy when no diff
        source is available.
    """
    try:
        w = _writer()
        b = _resolve_bundle(w, params.bundle)
        paths, source = _changed_paths(w, b, params.changed_paths, params.repo_root)
        recs = w.run(openwiki.WIKI_STALENESS_QUERY, paths=paths, bundle=b).records
        if not recs:
            return f"Bundle '{b}' has no pages (or no WikiRun watermark)."
        out = [f"diff source: {source} · {len(paths)} changed file(s)", "",
               "| page | verdict | files hit | weight |", "|---|---|---|---|"]
        for r in recs:
            out.append(f"| {r['page']} | {r['verdict']} | {r['hit_files']} | {r['weight']} |")
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="wiki_coverage_gaps",
    annotations=ToolAnnotations(title="Undocumented files and dangling groundings", **_READ_ONLY),
)
def wiki_coverage_gaps(params: CoverageInput) -> str:
    """Audit wiki ↔ repo alignment in both directions.

    Reports (a) repo files under path_prefix that no wiki page claims to
    document, and (b) dangling groundings — pages documenting files that no
    longer exist (drift the flat wiki cannot see).

    Args:
        params (CoverageInput): bundle (Optional[str]); path_prefix (str,
            default 'src/'); repo_root (Optional[str]) for live
            `git ls-files`, else the bundle's vendored file list is used.

    Returns:
        str: markdown — an 'undocumented files' list and a 'dangling
        groundings' table (vanished file, pages still documenting it,
        mentions). "Error: …" when no file list is available.
    """
    try:
        w = _writer()
        b = _resolve_bundle(w, params.bundle)
        files = _current_files(w, b, params.repo_root)
        scoped = [f for f in files if f.startswith(params.path_prefix)]
        grounded = {r["path"] for r in w.run(
            "MATCH (f:SourceFile {bundle: $b}) RETURN f.path AS path", b=b).records}
        def covered(f: str) -> bool:
            return f in grounded or any(f.startswith(d + "/") for d in grounded)
        gaps = [f for f in scoped if not covered(f)]
        out = [f"# Coverage — bundle '{b}', prefix '{params.path_prefix}'",
               f"{len(scoped)} files · {len(gaps)} undocumented:"]
        out += [f"- {f}" for f in gaps[:40]]
        if len(gaps) > 40:
            out.append(f"… and {len(gaps) - 40} more")
        dang = w.run(openwiki.DANGLING_GROUNDINGS_QUERY, bundle=b, all_files=files).records
        if dang:
            out += ["", "# Dangling groundings (documented file no longer in repo)",
                    "| vanished file | still documented by | mentions |", "|---|---|---|"]
            for r in dang:
                out.append(f"| {r['vanished_file']} | "
                           f"{', '.join(r['still_documented_by'])} | {r['total_mentions']} |")
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def main() -> None:
    """Console entry point (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
