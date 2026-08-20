"""OpenWiki wikis (langchain-ai/openwiki) as OKF bundles in Neo4j.

OpenWiki emits OKF v0.1 markdown bundles — `openwiki/` in a repo (code mode)
or `~/.openwiki/wiki` (personal mode). This module adapts them onto the
existing okf_graph parser/ingester and then materializes the structure
OpenWiki itself computes but throws away:

* **Source grounding.** Wiki prose is dense with backticked repo paths
  (`src/agent/index.ts`, 390 mentions in OpenWiki's own dogfooded wiki) and
  the OKF `openwiki:` frontmatter extension declares `source_paths` /
  `symbols` / `test_paths` / `invariants` — which nothing upstream consumes.
  Both become `(:Concept)-[:GROUNDED_IN]->(:SourceFile)` edges, turning
  "which pages document this file?" into one-hop Cypher.
* **Run metadata.** `.last-update.json` (the wiki's `gitHead` watermark)
  becomes a `(:WikiRun)` node, so staleness is queryable per page instead of
  being one SHA for the whole wiki.
* **Impact analysis.** OpenWiki's update agent derives an impact plan
  (`_plan.md`) from `git diff` on every run and deletes it afterwards. With
  groundings in the graph, `git diff --name-only <gitHead>..HEAD` matched
  against `:SourceFile` paths *is* the impact plan — persistent, ranked, and
  extended one hop over `LINKS_TO` for downstream pages.

Additions to the base mapping (parser.py docstring has the rest):

    OpenWiki construct                  -> graph element
    ------------------------------------------------------------------
    backticked repo path in prose       -> (:SourceFile {path, kind}) +
                                           [:GROUNDED_IN {sections, mentions,
                                                          declared: false}]
    openwiki.source_paths (frontmatter) -> same, declared: true
    openwiki.test_paths                 -> [:VALIDATED_BY] -> (:SourceFile {kind:'test'})
    openwiki.symbols                    -> [:DOCUMENTS] -> (:Symbol {name})
    openwiki.invariants                 -> [:HAS_INVARIANT] -> (:Invariant {text})
    openwiki.roles / change_kinds /
      validation_commands               -> Concept array properties
    .last-update.json                   -> (:WikiRun {git_head, updated_at,
                                           model, status})-[:PRODUCED]->(:Bundle)

Reserved (never concepts, per OpenWiki's own exclusion sets): `index.md`,
`log.md`, `INSTRUCTIONS.md`, `_plan.md`, `_skeleton.md`.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .ingest import GraphWriter
from .parser import BundleParser, ParsedBundle, _strip_fences

LAST_UPDATE_FILE = ".last-update.json"

#: OpenWiki's reserved documents (code-mode exclusion sets in
#: src/okf/index-sync.ts and src/visualize/graph.ts upstream).
OPENWIKI_RESERVED = {"INSTRUCTIONS.md", "_plan.md", "_skeleton.md"}

# Inline-code path mention: at least one '/', no spaces, no URL scheme.
# Optional trailing '/' or '/*' marks a directory mention; an optional
# ':L10' / '#L10' line anchor is stripped.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_PATH_RE = re.compile(
    r"^(?P<path>[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@*-]+)+)(?P<dir>/\*?)?"
    r"(?:[#:]L?\d+(?:-L?\d+)?)?$"
)
#: Repo-root files worth grounding to even without a slash in the mention.
_ROOT_FILE_RE = re.compile(
    r"^(?:AGENTS\.md|CLAUDE\.md|package\.json|pnpm-workspace\.yaml|"
    r"pnpm-lock\.yaml|tsconfig(?:\.[A-Za-z0-9_.-]+)?\.json|eslint\.config\.js|"
    r"\.openwikiignore|\.gitignore|Makefile|Dockerfile|docker-compose\.ya?ml)$"
)
#: A dotted final segment only reads as a *file* with a plausible extension —
#: otherwise model IDs in prose (`openai/gpt-5.6-terra`, `z-ai/glm-5.2`)
#: would masquerade as repo paths.
_KNOWN_EXTS = {
    "ts", "tsx", "js", "jsx", "mjs", "cjs", "py", "ipynb", "json", "jsonl",
    "yml", "yaml", "toml", "lock", "md", "mdx", "txt", "sh", "bash", "sql",
    "css", "html", "svg", "png", "gif", "xml", "ini", "cfg", "env", "example",
    "go", "rs", "java", "rb", "php", "c", "h", "cpp", "hpp", "cs", "swift",
    "kt", "gradle", "tf", "proto", "plist", "sqlite", "gitignore",
}


@dataclass
class WikiGrounding:
    concept_uid: str
    path: str                        # repo-relative, no trailing slash
    kind: str                        # 'file' | 'dir' | 'test'
    sections: list[str] = field(default_factory=list)
    mentions: int = 0
    declared: bool = False           # True when from openwiki: frontmatter


@dataclass
class WikiConceptExt:
    """The `openwiki:` producer extension (no consumer upstream — see the
    prompt contract in openwiki's src/agent/prompts/code.ts)."""
    concept_uid: str
    roles: list[str] = field(default_factory=list)
    change_kinds: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)


