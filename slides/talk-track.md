# Talk track — From Markdown to Knowledge Graph

**Runtime:** ~36 min as written (23 slides + two live demo segments) + Q&A.
**Cutting to a hard 30:** compress slides 11–12 to one beat each (−2 min), do §4.5/§4.6 only if ahead (−2 min), fold slide 21 into the close (−1 min).
Spoken text is written to be said, not read — trim to your own cadence. Stage directions in *[brackets]*.

---

## Pre-flight (before you're mic'd)

Night before: `docker compose up -d` (pulls APOC on first start), `uv sync`, `.env` with your OpenAI key, then **run the whole notebook top to bottom once with the real key**. This validates the §6 ranking with real embeddings and gives you a fully-executed copy — save it as a second tab; it is your no-network fallback.

At the podium: fresh notebook open (Kernel → Restart & Clear Output), executed copy in tab 2, Neo4j Browser at `localhost:7474` in tab 3 (optional flourish), terminal at repo root, font size cranked. The only thing that needs the internet is OpenAI — ingest, Cypher, and the viz all run local.

---

## Part I — The format

### 1 · Title — 0:00

Hi, I'm Will. This talk is about two things that turned out to want each other. Google shipped an open spec called OKF — the Open Knowledge Format — which formalizes something a lot of us have been doing informally: keeping organizational knowledge as a pile of markdown files that agents read *and write*. And on the other side there's Neo4j, which is where knowledge shaped like a graph wants to live.

The claim I want to earn in the next half hour: an OKF bundle already **is** a graph — it's just serialized as files. Materialize it in Neo4j and you get two things the files alone can't give you: governance you can query, and retrieval that's governed. Everything I show is runnable from the repo on the last slide — parser, notebook, this deck.

*[Advance.]*

### 2 · Why Google shipped a knowledge format at all — 0:45

Start with the problem, because the spec makes no sense without it. The context an agent needs to be useful — what this table means, how the metric is actually defined, which runbook to follow — all of that exists in your org. It just exists as five kinds of exhaust. *[gesture across the boxes]* Catalog, wiki, code comments, dashboards, and the senior engineer who leaves at five.

So every agent team rebuilds context assembly from scratch, per vendor, per team. And the vendors are happy to sell you a proprietary catalog to hold it, which is how knowledge gets locked in and goes stale silently.

OKF's bet is the unfashionable one: make knowledge a **format**, not a platform. Markdown, YAML frontmatter, links. Files you can cat, diff, and git-clone. There's a second observation buried in their announcement that I love: agents are often *better* wiki maintainers than we are — they don't get bored and they don't forget cross-references. Hold that thought, because it's why the trust machinery we'll see in a minute had to exist.

### 3 · The whole talk in one picture — 2:15

Here's the entire talk. Left: an OKF bundle — files with frontmatter, cross-linked. Right: the same bytes as a labeled property graph — concepts, sections, actors, sources.

The arrow in the middle is deliberately boring. No LLM, no extraction, no inference — just parsing structure that's already there. That boringness is a feature: ingestion is deterministic, reproducible, and free to re-run. We save the model calls for the right-hand side, at retrieval time, where they actually buy something.

### 4 · OKF in five facts — 3:00

Five facts and you know the spec. *[work down the list]* One: a bundle is a directory of markdown files, full stop — ship it as a git repo or a tarball. Two: exactly one required frontmatter key, `type`. No schema registry, no central authority — and consumers **must** tolerate unknown types and unknown keys. Remember that word *must*; it shaped our parser. Three: the file path is the identity, and markdown links are the relationships — and broken links are legal. The spec calls them not-yet-written knowledge, which is a lovely reframe we'll exploit later. Four — the v0.2 story: trust became first-class. Sources with credibility signals, who generated versus who verified, lifecycle status, staleness dates. That's what you need once agents maintain the corpus. Five: format, not platform — Google ships a reference producer and a static viewer, and their Knowledge Catalog ingests OKF, but nothing about the format needs any of it.

The repo also ships sample bundles. We'll live in `acme_retail` — it exercises every v0.2 feature and, as you'll see, contains a landmine.

### 5 · Anatomy of a bundle — 4:30

