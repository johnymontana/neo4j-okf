"""Parse an OKF (Open Knowledge Format) v0.2 bundle into an in-memory model.

The parser is deterministic (no LLM involved) and deliberately permissive,
per OKF's conformance rules (SPEC §11): unknown types, unknown frontmatter
keys, broken links, and missing optional families never cause a rejection.

Mapping produced here (see ingest.py for the Cypher side):

    OKF construct                      -> graph element
    ------------------------------------------------------------------
    bundle (directory)                 -> (:Bundle)
    concept file (*.md)                -> (:Concept:<TypeLabel>)
    concept id (path sans .md)         -> Concept.id / Concept.uid
    frontmatter scalars                -> node properties
    unknown frontmatter keys           -> Concept.extra_frontmatter (JSON)
    markdown link between concepts     -> [:LINKS_TO {section, text}]
    link to not-yet-written concept    -> stub (:Concept {stub: true})
    body h1 sections                   -> (:Section) + [:HAS_SECTION]/[:NEXT]
    footnote refs  [^id]               -> (:Section)-[:CITES]->(:Source)
    sources[] entries                  -> (:Source) + [:DERIVES_FROM]
      intrinsic signals (author, ...)  ->   Source properties
      declaration-scoped signals       ->   DERIVES_FROM properties
      internal resource paths          ->   (:Source)-[:RESOLVES_TO]->(...)
    generated / verified actors        -> (:Actor) + [:GENERATED_BY/VERIFIED_BY {at}]
    tags                               -> (:Tag) + [:TAGGED]
    executor.resource                  -> [:EXECUTED_BY] -> (:Concept)
    attester.resource                  -> [:ATTESTED_BY] -> (:Artifact)
    computation (file form)            -> [:COMPUTATION_FILE] -> (:Artifact)
    log.md entries                     -> (:LogEntry) + [:REFERENCES]
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

RESERVED_FILENAMES = {"index.md", "log.md"}

# Frontmatter keys lifted into first-class node properties. Everything else
# is preserved (round-trippable) in Concept.extra_frontmatter as JSON.
KNOWN_KEYS = {
    "type", "title", "description", "resource", "tags",
    "generated", "verified", "status", "stale_after",
    "sources", "usage_window",
    "runtime", "parameters", "computation", "executor", "attester",
    "timestamp",  # v0.1 legacy fallback for generated.at (SPEC §13.1)
}

MD_LINK_RE = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]]+)\]")
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:", re.MULTILINE)
LOG_DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")
LOG_KIND_RE = re.compile(r"^\*\*([^*]+)\*\*[:\s]*")

MAX_SECTION_CHARS = 2400  # long sections are split at paragraph boundaries

# Bundles carry code alongside concepts (attesters, computation files). We keep
# the text of small ones in the model so a bundle can be re-emitted from the
# graph alone (see emit.py); anything larger or non-UTF-8 stays a path.
MAX_ARTIFACT_BYTES = 64 * 1024


# --------------------------------------------------------------------------
# In-memory model
# --------------------------------------------------------------------------

@dataclass
class ParsedActor:
    id: str            # raw actor string, e.g. "human:jsmith@acme"
    kind: str          # human | process | agent
    name: str          # display name (id without prefix)
    producer: Optional[str] = None   # for agents: producer part
    version: Optional[str] = None    # for agents: version part


@dataclass
class ParsedSource:
    uid: str
    sid: Optional[str]              # sources[].id — footnote join key
    resource: str
    title: Optional[str] = None
    author: Optional[str] = None
    usage_count: Optional[int] = None
    last_modified: Optional[str] = None       # YYYY-MM-DD
    is_scope: bool = False                    # scope descriptor, not a path
    order: int = 0                            # position in the concept's sources[]
    resolves_to_concept: Optional[str] = None # concept uid if internal .md
    resolves_to_artifact: Optional[str] = None
    # declaration-scoped (edge) properties
    usage_window_from: Optional[str] = None
    usage_window_to: Optional[str] = None


@dataclass
class ParsedLink:
    source_uid: str
    target_uid: str
    target_id: str
    text: str
    section: Optional[str]
    raw: str
    resolved: bool


@dataclass
class ParsedSection:
    uid: str
    concept_uid: str
    heading: str
    order: int
    text: str
    cites: list[str] = field(default_factory=list)      # Source uids
    mentions: list[str] = field(default_factory=list)   # Concept uids
    # An over-long section is split across several nodes; `part` records which
    # piece this is (1 when the section was not split). Kept as data rather
    # than encoded in `heading`, so an authored "Notes (part 2)" is never
    # mistaken for a synthetic split when the body is reassembled.
    part: int = 1


@dataclass
class ParsedLogEntry:
    uid: str
    date: str
    kind: Optional[str]
    text: str
    order: int
    references: list[str] = field(default_factory=list)  # concept uids
    dir: str = ""            # which log.md it came from (SPEC §9 allows any level)


@dataclass
class ParsedConcept:
    uid: str
    id: str
    bundle: str
    path: str
    dir: str
    type: str
    type_label: str
    title: str
    description: Optional[str] = None
    resource: Optional[str] = None
    status: str = "stable"                 # SPEC §5.4: absent => stable
    stale_after: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    generated_by: Optional[ParsedActor] = None
    generated_at: Optional[str] = None
    verified: list[tuple[ParsedActor, Optional[str]]] = field(default_factory=list)
    trust_tier: str = "unverified"         # derived per SPEC §5.3
    sources: list[ParsedSource] = field(default_factory=list)
    sections: list[ParsedSection] = field(default_factory=list)
    body: str = ""
    extra_frontmatter: Optional[str] = None
    # Attested Computation family (SPEC §10)
    runtime: Optional[str] = None
    parameters_json: Optional[str] = None
    receipt: list[str] = field(default_factory=list)
    executor_resource: Optional[str] = None
    attester_resource: Optional[str] = None
    computation_path: Optional[str] = None
    # the path strings exactly as authored — the three fields above get
    # normalized to uids during link extraction, and emit.py needs the originals
    executor_raw: Optional[str] = None
    attester_raw: Optional[str] = None
    computation_raw: Optional[str] = None
    stub: bool = False
    # False when the file's YAML would not parse. The frontmatter families are
    # then all defaults, and a re-emit would stack a second block on top of the
    # unparsed original — so emit.py refuses instead, and the projection
    # manifest names the file.
    frontmatter_ok: bool = True


@dataclass
class ParsedBundle:
    name: str
    root: str
    okf_version: Optional[str]
    concepts: dict[str, ParsedConcept] = field(default_factory=dict)  # by uid
    stubs: dict[str, ParsedConcept] = field(default_factory=dict)
    links: list[ParsedLink] = field(default_factory=list)
    actors: dict[str, ParsedActor] = field(default_factory=dict)
    artifacts: dict[str, dict] = field(default_factory=dict)          # uid -> {path, kind}
    log_entries: list[ParsedLogEntry] = field(default_factory=list)
    log_frontmatter: dict[str, str] = field(default_factory=dict)     # dir -> raw YAML
    # Things a strictly-conformant consumer must tolerate but a *producer*
    # wants to know about (SPEC §11 tolerate-don't-reject cuts both ways).
    warnings: list[str] = field(default_factory=list)

    @property
    def all_sections(self) -> list[ParsedSection]:
        return [s for c in self.concepts.values() for s in c.sections]

    def stats(self) -> dict[str, int]:
        return {
            "concepts": len(self.concepts),
            "stub_concepts": len(self.stubs),
            "sections": len(self.all_sections),
            "links": len(self.links),
            "sources": sum(len(c.sources) for c in self.concepts.values()),
            "actors": len(self.actors),
            "artifacts": len(self.artifacts),
            "log_entries": len(self.log_entries),
        }


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def type_to_label(type_str: str) -> str:
    """'BigQuery Table' -> 'BigQueryTable' (original kept in Concept.type)."""
    cleaned = re.sub(r"[^0-9A-Za-z ]+", " ", type_str or "Concept")
    label = "".join(w[:1].upper() + w[1:] for w in cleaned.split())
    if not label or not label[0].isalpha():
        label = "Concept" + label
    return label


def parse_actor(raw: Any) -> Optional[ParsedActor]:
    """Actor convention (SPEC §7): human:<id> | process:<id> | <producer>/<version>."""
    if not raw or not isinstance(raw, str):
        return None
    if raw.startswith("human:"):
        return ParsedActor(raw, "human", raw[len("human:"):])
    if raw.startswith("process:"):
        return ParsedActor(raw, "process", raw[len("process:"):])
    producer, _, version = raw.partition("/")
    return ParsedActor(raw, "agent", raw, producer=producer or raw,
                       version=version or None)


def split_frontmatter_ex(text: str) -> tuple[dict[str, Any], str, bool]:
    """`(frontmatter, body, ok)`.

    `ok` is False when a `---` block is present but unusable (bad YAML,
    unterminated, or not a mapping). Parsing still succeeds with defaults, per
    the tolerate-don't-reject posture (§11) — but callers that re-serialize the
    concept need to know, or they will write a second frontmatter block on top
    of the unparsed original.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, True                  # no frontmatter at all is not a failure
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                fm = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                return {}, text, False         # tolerate, per conformance rules
            if not isinstance(fm, dict):
                return {}, text, False
            return fm, "\n".join(lines[i + 1:]).lstrip("\n"), True
    return {}, text, False                     # opened but never closed


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    fm, body, _ = split_frontmatter_ex(text)
    return fm, body


