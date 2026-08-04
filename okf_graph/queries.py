"""Reusable Cypher for the OKF graph: governance queries and GraphRAG
retrieval queries used with the neo4j-graphrag package.

The star of the show is GOVERNED_RETRIEVAL_QUERY: it is appended after the
vector index lookup by VectorCypherRetriever. `node` is the matched
(:Section) anchor and `score` its similarity. From that anchor we walk the
OKF graph to assemble *governed* context: lifecycle status, trust tier,
staleness, the sanctioned Attested Computation reachable over LINKS_TO,
provenance sources, and — for deprecated concepts — the current replacement.
A pure vector store cannot do any of this at retrieval time.
"""

# --- governance / exploration queries (plain Cypher) -----------------------

TRUST_TIER_DERIVATION = """
// SPEC §5.3 — trust tier is *derived* from VERIFIED_BY edges, not stored.
// (We also materialize it at ingest for cheap filtering; this query is the
// living definition.)
MATCH (c:Concept) WHERE NOT coalesce(c.stub, false)
OPTIONAL MATCH (c)-[v:VERIFIED_BY]->(a:Actor)
WITH c, collect(a.kind) AS verifier_kinds
RETURN CASE
         WHEN size(verifier_kinds) = 0        THEN 'unverified'
         WHEN 'human' IN verifier_kinds        THEN 'human-reviewed'
         ELSE 'machine-confirmed'
       END AS trust_tier,
       count(*) AS concepts,
       collect(c.id)[..5] AS examples
ORDER BY concepts DESC
"""

STALENESS_REPORT = """
// Which knowledge can we still serve today? (SPEC §5.5: stale when
// today >= stale_after; §5.4: deprecated is kept only for history.)
MATCH (c:Concept) WHERE NOT coalesce(c.stub, false)
WITH c,
     c.status = 'deprecated'                              AS is_deprecated,
     c.stale_after IS NOT NULL AND date() >= c.stale_after AS is_stale
RETURN c.id AS concept, c.type AS type, c.status AS status,
       c.trust_tier AS trust, c.stale_after AS stale_after,
       CASE WHEN is_deprecated THEN 'do not serve (deprecated)'
            WHEN is_stale      THEN 're-verify before serving'
            ELSE 'servable' END AS verdict
ORDER BY is_deprecated DESC, is_stale DESC, concept
"""

IMPACT_ANALYSIS = """
// Blast radius: what is transitively affected if $concept_id changes?
// Every dependency edge in the model points forward, so one variable-length
// pattern walks them all — including provenance chains, which pass *through*
// a Source node: (up)-[:DERIVES_FROM]->(:Source)-[:RESOLVES_TO]->(target),
// and section-level grounding via (up)-[:HAS_SECTION]->(:Section)-[:MENTIONS]->.
MATCH (target:Concept {id: $concept_id})
MATCH p = (upstream:Concept)
      -[:LINKS_TO|EXECUTED_BY|DERIVES_FROM|RESOLVES_TO|HAS_SECTION|MENTIONS*1..6]->(target)
WHERE upstream <> target
  AND NOT coalesce(upstream.stub, false)
WITH upstream, min(length(p)) AS hops
RETURN upstream.id AS impacted, upstream.type AS type,
       upstream.status AS status, upstream.trust_tier AS trust, hops
ORDER BY hops, impacted
"""

MOST_DEPENDED_ON = """
// Load-bearing knowledge: concepts with the highest incoming degree.
MATCH (c:Concept) WHERE NOT coalesce(c.stub, false)
WITH c,
     COUNT { (c)<-[:LINKS_TO]-() }                       AS inbound_links,
     COUNT { (c)<-[:RESOLVES_TO]-(:Source)<-[:DERIVES_FROM]-() } AS as_source
RETURN c.id AS concept, c.type AS type, inbound_links, as_source,
       inbound_links + as_source AS total
ORDER BY total DESC LIMIT 10
"""

NOT_YET_WRITTEN = """
// Broken links are 'not-yet-written knowledge' (SPEC §6.1) — we keep them
// as stub nodes, which turns them into a queryable authoring backlog.
MATCH (ghost:Concept {stub: true})<-[l:LINKS_TO]-(c:Concept)
RETURN ghost.id AS missing_concept,
       collect(c.id + '  (§ ' + l.section + ')') AS wanted_by
"""

