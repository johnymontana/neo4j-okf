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

…and back out again. The graph is not a dead end: it **projects** to a portable
OKF bundle, and arbitrary documents can be turned into one.

```
                    parse                              project
  OKF bundle  ─────────────▶  ┌───────────────┐  ◀─────────────  Neo4j
                              │ ParsedBundle  │                    ▲
  documents,  ─────────────▶  └───────────────┘  ─────────────┐    │
  web pages       wiki               │                emit    │    │ ingest
                                     └────────────────────────┴────┘
                                            OKF bundle (files)
```

Three things follow from making `ParsedBundle` the hub rather than the graph:

* **Projection is a query, not an export.** `okf-graph project` takes a
  selection — trust tier, staleness, tags, a seed concept and a hop radius —
  so the bundle you get out is one the graph decided on, not one that ever
  existed on disk. "Assemble the human-reviewed, non-stale context for gross
  margin, as a tarball" is a Cypher query.
* **LLM-authored knowledge enters through the same door.** `okf-graph wiki`
  turns documents into an OKF bundle on disk, then the *existing* deterministic
  parser ingests it. One mapping, nothing to drift, and the model's output is a
  git-diffable artifact a human can review in a PR.
* **Round-trip is a tested property.** `parse → ingest → project → emit → parse`
  returns an equivalent model, and the projection is byte-identical to
  serializing the parse directly (`tests/test_roundtrip.py`, `tests/test_project.py`).

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

Then the other two directions — both run offline, with no API key:

```bash
# graph → OKF. A governed subset, as a portable tarball.
uv run okf-graph project /tmp/servable --bundle acme_retail \
    --min-trust human-reviewed --status stable --exclude-stale --format tar

# a context pack: everything one hop from gross margin, and whatever its
# Attested Computations need in order to actually be runnable
uv run okf-graph project /tmp/pack --bundle acme_retail \
    --seed metrics/gross-margin --hops 1

# documents → an LLM-authored OKF bundle → the same graph
uv run okf-graph wiki bundles/acme_wiki --path corpus/acme_intranet --ingest
```

`--extractor heuristic` is the default and needs neither a key nor the network;
`--extractor openai` or `--extractor anthropic` swaps in a real model.

Offline smoke test, no key (also safe to rehearse with — the index dimension guard re-embeds when you switch
back to OpenAI): `uv run okf-graph ingest bundles/acme_retail --reset --embed --embedding-provider hash`

Tests: `uv run pytest -q` (the projection tests skip themselves when no Neo4j is running).

## What's here