Here's acme_retail. Tables, metrics, the sanctioned computations, finance policies, an executor skill, and — note — actual Python in `attesters/`. Bundles carry code artifacts alongside concepts.

Two reserved filenames. `index.md` is progressive disclosure — an agent can walk the tree one level at a time instead of slurping the whole corpus into context. `log.md` is chronological history, newest first.

And there's the landmine: `gross-margin-legacy.md`, status deprecated. The FY2026 margin definition changed — the old one is *kept*, on purpose, for reproducibility. The spec is explicit that you preserve deprecated knowledge rather than delete it. Totally correct archival practice. Also a loaded gun, and the demo steps on it later.

### 6 · Anatomy of a concept — 5:30

One real file, dissected — this is the slide to internalize. *[walk the callouts top to bottom]*

`type` — the only required key, and we mint a Neo4j label straight from it. Then the trust split, which is the heart of v0.2: `generated` says who *wrote* it — here an agent, gemini-2.5-pro under a reference agent. `verified` says who *confirmed* it — here a human, the VP of Finance, with a timestamp. Different actors, different edges, and the actor convention — `human:`, `process:`, or producer-slash-version — is what trust tiers key off.

Lifecycle: `status: stable`, `stale_after` end of year — staleness is just a date comparison. Provenance: `sources`, each with objective credibility signals — author, usage count, last modified. Signals, not scores — the spec refuses to store a credibility number, because scores are subjective and go stale; *inferring* trust is the consumer's job. Which is us. Which is a graph query.

Then the body. Conventional h1 sections — Definition, what-changed. Links whose meaning lives in the surrounding prose. And the footnotes — `[^margin-standard]` — are keyed to source IDs, so individual *claims* carry attribution that survives an agent rewriting the document. Every callout on the right names the graph element it becomes.

### 7 · The v0.2 trust machine — 7:15

Four families, all optional, and absence is meaningful — an unverified concept is a visible state, never a parse error.

Provenance and trust we just saw. Lifecycle: draft, stable, deprecated, plus absolute staleness dates — no TTL arithmetic against read time.

The fourth one is the deep cut: **attested computations**. A sanctioned computation is its own concept — typed parameters, the SQL, an executor that returns a receipt with the job id and the SQL that *actually ran*, and an attester, which is deterministic code — no LLM — that compares receipt against contract. The agent may bind parameters. It may **not** author SQL. That's the difference between "the model wrote plausible SQL" and "the blessed computation ran, provably."

One distinction worth quoting in the hallway: *verified* is doc-level and slow — the definition still matches policy. *Attestation* is per-run — this number was produced the sanctioned way. You need both.

### 8 · A bundle is already a graph — 9:00

Pivot. *[left, then right]* Same bytes, two views. Path equals node identity, links equal edges, frontmatter equals properties. This isn't me imposing a graph on their format — Google's own README says "graph-shaped, not just tree-shaped," and every bundle ships with a static graph viewer, `viz.html`.

So the question was never *whether* this is a graph. It's what tooling you deserve once you admit it is one.

### 9 · Why materialize it in a database — 9:45

Nothing against the static viewer — for browsing one bundle it's great, and it's honest: no backend, no lock-in, very OKF. But a viewer can't *answer* anything. Filter by trust tier. Staleness report. Blast radius of a policy change. Aggregate over frontmatter. Similarity search. Join two bundles.

Everything on the right is a database job, and here's the part I want to underline: it's about 400 lines of deterministic parser plus plain Cypher MERGE. No APOC in the ingestion path, no plugins — plus the neo4j-graphrag package for indexes and retrievers when we get to Part III.

## Part II — Into Neo4j

### 10 · The graph model — 10:30

The model. Center: `:Concept`, one per file, uid is bundle plus path. Going around: `VERIFIED_BY` and `GENERATED_BY` edges carry their timestamps *on the edge* and land on `:Actor` nodes — trust tiers derive from those. `:Source` gets a pair of relationships worth pausing on: `DERIVES_FROM` from the concept, and `RESOLVES_TO` back into the graph when a source's resource is itself a bundle path — that one edge is what turns provenance from a string in frontmatter into a chain you can walk.