CO_CITATION = """
// Concepts whose *claims* lean on the same source — footnote-level
// provenance (Section-CITES->Source), not just document-level.
MATCH (a:Concept)-[:HAS_SECTION]->(:Section)-[:CITES]->(src:Source)
      <-[:CITES]-(:Section)<-[:HAS_SECTION]-(b:Concept)
WHERE a.uid < b.uid
RETURN src.title AS shared_source, collect(DISTINCT a.id + ' ↔ ' + b.id) AS pairs
ORDER BY size(pairs) DESC
"""

# --- GraphRAG retrieval queries (neo4j-graphrag) ---------------------------

# Appended to the vector search by VectorCypherRetriever. Input bindings:
#   node  -> the matched (:Section) anchor
#   score -> vector similarity
GOVERNED_RETRIEVAL_QUERY = """
MATCH (c:Concept)-[:HAS_SECTION]->(node)

// 1. lifecycle + trust of the concept that owns the matched section
//    (a verification without a date still counts — no fabricated timestamps)
OPTIONAL MATCH (c)-[v:VERIFIED_BY]->(va:Actor)
WITH node, score, c,
     collect(DISTINCT va.id + coalesce(' at ' + toString(date(v.at)), ''))
         AS verifications

// 2. sanctioned computation: direct link only — unless the anchor is
//    deprecated, where a 2nd hop reaches the SQL via its replacement
OPTIONAL MATCH path = (c)-[:LINKS_TO*1..2]->(ac:AttestedComputation)
WHERE coalesce(ac.status, 'stable') <> 'deprecated'
  AND (length(path) = 1 OR c.status = 'deprecated')
WITH node, score, c, verifications, ac, min(length(path)) AS hops
OPTIONAL MATCH (ac)-[:HAS_SECTION]->(acs:Section)
WHERE acs.heading = 'Computation' OR acs.heading STARTS WITH 'Computation (part'
WITH node, score, c, verifications, ac, acs
ORDER BY hops, acs.order
WITH node, score, c, verifications,
     collect(DISTINCT {id: ac.id, runtime: ac.runtime, trust: ac.trust_tier,
                       stale_after: toString(ac.stale_after),
                       sql: left(acs.text, 1200)}) AS computations

// 3. provenance sources of the concept
OPTIONAL MATCH (c)-[:DERIVES_FROM]->(src:Source)
WITH node, score, c, verifications, computations,
     collect(DISTINCT coalesce(src.title, src.resource)) AS sources

// 4. deprecated concepts: ONE deterministic replacement (first by id)
OPTIONAL MATCH (c)-[:LINKS_TO]->(cand:Concept {type: c.type})
WHERE c.status = 'deprecated' AND coalesce(cand.status,'stable') = 'stable'
WITH node, score, c, verifications, computations, sources, cand
ORDER BY cand.id
WITH node, score, c, verifications, computations, sources,
     collect(cand)[0] AS current

RETURN
    '=== ' + c.title + '  (' + c.id + ') ===\\n'
  + 'type: '   + c.type + '\\n'
  + 'status: ' + c.status
      + CASE WHEN c.status = 'deprecated' AND current IS NOT NULL
             THEN '  →  SUPERSEDED BY: ' + current.id ELSE '' END + '\\n'
  + 'trust: '  + c.trust_tier
      + CASE WHEN size(verifications) > 0
             THEN ' (' + verifications[0] + ')' ELSE '' END + '\\n'
  + 'freshness: '
      + CASE WHEN c.stale_after IS NULL THEN 'no stale_after set'
             WHEN date() >= c.stale_after
               THEN 'STALE since ' + toString(c.stale_after) + ' — re-verify before serving'
             ELSE 'fresh until ' + toString(c.stale_after) END + '\\n'
  + CASE WHEN size(sources) > 0 AND sources[0] IS NOT NULL
         THEN 'derived from: ' + reduce(s = '', x IN sources | s + x + '; ') + '\\n'
         ELSE '' END
  + '--- matched section [' + node.heading + '] ---\\n'
  + node.text + '\\n'
  + reduce(s = '', comp IN [x IN computations WHERE x.id IS NOT NULL] |
        s + '--- sanctioned computation: ' + comp.id
          + ' (runtime ' + coalesce(comp.runtime,'?')
          + ', trust ' + coalesce(comp.trust,'?')
          + ', stale_after ' + coalesce(comp.stale_after,'-') + ') ---\\n'
          + coalesce(comp.sql, '') + '\\n')
    AS info,
    score,
    c.id   AS concept_id,
    c.status AS status,
    c.trust_tier AS trust_tier
ORDER BY score DESC
"""