@dataclass
class WikiRunMeta:
    git_head: Optional[str] = None
    updated_at: Optional[str] = None
    command: Optional[str] = None
    model: Optional[str] = None
    status: Optional[str] = None
    language: Optional[str] = None


@dataclass
class OpenWikiBundle:
    pb: ParsedBundle
    groundings: list[WikiGrounding] = field(default_factory=list)
    extensions: list[WikiConceptExt] = field(default_factory=list)
    run: Optional[WikiRunMeta] = None

    def stats(self) -> dict[str, int]:
        out = self.pb.stats()
        out["groundings"] = len(self.groundings)
        out["source_files"] = len({(g.path, g.kind != "dir") for g in self.groundings})
        out["extensions"] = len(self.extensions)
        return out


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

class OpenWikiParser(BundleParser):
    reserved_filenames = frozenset(BundleParser.reserved_filenames | OPENWIKI_RESERVED)


def _path_mention(raw: str) -> Optional[tuple[str, str]]:
    """`raw` (inline-code span content) -> (path, kind) or None."""
    s = raw.strip().rstrip(",;")
    if "://" in s or " " in s or s.startswith(("/", "~", "$")):
        return None
    if _ROOT_FILE_RE.match(s):
        return s, "file"
    m = _PATH_RE.match(s)
    if not m:
        return None
    path = m.group("path")
    if path.endswith(".md") and "/" not in path:
        return None
    # a glob tail or trailing slash means "this directory"
    if m.group("dir") or path.endswith("/"):
        return path.rstrip("/"), "dir"
    if "*" in path:  # e.g. src/agent/prompts/*.ts
        return str(Path(path).parent.as_posix()), "dir"
    last = path.rsplit("/", 1)[-1]
    if "." not in last:
        return path, "dir"   # extensionless final segment (src/telemetry)
    ext = last.rsplit(".", 1)[-1].lower()
    return (path, "file") if ext in _KNOWN_EXTS else None


def _extract_prose_groundings(pb: ParsedBundle) -> list[WikiGrounding]:
    """Backticked repo paths in section prose -> groundings.

    Fenced code blocks are stripped first (config samples, YAML, shell
    transcripts); only inline code spans in running prose count as claims
    that a page documents a path. Mentions of the wiki's own pages
    (anything ending in .md that resolves inside the bundle) are already
    modeled as LINKS_TO and are skipped here.
    """
    out: dict[tuple[str, str], WikiGrounding] = {}
    for c in pb.concepts.values():
        for sec in c.sections:
            for m in _INLINE_CODE_RE.finditer(_strip_fences(sec.text)):
                hit = _path_mention(m.group(1))
                if hit is None:
                    continue
                path, kind = hit
                if path.endswith(".md") and (Path(pb.root) / path).exists():
                    continue  # wiki-internal doc reference, not repo grounding
                g = out.setdefault((c.uid, path), WikiGrounding(c.uid, path, kind))
                g.mentions += 1
                if kind == "file" and g.kind == "dir":
                    g.kind = "file"  # most specific mention wins
                if sec.heading not in g.sections:
                    g.sections.append(sec.heading)
    return list(out.values())