Bottom: the lexical layer. `:Section` nodes — OKF's conventional headings hand us retrieval units, so chunking costs nothing and respects author intent. `CITES` carries the footnote attribution: claim-level provenance, per section, not per document. And `LINKS_TO` keeps the section it came from as an edge property, because the spec says a link's meaning lives in surrounding prose — so we preserve exactly where it appeared.

Attested computations get `EXECUTED_BY` to the skill and `ATTESTED_BY` to the checker code artifact.

### 11 · The mapping, construct by construct — 12:15

This is the reference card — same table's in the README, so I'll pull out three rows rather than read fourteen. *[point]* Broken links become stub nodes flagged `stub: true` — "not-yet-written knowledge" turns into a queryable authoring backlog, and I mean that literally; you'll see the query. Sources: intrinsic facts like author live on the node; declaration-scoped facts like usage counts live on the `DERIVES_FROM` edge — the spec hands you that node-versus-edge split if you read §5.1 closely. And `index.md` is deliberately **not** ingested. It's derivable. Progressive disclosure is a serving concern — we can regenerate indexes *from* the graph.

### 12 · Five decisions that earn their keep — 13:30

Five choices that pay off later, quickly. Sections, not fixed-size chunks — retrieval units follow author intent. Type strings become labels — `:Metric`, `:AttestedComputation` — so Cypher stays label-scoped and fast. Trust tier is both derived *and* materialized — the CASE expression on the right is the living definition, the property is the cheap filter, and ingestion re-syncs the property every run so they can't drift. Unknown frontmatter survives as queryable JSON — the conformance rules say must-not-reject, and acme actually ships a custom `not:` field, anti-definitions for the margin metric. And ingestion is a sync, not an append — re-running is safe, idempotent, and even preserves embeddings for unchanged sections.

### 13 · Ingestion architecture — 15:00

Architecture in one look. Phase 1, deterministic: parse frontmatter families, resolve links — including the root-relative fallback real bundles need — fence-aware sectioning, stub minting; then idempotent batched MERGEs with typed dates. LLM calls in phase 1: zero. *[point at the counter]*

Phase 2 is optional and separable: embed sections, build the vector and fulltext indexes via neo4j-graphrag's helpers. You can build and query the entire structural graph without an API key on the machine. Phase 3 is where models finally show up — at the edges, in the retrievers.

### 14 · The graph pays rent before any AI shows up — 16:00

Before anyone says GraphRAG, the graph already pays rent. Left: the serve/no-serve verdict — one query over the whole estate: deprecated beats stale beats servable. This is the query a serving layer should run before handing *any* document to an agent.

Right: blast radius. What breaks if the orders table changes? One variable-length traversal — and notice it flows *through* Source nodes, so provenance chains count as dependencies. Policy at one hop, the revenue computation through its provenance chain, the metrics beyond that. Wikis structurally cannot answer this, and it's the first thing a data platform team asks for.

Below, three freebies: co-citation at the *claim* level via the footnote edges, the authoring backlog from stub nodes, and load-bearing-ness by incoming degree. All in `queries.py`. Steal them.

### 15 · Demo 1 setup — 17:15

*[Dark slide up; switch to Jupyter. The slide stays as your safety net — it lists the beats.]*

---

## DEMO 1 — Ingest and interrogate (~5 min, no API key)

**Beat 1 — the raw material.** Run the tree cell and the `gross-margin.md` cell. Say: "Real files from Google's repo, unmodified. There's the frontmatter you saw on the slide — and there's the deprecated legacy file, present and correct."

**Beat 2 — parse + ingest.** Run the parse cell. Say while it returns: "Deterministic parse — nine concepts, twenty-two sections, fifteen links, four log entries. Note `okf_version: not declared` — optional key, the spec's fine with it, so are we." Run the ingest cell and the label-count cell. "That's the sync ingest — safe to re-run any time, keeps embeddings for unchanged text. And the labels came from `type`: three Metrics, two AttestedComputations, a Policy, a Skill, a BigQueryTable."

