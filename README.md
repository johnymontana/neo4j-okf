# neo4j-okf — Google's Open Knowledge Format meets Neo4j

Demo + talk assets showing how to map [OKF (Open Knowledge Format)](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
bundles onto a Neo4j property graph, ingest them deterministically, and query them with
[**neo4j-graphrag**](https://neo4j.com/docs/neo4j-graphrag-python/current/) — including the places where the
graph visibly beats vector-only RAG (governance-aware retrieval, impact analysis, Text2Cypher).

```
okf bundle (markdown + frontmatter)          Neo4j property graph
────────────────────────────────────         ─────────────────────────────────────────────
metrics/gross-margin.md      ────────▶       (:Concept:Metric {status, trust_tier, …})
  [link](../computations/x.md)  ─────▶         -[:LINKS_TO {section}]->(:Concept:AttestedComputation)
  sources: [...]                ─────▶         -[:DERIVES_FROM]->(:Source)-[:RESOLVES_TO]->(:Concept)
  verified: {by: human:…}       ─────▶         -[:VERIFIED_BY {at}]->(:Actor {kind:'human'})
  # Definition …                ─────▶         -[:HAS_SECTION]->(:Section {embedding})-[:CITES]->(:Source)
```

## Quickstart

Dependencies are managed with [uv](https://docs.astral.sh/uv/) — one `uv sync` creates the venv, installs
everything, and installs `okf_graph` itself (editable) with the `okf-graph` CLI.

```bash
docker compose up -d                   # Neo4j 2025.x at bolt://localhost:7687 (neo4j/demodemo)
uv sync                                # deps + package + CLI (.venv, uv.lock)
cp .env.example .env                   # add your OPENAI_API_KEY

# ingest the sample bundle (deterministic — runs with NO api key)
uv run okf-graph ingest bundles/acme_retail --reset

# + embeddings + vector/fulltext indexes (needs OPENAI_API_KEY)
uv run okf-graph ingest bundles/acme_retail --reset --embed

uv run jupyter lab notebooks/okf_graphrag_demo.ipynb    # the demo
```

Offline smoke test, no key (also safe to rehearse with — the index dimension guard re-embeds when you switch
back to OpenAI): `uv run okf-graph ingest bundles/acme_retail --reset --embed --embedding-provider hash`

Tests: `uv run pytest -q`

## What's here

| path | what |
|---|---|
| `okf_graph/parser.py` | OKF v0.2 bundle → in-memory model (frontmatter families, links, sections, footnote citations, logs). Deterministic, permissive per SPEC §11. |
| `okf_graph/ingest.py` | model → Neo4j: idempotent batched MERGEs, secondary labels from `type`, constraints, embeddings pass, vector + fulltext indexes. No APOC. |
| `okf_graph/queries.py` | governance Cypher (trust tiers, staleness, impact, co-citation) + the `GOVERNED_RETRIEVAL_QUERY` for `VectorCypherRetriever` + Text2Cypher schema/examples. |
| `okf_graph/embedding.py` | OpenAI embedder factory + deterministic hash embedder for offline rehearsal. |
| `notebooks/okf_graphrag_demo.ipynb` | the live demo: parse → ingest → explore → baseline RAG trap → graph-aware RAG → Text2Cypher → (appendix) SimpleKGPipeline domain layer. |
| `tests/` | parser smoke tests (`uv run pytest -q`) — counts, trust tiers, stubs, fence-aware sectioning, footnote citation semantics. |
| `bundles/acme_retail/` | sample OKF v0.2 bundle vendored from [GoogleCloudPlatform/knowledge-catalog](https://github.com/GoogleCloudPlatform/knowledge-catalog) (Apache-2.0) so the demo runs offline. |
| `slides/okf-neo4j.pptx` | the talk deck (diagrams in `slides/diagrams/`). |

## The mapping

| OKF construct (SPEC v0.2) | Graph element |
|---|---|
| bundle | `(:Bundle {name, okf_version})` |
| concept file | `(:Concept)` + secondary label from `type` (`BigQuery Table` → `:BigQueryTable`) |
| concept id = path sans `.md` | `Concept.id`, `uid = bundle + ':' + id` |
| frontmatter scalars | node properties (`status` defaults to `stable`, dates typed as `date()`/`datetime()`) |
| unknown frontmatter keys | preserved in `Concept.extra_frontmatter` (JSON) — consumers MUST NOT reject (§11) |
| markdown link | `[:LINKS_TO {section, text, resolved}]` — section-scoped, so prose context survives |
| broken link (§6.1) | stub `(:Concept {stub:true})` — "not-yet-written knowledge" becomes a queryable backlog |
| directory tree | `Concept.dir` property (+ `IN_BUNDLE`); the tree is derivable, the graph is what's indexed |
| `sources[]` (§5.1) | `(:Source)` deduped per bundle by resource; intrinsic signals (`author`, `last_modified`) on the node, declaration-scoped (`usage_count`, `usage_window`) on `[:DERIVES_FROM]` |
| source → internal path | `(:Source)-[:RESOLVES_TO]->(:Concept/:Artifact)` — provenance chains become traversable |
| `generated` / `verified` (§5.2) | `(:Actor {kind: human/agent/process})` + `[:GENERATED_BY {at}]` / `[:VERIFIED_BY {at}]` |
| trust tier (§5.3) | derived in Cypher from verifier kinds; also materialized as `Concept.trust_tier` |
| `status`, `stale_after` (§5.4–5.5) | properties; staleness = `date() >= stale_after` at query time |
| Attested Computation (§10) | `:AttestedComputation` label + `runtime`, `parameters_json`, `receipt`; `[:EXECUTED_BY]->(:Skill)`, `[:ATTESTED_BY]->(:Artifact)` |
| body `# sections` (§4.2) | `(:Section {heading, order, text, embedding})` + `[:HAS_SECTION]`, `[:NEXT]` — OKF's conventional headings are the chunking |
| links, section-scoped | `(:Section)-[:MENTIONS]->(:Concept)` — which section grounds which relationship |
| footnote refs `[^id]` (§5.1) | `(:Section)-[:CITES]->(:Source)` — claim-level provenance |
| `computation:` file form (§10.3) | `[:COMPUTATION_FILE]->(:Artifact)` |
| `tags` | `(:Tag)` + `[:TAGGED]` (and kept as array property) |
| `log.md` (§9) | `(:LogEntry {date, kind})-[:REFERENCES]->(:Concept)` |
| `index.md` (§8) | not ingested — it's derivable (progressive disclosure is a *serving* concern); root `okf_version` lifted to Bundle |

### Sync semantics

`ingest()` is a **sync**, not an append: for the bundle being ingested it clears replaceable
relationships (trust, links, provenance, tags), removes vanished sections/stubs/log entries, refreshes
secondary labels, and rebuilds from the parse — so the derived trust tier can never drift from the
materialized `trust_tier`, and a re-run never duplicates edges. Sections keep their embeddings unless
their text changed (changed text nulls the vector so the next embed pass picks it up). `reset(bundle)`
remains the hard wipe. The demo is single-tenant for clarity; for multi-bundle estates add a `bundle`
predicate to the governance/retrieval queries (or use per-bundle databases).

## Why a graph (the demo's argument)

1. **Governance queries are one-hop Cypher**: trust tiers, staleness reports, impact analysis, co-citation.
2. **Governed GraphRAG**: vector similarity alone happily retrieves a *deprecated* metric definition — it's
   well-written text about exactly the topic. `VectorCypherRetriever` walks from the matched `:Section` to its
   `:Concept`, reads `status`/`trust_tier`/`stale_after`, follows `LINKS_TO` to the sanctioned
   `:AttestedComputation` and its SQL, and hands the LLM context that carries governance.
3. **Text2Cypher** answers questions that have no similarity anchor ("which metrics were never human-reviewed?").
4. **Two construction modes compose**: deterministic structural ingestion (no LLM) + optional
   `SimpleKGPipeline` entity extraction over the prose — lexical layer and domain layer in one graph.

## Attribution

`bundles/acme_retail` and the OKF specification are from
[GoogleCloudPlatform/knowledge-catalog](https://github.com/GoogleCloudPlatform/knowledge-catalog), Apache License 2.0.
This repo is a community demo and is not affiliated with Google.