def _promote_extensions(pb: ParsedBundle) -> tuple[list[WikiGrounding], list[WikiConceptExt]]:
    """Lift the `openwiki:` frontmatter extension out of extra_frontmatter."""
    declared: list[WikiGrounding] = []
    exts: list[WikiConceptExt] = []
    for c in pb.concepts.values():
        if not c.extra_frontmatter:
            continue
        try:
            ext = json.loads(c.extra_frontmatter).get("openwiki")
        except (ValueError, AttributeError):
            continue
        if not isinstance(ext, dict):
            continue

        def _strs(key: str) -> list[str]:
            v = ext.get(key)
            if isinstance(v, str):
                return [v]
            return [str(x) for x in v] if isinstance(v, list) else []

        for p in _strs("source_paths"):
            hit = _path_mention(p) or (p.rstrip("/"), "file")
            declared.append(WikiGrounding(c.uid, hit[0], hit[1], declared=True))
        for p in _strs("test_paths"):
            declared.append(WikiGrounding(c.uid, p.rstrip("/"), "test", declared=True))
        wce = WikiConceptExt(
            concept_uid=c.uid,
            roles=_strs("roles"), change_kinds=_strs("change_kinds"),
            symbols=_strs("symbols"), invariants=_strs("invariants"),
            validation_commands=_strs("validation_commands"),
        )
        if any((wce.roles, wce.change_kinds, wce.symbols,
                wce.invariants, wce.validation_commands)):
            exts.append(wce)
    return declared, exts


def _merge_groundings(prose: list[WikiGrounding],
                      declared: list[WikiGrounding]) -> list[WikiGrounding]:
    by_key = {(g.concept_uid, g.path): g for g in prose}
    for d in declared:
        g = by_key.get((d.concept_uid, d.path))
        if g is None:
            by_key[(d.concept_uid, d.path)] = d
        else:
            g.declared = True
            if d.kind == "test":
                g.kind = "test"
    return list(by_key.values())


def _read_run_meta(root: Path) -> Optional[WikiRunMeta]:
    p = root / LAST_UPDATE_FILE
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    return WikiRunMeta(
        git_head=raw.get("gitHead"), updated_at=raw.get("updatedAt"),
        command=raw.get("command"), model=raw.get("model"),
        status=raw.get("status"), language=raw.get("language"),
    )


def parse_openwiki(root: str | Path, bundle_name: Optional[str] = None) -> OpenWikiBundle:
    parser = OpenWikiParser(root, bundle_name)
    pb = parser.parse()
    prose = _extract_prose_groundings(pb)
    declared, exts = _promote_extensions(pb)
    return OpenWikiBundle(
        pb=pb,
        groundings=_merge_groundings(prose, declared),
        extensions=exts,
        run=_read_run_meta(parser.root),
    )


# --------------------------------------------------------------------------
# Ingestion (on top of GraphWriter.ingest)
# --------------------------------------------------------------------------

WIKI_CONSTRAINTS = [
    "CREATE CONSTRAINT sourcefile_uid IF NOT EXISTS FOR (f:SourceFile) REQUIRE f.uid IS UNIQUE",
    "CREATE CONSTRAINT symbol_uid     IF NOT EXISTS FOR (s:Symbol)     REQUIRE s.uid IS UNIQUE",
    "CREATE CONSTRAINT invariant_uid  IF NOT EXISTS FOR (i:Invariant)  REQUIRE i.uid IS UNIQUE",
    "CREATE CONSTRAINT wikirun_uid    IF NOT EXISTS FOR (r:WikiRun)    REQUIRE r.uid IS UNIQUE",
    "CREATE INDEX sourcefile_path IF NOT EXISTS FOR (f:SourceFile) ON (f.path)",
]