# Schema handed to Text2CypherRetriever — hand-written and *small*, which
# beats a dumped SHOW SCHEMA for LLM accuracy.
TEXT2CYPHER_SCHEMA = """
Node labels and key properties:
  (:Bundle {name, okf_version})
  (:Concept {uid, id, bundle, type, title, description, status, trust_tier,
             stale_after: DATE, generated_at: DATETIME, runtime, stub: BOOLEAN})
      // secondary labels mirror OKF types, e.g. :Metric, :Policy,
      // :BigQueryTable, :AttestedComputation, :Skill
  (:Section {uid, heading, order, text, char_count})
  (:Source  {uid, sid, resource, title, author, usage_count, last_modified: DATE})
  (:Actor   {id, kind})            // kind: 'human' | 'agent' | 'process'
  (:Artifact {uid, path, kind})
  (:Tag {name})
  (:LogEntry {date: DATE, kind, text})

Relationships:
  (Concept)-[:IN_BUNDLE]->(Bundle)
  (Concept)-[:LINKS_TO {section, text, resolved}]->(Concept)
  (Concept)-[:HAS_SECTION]->(Section)   (Section)-[:NEXT]->(Section)
  (Section)-[:CITES]->(Source)          (Section)-[:MENTIONS]->(Concept)
  (Concept)-[:DERIVES_FROM {usage_count, usage_window_from, usage_window_to}]->(Source)
  (Source)-[:RESOLVES_TO]->(Concept)
  (Concept)-[:GENERATED_BY {at}]->(Actor)
  (Concept)-[:VERIFIED_BY {at}]->(Actor)
  (Concept)-[:EXECUTED_BY]->(Concept)   // Attested Computation -> Skill
  (Concept)-[:ATTESTED_BY]->(Artifact)  // deterministic checker code
  (Concept)-[:TAGGED]->(Tag)
  (LogEntry)-[:REFERENCES]->(Concept)   (LogEntry)-[:IN_BUNDLE]->(Bundle)

Conventions:
  status is one of 'draft' | 'stable' | 'deprecated' (absent = 'stable').
  trust_tier is one of 'unverified' | 'machine-confirmed' | 'human-reviewed'.
  A concept is stale when date() >= stale_after.
  Ignore concepts with stub = true unless asked about missing knowledge.
"""

TEXT2CYPHER_EXAMPLES = [
    "USER INPUT: 'Which concepts are stale?' QUERY: MATCH (c:Concept) WHERE NOT coalesce(c.stub,false) AND c.stale_after IS NOT NULL AND date() >= c.stale_after RETURN c.id, c.type, c.stale_after",
    "USER INPUT: 'Which metrics were never reviewed by a human?' QUERY: MATCH (m:Metric) WHERE NOT coalesce(m.stub,false) AND m.trust_tier <> 'human-reviewed' RETURN m.id, m.trust_tier",
    "USER INPUT: 'What links to the orders table?' QUERY: MATCH (c:Concept)-[l:LINKS_TO]->(t:Concept {id: 'tables/orders'}) RETURN c.id, l.section",
    "USER INPUT: 'Who verified the revenue metric and when?' QUERY: MATCH (c:Concept {id: 'metrics/revenue'})-[v:VERIFIED_BY]->(a:Actor) RETURN a.id, a.kind, v.at",
    "USER INPUT: 'Which sources back the most concepts?' QUERY: MATCH (c:Concept)-[:DERIVES_FROM]->(s:Source) RETURN coalesce(s.title, s.resource) AS source, count(DISTINCT c) AS concepts ORDER BY concepts DESC",
]