| path | what |
|---|---|
| `okf_graph/parser.py` | OKF v0.2 bundle → in-memory model (frontmatter families, links, sections, footnote citations, logs). Deterministic, permissive per SPEC §11. |
| `okf_graph/ingest.py` | model → Neo4j: idempotent batched MERGEs, secondary labels from `type`, constraints, embeddings pass, vector + fulltext indexes. No APOC needed here — only the optional `SimpleKGPipeline` appendix uses APOC (neo4j-graphrag's writer/resolver call it; `docker-compose.yml` installs the plugin). |
| `okf_graph/emit.py` | model → OKF markdown. The inverse of the parser: frontmatter in SPEC key order, regenerated `index.md` (§8), `log.md` (§9), path safety and collision detection. |
| `okf_graph/project.py` | Neo4j → model → files. Selection filters, closure over the Attested Computation consumer contract, directory / `.tar.gz` / `.zip` writers, and a `.okf/projection.json` manifest recording exactly what was cut. |
| `okf_graph/documents.py` | source acquisition: local `.md`/`.txt`/`.html`/`.pdf`, URL fetch and shallow crawl, HTML → markdown with headings preserved. Deny-by-default fetch policy (see [Fetching the web](#fetching-the-web)). |
| `okf_graph/wiki.py` | documents → concept drafts → an OKF bundle. Pluggable extractor: `heuristic` (offline, deterministic), `openai`, `anthropic`. |
| `okf_graph/queries.py` | governance Cypher (trust tiers, staleness, impact, co-citation) + the `GOVERNED_RETRIEVAL_QUERY` for `VectorCypherRetriever` + Text2Cypher schema/examples. |
| `okf_graph/embedding.py` | OpenAI embedder factory + deterministic hash embedder for offline rehearsal. |
| `notebooks/okf_graphrag_demo.ipynb` | the live demo: parse → ingest → explore → baseline RAG trap → graph-aware RAG → Text2Cypher → projection → LLM-authored wiki → (appendix) SimpleKGPipeline domain layer. |
| `tests/` | `uv run pytest -q` — parser semantics, round-trip equivalence, document normalization, fetch-policy refusals, the wiki pipeline, and (Neo4j-gated) projection. |
| `bundles/acme_retail/` | sample OKF v0.2 bundle vendored from [GoogleCloudPlatform/knowledge-catalog](https://github.com/GoogleCloudPlatform/knowledge-catalog) (Apache-2.0) so the demo runs offline. |
| `corpus/acme_intranet/` | five documents of simulated org exhaust — a wiki page, a finance memo, a warehouse README, a data dictionary, an on-call runbook — in HTML, markdown and plain text. Input for `okf-graph wiki`. |
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
| `log.md` (§9) | `(:LogEntry {date, kind, dir})-[:REFERENCES]->(:Concept)`; any frontmatter kept verbatim on `Bundle.log_frontmatter` |
| `index.md` (§8) | not ingested — it's derivable (progressive disclosure is a *serving* concern), and the projection regenerates it; root `okf_version` lifted to Bundle |
| non-`.md` files | `(:Artifact {path, kind, sha256, size, text})` — every one, referenced or not, with contents carried under 64 KB so a projection is runnable rather than just structurally complete |

### Sync semantics

`ingest()` is a **sync**, not an append: for the bundle being ingested it clears replaceable
relationships (trust, links, provenance, tags), removes vanished sections/stubs/log entries, refreshes
secondary labels, and rebuilds from the parse — so the derived trust tier can never drift from the
materialized `trust_tier`, and a re-run never duplicates edges. Sections keep their embeddings unless
their text changed (changed text nulls the vector so the next embed pass picks it up). `reset(bundle)`
remains the hard wipe. The demo is single-tenant for clarity; for multi-bundle estates add a `bundle`
predicate to the governance/retrieval queries (or use per-bundle databases).

## The reverse direction: graph → bundle

`ingest()` is a sync; `project()` is a **query with a serializer on the end**.

| flag | what it selects |
|---|---|
| `--bundle` | which bundle in the graph (a directory path works too) |
| `--concept ID` | just these concepts (repeatable) |
| `--seed ID --hops N` | everything within N dependency hops of a concept |
| `--tag`, `--type`, `--status` | frontmatter filters (repeatable) |
| `--min-trust` | `unverified` \| `machine-confirmed` \| `human-reviewed` (§5.3) |
| `--exclude-stale` | drop concepts where `today >= stale_after` (§5.5) |
| `--include-referenced` | add one hop of link targets so links resolve |
| `--format dir\|tar\|zip` | directory, `.tar.gz`, or `.zip` — all reproducible |
| `--dry-run` | print the manifest and file list, write nothing |

Two behaviours are worth calling out because they are governance, not plumbing:

* **The consumer contract is always closed over.** `skills/run-on-bq` is
  `unverified`, so `--min-trust human-reviewed` would drop it — while leaving
  two Attested Computations that name it as their `executor`. A bundle that
  ships a sanctioned computation without the skill needed to run it asserts a
  contract nobody can follow, so `EXECUTED_BY` / `ATTESTED_BY` /
  `COMPUTATION_FILE` targets are pulled back in regardless of the filter.
* **Nothing is quietly dropped.** Links that point at concepts the filter
  excluded stay in the markdown — a broken cross-link is legal OKF (§6.1) and
  it is the honest record — but every one is listed in `.okf/projection.json`,
  along with unwritten artifacts and the full list of round-trip caveats.

The manifest lives under a dot-directory on purpose: the parser skips dotted
paths, so it can never be mistaken for bundle content on the way back in.

## Documents → an LLM-authored wiki → the graph

`okf-graph wiki` is the "agents are better wiki maintainers than we are" claim,
wired up. Documents go in; an OKF bundle comes out; the ordinary parser ingests
it.

```
corpus/acme_intranet/*.{html,md,txt}
      │  documents.py     strip boilerplate, keep headings, capture provenance
      ▼
  concept drafts          heuristic | openai | anthropic
      │  wiki.py          merge by id, resolve cross-references, mint stubs
      ▼
  bundles/acme_wiki/      real OKF: frontmatter, sections, sources[], footnotes
      │  parser.py        ← the same parser that reads Google's bundles
      ▼
  Neo4j
```

What the generated bundle asserts about itself:

| OKF construct | what the wiki builder writes |
|---|---|
| `generated.by` (§5.2) | the real producer — `okf-wiki/<model>`, or `process:okf-wiki-heuristic` |
| `verified` (§5.2) | **absent**, always — so trust tier is `unverified` (§5.3) |
| `status` (§5.4) | `draft` |
| `sources[]` (§5.1) | one entry per source document, with its author and last-modified date |
| `references/<slug>.md` (§6.3) | each source document mirrored as a first-class concept, so `sources[].resource` resolves *inside* the bundle and provenance becomes `(:Source)-[:RESOLVES_TO]->(:Concept)` — a traversal, not a string |
| `[^sid]` footnotes (§5.1) | per section, so claims carry attribution rather than files |
| links to unwritten concepts (§6.1) | kept — they become stub nodes, i.e. a queryable authoring backlog |

The demo point: `corpus/acme_intranet/wiki-gross-margin.html` is an intranet page
still describing the **pre-2026 margin formula**. The extractor faithfully
records it — and because the result is `draft` / `unverified`, it lands in the
same graph as Finance's `human-reviewed` definition without being able to
outrank it. That is the whole argument for putting trust in the format.

`build_wiki` re-parses what it just wrote and refuses to return if any concept
did not survive. Emitting markdown and reading it back makes the filesystem a
channel, and its failure modes — reserved filenames, case-folding collisions,
duplicate slugs — are all silent; one equality check catches the class.

### Fetching the web

`okf-graph fetch` and `--url` go through a deny-by-default policy: `http`/`https`
only, ports 80/443, robots.txt respected, credentials-in-URL refused, response
size capped on *decoded* bytes, and every redirect hop re-validated against
private, loopback, link-local, reserved and IPv4-mapped-IPv6 address space.
`--allow-private-hosts` opts out for intranet testing.

One residual risk, stated rather than papered over: the name is resolved for the
check and then again by the HTTP client, so a DNS answer that changes between
the two (rebinding) is not caught. Fetch untrusted URLs from somewhere that
cannot reach anything you care about.

## Why a graph (the demo's argument)

1. **Governance queries are one-hop Cypher**: trust tiers, staleness reports, impact analysis, co-citation.
2. **Governed GraphRAG**: vector similarity alone happily retrieves a *deprecated* metric definition — it's
   well-written text about exactly the topic. `VectorCypherRetriever` walks from the matched `:Section` to its
   `:Concept`, reads `status`/`trust_tier`/`stale_after`, follows `LINKS_TO` to the sanctioned
   `:AttestedComputation` and its SQL, and hands the LLM context that carries governance.
3. **Text2Cypher** answers questions that have no similarity anchor ("which metrics were never human-reviewed?").
4. **Two construction modes compose**: deterministic structural ingestion (no LLM) + optional
   `SimpleKGPipeline` entity extraction over the prose — lexical layer and domain layer in one graph.
5. **The format is the interchange, the graph is the selection.** Because the
   projection is lossless, nothing is locked in: OKF goes in, OKF comes out, and
   what the database adds is the ability to decide *which* OKF comes out.

## Round-trip fidelity, precisely

The claim is **equivalence**, not byte equality, and the difference is
enumerated rather than hand-waved:

* `parse → emit → parse` reproduces the model exactly — concepts, sections,
  links, citations, sources, trust tiers, log references and artifact contents.
* `parse → ingest → project → emit` is **byte-identical** to `parse → emit`.
* `emit` is idempotent from the second pass; the first pass normalizes.

Where re-emitted text differs from hand-authored text — regenerated `index.md`,
explicit `status: stable`, `Z` normalized to `+00:00`, unknown keys moved below
the known families — every case is listed in `emit.ROUNDTRIP_NOTES` and copied
into each projection's manifest. Known limits: unknown keys *nested* inside a
known family (`generated.model`, `sources[].license`) are not retained, and
artifacts over 64 KB or non-UTF-8 are recorded by hash and path rather than
written.

## Attribution

`bundles/acme_retail` and the OKF specification are from
[GoogleCloudPlatform/knowledge-catalog](https://github.com/GoogleCloudPlatform/knowledge-catalog), Apache License 2.0.
This repo is a community demo and is not affiliated with Google.