def ingest_openwiki(writer: GraphWriter, owb: OpenWikiBundle) -> dict[str, int]:
    """Standard OKF ingest, then the OpenWiki layer. Same sync semantics:
    re-running replaces groundings/extensions; WikiRun history accumulates."""
    stats = writer.ingest(owb.pb)
    b = owb.pb.name
    for stmt in WIKI_CONSTRAINTS:
        writer.run(stmt)

    # -- clear replaceable wiki-layer edges, then orphaned targets ---------
    writer.run(
        """MATCH (c:Concept {bundle: $b})
               -[r:GROUNDED_IN|VALIDATED_BY|DOCUMENTS|HAS_INVARIANT]->()
           DELETE r""", b=b)
    for label in ("SourceFile", "Symbol", "Invariant"):
        writer.run(
            f"MATCH (n:{label} {{bundle: $b}}) WHERE NOT (n)--() DELETE n", b=b)

    # -- source files + groundings ----------------------------------------
    file_rows, edge_rows = {}, []
    for g in owb.groundings:
        uid = f"{b}:file:{g.path}"
        row = file_rows.setdefault(uid, {"uid": uid, "path": g.path,
                                         "kind": g.kind, "bundle": b})
        if g.kind == "file" and row["kind"] == "dir":
            row["kind"] = "file"
        edge_rows.append({
            "concept": g.concept_uid, "file": uid, "sections": g.sections,
            "mentions": g.mentions, "declared": g.declared,
            "validated": g.kind == "test",
        })
    writer.run(
        """UNWIND $rows AS row
           MERGE (f:SourceFile {uid: row.uid})
           SET f.path = row.path, f.kind = row.kind, f.bundle = row.bundle""",
        rows=list(file_rows.values()))
    writer.run(
        """UNWIND $rows AS row
           MATCH (c:Concept {uid: row.concept}), (f:SourceFile {uid: row.file})
           FOREACH (_ IN CASE WHEN row.validated THEN [] ELSE [1] END |
               CREATE (c)-[g:GROUNDED_IN]->(f)
               SET g.sections = row.sections, g.mentions = row.mentions,
                   g.declared = row.declared)
           FOREACH (_ IN CASE WHEN row.validated THEN [1] ELSE [] END |
               CREATE (c)-[:VALIDATED_BY]->(f))""",
        rows=edge_rows)

    # -- openwiki: extension ----------------------------------------------
    for ext in owb.extensions:
        writer.run(
            """MATCH (c:Concept {uid: $uid})
               SET c.openwiki_roles = $roles,
                   c.openwiki_change_kinds = $kinds,
                   c.openwiki_validation_commands = $cmds""",
            uid=ext.concept_uid, roles=ext.roles or None,
            kinds=ext.change_kinds or None, cmds=ext.validation_commands or None)
        writer.run(
            """UNWIND $rows AS row
               MERGE (s:Symbol {uid: row.uid})
               SET s.name = row.name, s.bundle = $b
               WITH s
               MATCH (c:Concept {uid: $cuid})
               CREATE (c)-[:DOCUMENTS]->(s)""",
            rows=[{"uid": f"{b}:sym:{s}", "name": s} for s in ext.symbols],
            b=b, cuid=ext.concept_uid)
        writer.run(
            """UNWIND $rows AS row
               MERGE (i:Invariant {uid: row.uid})
               SET i.text = row.text, i.bundle = $b
               WITH i
               MATCH (c:Concept {uid: $cuid})
               CREATE (c)-[:HAS_INVARIANT]->(i)""",
            rows=[{"uid": f"{ext.concept_uid}#inv{n}", "text": t}
                  for n, t in enumerate(ext.invariants)],
            b=b, cuid=ext.concept_uid)

    # -- run watermark -----------------------------------------------------
    if owb.run and owb.run.git_head:
        writer.run(
            """MERGE (r:WikiRun {uid: $uid})
               SET r.git_head = $head, r.command = $command, r.model = $model,
                   r.status = $status, r.language = $language, r.bundle = $b,
                   r.updated_at = CASE WHEN $at IS NULL THEN NULL
                                       ELSE datetime($at) END
               WITH r
               MATCH (bn:Bundle {name: $b})
               MERGE (r)-[:PRODUCED]->(bn)
               SET bn.git_head = $head""",
            uid=f"{b}:run:{owb.run.git_head}", head=owb.run.git_head,
            command=owb.run.command, model=owb.run.model, status=owb.run.status,
            language=owb.run.language, at=owb.run.updated_at, b=b)

    return owb.stats() | stats


# --------------------------------------------------------------------------
# Git helpers — the impact-analysis inputs
# --------------------------------------------------------------------------