_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


def _iter_nonfence_lines(body: str) -> Iterable[tuple[int, str, bool]]:
    """Yield (line_no, line, in_fence) tracking fenced code blocks.

    CommonMark-ish: a fence closes only on a marker of the same character and
    at least the same length, so ```` blocks may contain ``` lines.
    """
    open_marker: Optional[str] = None
    for i, line in enumerate(body.splitlines()):
        m = _FENCE_RE.match(line.lstrip())
        if m:
            marker = m.group(1)
            if open_marker is None:
                open_marker = marker
                yield i, line, True
            elif marker[0] == open_marker[0] and len(marker) >= len(open_marker):
                open_marker = None
                yield i, line, True
            else:
                yield i, line, True      # a shorter/other fence inside the block
            continue
        yield i, line, open_marker is not None


def split_sections(body: str) -> list[tuple[str, str, int]]:
    """Split a body into (heading, text, part) on h1 headings, fence-aware.

    OKF bodies conventionally use `# Schema`, `# Computation`, ... (SPEC §4.2),
    which gives us semantically meaningful retrieval units without arbitrary
    fixed-size chunking. Content before the first h1 becomes '_preamble'.
    """
    sections: list[tuple[str, list[str]]] = []
    current_heading = "_preamble"
    current_lines: list[str] = []
    for _, line, in_fence in _iter_nonfence_lines(body):
        if not in_fence and line.startswith("# "):
            if current_lines and any(l.strip() for l in current_lines):
                sections.append((current_heading, current_lines))
            current_heading = line[2:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines and any(l.strip() for l in current_lines):
        sections.append((current_heading, current_lines))

    out: list[tuple[str, str, int]] = []
    for heading, lines in sections:
        text = "\n".join(lines).strip()
        if not text:
            continue
        if len(text) <= MAX_SECTION_CHARS:
            out.append((heading, text, 1))
        else:  # split long sections at paragraph boundaries
            paras, buf, size = text.split("\n\n"), [], 0
            part = 1
            for p in paras:
                if buf and size + len(p) > MAX_SECTION_CHARS:
                    out.append((heading, "\n\n".join(buf), part))
                    part += 1
                    buf, size = [], 0
                buf.append(p)
                size += len(p) + 2
            if buf:
                out.append((heading, "\n\n".join(buf), part))
    return out


# --------------------------------------------------------------------------
# Bundle parser
# --------------------------------------------------------------------------

class BundleParser:
    #: Filenames that are never concepts. Instance-level so producer-specific
    #: subclasses (e.g. OpenWikiParser) can extend the set without forking.
    reserved_filenames: frozenset[str] = frozenset(RESERVED_FILENAMES)

    def __init__(self, root: str | Path, bundle_name: Optional[str] = None):
        self.root = Path(root).resolve()
        self.bundle = bundle_name or self.root.name
        if not self.root.is_dir():
            raise FileNotFoundError(f"bundle root not found: {self.root}")

    # ---- path resolution -------------------------------------------------

    def _norm(self, path: str) -> str:
        return path.replace("\\", "/").lstrip("./")

    def resolve_target(self, raw: str, from_dir: str) -> Optional[str]:
        """Resolve a link/path-valued field to a bundle-relative path.

        SPEC §6.2 allows absolute URLs, /-prefixed bundle-relative paths and
        relative paths. Real bundles also use bare root-relative paths
        (e.g. `skills/run-on-bq.md` referenced from computations/), so we
        try concept-relative first, then bundle-root-relative as a fallback.
        """
        target = raw.split("#", 1)[0].split("?", 1)[0]
        if not target or "://" in target or target.startswith(("mailto:", "tel:", "//")):
            return None
        if target.startswith("/"):
            cand = (self.root / target.lstrip("/")).resolve()
            return self._rel_or_none(cand)
        # relative to the linking file's directory
        cand = (self.root / from_dir / target).resolve()
        rel = self._rel_or_none(cand)
        if rel is not None and (self.root / rel).exists():
            return rel
        # fallback: relative to bundle root
        cand2 = (self.root / target).resolve()
        rel2 = self._rel_or_none(cand2)
        if rel2 is not None and (self.root / rel2).exists():
            return rel2
        return rel or rel2  # unresolved but inside bundle -> stub target

    def _rel_or_none(self, p: Path) -> Optional[str]:
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return None  # escapes the bundle

    def concept_uid(self, rel_md_path: str) -> str:
        return f"{self.bundle}:{rel_md_path[:-3]}"  # strip .md

    # ---- main entry point --------------------------------------------------

    def parse(self) -> ParsedBundle:
        okf_version = self._read_okf_version()
        pb = ParsedBundle(name=self.bundle, root=str(self.root), okf_version=okf_version)

        md_files = sorted(
            p for p in self.root.rglob("*.md")
            if p.name not in self.reserved_filenames
            and not any(part.startswith(".")            # skip .github/, .obsidian/ …
                        for part in p.relative_to(self.root).parts)
        )
        # pass 1: concepts with frontmatter, sections, sources, actors
        for path in md_files:
            concept = self._parse_concept(path, pb)
            pb.concepts[concept.uid] = concept

        # pass 2: body links (needs full concept set for resolution + stubs)
        for concept in list(pb.concepts.values()):
            self._extract_links(concept, pb)

        # pass 3: log files
        self._parse_logs(pb)
        # pass 4: every remaining non-markdown file. Bundles ship code and
        # assets alongside concepts; registering only the *referenced* ones
        # meant an unreferenced file (acme's own viz.html) silently vanished
        # from any projection.
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() == ".md":
                continue
            rel = path.relative_to(self.root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            self._register_artifact(pb, rel.as_posix())
        return pb

    def _read_okf_version(self) -> Optional[str]:
        idx = self.root / "index.md"
        if idx.exists():
            fm, _ = split_frontmatter(idx.read_text(encoding="utf-8-sig"))
            v = fm.get("okf_version")
            return str(v) if v else None
        return None

    # ---- concept parsing ---------------------------------------------------

    def _parse_concept(self, path: Path, pb: ParsedBundle) -> ParsedConcept:
        rel = path.relative_to(self.root).as_posix()
        cid = rel[:-3]
        uid = f"{self.bundle}:{cid}"
        fm, body, fm_ok = split_frontmatter_ex(path.read_text(encoding="utf-8-sig"))
        if not fm_ok:
            pb.warnings.append(
                f"{rel}: frontmatter present but unparseable — defaults applied, "
                "and this file will not be re-serialized")

        ctype = _scalar(fm.get("type")) or "Concept"
        title = (_scalar(fm.get("title"))
                 or Path(cid).name.replace("-", " ").replace("_", " ").title())

        concept = ParsedConcept(
            uid=uid, id=cid, bundle=self.bundle, path=rel,
            dir=str(Path(cid).parent.as_posix()) if "/" in cid else "",
            type=ctype, type_label=type_to_label(ctype), title=title,
            description=_scalar(fm.get("description")),
            resource=_scalar(fm.get("resource")),
            status=_scalar(fm.get("status")) or "stable",
            stale_after=_date_str(fm.get("stale_after")),
            tags=_tags_list(fm.get("tags")),
            body=body,
            frontmatter_ok=fm_ok,
        )

        # trust family (SPEC §5.2) with v0.1 `timestamp` fallback (§13.1)
        gen = fm.get("generated") or {}
        if isinstance(gen, dict):
            actor = parse_actor(gen.get("by"))
            if actor:
                concept.generated_by = actor
                pb.actors.setdefault(actor.id, actor)
            concept.generated_at = _dt_str(gen.get("at")) or _dt_str(fm.get("timestamp"))
        else:
            concept.generated_at = _dt_str(fm.get("timestamp"))
        if concept.generated_at is None:
            concept.generated_at = _dt_str(fm.get("timestamp"))

        verified = fm.get("verified")
        if isinstance(verified, dict):     # bare mapping == one-element list (§5.2)
            verified = [verified]
        for v in verified or []:
            if isinstance(v, dict):
                actor = parse_actor(v.get("by"))
                if actor:
                    pb.actors.setdefault(actor.id, actor)
                    concept.verified.append((actor, _dt_str(v.get("at"))))
        concept.trust_tier = _trust_tier(concept)      # SPEC §5.3

        # provenance family (SPEC §5.1)
        shared_window = fm.get("usage_window") or {}
        for i, entry in enumerate(fm.get("sources") or []):
            if not isinstance(entry, dict) or not entry.get("resource"):
                # §5.1 requires `resource`; without it there is nothing to
                # dedupe or resolve on, so the entry is dropped — but any
                # `[^id]` footnote keyed to it then silently cites nothing.
                sid = entry.get("id") if isinstance(entry, dict) else None
                pb.warnings.append(
                    f"{rel}: sources[{i}] has no `resource` and was dropped"
                    + (f" — footnotes keyed [^{sid}] will not cite" if sid else ""))
                continue
            concept.sources.append(self._parse_source(concept, i, entry, shared_window))

        # computation family (SPEC §10)
        if fm.get("runtime"):
            concept.runtime = str(fm["runtime"])
        if fm.get("parameters") is not None:
            concept.parameters_json = json.dumps(fm["parameters"], default=str)
        executor = fm.get("executor") or {}
        if isinstance(executor, dict):
            res = executor.get("resource")
            concept.executor_resource = res if isinstance(res, str) else None
            concept.executor_raw = concept.executor_resource
            receipt = executor.get("receipt")
            concept.receipt = [str(r) for r in receipt] if isinstance(receipt, list) else []
        attester = fm.get("attester") or {}
        if isinstance(attester, dict):
            res = attester.get("resource")
            concept.attester_resource = res if isinstance(res, str) else None
            concept.attester_raw = concept.attester_resource
        if isinstance(fm.get("computation"), str):
            concept.computation_path = fm["computation"]
            concept.computation_raw = fm["computation"]

        if _has_unclosed_fence(body):
            # everything after the dangling ``` is invisible to sectioning and
            # link extraction; Concept.body still holds it, so a projection is
            # safe, but the graph is missing sections the author wrote
            pb.warnings.append(
                f"{rel}: unclosed code fence — sections and links after it were "
                "not extracted")

        # preserve unknown keys (conformance: MUST NOT reject; SHOULD round-trip)
        extra = {str(k): v for k, v in fm.items() if str(k) not in KNOWN_KEYS}
        if extra:
            concept.extra_frontmatter = json.dumps(extra, default=str)

        # sections (lexical layer)
        source_by_sid = {s.sid: s.uid for s in concept.sources if s.sid}
        for order, (heading, text, part) in enumerate(split_sections(body)):
            sec = ParsedSection(
                uid=f"{uid}#s{order}", concept_uid=uid,
                heading=heading, order=order, text=text, part=part,
            )
            # Count only *references*, and only outside code: drop definition
            # lines ([^id]: …) so a section holding just the footnote text
            # doesn't claim a citation, and strip fences so a regex like
            # `[^A-Z]` inside a SQL block can't mint claim-level provenance.
            non_def_text = "\n".join(
                ln for ln in _strip_fences(text).splitlines()
                if not FOOTNOTE_DEF_RE.match(ln)
            )
            refs = FOOTNOTE_REF_RE.findall(non_def_text)
            sec.cites = sorted({source_by_sid[r] for r in refs if r in source_by_sid})
            concept.sections.append(sec)
        return concept

    def _parse_source(self, concept: ParsedConcept, i: int, entry: dict,
                      shared_window: dict) -> ParsedSource:
        resource = str(entry["resource"])
        sid = entry.get("id")
        is_scope = "://" not in resource and not resource.startswith("/") \
            and " " in resource
        src = ParsedSource(
            uid=f"{self.bundle}:src:{resource}",   # dedupe by resource per bundle
            sid=str(sid) if sid else None,
            resource=resource,
            title=entry.get("title"),
            author=entry.get("author"),
            usage_count=entry.get("usage_count"),
            last_modified=_date_str(entry.get("last_modified")),
            is_scope=is_scope,
            order=i,
        )
        window = entry.get("usage_window") or shared_window
        if isinstance(window, dict):
            src.usage_window_from = _date_str(window.get("from"))
            src.usage_window_to = _date_str(window.get("to"))
        if not is_scope and "://" not in resource:
            resolved = self.resolve_target(resource, concept.dir)
            if resolved and resolved.endswith(".md") and (self.root / resolved).exists():
                src.resolves_to_concept = self.concept_uid(resolved)
            elif resolved and (self.root / resolved).exists():
                src.resolves_to_artifact = f"{self.bundle}:art:{resolved}"
        return src

    # ---- link extraction -----------------------------------------------------

    def _extract_links(self, concept: ParsedConcept, pb: ParsedBundle) -> None:
        seen: set[tuple] = set()
        for sec in concept.sections:
            clean = _strip_fences(sec.text)
            for m in MD_LINK_RE.finditer(clean):
                text, raw_target = m.group(1), m.group(2)
                rel = self.resolve_target(raw_target, concept.dir)
                if rel is None or not rel.endswith(".md") or Path(rel).name in self.reserved_filenames:
                    continue
                target_id = rel[:-3]
                target_uid = f"{self.bundle}:{target_id}"
                resolved = target_uid in pb.concepts
                if not resolved and target_uid not in pb.stubs:
                    # broken link == not-yet-written knowledge (SPEC §6.1)
                    pb.stubs[target_uid] = ParsedConcept(
                        uid=target_uid, id=target_id, bundle=self.bundle,
                        path=rel, dir=str(Path(target_id).parent.as_posix()) if "/" in target_id else "",
                        type="Concept", type_label="Concept",
                        title=Path(target_id).name.replace("-", " ").title(),
                        stub=True,
                    )
                key = (concept.uid, target_uid, sec.heading, raw_target)
                if key not in seen:            # MERGE would collapse repeats anyway
                    seen.add(key)
                    pb.links.append(ParsedLink(
                        source_uid=concept.uid, target_uid=target_uid,
                        target_id=target_id, text=text, section=sec.heading,
                        raw=raw_target, resolved=resolved,
                    ))
                if target_uid not in sec.mentions:
                    sec.mentions.append(target_uid)

        # frontmatter path-valued fields -> edges/artifacts
        for res, kind in ((concept.executor_resource, "executor"),
                          (concept.attester_resource, "attester"),
                          (concept.computation_path, "computation")):
            if not res or "://" in res:
                continue
            rel = self.resolve_target(res, concept.dir)
            if rel is None:
                continue
            if rel.endswith(".md"):
                target_uid = f"{self.bundle}:{rel[:-3]}"
                if target_uid not in pb.concepts:
                    continue
                if kind == "executor":
                    concept.executor_resource = target_uid       # normalized to uid
            else:
                art_uid = self._register_artifact(pb, rel)
                if kind == "attester":
                    concept.attester_resource = art_uid
                elif kind == "computation":
                    concept.computation_path = art_uid
        for src in concept.sources:
            if src.resolves_to_artifact:
                self._register_artifact(
                    pb, src.resolves_to_artifact.split(":art:", 1)[1])

    def _register_artifact(self, pb: ParsedBundle, rel: str) -> str:
        """Record a non-.md bundle file, carrying its text when small enough.

        Size is checked from the directory entry before anything is read: a
        `sources[].resource` is free-form producer input, so pointing it at a
        multi-gigabyte file must not turn a parse into a full read.
        """
        art_uid = f"{self.bundle}:art:{rel}"
        if art_uid in pb.artifacts:
            return art_uid
        path = self.root / rel
        text: Optional[str] = None
        digest: Optional[str] = None
        size: Optional[int] = None
        reason: Optional[str] = None
        try:
            size = path.stat().st_size
            digest = _sha256_file(path)
            if size > MAX_ARTIFACT_BYTES:
                reason = "too_big"
            else:
                try:
                    text = path.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    reason = "binary"
        except OSError:
            reason = "missing"
        pb.artifacts[art_uid] = {
            "uid": art_uid, "path": rel,
            "kind": "code" if rel.endswith((".py", ".sql", ".js")) else "file",
            "text": text, "sha256": digest, "size": size, "reason": reason,
        }
        return art_uid

    # ---- log parsing -----------------------------------------------------------

    def _parse_logs(self, pb: ParsedBundle) -> None:
        order = 0
        for log_path in sorted(self.root.rglob("log.md")):
            rel_dir = log_path.parent.relative_to(self.root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            raw = log_path.read_text(encoding="utf-8-sig")
            fm, body = split_frontmatter(raw)
            if fm:
                # §9 does not define frontmatter for log.md, but the reference
                # bundle ships some. Keep it verbatim so a re-emit preserves
                # it rather than synthesizing a different one.
                pb.log_frontmatter[rel_dir] = raw.split("---", 2)[1].strip("\n")
            current_date = None
            for _, line, in_fence in _iter_nonfence_lines(body):
                if in_fence:
                    continue
                dm = LOG_DATE_RE.match(line.strip())
                if dm:
                    current_date = _date_str(dm.group(1))   # calendar-validated
                    continue
                stripped = line.strip()
                if current_date and stripped.startswith(("- ", "* ")):
                    text = stripped[2:].strip()
                    km = LOG_KIND_RE.match(text)
                    kind = km.group(1).strip() if km else None
                    entry = ParsedLogEntry(
                        uid=f"{self.bundle}:log:{order}", date=current_date,
                        kind=kind, text=re.sub(r"\s+", " ", text), order=order,
                        dir=rel_dir,
                    )
                    targets = [m.group(2) for m in MD_LINK_RE.finditer(text)]
                    # liberal consumer: `path/to/x.md` in backticks counts too
                    targets += re.findall(r"`([^`\s]+\.md)`", text)
                    for raw in targets:
                        rel = self.resolve_target(raw, rel_dir)
                        if rel and rel.endswith(".md"):
                            uid = self.concept_uid(rel)
                            if uid in pb.concepts and uid not in entry.references:
                                entry.references.append(uid)
                    pb.log_entries.append(entry)
                    order += 1


# --------------------------------------------------------------------------
# tiny value coercers
# --------------------------------------------------------------------------

def _has_unclosed_fence(body: str) -> bool:
    open_marker: Optional[str] = None
    for line in body.splitlines():
        m = _FENCE_RE.match(line.lstrip())
        if not m:
            continue
        marker = m.group(1)
        if open_marker is None:
            open_marker = marker
        elif marker[0] == open_marker[0] and len(marker) >= len(open_marker):
            open_marker = None
    return open_marker is not None


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _strip_fences(text: str) -> str:
    out = []
    for _, line, in_fence in _iter_nonfence_lines(text):
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def _date_str(v: Any) -> Optional[str]:
    """Calendar-validated YYYY-MM-DD, or None. Never lets an invalid date
    reach Cypher's date() — a malformed producer value must not abort an
    ingest (SPEC §11 posture: tolerate, don't reject)."""
    if v is None:
        return None
    s = str(v)[:10]
    try:
        return _dt.date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _dt_str(v: Any) -> Optional[str]:
    """ISO-validated datetime string, or None (same tolerance rationale)."""
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day).isoformat()
    s = str(v).strip()
    s = s.replace(" ", "T", 1) if "T" not in s and " " in s else s
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _scalar(v: Any) -> Optional[str]:
    """Known scalar keys must land as Neo4j primitives; a producer that puts a
    map/list there gets it stringified as JSON rather than aborting ingest."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    return json.dumps(v, default=str)


def _tags_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):                      # 'a, b, c' — real ga4/so bundles
        return [t.strip() for t in v.split(",") if t.strip()]
    if isinstance(v, (list, tuple)):
        return [str(t) for t in v]
    return [str(v)]


def _trust_tier(c: ParsedConcept) -> str:
    """SPEC §5.3: no verified => unverified; non-human only => machine-confirmed;
    any human: actor => human-reviewed."""
    if not c.verified:
        return "unverified"
    if any(a.kind == "human" for a, _ in c.verified):
        return "human-reviewed"
    return "machine-confirmed"


def parse_bundle(root: str | Path, bundle_name: Optional[str] = None) -> ParsedBundle:
    return BundleParser(root, bundle_name).parse()
