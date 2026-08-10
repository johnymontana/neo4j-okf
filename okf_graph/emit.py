"""Serialize the in-memory model back into OKF markdown — the inverse of parser.py.

`parser.py` turns a bundle of files into a `ParsedBundle`; this module turns a
`ParsedBundle` back into files. Two callers need it:

* `project.py` — reads a bundle out of Neo4j and re-emits it as a portable OKF
  bundle (optionally filtered: "give me the human-reviewed, non-stale finance
  subset as a tarball").
* `wiki.py`   — an LLM authors concept drafts from documents; we emit them as a
  real OKF bundle, then let the *existing* parser + ingest build the graph. One
  path into Neo4j, no second mapping to keep in sync.

Producer conventions followed (SPEC v0.2):

    §4    frontmatter first, in the spec's own key order; `type` always present
    §5.1  sources[] with credibility signals; usage_window merged onto its entry
    §5.2  generated / verified written in the reference bundles' flow style
    §8    index.md regenerated per directory (it is *derivable*, never ingested)
    §9    log.md, newest date first
    §11   unknown *top-level* frontmatter keys are written back out — consumers
          MUST NOT reject them, so a producer must not drop them. Unknown keys
          nested inside a known family are a documented gap (ROUNDTRIP_NOTES).

Round-trip caveats are collected in ROUNDTRIP_NOTES below and surfaced in the
projection manifest, so a consumer can see exactly where a re-emitted bundle is
equivalent rather than byte-identical.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any, Iterable, Optional

import yaml

from .parser import ParsedBundle, ParsedConcept, ParsedSection

# SPEC key order: identity, then the computation family (§10), then the
# trust/lifecycle/provenance families (§5). Unknown keys are appended after.
FRONTMATTER_ORDER = [
    "type", "title", "description", "resource", "tags",
    "runtime", "parameters", "computation", "executor", "attester",
    "generated", "verified", "status", "stale_after", "sources",
]

# A projection is content-equivalent, not byte-identical. Each item below is a
# place where re-emitting changes the text but not the meaning; they are copied
# into the projection manifest so a consumer can audit the difference instead of
# discovering it in a diff.
ROUNDTRIP_NOTES = [
    "index.md is regenerated from the graph (SPEC §8 — derivable, never ingested), "
    "so hand-written directory blurbs are replaced by concept descriptions.",
    "`status` is always written explicitly, even when it is the default `stable` (§5.4).",
    "Datetimes are normalized to ISO-8601 with an explicit offset ('Z' becomes '+00:00'); "
    "date-typed fields (stale_after, last_modified, usage_window) keep only YYYY-MM-DD.",
    "A `usage_window` declared once for all of a concept's sources is written onto each "
    "source entry it applied to (same meaning, different placement).",
    "A bare `verified:` mapping is written back as a one-element list, and a comma-string "
    "`tags:` as a flow sequence — both are the normalized form of the same value (§5.2).",
    "Unknown top-level frontmatter keys survive but move below the known families, and "
    "YAML dates/datetimes inside them become strings. Unknown keys *nested* inside a "
    "known family (e.g. generated.model, sources[].license) are not retained.",
    "Log entry text is whitespace-normalized to a single line; only `- `/`* ` bullets "
    "under a calendar-valid `## YYYY-MM-DD` heading are captured at all.",
    "Bodies are stored near-verbatim: line endings normalize to LF, a BOM is dropped, "
    "and the file ends in exactly one newline.",
    "Artifacts over the size limit, non-UTF-8, or unreadable carry no text and are not "
    "written; each is listed under `unmaterialized_artifacts` with a reason.",
    "Synthesized defaults are indistinguishable from authored values on the way back: "
    "an absent `type`, `title` or `status` is re-emitted as Concept / the filename / stable.",
]

# Names Windows refuses, plus the OKF reserved files. A concept whose id
# slugified to `log` or `index` would collide with a generated reserved file
# and — because the parser skips reserved names — vanish on the way back in.
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                     *(f"lpt{i}" for i in range(1, 10))}
RESERVED_STEMS = {"index", "log"}


class EmitCollision(ValueError):
    """Two bundle entries resolved to the same path."""


def safe_bundle_path(path: str) -> str:
    """Validate a bundle-relative path before it is ever written.

    In this repo a `Concept.path` can originate in a fetched web page, so it is
    untrusted input all the way to the filesystem. Rejecting here — rather than
    only in the directory writer — means the tarball and zip writers, and any
    consumer of the returned dict, get the same guarantee.
    """
    if not path or path != path.strip() or "\x00" in path:
        raise ValueError(f"unsafe bundle path: {path!r}")
    parts = path.replace("\\", "/").split("/")
    if parts[0] == "" or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"unsafe bundle path: {path!r}")
    if len(parts) > 1 and ":" in parts[0]:                # drive-letter absolute
        raise ValueError(f"unsafe bundle path: {path!r}")
    stem = parts[-1].split(".")[0].lower()
    if stem in _WINDOWS_RESERVED:
        raise ValueError(f"reserved device name in bundle path: {path!r}")
    return "/".join(parts)


# --------------------------------------------------------------------------
# YAML with the reference bundles' shape (flow style for the compact families)
# --------------------------------------------------------------------------

class _FlowMap(dict):
    """A mapping to render inline: `generated: { by: x, at: y }`."""


class _FlowSeq(list):
    """A sequence to render inline: `tags: [a, b, c]`."""


class _OkfDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False):
        # indent block sequences under their key (`sources:` then `  - id: …`),
        # which is what the reference bundles look like
        return super().increase_indent(flow, False)


_OkfDumper.add_representer(
    _FlowMap,
    lambda d, data: d.represent_mapping("tag:yaml.org,2002:map", data, flow_style=True),
)
_OkfDumper.add_representer(
    _FlowSeq,
    lambda d, data: d.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True),
)


def dump_yaml(mapping: dict[str, Any]) -> str:
    """Dump frontmatter: declared order preserved, no line folding.

    `width` is deliberately huge — folding a long `description` across lines is
    valid YAML but produces noisy diffs, and these files live in git.
    """
    return yaml.dump(
        mapping, Dumper=_OkfDumper, sort_keys=False, default_flow_style=False,
        allow_unicode=True, width=10**6,
    )


def _prune(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None and v != [] and v != {}}


def _as_date(value: Optional[str]) -> Optional[Any]:
    """Render a validated `YYYY-MM-DD` bare, the way authored bundles write it.

    Datetimes stay quoted ISO strings: YAML's native timestamp form would
    rewrite `2026-06-30T14:00:00+00:00` with a space separator, and an exact
    instant is worth more than the missing quotes.
    """
    if not value:
        return value
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        return value


# --------------------------------------------------------------------------
# Concept frontmatter
# --------------------------------------------------------------------------

def relative_link(from_dir: str, target_path: str) -> str:
    """`target_path` written as a file-relative link from a file in `from_dir`.

    File-relative rather than `/`-rooted: every bundle in Google's repo links
    this way, and it is what renders on GitHub and in Obsidian — which is the
    portability the format is selling. An empty target yields an empty string,
    never `"./"`, so callers can test the result for truthiness.
    """
    if not target_path:
        return ""
    from_parts = from_dir.split("/") if from_dir else []
    to_parts = target_path.split("/")
    filename = to_parts.pop()
    common = 0
    while common < len(from_parts) and common < len(to_parts) \
            and from_parts[common] == to_parts[common]:
        common += 1
    hops = [".."] * (len(from_parts) - common)
    rest = to_parts[common:]
    if not hops and not rest:
        return f"./{filename}"
    return "/".join(hops + rest + [filename])


def _uid_to_path(uid: str, bundle: str) -> Optional[str]:
    """`bundle:metrics/x` -> `metrics/x.md`; `bundle:art:a/b.py` -> `a/b.py`."""
    prefix = f"{bundle}:"
    if not uid or not uid.startswith(prefix):
        return None
    rest = uid[len(prefix):]
    return rest[len("art:"):] if rest.startswith("art:") else rest + ".md"


def frontmatter_dict(c: ParsedConcept) -> dict[str, Any]:
    """Build the frontmatter mapping for a concept, in SPEC key order."""
    fm: dict[str, Any] = {
        "type": c.type,
        "title": c.title,
        "description": c.description,
        "resource": c.resource,
        "tags": _FlowSeq(c.tags) if c.tags else None,
        "runtime": c.runtime,
    }

    if c.parameters_json:
        try:
            params = json.loads(c.parameters_json)
        except json.JSONDecodeError:
            params = None
        if isinstance(params, list):
            fm["parameters"] = [_FlowMap(p) if isinstance(p, dict) else p for p in params]
        elif params is not None:
            fm["parameters"] = params

    # Path-valued fields (§6.2). The parser normalizes these to uids, so prefer
    # the raw string it captured and fall back to reconstructing from the uid.
    def path_field(raw: Optional[str], uid: Optional[str]) -> str:
        return raw or relative_link(c.dir, _uid_to_path(uid or "", c.bundle) or "")

    computation = path_field(c.computation_raw, c.computation_path)
    if computation:
        fm["computation"] = computation

    executor_res = path_field(c.executor_raw, c.executor_resource)
    if executor_res or c.receipt:
        executor = _prune({"resource": executor_res or None})
        if c.receipt:
            executor["receipt"] = _FlowSeq(c.receipt)
        fm["executor"] = executor

    attester_res = path_field(c.attester_raw, c.attester_resource)
    if attester_res:
        fm["attester"] = {"resource": attester_res}

    # trust family (§5.2) — flow style, matching the reference bundles
    if c.generated_by or c.generated_at:
        gen = _prune({"by": c.generated_by.id if c.generated_by else None,
                      "at": c.generated_at})
        if gen:
            fm["generated"] = _FlowMap(gen)
    if c.verified:
        fm["verified"] = [_FlowMap(_prune({"by": a.id, "at": at}))
                          for a, at in c.verified]

    fm["status"] = c.status
    fm["stale_after"] = _as_date(c.stale_after)

    # provenance family (§5.1) — block maps, one per source
    sources = []
    for s in c.sources:
        entry = _prune({
            "id": s.sid,
            "resource": s.resource,
            "title": s.title,
            "author": s.author,
            "usage_count": s.usage_count,
            "last_modified": _as_date(s.last_modified),
        })
        window = _prune({"from": _as_date(s.usage_window_from),
                         "to": _as_date(s.usage_window_to)})
        if window:
            entry["usage_window"] = _FlowMap(window)
        sources.append(entry)
    if sources:
        fm["sources"] = sources

    fm = _prune(fm)

    # §11: consumers MUST NOT reject unknown keys — so producers must not drop
    # them either. Anything the parser did not claim comes back verbatim.
    if c.extra_frontmatter:
        try:
            extra = json.loads(c.extra_frontmatter)
        except json.JSONDecodeError:
            extra = None
        if isinstance(extra, dict):
            for k, v in extra.items():
                fm.setdefault(k, v)

    ordered = {k: fm[k] for k in FRONTMATTER_ORDER if k in fm}
    ordered.update({k: v for k, v in fm.items() if k not in ordered})
    return ordered


# --------------------------------------------------------------------------
# Concept body
# --------------------------------------------------------------------------

def sections_to_body(sections: Iterable[ParsedSection]) -> str:
    """Reassemble a body from Sections, undoing the parser's length splitting.

    A section over `MAX_SECTION_CHARS` becomes several nodes sharing a heading,
    distinguished by `part`. Rejoining on `part > 1` — rather than on a
    "(part N)" suffix in the heading text — means an author who really did
    write `# Notes (part 2)` gets it back.
    """
    parts: list[str] = []
    for sec in sorted(sections, key=lambda s: s.order):
        text = sec.text.strip("\n")
        if sec.part > 1 or sec.heading == "_preamble":
            parts.append(text)                     # continuation, or pre-heading prose
        else:
            parts.append(f"# {sec.heading}\n\n{text}")
    return "\n\n".join(p for p in parts if p.strip())


def concept_markdown(c: ParsedConcept) -> str:
    """Frontmatter + body for one concept file.

    The stored `body` is preferred when present (verbatim — links, fences and
    footnote definitions all survive); graph-native concepts that only have
    Sections are reassembled from them.
    """
    if not c.frontmatter_ok:
        # The original frontmatter never parsed, so `body` still contains the
        # raw `---` block. Writing a synthesized block above it would stack two
        # and permanently demote the real `type` to prose — pass it through.
        return (c.body if c.body.endswith("\n") else c.body + "\n")
    body = c.body.strip("\n") if c.body and c.body.strip() else sections_to_body(c.sections)
    return f"---\n{dump_yaml(frontmatter_dict(c))}---\n\n{body}\n" if body else \
        f"---\n{dump_yaml(frontmatter_dict(c))}---\n"


# --------------------------------------------------------------------------
# Reserved files: index.md (§8) and log.md (§9)
# --------------------------------------------------------------------------

def _entry(title: str, href: str, description: Optional[str]) -> str:
    line = f"* [{title}]({href})"
    return f"{line} - {description}" if description else line


def index_markdown(directory: str, concepts: list[ParsedConcept],
                   subdirs: dict[str, Optional[str]], artifacts: list[dict],
                   okf_version: Optional[str] = None) -> str:
    """One `index.md` for one directory (SPEC §8 — progressive disclosure).

    Regenerated, never round-tripped: `index.md` is derivable from the graph,
    which is exactly why the ingester ignores it. Shape follows Google's
    reference producer — heading is the concept's `type` verbatim (`# Metric`,
    `# BigQuery Table`), untyped concepts fall under `# Other`, blocks are
    sorted by heading and entries by title, and a missing description drops the
    ` - ` separator rather than trailing it.

    Only the bundle-root index may carry frontmatter, and only `okf_version`
    (§8/§12) — a frontmatter block in any other index is a conformance error.
    """
    blocks: dict[str, list[str]] = {}

    if subdirs:
        blocks["Subdirectories"] = [
            _entry(d, f"{d}/index.md", subdirs[d]) for d in sorted(subdirs)
        ]

    by_type: dict[str, list[ParsedConcept]] = {}
    for c in concepts:
        by_type.setdefault(c.type or "Other", []).append(c)
    for ctype, group in by_type.items():
        blocks.setdefault(ctype, []).extend(
            _entry(c.title, c.path.rsplit("/", 1)[-1], c.description)
            for c in sorted(group, key=lambda c: (c.title.lower(), c.id))
        )

    if artifacts:
        # A superset of the reference producer, which indexes only `.md`:
        # bundles carry code, and an unlisted attester is an undiscoverable one.
        blocks.setdefault("Artifact", []).extend(
            _entry(name, name, None)
            for name in sorted(a["path"].rsplit("/", 1)[-1] for a in artifacts)
        )

    body = "\n\n".join(
        f"# {heading}\n\n" + "\n".join(blocks[heading])
        for heading in sorted(blocks)
    ) if blocks else "# Contents\n\n(empty)"

    if directory == "" and okf_version:
        header = dump_yaml({"okf_version": str(okf_version)})
        return f"---\n{header}---\n\n{body}\n"
    return body + "\n"


def log_markdown(entries: list, bundle_name: str,
                 frontmatter: Optional[str] = None) -> str:
    """`log.md` — chronological history, newest date first (SPEC §9).

    Entry text already carries its `**Kind**` prefix, so entries are written
    back as-is rather than re-synthesizing the prefix from the derived `kind`.
    Frontmatter is reproduced only when the source log had some: §9 defines no
    frontmatter for log files, so inventing one would be a producer adding
    structure the format does not ask for.
    """
    by_date: dict[str, list] = {}
    for e in entries:
        if e.date:
            by_date.setdefault(e.date, []).append(e)

    blocks = []
    for date in sorted(by_date, reverse=True):
        lines = [f"- {e.text}" for e in sorted(by_date[date], key=lambda e: e.order)]
        blocks.append(f"## {date}\n\n" + "\n".join(lines))

    body = "\n\n".join(blocks) if blocks else "(no entries)"
    title = f"{bundle_name} history"
    if frontmatter:
        declared = yaml.safe_load(frontmatter)
        if isinstance(declared, dict) and declared.get("title"):
            title = str(declared["title"])       # keep the log's own name
        return f"---\n{frontmatter}\n---\n\n# {title}\n\n{body}\n"
    return f"# {title}\n\n{body}\n"


# --------------------------------------------------------------------------
# Whole bundle
# --------------------------------------------------------------------------

def render_bundle(pb: ParsedBundle, *, write_index: bool = True,
                  write_log: bool = True) -> dict[str, str]:
    """Render a whole bundle to `{bundle-relative path: file text}`.

    Nothing touches the filesystem here — the same dict backs a directory
    write, a tarball, a zip, or an in-memory bundle handed straight to an
    agent. Stubs (SPEC §6.1 not-yet-written knowledge) are deliberately *not*
    written: they exist only as link targets, and materializing them would
    invent knowledge that was never authored.
    """
    files: dict[str, str] = {}

    def put(path: str, text: str) -> None:
        """Every write goes through here: no traversal, no silent overwrite."""
        clean = safe_bundle_path(path)
        if clean in files:
            raise EmitCollision(
                f"two bundle entries both want {clean!r} — refusing to overwrite")
        files[clean] = text

    for c in pb.concepts.values():
        put(c.path or f"{c.id}.md", concept_markdown(c))

    for art in pb.artifacts.values():
        if art.get("text") is not None:
            put(art["path"], art["text"])

    if write_log and pb.log_entries:
        # §9 permits log.md at any level, and entry links are resolved relative
        # to their own directory — so a nested log has to go back where it came
        # from or its REFERENCES edges will not survive a re-parse.
        by_dir: dict[str, list] = {}
        for entry in pb.log_entries:
            by_dir.setdefault(getattr(entry, "dir", "") or "", []).append(entry)
        for directory, entries in by_dir.items():
            path = f"{directory}/log.md" if directory else "log.md"
            put(path, log_markdown(entries, pb.name,
                                   pb.log_frontmatter.get(directory)))

    if write_index:
        dirs: dict[str, list[ParsedConcept]] = {}
        for c in pb.concepts.values():
            dirs.setdefault(c.dir, []).append(c)
        art_dirs: dict[str, list[dict]] = {}
        for art in pb.artifacts.values():
            if art.get("text") is None:
                continue
            d = art["path"].rsplit("/", 1)[0] if "/" in art["path"] else ""
            art_dirs.setdefault(d, []).append(art)

        all_dirs = set(dirs) | set(art_dirs) | {""}
        for d in list(all_dirs):                       # ensure intermediate dirs exist
            while "/" in d:
                d = d.rsplit("/", 1)[0]
                all_dirs.add(d)

        for d in sorted(all_dirs):
            children = sorted({
                other[len(d) + 1:].split("/", 1)[0] if d else other.split("/", 1)[0]
                for other in all_dirs
                if other != d and (other.startswith(d + "/") if d else other)
            })
            child_prefix = f"{d}/" if d else ""
            described = {
                name: _subtree_summary(pb, child_prefix + name)
                for name in children
            }
            path = f"{d}/index.md" if d else "index.md"
            put(path, index_markdown(
                d, dirs.get(d, []), described, art_dirs.get(d, []),
                okf_version=pb.okf_version,
            ))

    return files


def _subtree_summary(pb: ParsedBundle, directory: str) -> Optional[str]:
    """A one-line blurb for a subdirectory link, derived from what is in it.

    Hand-written directory blurbs cannot survive a graph round trip (index.md
    is never ingested), so we synthesize an honest one from the concept types
    actually present rather than leaving the entry bare.
    """
    prefix = directory + "/"
    inside = [c for c in pb.concepts.values()
              if c.dir == directory or c.dir.startswith(prefix)]
    arts = [a for a in pb.artifacts.values()
            if a.get("text") is not None
            and (a["path"].startswith(prefix) or a["path"].rsplit("/", 1)[0] == directory)]
    if not inside and not arts:
        return None
    bits = []
    if inside:
        types = sorted({c.type for c in inside})
        noun = "concept" if len(inside) == 1 else "concepts"
        bits.append(f"{len(inside)} {noun} ({', '.join(types)})")
    if arts:
        noun = "artifact" if len(arts) == 1 else "artifacts"
        bits.append(f"{len(arts)} {noun}")
    return "; ".join(bits)