def git_changed_files(repo_root: str | Path, since: str,
                      until: str = "HEAD") -> list[str]:
    """`git diff --name-only since..until` in the documented repo."""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{since}..{until}"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    )
    return [l for l in out.stdout.splitlines() if l.strip()]


def git_ls_files(repo_root: str | Path) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=str(repo_root),
                         capture_output=True, text=True, check=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def load_changes_json(path: str | Path) -> dict[str, Any]:
    """Vendored sample shape: {"since", "until", "files": [...]}."""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


# --------------------------------------------------------------------------
# Wiki-layer Cypher
# --------------------------------------------------------------------------

WIKI_IMPACT_QUERY = """
// The persistent impact plan: changed repo paths -> grounded wiki pages,
// ranked by how load-bearing the grounding is, extended one hop to the
// pages that link to them. (OpenWiki derives this per run in _plan.md and
// deletes it; here it is one query.)
UNWIND $paths AS changed
MATCH (f:SourceFile {bundle: $bundle})
WHERE f.path = changed
   OR (f.kind = 'dir' AND changed STARTS WITH f.path + '/')
MATCH (page:Concept)-[g:GROUNDED_IN]->(f)
WITH page, collect(DISTINCT changed) AS changed_files,
     sum(g.mentions) AS weight, max(g.declared) AS declared
OPTIONAL MATCH (page)<-[:LINKS_TO]-(downstream:Concept)
WHERE NOT coalesce(downstream.stub, false)
RETURN page.id AS page, page.type AS type, changed_files,
       weight, declared,
       collect(DISTINCT downstream.id) AS downstream_pages
ORDER BY weight DESC, page
"""

WIKI_STALENESS_QUERY = """
// Page-level staleness verdicts against the wiki's own gitHead watermark.
// $paths = files changed since (:WikiRun).git_head.
MATCH (b:Bundle {name: $bundle})<-[:PRODUCED]-(r:WikiRun)
WITH b, r ORDER BY r.updated_at DESC LIMIT 1
MATCH (page:Concept {bundle: $bundle}) WHERE NOT coalesce(page.stub, false)
OPTIONAL MATCH (page)-[g:GROUNDED_IN]->(f:SourceFile)
WHERE any(p IN $paths WHERE f.path = p
          OR (f.kind = 'dir' AND p STARTS WITH f.path + '/'))
WITH r, page, count(f) AS hit_files, sum(coalesce(g.mentions, 0)) AS weight
RETURN page.id AS page,
       CASE WHEN hit_files > 0 THEN 're-verify (grounded in changed code)'
            ELSE 'current as of ' + left(r.git_head, 8) END AS verdict,
       hit_files, weight, r.git_head AS wiki_git_head,
       toString(r.updated_at) AS wiki_updated_at
ORDER BY weight DESC, page
"""

DANGLING_GROUNDINGS_QUERY = """
// Pages grounded in files that no longer exist in the repo ($all_files =
// current `git ls-files`): documentation drift the flat wiki cannot see.
MATCH (page:Concept {bundle: $bundle})-[g:GROUNDED_IN]->(f:SourceFile {kind: 'file'})
WHERE NOT f.path IN $all_files
RETURN f.path AS vanished_file, collect(page.id) AS still_documented_by,
       sum(g.mentions) AS total_mentions
ORDER BY total_mentions DESC
"""

WIKI_HUBS_QUERY = """
// Load-bearing pages: inbound links x grounding breadth.
MATCH (page:Concept {bundle: $bundle}) WHERE NOT coalesce(page.stub, false)
RETURN page.id AS page, page.type AS type,
       COUNT { (page)<-[:LINKS_TO]-() }        AS inbound_links,
       COUNT { (page)-[:GROUNDED_IN]->() }     AS grounded_files
ORDER BY inbound_links + grounded_files DESC LIMIT 15
"""