**Beat 3 — governance queries.** Trust tiers: "Eight human-reviewed, one unverified — the executor skill nobody signed off. Derived live from edges, not trusted from a cache." Staleness: "One do-not-serve — there's our legacy metric. Everything else servable until New Year." Impact: "Blast radius of the revenue-recognition policy — this is the slide-14 query running for real."

**Beat 4 — the picture.** Run the neo4j-viz cell, drag a node or two. "Colored by status — the red deprecated island, connected to its replacement in both directions. This is the OKF viz.html idea, except it's sitting *inside* a database that can answer questions."

**If ahead of schedule — §4.6:** "One more, because real corpora aren't this tidy: a second tiny bundle, ours, with deliberately broken links and a process-verified runbook. Broken links became stub nodes — there's the authoring backlog, `dashboards/margin-daily` and `runbooks/failover`, with who wants them. And the trust ladder now shows all three tiers, across two bundles in one graph — bundle is just a property."

*[Back to slides.]*

---

## Part III — GraphRAG

### 16 · Three retrieval patterns, one graph — 22:30

Retrieval. Three patterns, one store, all from the neo4j-graphrag package — this is the payoff of the modeling work; the retrievers are configuration, not code.

Left: plain `VectorRetriever`. Fast, zero modeling — and blind to everything we just built. Look at its ranking: the *deprecated* definition arrives at the top. Middle: `VectorCypherRetriever`. The vector hit is just an **anchor**; a Cypher query walks out from it and assembles the context. The graph decides what the LLM sees. Right: `Text2Cypher`, for questions that have no paragraph to be similar to — negations, aggregates — where the generated Cypher comes back with the answer, so every result is auditable.

Rule of thumb: vector for "what does X mean," vector-plus-Cypher for "answer with governance," Text2Cypher for "report over the estate."

### 17 · The governance trap, step by step — 24:00

The money slide. Question: how do we calculate gross margin? The vector index returns the *legacy* section at or near the top — that's from our recorded runs, and honestly, it should rank there. It's a well-written paragraph about exactly this topic. Cosine similarity is doing its job **perfectly**. The failure is asking similarity to also do governance — nothing in a dot product encodes "deprecated."

Path A hands the LLM two confident texts, no signals. The answer can serve a formula Finance retired in February — silently — and that's a four-to-six point margin misstatement wearing a friendly chatbot tone.

Path B, same anchors, same embeddings: walk up to the concept, read status and trust, follow the link to the replacement *and* to the sanctioned computation, pull the verification and the staleness verdict. Same model, same top-k. The only variable is what retrieval hands the LLM.

### 18 · The governed retrieval_query — 25:45

Mechanics, because you'll want to steal this. Left: the application-side diff between ungoverned and governed RAG is **one constructor argument** — `retrieval_query`. That's neo4j-graphrag earning its keep. Right: the query itself — `node` and `score` arrive from the index, everything after that is ordinary Cypher you own. Lifecycle, verification, provenance, replacement — formatted into a readable block, because the LLM does better with "status: deprecated, superseded by" written in words next to the text.

One subtlety on the hop bound: sanctioned SQL is direct-link only — *except* when the anchor is deprecated, where a second hop lets it reach the SQL through its replacement. That asymmetry is the fix for the trap.

### 19 · Text2Cypher — 27:00

And for questions with no similarity anchor at all: "which metrics were never reviewed by a human" — there's no paragraph about that; it's negation over the estate. Text2Cypher compiles it against a hand-written schema — keep that schema small and curated, it beats dumping SHOW SCHEMA — plus a few examples, all in `queries.py`. The generated Cypher returns in metadata: the query is the receipt. Guardrails: read-only user, and promote recurring questions to canned parameterized Cypher.

### 20 · Demo 2 setup — 28:00

*[Dark slide, switch to Jupyter.]*

---

## DEMO 2 — GraphRAG on the bundle (~6 min, needs the key)

**Beat 1 — embed + index.** Run §5. "Twenty-two sections through text-embedding-3-small, vector plus fulltext indexes from the package. There's also an offline hash embedder in the repo for rehearsal — with a dimension guard so switching back to real embeddings just works."

