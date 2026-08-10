"""Project a Neo4j subgraph back into a portable OKF bundle.

This is the arrow the demo was missing. `ingest.py` runs OKF files → graph;
this runs graph → OKF files, and it is not merely an "export": the projection
takes a **selection query**, so the bundle you get out is one the graph decided
on rather than one that ever existed on disk.

    okf-graph project out/ --bundle acme_retail                      # everything
    okf-graph project pack/ --min-trust human-reviewed --exclude-stale
    okf-graph project ctx/  --seed metrics/gross-margin --hops 2 --format tar

That last one is the interesting one. "Assemble the context an agent needs for
gross margin, human-reviewed only, nothing stale, as a tarball" is a Cypher
query in this module and a governance argument in the talk: OKF is the
interchange format, the graph is where selection happens.

Round-trip: `parse → ingest → project → emit → parse` returns an equivalent
model (see tests/test_roundtrip.py). Where equivalence is not identity, the
reasons are enumerated in `emit.ROUNDTRIP_NOTES` and copied into the manifest
written alongside the bundle.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import os
import tarfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import emit
from .ingest import GraphWriter
from .parser import (MAX_ARTIFACT_BYTES, ParsedActor, ParsedBundle, ParsedConcept,
                     ParsedLink, ParsedLogEntry, ParsedSection, ParsedSource,
                     _dt_str, type_to_label)

TRUST_RANK = {"unverified": 0, "machine-confirmed": 1, "human-reviewed": 2}

# Dependency edges walked when expanding a neighbourhood. Same set the impact
# query uses, so "blast radius" and "context pack" agree on what depends on what.
NEIGHBOUR_RELS = "LINKS_TO|EXECUTED_BY|DERIVES_FROM|RESOLVES_TO|ATTESTED_BY|COMPUTATION_FILE"

# Under a dot-directory on purpose: the parser skips dotted paths entirely, so
# the manifest can never be mistaken for bundle content on the way back in.
MANIFEST_NAME = ".okf/projection.json"


@dataclass
class ProjectionSpec:
    """What to pull out of the graph, and how to shape it."""

    bundle: str
    name: Optional[str] = None              # output bundle name (default: bundle)
    ids: list[str] = field(default_factory=list)      # explicit concept ids
    seed: Optional[str] = None              # concept id to grow a neighbourhood from
    hops: int = 1
    tags: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    min_trust: Optional[str] = None         # unverified | machine-confirmed | human-reviewed
    exclude_stale: bool = False             # drop concepts where date() >= stale_after
    include_referenced: bool = False        # +1 hop of link targets so links resolve
    include_logs: bool = True
    include_index: bool = True
    include_artifacts: bool = True

    def __post_init__(self) -> None:
        if self.min_trust and self.min_trust not in TRUST_RANK:
            raise ValueError(
                f"min_trust must be one of {sorted(TRUST_RANK)}, got {self.min_trust!r}")
        if not 0 <= self.hops <= 6:
            raise ValueError("hops must be between 0 and 6")

    @property
    def out_name(self) -> str:
        return self.name or self.bundle


@dataclass
class Projection:
    """The result: a model, the rendered files, and an audit trail."""

    bundle: ParsedBundle
    files: dict[str, str]
    manifest: dict[str, Any]


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def _select_uids(writer: GraphWriter, spec: ProjectionSpec) -> list[str]:
    """Resolve the spec to a concrete set of Concept uids.

    Stubs are never selected: SPEC §6.1 stubs stand for knowledge nobody has
    written, and materializing them as files would fabricate it.
    """
    params: dict[str, Any] = {
        "bundle": spec.bundle,
        "ids": spec.ids or None,
        "types": spec.types or None,
        "statuses": spec.statuses or None,
        "tags": spec.tags or None,
        "rank": TRUST_RANK.get(spec.min_trust or "", None),
    }
    predicates = """
        NOT coalesce(c.stub, false)
        AND ($ids IS NULL OR c.id IN $ids)
        AND ($types IS NULL OR c.type IN $types)
        AND ($statuses IS NULL OR c.status IN $statuses)
        AND ($tags IS NULL OR any(t IN coalesce(c.tags, []) WHERE t IN $tags))
        AND ($rank IS NULL OR
             (CASE c.trust_tier WHEN 'human-reviewed' THEN 2
                                WHEN 'machine-confirmed' THEN 1 ELSE 0 END) >= $rank)
    """
    if spec.exclude_stale:
        predicates += " AND (c.stale_after IS NULL OR date() < c.stale_after)"

    if spec.seed:
        # hops is validated to 0..6 in __post_init__; Cypher cannot parameterize
        # a variable-length bound, so it is interpolated as a checked integer.
        query = f"""
            MATCH (seed:Concept {{bundle: $bundle, id: $seed}})
            MATCH (seed)-[:{NEIGHBOUR_RELS}*0..{int(spec.hops)}]-(c:Concept)
            WHERE c.bundle = $bundle AND ({predicates})
            RETURN collect(DISTINCT c.uid) AS uids
        """
        params["seed"] = spec.seed
    else:
        query = f"""
            MATCH (c:Concept {{bundle: $bundle}})
            WHERE {predicates}
            RETURN collect(DISTINCT c.uid) AS uids
        """

    records = writer.run(query, **params).records
    uids: list[str] = records[0]["uids"] if records else []

    # A filter can cut a concept out from under an Attested Computation that
    # names it as executor or attester — and nothing downstream notices, because
    # the governed retrieval query walks LINKS_TO to a computation and never
    # checks that its executor exists. A bundle whose consumer contract (§10.5)
    # cannot be followed must not claim it can, so those edges are always closed
    # over, regardless of --include-referenced.
    if uids:
        contract = writer.run(
            """MATCH (c:Concept)-[:EXECUTED_BY|ATTESTED_BY|COMPUTATION_FILE]->(t)
               WHERE c.uid IN $uids AND t:Concept AND t.bundle = $bundle
                 AND NOT coalesce(t.stub, false) AND NOT t.uid IN $uids
               RETURN collect(DISTINCT t.uid) AS uids""",
            uids=uids, bundle=spec.bundle,
        ).records
        uids = sorted(set(uids) | set(contract[0]["uids"] if contract else []))

    if spec.include_referenced and uids:
        extra = writer.run(
            """MATCH (c:Concept)-[:LINKS_TO]->(t:Concept)
               WHERE c.uid IN $uids AND t.bundle = $bundle
                 AND NOT coalesce(t.stub, false) AND NOT t.uid IN $uids
               RETURN collect(DISTINCT t.uid) AS uids""",
            uids=uids, bundle=spec.bundle,
        ).records
        uids = sorted(set(uids) | set(extra[0]["uids"] if extra else []))

    return sorted(uids)


def _dangling_links(writer: GraphWriter, uids: list[str]) -> list[dict]:
    """Links from selected concepts to concepts the selection left behind.

    Reported rather than rewritten. A link to a file that is not in the bundle
    is legal OKF (§6.1 — consumers MUST tolerate broken cross-links) and it is
    the honest record of what the filter cut, but a consumer deserves to be
    told, so it goes in the manifest.
    """
    if not uids:
        return []
    return [
        {"from": r["from"], "to": r["to"], "section": r["section"], "reason": r["reason"]}
        for r in writer.run(
            """MATCH (c:Concept)-[l:LINKS_TO]->(t:Concept)
               WHERE c.uid IN $uids AND NOT t.uid IN $uids
               RETURN c.id AS from, t.id AS to, l.section AS section,
                      CASE WHEN coalesce(t.stub, false)
                           THEN 'not-yet-written (SPEC 6.1 stub)'
                           ELSE 'excluded by projection filter' END AS reason
               ORDER BY from, to""",
            uids=uids,
        ).records
    ]


# --------------------------------------------------------------------------
# Read-back: graph -> ParsedBundle
# --------------------------------------------------------------------------

def project_bundle(writer: GraphWriter, spec: ProjectionSpec) -> tuple[ParsedBundle, dict]:
    """Rebuild the in-memory model from Neo4j for the concepts `spec` selects."""
    uids = _select_uids(writer, spec)
    out_name = spec.out_name

    meta = writer.run(
        """MATCH (b:Bundle {name: $b})
           RETURN b.okf_version AS v, b.root AS root,
                  b.log_frontmatter AS log_frontmatter""",
        b=spec.bundle,
    ).records
    pb = ParsedBundle(
        name=out_name,
        root=(meta[0]["root"] if meta else "") or "",
        okf_version=(meta[0]["v"] if meta else None),
        log_frontmatter=json.loads(meta[0]["log_frontmatter"])
        if meta and meta[0]["log_frontmatter"] else {},
    )
    if not uids:
        return pb, {"selected": 0}

    # -- concepts ---------------------------------------------------------
    for r in writer.run(
        """MATCH (c:Concept) WHERE c.uid IN $uids
           RETURN c.uid AS uid, c.id AS id, c.path AS path, c.dir AS dir,
                  c.type AS type, c.title AS title, c.description AS description,
                  c.resource AS resource, c.status AS status,
                  toString(c.stale_after) AS stale_after,
                  toString(c.generated_at) AS generated_at,
                  c.trust_tier AS trust_tier, c.tags AS tags,
                  c.extra_frontmatter AS extra, c.body AS body,
                  c.runtime AS runtime, c.parameters_json AS parameters_json,
                  c.receipt AS receipt,
                  c.executor_raw AS executor_raw, c.attester_raw AS attester_raw,
                  c.computation_raw AS computation_raw
           ORDER BY c.id""",
        uids=uids,
    ).records:
        ctype = r["type"] or "Concept"
        pb.concepts[r["uid"]] = ParsedConcept(
            uid=r["uid"], id=r["id"], bundle=out_name,
            path=r["path"] or f"{r['id']}.md", dir=r["dir"] or "",
            type=ctype, type_label=type_to_label(ctype), title=r["title"] or r["id"],
            description=r["description"], resource=r["resource"],
            status=r["status"] or "stable", stale_after=r["stale_after"],
            tags=list(r["tags"] or []), generated_at=_dt_str(r["generated_at"]),
            trust_tier=r["trust_tier"] or "unverified",
            body=r["body"] or "", extra_frontmatter=r["extra"],
            runtime=r["runtime"], parameters_json=r["parameters_json"],
            receipt=list(r["receipt"] or []),
            executor_raw=r["executor_raw"], attester_raw=r["attester_raw"],
            computation_raw=r["computation_raw"],
        )

    # The computation family's resolved targets live on edges, not properties
    # (ingest.py writes EXECUTED_BY / ATTESTED_BY / COMPUTATION_FILE), so they
    # are read back from the edges — the `*_raw` strings above are only the
    # authored spelling.
    for rel, attr in (("EXECUTED_BY", "executor_resource"),
                      ("ATTESTED_BY", "attester_resource"),
                      ("COMPUTATION_FILE", "computation_path")):
        for r in writer.run(
            f"""MATCH (c:Concept)-[:{rel}]->(t) WHERE c.uid IN $uids
                RETURN c.uid AS uid, t.uid AS target""",
            uids=uids,
        ).records:
            setattr(pb.concepts[r["uid"]], attr,
                    _rebundle(r["target"], spec.bundle, out_name))

    # Links are not needed to write the files (bodies are emitted verbatim, so
    # the markdown links ride along) but they belong in the model, and the
    # manifest's counts should not read as zero.
    for r in writer.run(
        """MATCH (c:Concept)-[l:LINKS_TO]->(t:Concept) WHERE c.uid IN $uids
           RETURN c.uid AS src, t.uid AS dst, t.id AS tid, l.text AS text,
                  l.section AS section, l.raw AS raw, l.resolved AS resolved""",
        uids=uids,
    ).records:
        pb.links.append(ParsedLink(
            source_uid=_rebundle(r["src"], spec.bundle, out_name),
            target_uid=_rebundle(r["dst"], spec.bundle, out_name),
            target_id=r["tid"], text=r["text"] or "", section=r["section"],
            raw=r["raw"] or "", resolved=r["dst"] in uids,
        ))

    # -- trust family (§5.2) ---------------------------------------------
    for r in writer.run(
        """MATCH (c:Concept)-[g:GENERATED_BY]->(a:Actor) WHERE c.uid IN $uids
           RETURN c.uid AS uid, a.id AS id, a.kind AS kind, a.name AS name,
                  a.producer AS producer, a.version AS version""",
        uids=uids,
    ).records:
        actor = ParsedActor(r["id"], r["kind"] or "agent", r["name"] or r["id"],
                            producer=r["producer"], version=r["version"])
        pb.actors.setdefault(actor.id, actor)
        pb.concepts[r["uid"]].generated_by = actor

    for r in writer.run(
        """MATCH (c:Concept)-[v:VERIFIED_BY]->(a:Actor) WHERE c.uid IN $uids
           RETURN c.uid AS uid, a.id AS id, a.kind AS kind, a.name AS name,
                  a.producer AS producer, a.version AS version,
                  toString(v.at) AS at, coalesce(v.order, 0) AS ord
           ORDER BY uid, ord""",
        uids=uids,
    ).records:
        actor = ParsedActor(r["id"], r["kind"] or "agent", r["name"] or r["id"],
                            producer=r["producer"], version=r["version"])
        pb.actors.setdefault(actor.id, actor)
        pb.concepts[r["uid"]].verified.append((actor, _dt_str(r["at"])))

    # -- provenance family (§5.1) ----------------------------------------
    for r in writer.run(
        """MATCH (c:Concept)-[d:DERIVES_FROM]->(s:Source) WHERE c.uid IN $uids
           OPTIONAL MATCH (s)-[:RESOLVES_TO]->(target)
           WITH c, d, s, collect(target) AS targets
           RETURN c.uid AS uid, s.uid AS suid, d.sid AS sid, s.resource AS resource,
                  coalesce(d.title, s.title) AS title,
                  coalesce(d.author, s.author) AS author,
                  d.usage_count AS usage_count,
                  toString(coalesce(d.last_modified, s.last_modified)) AS last_modified,
                  s.is_scope AS is_scope,
                  toString(d.usage_window_from) AS wfrom,
                  toString(d.usage_window_to) AS wto,
                  coalesce(d.order, 0) AS ord,
                  [t IN targets WHERE t:Concept | t.uid]  AS concept_targets,
                  [t IN targets WHERE t:Artifact | t.uid] AS artifact_targets
           ORDER BY uid, ord""",
        uids=uids,
    ).records:
        pb.concepts[r["uid"]].sources.append(ParsedSource(
            uid=r["suid"], sid=r["sid"], resource=r["resource"], title=r["title"],
            author=r["author"], usage_count=r["usage_count"],
            last_modified=r["last_modified"], is_scope=bool(r["is_scope"]),
            order=r["ord"],
            usage_window_from=r["wfrom"], usage_window_to=r["wto"],
            resolves_to_concept=_rebundle(
                (r["concept_targets"] or [None])[0], spec.bundle, out_name),
            resolves_to_artifact=_rebundle(
                (r["artifact_targets"] or [None])[0], spec.bundle, out_name),
        ))

    # -- lexical layer ----------------------------------------------------
    for r in writer.run(
        """MATCH (c:Concept)-[:HAS_SECTION]->(s:Section) WHERE c.uid IN $uids
           OPTIONAL MATCH (s)-[:CITES]->(src:Source)
           RETURN c.uid AS uid, s.uid AS suid, s.heading AS heading,
                  s.order AS ord, s.text AS text,
                  collect(DISTINCT src.uid) AS cites
           ORDER BY uid, ord""",
        uids=uids,
    ).records:
        pb.concepts[r["uid"]].sections.append(ParsedSection(
            uid=r["suid"], concept_uid=r["uid"], heading=r["heading"] or "_preamble",
            order=r["ord"] or 0, text=r["text"] or "",
            cites=sorted(x for x in r["cites"] if x),
        ))

    # -- artifacts --------------------------------------------------------
    if spec.include_artifacts:
        # Every artifact the bundle owns, not only those a selected concept
        # points at: `viz.html` is referenced by nothing and is still part of
        # the bundle. Turn this off with include_artifacts for a lean pack.
        for r in writer.run(
            """MATCH (a:Artifact {bundle: $bundle})
               RETURN a.uid AS uid, a.path AS path, a.kind AS kind,
                      a.text AS text, a.sha256 AS sha256, a.size AS size,
                      a.reason AS reason
               ORDER BY a.path""",
            bundle=spec.bundle,
        ).records:
            uid = _rebundle(r["uid"], spec.bundle, out_name)
            pb.artifacts[uid] = {"uid": uid, "path": r["path"], "kind": r["kind"],
                                 "text": r["text"], "sha256": r["sha256"],
                                 "size": r["size"], "reason": r["reason"]}

    # -- log (§9) ---------------------------------------------------------
    if spec.include_logs:
        for r in writer.run(
            """MATCH (l:LogEntry {bundle: $bundle})
               OPTIONAL MATCH (l)-[:REFERENCES]->(c:Concept)
               WITH l, collect(c.uid) AS refs
               WHERE size(refs) = 0 OR any(u IN refs WHERE u IN $uids)
               RETURN l.uid AS uid, toString(l.date) AS date, l.kind AS kind,
                      l.text AS text, l.order AS ord, coalesce(l.dir, '') AS dir,
                      refs
               ORDER BY date DESC, ord""",
            bundle=spec.bundle, uids=uids,
        ).records:
            pb.log_entries.append(ParsedLogEntry(
                uid=r["uid"], date=r["date"], kind=r["kind"], text=r["text"] or "",
                order=r["ord"] or 0, dir=r["dir"] or "",
                references=[u for u in r["refs"] if u in pb.concepts],
            ))

    stats = pb.stats()
    stats["selected"] = len(uids)
    return pb, stats


def _rebundle(uid: Optional[str], src: str, dst: str) -> Optional[str]:
    """Re-scope a uid when the projection is written under a new bundle name."""
    if not uid or src == dst:
        return uid
    return f"{dst}:{uid[len(src) + 1:]}" if uid.startswith(f"{src}:") else uid


# --------------------------------------------------------------------------
# Render + write
# --------------------------------------------------------------------------

def project(writer: GraphWriter, spec: ProjectionSpec,
            now: Optional[_dt.datetime] = None) -> Projection:
    """Select, rebuild, render. Nothing touches the filesystem here."""
    pb, stats = project_bundle(writer, spec)
    files = emit.render_bundle(
        pb, write_index=spec.include_index, write_log=spec.include_logs)
    # An artifact the graph knows only by path is a file the projection cannot
    # write — and `attester.resource` still points at it. Name each one and say
    # why, rather than letting the file that decides whether a number is
    # trustworthy disappear silently.
    unmaterialized = sorted(
        ({"path": a["path"],
          "reason": a.get("reason") or "not carried in this graph",
          "sha256": a.get("sha256"), "size": a.get("size")}
         for a in pb.artifacts.values() if a.get("text") is None),
        key=lambda a: a["path"],
    )
    manifest = {
        "okf_version": pb.okf_version,
        "projected_at": (now or _dt.datetime.now(_dt.timezone.utc)).isoformat(),
        "source": {
            "kind": "neo4j",
            "uri": os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            "database": writer.database,
            "bundle": spec.bundle,
        },
        "spec": {k: v for k, v in asdict(spec).items() if v not in (None, [], False)},
        "stats": stats,
        "files": len(files),
        "dangling_links": _dangling_links(writer, sorted(pb.concepts)),
        "unmaterialized_artifacts": unmaterialized,
        "artifact_size_limit_bytes": MAX_ARTIFACT_BYTES,
        "roundtrip_notes": emit.ROUNDTRIP_NOTES,
    }
    files[MANIFEST_NAME] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return Projection(bundle=pb, files=files, manifest=manifest)


def _safe_relpath(rel: str) -> Path:
    """Reject anything that would escape the output directory.

    Bundle paths reach here from graph properties, which for an LLM-authored
    wiki ultimately trace back to fetched documents — so they are untrusted
    input, not our own filenames. `emit.safe_bundle_path` has already vetted
    anything that came through `render_bundle`; this re-checks because these
    writers are public and a caller may hand us a dict from anywhere.
    """
    return Path(emit.safe_bundle_path(rel))


def write_dir(files: dict[str, str], out_dir: str | Path) -> Path:
    root = Path(out_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in sorted(files.items()):
        path = (root / _safe_relpath(rel)).resolve()
        if not path.is_relative_to(root):        # symlinked parent, race, …
            raise ValueError(f"refusing to write outside {root}: {rel!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def write_tar(files: dict[str, str], out_path: str | Path,
              arcroot: Optional[str] = None) -> Path:
    """Write a `.tar.gz` with every entry under a single top-level directory."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = arcroot or path.name.split(".")[0]
    with tarfile.open(path, "w:gz") as tar:
        for rel, text in sorted(files.items()):
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{_safe_relpath(rel).as_posix()}")
            info.size = len(data)
            info.mtime = 0                     # reproducible archives
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return path


def write_zip(files: dict[str, str], out_path: str | Path,
              arcroot: Optional[str] = None) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = arcroot or path.stem
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, text in sorted(files.items()):
            info = zipfile.ZipInfo(f"{root}/{_safe_relpath(rel).as_posix()}",
                                   date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            zf.writestr(info, text.encode("utf-8"))
    return path