# Appended after the vector/fulltext anchor by VectorCypherRetriever:
# `node` is the matched (:Section), `score` its similarity. The assembled
# context carries what a flat-file grep cannot: the owning page, its
# groundings, and its link neighborhood.
WIKI_RETRIEVAL_QUERY = """
MATCH (page:Concept)-[:HAS_SECTION]->(node)
OPTIONAL MATCH (page)-[g:GROUNDED_IN]->(f:SourceFile)
WITH node, score, page, f, g ORDER BY g.mentions DESC
WITH node, score, page,
     collect(DISTINCT f.path)[..8] AS grounded_in
OPTIONAL MATCH (page)-[:LINKS_TO]->(out:Concept)
WHERE NOT coalesce(out.stub, false)
WITH node, score, page, grounded_in,
     collect(DISTINCT out.id)[..6] AS links_out
OPTIONAL MATCH (page)<-[:LINKS_TO]-(inp:Concept)
WITH node, score, page, grounded_in, links_out,
     collect(DISTINCT inp.id)[..6] AS linked_from
RETURN
    '=== ' + page.title + '  (' + page.id + ') ===\\n'
  + 'type: ' + page.type + '\\n'
  + CASE WHEN size(grounded_in) > 0
         THEN 'grounded in: ' + reduce(s='', x IN grounded_in | s + x + '; ') + '\\n'
         ELSE '' END
  + CASE WHEN size(links_out) > 0
         THEN 'links to: ' + reduce(s='', x IN links_out | s + x + '; ') + '\\n'
         ELSE '' END
  + CASE WHEN size(linked_from) > 0
         THEN 'linked from: ' + reduce(s='', x IN linked_from | s + x + '; ') + '\\n'
         ELSE '' END
  + '--- matched section [' + node.heading + '] ---\\n' + node.text
    AS info,
    score, page.id AS page_id
ORDER BY score DESC
"""

WIKI_TEXT2CYPHER_SCHEMA = """
Node labels and key properties:
  (:Bundle {name, okf_version, git_head})
  (:Concept {uid, id, bundle, type, title, description, tags, stub: BOOLEAN})
      // one wiki page; secondary label mirrors its OKF type
  (:Section {uid, heading, order, text})
  (:SourceFile {uid, path, kind})   // kind: 'file' | 'dir' | 'test'
  (:Symbol {uid, name})  (:Invariant {uid, text})
  (:WikiRun {uid, git_head, updated_at: DATETIME, model, status})
  (:Tag {name})

Relationships:
  (Concept)-[:IN_BUNDLE]->(Bundle)
  (Concept)-[:LINKS_TO {section, text, resolved}]->(Concept)
  (Concept)-[:HAS_SECTION]->(Section)     (Section)-[:NEXT]->(Section)
  (Section)-[:MENTIONS]->(Concept)
  (Concept)-[:GROUNDED_IN {sections, mentions, declared}]->(SourceFile)
  (Concept)-[:VALIDATED_BY]->(SourceFile) (Concept)-[:DOCUMENTS]->(Symbol)
  (Concept)-[:HAS_INVARIANT]->(Invariant) (Concept)-[:TAGGED]->(Tag)
  (WikiRun)-[:PRODUCED]->(Bundle)

Conventions:
  A page "documents" a repo file via GROUNDED_IN; mentions is how often.
  Ignore concepts with stub = true unless asked about missing pages.
"""

WIKI_TEXT2CYPHER_EXAMPLES = [
    "USER INPUT: 'Which pages document src/agent/index.ts?' QUERY: MATCH (p:Concept)-[g:GROUNDED_IN]->(f:SourceFile {path: 'src/agent/index.ts'}) RETURN p.id, g.mentions ORDER BY g.mentions DESC",
    "USER INPUT: 'What are the most linked pages?' QUERY: MATCH (p:Concept)<-[:LINKS_TO]-(o:Concept) WHERE NOT coalesce(p.stub,false) RETURN p.id, count(o) AS inbound ORDER BY inbound DESC LIMIT 10",
    "USER INPUT: 'Which repo files have no documentation?' QUERY: MATCH (f:SourceFile {kind:'file'}) WHERE NOT (f)<-[:GROUNDED_IN]-() RETURN f.path",
    "USER INPUT: 'When was the wiki last updated and at which commit?' QUERY: MATCH (r:WikiRun)-[:PRODUCED]->(b:Bundle) RETURN b.name, r.git_head, r.updated_at ORDER BY r.updated_at DESC LIMIT 1",
]
