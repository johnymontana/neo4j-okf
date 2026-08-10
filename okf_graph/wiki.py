"""Documents in, OKF bundle out — an LLM-authored wiki that lands in the graph.

The pipeline:

    documents  →  concept drafts  →  an OKF bundle on disk  →  parse  →  Neo4j
                  (extractor)         (emit.py)                (parser.py)

The middle arrow is the important one. The extractor does **not** write to
Neo4j; it writes *markdown*, and the graph is then built by the same
deterministic parser that ingests Google's sample bundles. Three things fall
out of that:

* one mapping to maintain instead of two — nothing can drift;
* the LLM's output is a git-diffable artifact a human can review in a PR,
  which is exactly the authoring workflow OKF is designed for;
* everything the graph already does — trust tiers, staleness, impact analysis,
  governed retrieval — applies to LLM-authored knowledge for free.

Generated concepts are honest about what they are: `generated: {by: <producer>/
<model>}` with no `verified` key, which puts every one of them in SPEC §5.3's
`unverified` tier and `status: draft`. Drop them in the same database as a
human-reviewed bundle and the governance queries will rank them accordingly —
which is the point. An agent that writes a plausible-sounding metric definition
should not be able to outrank Finance.

Extractors:

    heuristic   deterministic, no API key, no network — the offline path, and
                what the tests run against
    openai      OpenAI structured outputs (matches the repo's existing LLM setup)
    anthropic   Claude structured outputs (optional; needs the `anthropic` package)
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Protocol

from . import emit
from .documents import Document, body_without_title, slugify
from .parser import (ParsedBundle, ParsedConcept, ParsedSection, ParsedSource,
                     parse_actor, type_to_label)

DEFAULT_OPENAI_MODEL = "gpt-4o"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

# Where a concept of a given OKF type lives in the bundle. Unknown types are
# fine (SPEC §11) and land in `concepts/`.
TYPE_DIRS = {
    "metric": "metrics",
    "policy": "policies",
    "attested computation": "computations",
    "table": "tables",
    "bigquery table": "tables",
    "dataset": "tables",
    "runbook": "runbooks",
    "playbook": "runbooks",
    "skill": "skills",
    "term": "glossary",
    "glossary term": "glossary",
    "system": "systems",
    "service": "systems",
    "process": "processes",
    "dashboard": "dashboards",
    "reference": "references",
}
DEFAULT_DIR = "concepts"

# The ontology offered to an extractor. Deliberately short: OKF requires only
# that `type` be a non-empty string, and a long enum invites over-fitting.
DEFAULT_ONTOLOGY = ["Metric", "Policy", "Table", "Term", "Process",
                    "Runbook", "System", "Dashboard"]

# Used to route a *proposed but not-yet-written* link target to a plausible
# directory, so the resulting stub reads as a real backlog item.
_TYPE_HINTS = [
    ("dashboard", "dashboards"), ("runbook", "runbooks"), ("playbook", "runbooks"),
    ("policy", "policies"), ("standard", "policies"), ("memo", "policies"),
    ("metric", "metrics"), ("margin", "metrics"), ("revenue", "metrics"),
    ("table", "tables"), ("dataset", "tables"),
]

MAX_MIRROR_CHARS = 20_000


# --------------------------------------------------------------------------
# Draft model
# --------------------------------------------------------------------------

@dataclass
class DraftSection:
    heading: str
    text: str
    source_sids: list[str] = field(default_factory=list)


@dataclass
class ConceptDraft:
    slug: str
    type: str
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    sections: list[DraftSection] = field(default_factory=list)
    links: list[str] = field(default_factory=list)     # proposed targets, by title
    stale_after: Optional[str] = None
    source_sids: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{TYPE_DIRS.get(self.type.strip().lower(), DEFAULT_DIR)}/{self.slug}"


def safe_slug(raw: str, fallback: str, taken: set[str]) -> str:
    """A slug that is safe as a filename and unique within its directory.

    Three separate hazards, all of which fail silently otherwise: `index` and
    `log` are OKF reserved filenames, so a concept slugged that way is written
    and then skipped on re-parse; case-only differences collapse on macOS and
    Windows but not on Linux CI; and two documents about the same subject
    produce the same slug and one quietly overwrites the other.
    """
    slug = slugify(raw, fallback) or fallback
    if slug in emit.RESERVED_STEMS:
        slug = f"{slug}-concept"
    base, n = slug, 2
    while slug.lower() in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug.lower())
    return slug


@dataclass
class WikiSpec:
    """How to turn a document set into a bundle."""

    name: str = "acme_wiki"
    extractor: str = "heuristic"                  # heuristic | openai | anthropic
    model: Optional[str] = None
    ontology: list[str] = field(default_factory=lambda: list(DEFAULT_ONTOLOGY))
    max_concepts_per_doc: int = 3
    mirror_sources: bool = True                   # write references/ (SPEC §6.3)
    status: str = "draft"
    stale_after: Optional[str] = None
    okf_version: str = "0.2"

    def __post_init__(self) -> None:
        # An empty ontology would leave the type-folding fallback with nothing
        # to fall back to; `--ontology ""` should mean "the defaults", not a crash.
        if not self.ontology:
            self.ontology = list(DEFAULT_ONTOLOGY)

    @property
    def producer(self) -> str:
        """The `generated.by` actor for everything this build authors (§7)."""
        if self.extractor == "heuristic":
            return "process:okf-wiki-heuristic"
        model = self.model or (DEFAULT_OPENAI_MODEL if self.extractor == "openai"
                               else DEFAULT_ANTHROPIC_MODEL)
        return f"okf-wiki/{model}"


@dataclass
class WikiBuild:
    bundle: ParsedBundle
    files: dict[str, str]
    drafts: list[ConceptDraft]
    stats: dict


# --------------------------------------------------------------------------
# Extractors
# --------------------------------------------------------------------------

class Extractor(Protocol):
    def extract(self, doc: Document, spec: WikiSpec) -> list[ConceptDraft]: ...


def get_extractor(spec: WikiSpec) -> Extractor:
    kind = (spec.extractor or "heuristic").lower()
    if kind == "heuristic":
        return HeuristicExtractor()
    if kind == "openai":
        return LLMExtractor("openai", spec.model or DEFAULT_OPENAI_MODEL)
    if kind == "anthropic":
        return LLMExtractor("anthropic", spec.model or DEFAULT_ANTHROPIC_MODEL)
    raise ValueError(
        f"unknown extractor {spec.extractor!r} — use heuristic, openai or anthropic")


_TYPE_KEYWORDS = [
    ("Runbook", ("runbook", "playbook", "on-call", "oncall", "triage", "incident",
                 "escalat", "step 1", "what not to do")),
    ("Table", ("table", "column", "schema", "dataset", "one row per", "primary key",
               "data dictionary", "warehouse")),
    ("Policy", ("policy", "standard", "memo", "effective 20", "supersede",
                "must be", "adopted", "compliance")),
    ("Metric", ("metric", "margin", "revenue", "kpi", "calculated as", "= revenue",
                "headline")),
    ("Dashboard", ("dashboard", "report", "weekly deck", "chart")),
    ("System", ("service", "pipeline", "job", "cluster", "api")),
]


def infer_type(title: str, text: str) -> str:
    """Score OKF types by keyword evidence. Deterministic, and good enough."""
    haystack = f"{title}\n{text[:1200]}".lower()
    best, best_score = "Term", 0
    for ctype, keywords in _TYPE_KEYWORDS:
        score = sum(3 if kw in title.lower() else 1
                    for kw in keywords if kw in haystack)
        if score > best_score:
            best, best_score = ctype, score
    return best


class HeuristicExtractor:
    """Offline extractor: one concept per document, structure from its headings.

    Carries no semantics — it will not notice that two documents describe the
    same metric — but it exercises every downstream stage with no API key, the
    same role `HashEmbedder` plays for the retrieval path.
    """

    def extract(self, doc: Document, spec: WikiSpec) -> list[ConceptDraft]:
        body = body_without_title(doc)
        sections = _split_markdown_sections(body)
        ctype = infer_type(doc.title, body)
        return [ConceptDraft(
            slug=slugify(doc.title, doc.slug),
            type=ctype,
            title=doc.title,
            description=_first_sentence(body),
            tags=sorted({ctype.lower(), "okf-wiki"}),
            sections=[DraftSection(h, t) for h, t in sections],
            links=propose_links(body),
            stale_after=spec.stale_after,
        )]


# "the Margin Daily dashboard", "the Margin Drop Triage runbook" — a capitalized
# phrase followed by a kind word is how prose names another document.
_LINK_KINDS = ("dashboard", "runbook", "playbook", "policy", "memo", "standard",
               "table", "report", "metric", "guide", "checklist")
_LINK_MARKERS = ("page", "doc", "document", "wiki", "article")
_PROPOSAL_RE = re.compile(
    r"\b(?P<name>[A-Z][A-Za-z0-9]*(?:[ -][A-Z0-9][A-Za-z0-9]*){0,4})"
    r"\s+(?P<kind>" + "|".join(_LINK_KINDS + _LINK_MARKERS) + r")\b")


# "This standard is reviewed annually" is a sentence, not a reference. A
# capitalized determiner at the start of one looks exactly like a proper noun.
_DETERMINERS = {"a", "an", "the", "this", "that", "these", "those", "our", "its",
                "their", "his", "her", "each", "every", "any", "some", "all",
                "both", "no", "new", "old", "same", "other", "another", "such",
                "more", "most", "one", "two", "several", "many", "few", "if",
                "for", "and", "but", "when", "use", "see", "check", "do"}


def propose_links(text: str) -> list[str]:
    """Guess which other documents this prose refers to, by name.

    Deterministic stand-in for what an LLM does explicitly. Targets that turn
    out not to exist are the interesting ones: they become SPEC §6.1 stubs, and
    the graph reads them back as an authoring backlog.
    """
    out: list[str] = []
    for m in _PROPOSAL_RE.finditer(text):
        words = m.group("name").split()
        while words and words[0].lower() in _DETERMINERS:
            words.pop(0)
        if not words:
            continue
        kind = m.group("kind")
        name = " ".join(words)
        title = name if kind in _LINK_MARKERS else f"{name} {kind}"
        if len(title) >= 6 and title not in out:
            out.append(title)
    return out


EXTRACTION_SYSTEM = """\
You convert a single source document into OKF (Open Knowledge Format) concept \
drafts. OKF concepts are markdown files: YAML frontmatter plus `# ` sections.