**Beat 2 — the trap, on the record.** Run the baseline retrieval cell. **Point at the table before generating anything**: "Concept, section, status, trust, score. Read the status column — deprecated, in the top hits, with a healthy score." Run the answer cell. If it parrots the legacy formula: "There it is — confidently wrong." If it answers correctly: "The model got lucky this run — but look at what it was given: nothing in that context says which definition is sanctioned. Luck is not a governance strategy." *(Both outcomes land the point; the table is the evidence, not the answer.)*

**Beat 3 — governed.** Print `top.items[0].content` first: "This is what the LLM sees now — status, superseded-by, trust with a name and a date, freshness verdict, and the sanctioned SQL pulled through the graph." Run the answer. Then the follow-up question — "sanctioned SQL, who verified it, until when" — "one answer assembled from a Metric, a Computation, an Actor, and a Policy. Four node types, one retrieval."

**Beat 4 — Text2Cypher.** Run the loop. For the impact question, show `metadata["cypher"]`: "There's the generated query — auditable. And no embeddings were involved anywhere in this one."

**If ahead:** hybrid retriever one-liner — "exact tokens like `net_amount` are where fulltext rescues embeddings." **If OpenAI misbehaves:** switch to the executed tab — "same cells, run this morning" — and narrate the outputs.

*[Back to slides.]*

---

### 21 · Two construction modes, one graph — 34:00

Quick coda for the ontology people. Everything so far was deterministic structure — exact, cheap, reproducible, but only as connected as the explicit links. The same package's `SimpleKGPipeline` adds the complementary layer: schema-guided entity extraction over the *prose* — cost components, policy rules, teams — stitched to the concepts that mention them. Now "payment fees" connects the margin standard, the computation, and the orders table even though no markdown link ties them. The appendix runs it on two policies in about a minute — that part does use APOC, which the repo's compose file installs. Keep the layers distinguishable: extracted facts are lower-trust by construction — and conveniently, OKF just taught us the vocabulary for saying exactly that.

### 22 · What to take home — 35:00

Three takeaways for three hats. Data estate: OKF is a low-friction interchange — everything writes files — and one Cypher layer turns those files into serve/no-serve verdicts and impact analysis. RAG stack: chunk on authored sections; anchor with vectors, assemble with the graph — similarity is not governance. Agents: trust tiers and staleness become retrieval-time *filters*, and attested computations mean agents bind parameters but never author SQL.

If you remember one sentence: **an OKF bundle is a graph that happens to be stored as files — Neo4j makes it answer questions, and GraphRAG makes it govern what your LLM says.**

### 23 · Close — 36:00

Everything's in the repo — parser, notebook, deck, both bundles; `docker compose up`, `uv sync`, and you're where this demo started. The spec and Google's samples are linked. Thanks — questions.

---

## Pocket Q&A

**"Doesn't the materialized trust_tier drift from the edges?"** Not between syncs — ingest is a sync, it clears and rebuilds trust edges and re-derives the property every run. Between syncs, the CASE query is the authority; run ingest on a schedule or a git hook.

**"Two bundles define the same metric — who wins?"** Nobody, by design — uids are bundle-scoped, both exist. Conflict *detection* is a query: same title or tag across bundles, different sanctioned computations. Resolution is human. The graph's job is making the conflict visible.

**"Why not just a vector DB?"** Slide 17 is the whole answer — happy to replay it. Similarity ranked the deprecated definition first *and it was right to*; the missing information was never in the embedding space.

**"What about agents writing back?"** Git is the write path — agents author markdown, humans review PRs, exactly the workflow OKF wants. The graph is a read model: re-sync on merge. `log.md` entries already land as LogEntry nodes, so the history rides along.

**"Multi-tenant retrieval?"** This demo is deliberately bundle-agnostic; production adds a `bundle` predicate in the retrieval query and governance queries, or per-bundle databases. The model needs no change — bundle is already a property everywhere.

**"Scale?"** These bundles are toys, but the shapes hold: batched UNWIND MERGEs, label-scoped indexes, vector index is ANN. When plain Cypher runs out, GDS: PageRank for load-bearing concepts, communities for topic clusters.

**"Does this need APOC?"** The structural layer, no — plain Cypher. The optional extraction appendix, yes — neo4j-graphrag's writer and resolver call APOC procedures; the compose file ships the plugin.
