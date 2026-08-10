"""Write a ParsedBundle into Neo4j.

Design notes
------------
* Ingestion of the *structural* graph is deterministic — no LLM anywhere.
  OKF already carries its structure (paths, links, frontmatter families);
  we only materialize it. LLMs enter later, at retrieval/generation time,
  and optionally for entity extraction on top (see the notebook appendix).
* `ingest()` is a **sync**, not an append: for the bundle being ingested it
  clears replaceable relationships (trust, links, provenance, tags, …),
  drops vanished sections/stubs/log entries, refreshes secondary labels,
  and rebuilds from the parse. Concept and Section nodes are MERGEd by
  stable uid, and a Section keeps its embedding unless its text changed —
  so re-running an ingest never silently invalidates retrieval, and the
  derived trust tier can never drift from the materialized one.
* Dates/datetimes are validated in the parser (invalid values become null);
  Cypher date()/datetime() never sees garbage, per OKF's "tolerate, don't
  reject" conformance posture (SPEC §11).
* Embeddings are a separate, optional pass (embed_sections) so the graph can
  be built and inspected without any API key.
* No APOC required.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import neo4j

from .parser import ParsedBundle

VECTOR_INDEX = "section_embeddings"
FULLTEXT_INDEX = "section_fulltext"

CONSTRAINTS = [
    "CREATE CONSTRAINT concept_uid IF NOT EXISTS FOR (c:Concept) REQUIRE c.uid IS UNIQUE",
    "CREATE CONSTRAINT section_uid IF NOT EXISTS FOR (s:Section) REQUIRE s.uid IS UNIQUE",
    "CREATE CONSTRAINT source_uid  IF NOT EXISTS FOR (s:Source)  REQUIRE s.uid IS UNIQUE",
    "CREATE CONSTRAINT actor_id    IF NOT EXISTS FOR (a:Actor)   REQUIRE a.id  IS UNIQUE",
    "CREATE CONSTRAINT bundle_name IF NOT EXISTS FOR (b:Bundle)  REQUIRE b.name IS UNIQUE",
    "CREATE CONSTRAINT artifact_uid IF NOT EXISTS FOR (a:Artifact) REQUIRE a.uid IS UNIQUE",
    "CREATE CONSTRAINT tag_name    IF NOT EXISTS FOR (t:Tag)     REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT log_uid     IF NOT EXISTS FOR (l:LogEntry) REQUIRE l.uid IS UNIQUE",
    "CREATE INDEX concept_status IF NOT EXISTS FOR (c:Concept) ON (c.status)",
    "CREATE INDEX concept_type   IF NOT EXISTS FOR (c:Concept) ON (c.type)",
    "CREATE INDEX concept_stale  IF NOT EXISTS FOR (c:Concept) ON (c.stale_after)",
]


def get_driver(uri: Optional[str] = None, user: Optional[str] = None,
               password: Optional[str] = None) -> neo4j.Driver:
    return neo4j.GraphDatabase.driver(
        uri or os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(user or os.getenv("NEO4J_USERNAME", "neo4j"),
              password or os.getenv("NEO4J_PASSWORD", "password")),
        # Every OKF frontmatter family is optional (SPEC §5), so a query over a
        # bundle that never used `runtime` or `computation` earns a stream of
        # "property key does not exist" notifications. Expected here, and the
        # noise buries real output — so only UNRECOGNIZED is muted; performance
        # and deprecation notifications still come through.
        notifications_disabled_classifications=[
            neo4j.NotificationDisabledClassification.UNRECOGNIZED],
    )


class GraphWriter:
    def __init__(self, driver: neo4j.Driver, database: Optional[str] = None):
        self.driver = driver
        self.database = database or os.getenv("NEO4J_DATABASE", "neo4j")

    # -- plumbing ----------------------------------------------------------

    def run(self, query: str, **params: Any) -> neo4j.EagerResult:
        return self.driver.execute_query(query, params, database_=self.database)

    def setup_constraints(self) -> None:
        for stmt in CONSTRAINTS:
            self.run(stmt)

    def reset(self, bundle: Optional[str] = None) -> None:
        """Delete one bundle's subgraph, or everything when bundle is None.

        Uses an implicit (auto-commit) transaction because CALL ... IN
        TRANSACTIONS is not allowed inside managed transactions. Afterwards,
        sweeps globally-shared nodes (:Tag/:Actor) left without relationships.
        """
        if bundle is None:
            query, params = (
                "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 500 ROWS", {})
        else:
            query = ("MATCH (n) WHERE n.bundle = $bundle OR (n:Bundle AND n.name = $bundle) "
                     "CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 500 ROWS")
            params = {"bundle": bundle}
        with self.driver.session(database=self.database) as session:
            session.run(query, params).consume()
        self._sweep_orphans()

    def _sweep_orphans(self) -> None:
        """Tag/Actor nodes are shared across bundles; drop the unreferenced."""
        self.run("MATCH (t:Tag) WHERE NOT (t)--() DELETE t")
        self.run("MATCH (a:Actor) WHERE NOT (a)--() DELETE a")

    # -- main ingestion ------------------------------------------------------

    def ingest(self, pb: ParsedBundle) -> dict[str, int]:
        self.setup_constraints()
        self._clear_replaceable(pb)
        self._write_bundle(pb)
        self._write_concepts(pb)
        self._write_type_labels(pb)
        self._write_actors_and_trust(pb)
        self._write_sources(pb)
        self._write_sections(pb)
        self._write_links(pb)
        self._write_tags(pb)
        self._write_computation_edges(pb)
        self._write_logs(pb)
        self._sweep_bundle_orphans(pb)
        return pb.stats()

    def _clear_replaceable(self, pb: ParsedBundle) -> None:
        """Make ingest a sync: drop everything this parse will rebuild.

        Kept: Concept nodes (stable uids), Section nodes with unchanged text
        (their embeddings survive), Source/Tag/Actor nodes (MERGEd; orphans
        swept at the end).
        """
        b = pb.name
        self.run(
            """MATCH (c:Concept {bundle: $b})-[r:LINKS_TO|DERIVES_FROM|GENERATED_BY|
               VERIFIED_BY|TAGGED|EXECUTED_BY|ATTESTED_BY|COMPUTATION_FILE]->()
               DELETE r""", b=b)
        self.run(
            """MATCH (:Concept {bundle: $b})-[:HAS_SECTION]->(s:Section)
               -[r:CITES|MENTIONS|NEXT]->() DELETE r""", b=b)
        # sections that no longer exist in the bundle
        self.run(
            """MATCH (:Concept {bundle: $b})-[:HAS_SECTION]->(s:Section)
               WHERE NOT s.uid IN $uids DETACH DELETE s""",
            b=b, uids=[s.uid for s in pb.all_sections])
        # stubs, artifacts and log entries are cheap — rebuild from scratch
        self.run("MATCH (c:Concept {bundle: $b}) WHERE c.stub DETACH DELETE c", b=b)
        self.run("MATCH (a:Artifact {bundle: $b}) DETACH DELETE a", b=b)
        self.run("MATCH (l:LogEntry {bundle: $b}) DETACH DELETE l", b=b)

    def _sweep_bundle_orphans(self, pb: ParsedBundle) -> None:
        self.run(
            "MATCH (s:Source {bundle: $b}) WHERE NOT (s)--() DELETE s", b=pb.name)
        self._sweep_orphans()

    def _write_bundle(self, pb: ParsedBundle) -> None:
        self.run(
            """MERGE (b:Bundle {name: $name})
               SET b.okf_version = $version, b.root = $root,
                   b.log_frontmatter = $log_frontmatter""",
            name=pb.name, version=pb.okf_version,
            # basename only: the absolute ingest path is machine-local state,
            # not bundle content, and a shared graph should not carry it
            root=os.path.basename(pb.root.rstrip("/")) if pb.root else "",
            # §9 defines no frontmatter for log.md but the reference bundle
            # ships some; kept verbatim so a projection reproduces it exactly
            log_frontmatter=json.dumps(pb.log_frontmatter) if pb.log_frontmatter else None,
        )

    def _write_concepts(self, pb: ParsedBundle) -> None:
        rows = []
        for c in list(pb.concepts.values()) + list(pb.stubs.values()):
            rows.append({
                "uid": c.uid, "id": c.id, "bundle": c.bundle, "path": c.path,
                "dir": c.dir, "type": c.type, "title": c.title,
                "description": c.description, "resource": c.resource,
                "status": c.status, "stale_after": c.stale_after,
                "generated_at": c.generated_at,
                "trust_tier": c.trust_tier, "tags": c.tags,
                "extra": c.extra_frontmatter, "body": c.body,
                "runtime": c.runtime, "parameters_json": c.parameters_json,
                "receipt": c.receipt or None, "stub": c.stub,
                "type_label": c.type_label,
                # authored path strings, kept so the bundle can be re-emitted
                # from the graph alone (emit.py) instead of guessed at
                "executor_raw": c.executor_raw,
                "attester_raw": c.attester_raw,
                "computation_raw": c.computation_raw,
            })
        self.run(
            """UNWIND $rows AS row
               MERGE (c:Concept {uid: row.uid})
               SET c.id = row.id, c.bundle = row.bundle, c.path = row.path,
                   c.dir = row.dir, c.type = row.type, c.title = row.title,
                   c.description = row.description, c.resource = row.resource,
                   c.status = row.status,
                   c.stale_after = CASE WHEN row.stale_after IS NULL THEN NULL
                                        ELSE date(row.stale_after) END,
                   c.generated_at = CASE WHEN row.generated_at IS NULL THEN NULL
                                         ELSE datetime(row.generated_at) END,
                   c.trust_tier = row.trust_tier, c.tags = row.tags,
                   c.extra_frontmatter = row.extra, c.body = row.body,
                   c.runtime = row.runtime, c.parameters_json = row.parameters_json,
                   c.receipt = row.receipt, c.stub = row.stub,
                   c.type_label = row.type_label,
                   c.executor_raw = row.executor_raw,
                   c.attester_raw = row.attester_raw,
                   c.computation_raw = row.computation_raw
               WITH c, row
               MATCH (b:Bundle {name: row.bundle})
               MERGE (c)-[:IN_BUNDLE]->(b)""",
            rows=rows,
        )

    def _write_type_labels(self, pb: ParsedBundle) -> None:
        """Secondary label per OKF type ('BigQuery Table' -> :BigQueryTable).

        Labels cannot be parameterized, so group by label; label names come
        from sanitized type strings, not user input at query time. Stale
        labels from a previous ingest (a concept whose type changed) are
        removed so label-scoped queries never see ghosts.
        """
        by_label: dict[str, list[str]] = {}
        for c in pb.concepts.values():
            by_label.setdefault(c.type_label, []).append(c.uid)

        # find and remove stale secondary labels
        stale: dict[str, list[str]] = {}
        for rec in self.run(
            """MATCH (c:Concept {bundle: $b}) WHERE NOT coalesce(c.stub, false)
               RETURN c.uid AS uid, labels(c) AS labels, c.type_label AS want""",
            b=pb.name,
        ).records:
            for lbl in rec["labels"]:
                if lbl not in ("Concept", rec["want"]):
                    stale.setdefault(lbl, []).append(rec["uid"])
        for label, uids in stale.items():
            safe = label.replace("`", "")
            self.run(f"MATCH (c:Concept) WHERE c.uid IN $uids REMOVE c:`{safe}`",
                     uids=uids)
        for label, uids in by_label.items():
            safe = label.replace("`", "")
            self.run(f"MATCH (c:Concept) WHERE c.uid IN $uids SET c:`{safe}`",
                     uids=uids)

    def _write_actors_and_trust(self, pb: ParsedBundle) -> None:
        self.run(
            """UNWIND $rows AS row
               MERGE (a:Actor {id: row.id})
               SET a.kind = row.kind, a.name = row.name,
                   a.producer = row.producer, a.version = row.version""",
            rows=[vars(a) for a in pb.actors.values()],
        )
        gen_rows = [
            {"uid": c.uid, "actor": c.generated_by.id, "at": c.generated_at}
            for c in pb.concepts.values() if c.generated_by
        ]
        # replaceable edges were cleared above -> plain CREATE, nullable at
        self.run(
            """UNWIND $rows AS row
               MATCH (c:Concept {uid: row.uid}), (a:Actor {id: row.actor})
               CREATE (c)-[g:GENERATED_BY]->(a)
               SET g.at = CASE WHEN row.at IS NULL THEN NULL ELSE datetime(row.at) END""",
            rows=gen_rows,
        )
        ver_rows = [
            {"uid": c.uid, "actor": actor.id, "at": at, "order": i}
            for c in pb.concepts.values() for i, (actor, at) in enumerate(c.verified)
        ]
        self.run(
            """UNWIND $rows AS row
               MATCH (c:Concept {uid: row.uid}), (a:Actor {id: row.actor})
               CREATE (c)-[v:VERIFIED_BY]->(a)
               SET v.order = row.order,
                   v.at = CASE WHEN row.at IS NULL THEN NULL ELSE datetime(row.at) END""",
            rows=ver_rows,
        )

    def _write_sources(self, pb: ParsedBundle) -> None:
        src_rows, edge_rows, resolve_rows, resolve_art_rows = [], [], [], []
        for c in pb.concepts.values():
            for s in c.sources:
                src_rows.append({
                    "uid": s.uid, "sid": s.sid, "resource": s.resource,
                    "title": s.title, "author": s.author,
                    "last_modified": s.last_modified,
                    "is_scope": s.is_scope, "bundle": pb.name,
                })
                edge_rows.append({
                    "concept": c.uid, "source": s.uid, "sid": s.sid,
                    "usage_count": s.usage_count, "order": s.order,
                    "wfrom": s.usage_window_from, "wto": s.usage_window_to,
                    # Source nodes are deduped by resource, so two concepts
                    # citing the same document share one node and the last
                    # writer wins. These three are declared *per citation*, so
                    # the edge is where the authored value actually lives.
                    "title": s.title, "author": s.author,
                    "last_modified": s.last_modified,
                })
                if s.resolves_to_concept:
                    resolve_rows.append({"source": s.uid, "target": s.resolves_to_concept})
                if s.resolves_to_artifact:
                    resolve_art_rows.append({"source": s.uid, "target": s.resolves_to_artifact})
        # intrinsic facts -> Source node; declaration-scoped (usage_count,
        # usage_window: declared by the citing concept) -> DERIVES_FROM edge
        self.run(
            """UNWIND $rows AS row
               MERGE (s:Source {uid: row.uid})
               SET s.sid = row.sid, s.resource = row.resource, s.bundle = row.bundle,
                   s.title = coalesce(row.title, s.title),
                   s.author = coalesce(row.author, s.author),
                   s.is_scope = row.is_scope,
                   s.last_modified = CASE WHEN row.last_modified IS NULL THEN s.last_modified
                                          ELSE date(row.last_modified) END""",
            rows=src_rows,
        )
        self.run(
            """UNWIND $rows AS row
               MATCH (c:Concept {uid: row.concept}), (s:Source {uid: row.source})
               CREATE (c)-[d:DERIVES_FROM]->(s)
               SET d.sid = row.sid, d.usage_count = row.usage_count, d.order = row.order,
                   d.title = row.title, d.author = row.author,
                   d.last_modified = CASE WHEN row.last_modified IS NULL THEN NULL
                                          ELSE date(row.last_modified) END,
                   d.usage_window_from = CASE WHEN row.wfrom IS NULL THEN NULL ELSE date(row.wfrom) END,
                   d.usage_window_to   = CASE WHEN row.wto   IS NULL THEN NULL ELSE date(row.wto)   END""",
            rows=edge_rows,
        )
        self.run(
            """UNWIND $rows AS row
               MATCH (s:Source {uid: row.source}), (c:Concept {uid: row.target})
               MERGE (s)-[:RESOLVES_TO]->(c)""",
            rows=resolve_rows,
        )
        # artifacts referenced by sources are written in _write_computation_edges;
        # defer the RESOLVES_TO edge until after (store rows on self)
        self._pending_source_artifacts = resolve_art_rows

    def _write_sections(self, pb: ParsedBundle) -> None:
        rows, cites, mentions, nexts = [], [], [], []
        for c in pb.concepts.values():
            prev = None
            for s in c.sections:
                rows.append({
                    "uid": s.uid, "concept": c.uid, "heading": s.heading,
                    "order": s.order, "text": s.text, "chars": len(s.text),
                    "bundle": pb.name,
                })
                for src_uid in s.cites:
                    cites.append({"section": s.uid, "source": src_uid})
                for target in s.mentions:
                    mentions.append({"section": s.uid, "concept": target})
                if prev:
                    nexts.append({"a": prev, "b": s.uid})
                prev = s.uid
        # a Section keeps its embedding only while its text is unchanged
        self.run(
            """UNWIND $rows AS row
               MERGE (s:Section {uid: row.uid})
               ON MATCH SET s.embedding = CASE WHEN s.text = row.text
                                               THEN s.embedding ELSE NULL END
               SET s.heading = row.heading, s.order = row.order, s.text = row.text,
                   s.char_count = row.chars, s.bundle = row.bundle
               WITH s, row
               MATCH (c:Concept {uid: row.concept})
               MERGE (c)-[:HAS_SECTION]->(s)""",
            rows=rows,
        )
        self.run(
            """UNWIND $rows AS row
               MATCH (s:Section {uid: row.section}), (src:Source {uid: row.source})
               CREATE (s)-[:CITES]->(src)""",
            rows=cites,
        )
        self.run(
            """UNWIND $rows AS row
               MATCH (s:Section {uid: row.section}), (c:Concept {uid: row.concept})
               CREATE (s)-[:MENTIONS]->(c)""",
            rows=mentions,
        )
        self.run(
            """UNWIND $rows AS row
               MATCH (a:Section {uid: row.a}), (b:Section {uid: row.b})
               CREATE (a)-[:NEXT]->(b)""",
            rows=nexts,
        )

    def _write_links(self, pb: ParsedBundle) -> None:
        rows = [{
            "source": l.source_uid, "target": l.target_uid, "text": l.text,
            "section": l.section, "raw": l.raw, "resolved": l.resolved,
        } for l in pb.links]
        self.run(
            """UNWIND $rows AS row
               MATCH (a:Concept {uid: row.source}), (b:Concept {uid: row.target})
               CREATE (a)-[r:LINKS_TO]->(b)
               SET r.section = coalesce(row.section, ''), r.raw = row.raw,
                   r.text = row.text, r.resolved = row.resolved""",
            rows=rows,
        )

    def _write_tags(self, pb: ParsedBundle) -> None:
        rows = [{"uid": c.uid, "tag": t}
                for c in pb.concepts.values() for t in c.tags]
        self.run(
            """UNWIND $rows AS row
               MERGE (t:Tag {name: row.tag})
               WITH t, row
               MATCH (c:Concept {uid: row.uid})
               CREATE (c)-[:TAGGED]->(t)""",
            rows=rows,
        )

    def _write_computation_edges(self, pb: ParsedBundle) -> None:
        self.run(
            """UNWIND $rows AS row
               MERGE (a:Artifact {uid: row.uid})
               SET a.path = row.path, a.kind = row.kind, a.bundle = row.bundle,
                   a.text = row.text, a.sha256 = row.sha256""",
            rows=[{"text": None, "sha256": None, **a, "bundle": pb.name}
                  for a in pb.artifacts.values()],
        )
        exec_rows, attest_rows, comp_rows = [], [], []
        for c in pb.concepts.values():
            if c.executor_resource and c.executor_resource.startswith(f"{pb.name}:"):
                exec_rows.append({"uid": c.uid, "target": c.executor_resource})
            if c.attester_resource and c.attester_resource.startswith(f"{pb.name}:art:"):
                attest_rows.append({"uid": c.uid, "target": c.attester_resource})
            if c.computation_path and c.computation_path.startswith(f"{pb.name}:art:"):
                comp_rows.append({"uid": c.uid, "target": c.computation_path})
        self.run(
            """UNWIND $rows AS row
               MATCH (c:Concept {uid: row.uid}), (t:Concept {uid: row.target})
               CREATE (c)-[:EXECUTED_BY]->(t)""",
            rows=exec_rows,
        )
        self.run(
            """UNWIND $rows AS row
               MATCH (c:Concept {uid: row.uid}), (t:Artifact {uid: row.target})
               CREATE (c)-[:ATTESTED_BY]->(t)""",
            rows=attest_rows,
        )
        self.run(
            """UNWIND $rows AS row
               MATCH (c:Concept {uid: row.uid}), (t:Artifact {uid: row.target})
               CREATE (c)-[:COMPUTATION_FILE]->(t)""",
            rows=comp_rows,
        )
        # sources whose resource path points at a bundle artifact (README:
        # (:Source)-[:RESOLVES_TO]->(:Concept|:Artifact))
        self.run(
            """UNWIND $rows AS row
               MATCH (s:Source {uid: row.source}), (a:Artifact {uid: row.target})
               MERGE (s)-[:RESOLVES_TO]->(a)""",
            rows=getattr(self, "_pending_source_artifacts", []),
        )

    def _write_logs(self, pb: ParsedBundle) -> None:
        rows = [{
            "uid": e.uid, "date": e.date, "kind": e.kind, "text": e.text,
            "order": e.order, "bundle": pb.name, "refs": e.references,
            "dir": e.dir,
        } for e in pb.log_entries if e.date]
        self.run(
            """UNWIND $rows AS row
               MERGE (l:LogEntry {uid: row.uid})
               SET l.date = date(row.date), l.kind = row.kind, l.text = row.text,
                   l.order = row.order, l.bundle = row.bundle, l.dir = row.dir
               WITH l, row
               MATCH (b:Bundle {name: row.bundle})
               MERGE (l)-[:IN_BUNDLE]->(b)
               WITH l, row
               UNWIND row.refs AS ref
               MATCH (c:Concept {uid: ref})
               MERGE (l)-[:REFERENCES]->(c)""",
            rows=rows,
        )

    # -- lexical layer: embeddings + indexes ------------------------------------

    def embed_sections(self, embedder, bundle: Optional[str] = None,
                       batch_size: int = 32,
                       dimensions: Optional[int] = None) -> int:
        """Embed `title — heading\\n\\ntext` for every Section missing a vector.

        When `dimensions` is given, sections whose stored vector has a
        different size are re-embedded too — switching embedding providers
        (e.g. offline hash rehearsal -> OpenAI) would otherwise leave a mixed
        set of vectors behind the same index.
        """
        where = "WHERE (s.embedding IS NULL"
        where += " OR size(s.embedding) <> $dims)" if dimensions else ")"
        where += " AND s.bundle = $bundle" if bundle else ""
        records = self.run(
            f"""MATCH (c:Concept)-[:HAS_SECTION]->(s:Section) {where}
                RETURN s.uid AS uid, c.title AS title, s.heading AS heading,
                       s.text AS text""",
            bundle=bundle, dims=dimensions,
        ).records
        total = 0
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            rows = []
            for r in batch:
                text = f"{r['title']} — {r['heading']}\n\n{r['text']}"
                rows.append({"uid": r["uid"], "vector": embedder.embed_query(text)})
            self.run(
                """UNWIND $rows AS row
                   MATCH (s:Section {uid: row.uid})
                   CALL db.create.setNodeVectorProperty(s, 'embedding', row.vector)""",
                rows=rows,
            )
            total += len(rows)
        return total

    def create_retrieval_indexes(self, dimensions: int) -> None:
        from neo4j_graphrag.indexes import create_fulltext_index, create_vector_index
        # Guard against the provider-switch landmine: a vector index keeps its
        # dimensions forever, so rehearsing with the 256-dim hash embedder and
        # then re-running with OpenAI (1536) would silently break retrieval.
        for rec in self.run("SHOW VECTOR INDEXES YIELD name, options").records:
            if rec["name"] != VECTOR_INDEX:
                continue
            existing = (rec["options"].get("indexConfig") or {}).get("vector.dimensions")
            if existing is not None and int(existing) != int(dimensions):
                self.run(f"DROP INDEX {VECTOR_INDEX}")
        create_vector_index(
            self.driver, VECTOR_INDEX, label="Section",
            embedding_property="embedding", dimensions=dimensions,
            similarity_fn="cosine", neo4j_database=self.database,
        )
        create_fulltext_index(
            self.driver, FULLTEXT_INDEX, label="Section",
            node_properties=["text", "heading"], neo4j_database=self.database,
        )