Rules:
1. Use ONLY information present in the document. Never add outside knowledge, \
and never resolve a contradiction the document leaves open — record what it says.
2. Preserve exact figures, dates, formulas, SQL, and fully-qualified table and \
column names verbatim. They are the whole value of the extraction.
3. Split by topic, not by page. One document may yield several concepts, or one.
4. Section headings: prefer OKF's conventional ones where they fit (Definition, \
Schema, Computation, Examples, Notes); otherwise reuse the document's own.
5. `links` lists the titles of OTHER concepts this one refers to. Include \
targets you believe do not exist yet — a link to unwritten knowledge is \
explicitly legal in OKF and becomes an authoring backlog item.
6. `description` is one sentence. `slug` is lowercase kebab-case with no slashes.
7. Do not invent trust: you are not verifying anything, only drafting.\
"""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "type": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string"},
                                "text": {"type": "string"},
                            },
                            "required": ["heading", "text"],
                            "additionalProperties": False,
                        },
                    },
                    "links": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["slug", "type", "title", "description", "tags",
                             "sections", "links"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["concepts"],
    "additionalProperties": False,
}


class LLMExtractor:
    """Structured-output extraction against OpenAI or Anthropic."""

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model

    def extract(self, doc: Document, spec: WikiSpec) -> list[ConceptDraft]:
        prompt = (
            f"Ontology (prefer these `type` values, but a better short type is "
            f"acceptable): {', '.join(spec.ontology)}\n"
            f"Produce at most {spec.max_concepts_per_doc} concepts.\n\n"
            f"--- DOCUMENT ---\n"
            f"title: {doc.title}\n"
            f"source: {doc.location}\n"
            f"last_modified: {doc.last_modified}\n\n"
            f"{doc.text}\n--- END DOCUMENT ---"
        )
        payload = (_openai_json if self.provider == "openai" else _anthropic_json)(
            system=EXTRACTION_SYSTEM, prompt=prompt,
            schema=EXTRACTION_SCHEMA, model=self.model,
        )
        allowed = {t.lower(): t for t in spec.ontology}
        allowed.update({t: t.title() for t in TYPE_DIRS})
        drafts: list[ConceptDraft] = []
        for raw in (payload.get("concepts") or [])[:spec.max_concepts_per_doc]:
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            # Constrain `type` to the ontology. OKF tolerates unknown types, but
            # every distinct string becomes its own Neo4j label and its own
            # `SET c:Label` round-trip at ingest — a model inventing 200 types
            # gives 200 queries and a schema view nobody can read.
            proposed = str(raw.get("type") or "").strip()
            ctype = allowed.get(proposed.lower(), spec.ontology[0] if not proposed
                                else _closest_type(proposed, spec.ontology))
            sections = [
                DraftSection(str(s.get("heading") or "Notes").strip(),
                             str(s.get("text") or "").strip())
                for s in (raw.get("sections") or [])
                if str(s.get("text") or "").strip()
            ]
            drafts.append(ConceptDraft(
                slug=slugify(str(raw.get("slug") or title), slugify(title, doc.slug)),
                type=ctype,
                title=title,
                description=str(raw.get("description") or "").strip(),
                tags=[str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()],
                sections=sections,
                links=[str(l).strip() for l in (raw.get("links") or []) if str(l).strip()],
                stale_after=spec.stale_after,
            ))
        return drafts


def _closest_type(proposed: str, ontology: list[str]) -> str:
    """Fold an off-ontology type onto the nearest allowed one by word overlap."""
    words = set(re.findall(r"[a-z]+", proposed.lower()))
    best, best_score = ontology[0], 0
    for candidate in ontology:
        score = len(words & set(re.findall(r"[a-z]+", candidate.lower())))
        if score > best_score:
            best, best_score = candidate, score
    return best


def _openai_json(system: str, prompt: str, schema: dict, model: str) -> dict:
    from openai import OpenAI

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Run the offline path instead: "
            "--extractor heuristic")
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": prompt}],
        response_format={"type": "json_schema", "json_schema": {
            "name": "okf_extraction", "strict": True, "schema": schema}},
    )
    return json.loads(resp.choices[0].message.content or "{}")


def _anthropic_json(system: str, prompt: str, schema: dict, model: str) -> dict:
    try:
        import anthropic
    except ImportError as exc:                     # pragma: no cover - optional dep
        raise RuntimeError(
            "the anthropic extractor needs the `anthropic` package "
            "(`uv sync --extra anthropic`)") from exc

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=16000,                # caps thinking + output on Claude 5 models
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined this document: {resp.stop_details}")
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return json.loads(text or "{}")


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^#\s+(?P<heading>.+)$", re.MULTILINE)


def _split_markdown_sections(body: str) -> list[tuple[str, str]]:
    """Split on `# ` headings, keeping any preamble under `Overview`."""
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        return [("Overview", body.strip())] if body.strip() else []
    out: list[tuple[str, str]] = []
    preamble = body[: matches[0].start()].strip()
    if preamble:
        out.append(("Overview", preamble))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[m.end():end].strip()
        if text:
            out.append((m.group("heading").strip(), text))
    return out


def _first_sentence(text: str, limit: int = 220) -> str:
    for para in text.split("\n\n"):
        clean = re.sub(r"\s+", " ", re.sub(r"^#+\s+.*$", "", para, flags=re.MULTILINE)).strip()
        clean = re.sub(r"[*`>|]", "", clean).strip()
        if len(clean) < 25:
            continue
        m = re.search(r"^(.{25,%d}?[.!?])\s" % limit, clean + " ")
        return (m.group(1) if m else clean[:limit]).strip()
    return ""


def _merge(into: ConceptDraft, other: ConceptDraft) -> None:
    """Fold a second draft of the same concept into the first."""
    if len(other.description) > len(into.description):
        into.description = other.description
    into.tags = sorted(set(into.tags) | set(other.tags))
    into.links = sorted(set(into.links) | set(other.links))
    into.source_sids = sorted(set(into.source_sids) | set(other.source_sids))
    seen = {s.heading for s in into.sections}
    for sec in other.sections:
        heading = sec.heading
        n = 2
        while heading in seen:                     # keep both, do not silently drop
            heading, n = f"{sec.heading} ({n})", n + 1
        seen.add(heading)
        into.sections.append(DraftSection(heading, sec.text, list(sec.source_sids)))


def build_wiki(docs: Iterable[Document], spec: Optional[WikiSpec] = None,
               extractor: Optional[Extractor] = None,
               now: Optional[_dt.datetime] = None) -> WikiBuild:
    """Documents → drafts → a rendered OKF bundle (files, not the filesystem)."""
    spec = spec or WikiSpec()
    extractor = extractor or get_extractor(spec)
    docs = list(docs)
    stamp = (now or _dt.datetime.now(_dt.timezone.utc)).isoformat(timespec="seconds")

    # 1. extract, keyed by concept id so two documents can describe one concept
    drafts: dict[str, ConceptDraft] = {}
    taken: dict[str, set[str]] = {}                # per-directory slug registry
    for doc in docs:
        found = extractor.extract(doc, spec) or []
        sid = _source_sid(doc)
        for draft in found:
            draft.source_sids = [sid]
            for sec in draft.sections:
                sec.source_sids = [sid]            # claim-level provenance (§5.1)
            directory = draft.id.rsplit("/", 1)[0]
            if draft.id in drafts:
                _merge(drafts[draft.id], draft)
                continue
            draft.slug = safe_slug(draft.slug, "concept",
                                   taken.setdefault(directory, set()))
            drafts[draft.id] = draft

    # 2. resolve cross-references into real markdown links, minting stubs for
    #    the targets nobody has written (SPEC §6.1)
    targets = _link_targets(drafts, spec)
    for draft in drafts.values():
        directory = draft.id.rsplit("/", 1)[0] if "/" in draft.id else ""
        for sec in draft.sections:
            sec.text = _insert_links(sec.text, targets, exclude=draft.id,
                                     from_dir=directory)

    # 3. build the bundle model
    pb = ParsedBundle(name=spec.name, root="", okf_version=spec.okf_version)
    docs_by_sid = {_source_sid(d): d for d in docs}
    actor = parse_actor(spec.producer)
    if actor:
        pb.actors[actor.id] = actor

    for draft in drafts.values():
        pb.concepts[f"{spec.name}:{draft.id}"] = _to_concept(
            draft, spec, docs_by_sid, actor, stamp)

    if spec.mirror_sources:
        for doc in docs:
            concept = _mirror_concept(doc, spec, actor, stamp)
            pb.concepts[concept.uid] = concept

    files = emit.render_bundle(pb)
    stats = {
        "documents": len(docs),
        "concepts": len(drafts),
        "mirrored_sources": len(docs) if spec.mirror_sources else 0,
        "sections": sum(len(d.sections) for d in drafts.values()),
        "files": len(files),
        "extractor": spec.extractor,
        "producer": spec.producer,
    }
    _assert_survives_reparse(files, spec, expected=len(pb.concepts))
    return WikiBuild(bundle=pb, files=files, drafts=list(drafts.values()), stats=stats)


def _assert_survives_reparse(files: dict[str, str], spec: WikiSpec,
                             expected: int) -> None:
    """Re-parse what we just wrote and check nothing vanished.

    Emitting markdown and re-reading it makes the filesystem a channel, and its
    failure modes — reserved names, case folding, slug collisions — are all
    silent. One equality check catches the whole class, so it runs on every
    build rather than only in the tests.
    """
    import tempfile
    from pathlib import Path

    from .parser import parse_bundle
    from .project import write_dir

    with tempfile.TemporaryDirectory() as tmp:
        root = write_dir(files, Path(tmp) / spec.name)
        got = len(parse_bundle(root, spec.name).concepts)
    if got != expected:
        raise RuntimeError(
            f"{expected} concepts were authored but only {got} survive a re-parse — "
            "a slug collided with a reserved filename or another concept")


def _source_sid(doc: Document) -> str:
    """The footnote key for a document (`sources[].id`, SPEC §5.1)."""
    return doc.slug


def _to_concept(draft: ConceptDraft, spec: WikiSpec, docs_by_sid: dict[str, Document],
                actor, stamp: str) -> ParsedConcept:
    uid = f"{spec.name}:{draft.id}"
    directory = draft.id.rsplit("/", 1)[0] if "/" in draft.id else ""

    sources: list[ParsedSource] = []
    for i, sid in enumerate(draft.source_sids):
        doc = docs_by_sid.get(sid)
        if not doc:
            continue
        resource = (f"references/{doc.slug}.md" if spec.mirror_sources
                    else doc.resource)
        sources.append(ParsedSource(
            uid=f"{spec.name}:src:{resource}", sid=sid, resource=resource,
            title=doc.title, author=doc.author, last_modified=doc.last_modified,
            order=i,
            resolves_to_concept=(f"{spec.name}:references/{doc.slug}"
                                 if spec.mirror_sources else None),
        ))

    concept = ParsedConcept(
        uid=uid, id=draft.id, bundle=spec.name, path=f"{draft.id}.md", dir=directory,
        type=draft.type, type_label=type_to_label(draft.type), title=draft.title,
        description=draft.description or None,
        status=spec.status, stale_after=draft.stale_after,
        tags=sorted(set(draft.tags)),
        generated_by=actor, generated_at=stamp,
        trust_tier="unverified",                   # no `verified` key — §5.3
        sources=sources,
    )
    for order, sec in enumerate(draft.sections):
        text = sec.text
        cites = [s.uid for s in sources if s.sid in sec.source_sids]
        if cites and sec.source_sids:
            text = _append_footnote_refs(text, sec.source_sids)
        concept.sections.append(ParsedSection(
            uid=f"{uid}#s{order}", concept_uid=uid, heading=sec.heading,
            order=order, text=text, cites=sorted(cites),
        ))
    _append_footnote_definitions(concept, docs_by_sid)
    return concept


def _append_footnote_refs(text: str, sids: list[str]) -> str:
    """Attach `[^sid]` to a section so the citation is claim-level, not file-level."""
    refs = " ".join(f"[^{sid}]" for sid in sids if f"[^{sid}]" not in text)
    return f"{text.rstrip()} {refs}".rstrip() if refs else text


def _append_footnote_definitions(concept: ParsedConcept,
                                 docs_by_sid: dict[str, Document]) -> None:
    """Markdown needs the `[^sid]: …` definitions somewhere in the body."""
    if not concept.sections or not concept.sources:
        return
    lines = []
    for src in concept.sources:
        doc = docs_by_sid.get(src.sid or "")
        label = src.title or (doc.title if doc else src.resource)
        origin = f" — {doc.location}" if doc else ""
        lines.append(f"[^{src.sid}]: {label}{origin}")
    concept.sections[-1].text = concept.sections[-1].text.rstrip() + "\n\n" + "\n".join(lines)


def _mirror_concept(doc: Document, spec: WikiSpec, actor, stamp: str) -> ParsedConcept:
    """Mirror a source document as a first-class concept under `references/`.

    SPEC §6.3 puts external material in `references/`, and doing so pays off in
    the graph: `sources[].resource` then points *inside* the bundle, so the
    parser emits `(:Source)-[:RESOLVES_TO]->(:Concept)` and a provenance chain
    from a generated claim all the way back to the page it was read from
    becomes a traversal instead of a string.
    """
    cid = f"references/{doc.slug}"
    uid = f"{spec.name}:{cid}"
    body = body_without_title(doc)[:MAX_MIRROR_CHARS]
    concept = ParsedConcept(
        uid=uid, id=cid, bundle=spec.name, path=f"{cid}.md", dir="references",
        type="Reference", type_label="Reference", title=doc.title,
        description=f"Mirror of {doc.origin} source: {doc.location}",
        resource=doc.location,
        status="stable", tags=["reference", doc.origin],
        generated_by=actor, generated_at=stamp, trust_tier="unverified",
        extra_frontmatter=json.dumps({
            "retrieved_at": doc.retrieved_at,
            "sha256": doc.sha256,
            "media_type": doc.media_type,
        }),
        sources=[ParsedSource(
            uid=f"{spec.name}:src:{doc.location}", sid=doc.slug,
            resource=doc.location, title=doc.title, author=doc.author,
            last_modified=doc.last_modified,
            is_scope=False, order=0,
        )],
    )
    for order, (heading, text) in enumerate(_split_markdown_sections(body)):
        concept.sections.append(ParsedSection(
            uid=f"{uid}#s{order}", concept_uid=uid, heading=heading,
            order=order, text=text,
        ))
    if not concept.sections:
        concept.sections.append(ParsedSection(
            uid=f"{uid}#s0", concept_uid=uid, heading="Overview", order=0,
            text=f"(empty document at {doc.location})",
        ))
    return concept


# --------------------------------------------------------------------------
# Cross-reference resolution
# --------------------------------------------------------------------------

def _link_targets(drafts: dict[str, ConceptDraft], spec: WikiSpec) -> dict[str, str]:
    """`{title: bundle-relative path}` for every concept a link could point at.

    Includes proposed-but-missing targets: linking to knowledge nobody has
    written yet is legal OKF, and it is what turns "the extractor kept
    mentioning a Margin Daily dashboard" into a queryable backlog item rather
    than a dropped sentence.
    """
    targets: dict[str, str] = {}
    for draft in drafts.values():
        targets[draft.title] = f"{draft.id}.md"
    for draft in drafts.values():
        for proposed in draft.links:
            if proposed in targets:
                continue
            directory = next((d for kw, d in _TYPE_HINTS if kw in proposed.lower()),
                             DEFAULT_DIR)
            targets[proposed] = f"{directory}/{slugify(proposed, 'concept')}.md"
    return targets


_FENCE_SPLIT = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_EXISTING_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")


def _insert_links(text: str, targets: dict[str, str], exclude: str,
                  from_dir: str = "") -> str:
    """Turn the first mention of each other concept's title into a real link.

    Only the first mention per section, never inside a code span or fence, and
    never inside an existing link — the resulting markdown has to survive
    re-parsing by `parser.py`, which is what actually creates the `LINKS_TO`
    edges. Links are file-relative so they render on GitHub, matching how every
    bundle in Google's repo writes them.
    """
    # longest title first, so "Customer Orders table" wins over "Customer Orders"
    ordered = sorted(
        ((title, path) for title, path in targets.items()
         if path != f"{exclude}.md" and len(title) >= 4),
        key=lambda kv: -len(kv[0]),
    )
    linked: set[str] = set()
    parts = _FENCE_SPLIT.split(text)
    for i, part in enumerate(parts):
        if i % 2:                                   # odd chunks are code — skip
            continue
        for title, path in ordered:
            if title in linked:
                continue
            pattern = re.compile(rf"(?<!\[)\b{re.escape(title)}\b(?!\])", re.IGNORECASE)
            spans = [m.span() for m in _EXISTING_LINK.finditer(part)]

            href = emit.relative_link(from_dir, path)

            def replace(m: re.Match, spans=spans, href=href) -> str:
                if any(a <= m.start() < b for a, b in spans):
                    return m.group(0)
                return f"[{m.group(0)}]({href})"

            part, count = pattern.subn(replace, part, count=1)
            if count:
                linked.add(title)
        parts[i] = part
    return "".join(parts)
