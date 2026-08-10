"""Command-line interface.

    # OKF bundle -> Neo4j (the original direction)
    okf-graph ingest bundles/acme_retail --reset --embed

    # Neo4j -> OKF bundle (the projection)
    okf-graph project out/ --bundle acme_retail
    okf-graph project pack/ --bundle acme_retail --min-trust human-reviewed \
        --exclude-stale --format tar

    # documents -> an LLM-authored OKF bundle -> Neo4j
    okf-graph fetch docs/ --url https://example.com/handbook --crawl 10
    okf-graph wiki bundles/acme_wiki --path corpus/acme_intranet --ingest

    okf-graph stats
    okf-graph reset --name acme_retail
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .documents import FetchPolicy, crawl, load_paths
from .embedding import get_embedder
from .ingest import GraphWriter, get_driver
from .parser import parse_bundle
# `okf_graph.project` re-exports a function of the same name, so the submodule
# members are imported by name rather than via the package attribute.
from .project import (TRUST_RANK, ProjectionSpec, project, write_dir, write_tar,
                      write_zip)
from .wiki import DEFAULT_ONTOLOGY, WikiSpec, build_wiki


def _add_ingest(sub) -> None:
    p = sub.add_parser("ingest", help="parse a bundle and write it to Neo4j")
    p.add_argument("bundle_dir")
    p.add_argument("--name", default=None, help="bundle name (default: dir name)")
    p.add_argument("--embed", action="store_true", help="embed sections + create indexes")
    p.add_argument("--embedding-provider", default=None, choices=["openai", "hash"])
    p.add_argument("--reset", action="store_true", help="delete this bundle first")


def _add_project(sub) -> None:
    p = sub.add_parser(
        "project", help="read a bundle back out of Neo4j as portable OKF")
    p.add_argument("out", help="output directory, or archive path with --format tar/zip")
    p.add_argument("--bundle", required=True,
                   help="bundle name in the graph (a directory path also works)")
    p.add_argument("--name", default=None, help="rename the projected bundle")
    p.add_argument("--concept", action="append", default=[], dest="ids",
                   metavar="ID", help="project only this concept id (repeatable)")
    p.add_argument("--seed", default=None,
                   help="grow a neighbourhood around this concept id")
    p.add_argument("--hops", type=int, default=1, help="neighbourhood radius (0-6)")
    p.add_argument("--tag", action="append", default=[], dest="tags")
    p.add_argument("--type", action="append", default=[], dest="types")
    p.add_argument("--status", action="append", default=[], dest="statuses",
                   help="draft | stable | deprecated (repeatable)")
    p.add_argument("--min-trust", default=None,
                   choices=sorted(TRUST_RANK),
                   help="drop concepts below this SPEC 5.3 tier")
    p.add_argument("--exclude-stale", action="store_true",
                   help="drop concepts where today >= stale_after")
    p.add_argument("--include-referenced", action="store_true",
                   help="add one hop of link targets so links resolve")
    p.add_argument("--no-index", action="store_true", help="do not regenerate index.md")
    p.add_argument("--no-log", action="store_true", help="do not write log.md")
    p.add_argument("--no-artifacts", action="store_true",
                   help="omit code/asset files (leaner context pack)")
    p.add_argument("--format", default="dir", choices=["dir", "tar", "zip"])
    p.add_argument("--dry-run", action="store_true",
                   help="print the manifest and file list, write nothing")


def _add_fetch(sub) -> None:
    p = sub.add_parser("fetch", help="download documents to a local corpus directory")
    p.add_argument("out", help="directory to write the fetched documents into")
    p.add_argument("--url", action="append", default=[], dest="urls", required=True)
    p.add_argument("--crawl", type=int, default=1, metavar="N",
                   help="follow links up to N pages total (default 1: seeds only)")
    p.add_argument("--any-domain", action="store_true",
                   help="follow links off the seed domains")
    p.add_argument("--include", default=None, help="regex a URL must match")
    p.add_argument("--exclude", default=None, help="regex a URL must not match")
    p.add_argument("--ignore-robots", action="store_true")
    p.add_argument("--allow-private-hosts", action="store_true",
                   help="permit loopback/intranet addresses and non-standard ports")


def _add_wiki(sub) -> None:
    p = sub.add_parser(
        "wiki", help="turn documents into an LLM-authored OKF bundle")
    p.add_argument("out", help="directory to write the bundle into")
    p.add_argument("--path", action="append", default=[], dest="paths",
                   help="file or directory of source documents (repeatable)")
    p.add_argument("--url", action="append", default=[], dest="urls",
                   help="fetch this URL as a source document (repeatable)")
    p.add_argument("--crawl", type=int, default=1, metavar="N")
    p.add_argument("--allow-private-hosts", action="store_true")
    p.add_argument("--name", default=None, help="bundle name (default: dir name)")
    p.add_argument("--extractor", default="heuristic",
                   choices=["heuristic", "openai", "anthropic"],
                   help="heuristic needs no API key and no network")
    p.add_argument("--model", default=None)
    p.add_argument("--ontology", default=None,
                   help="comma-separated OKF types the extractor may use")
    p.add_argument("--max-concepts", type=int, default=3, metavar="N",
                   help="concepts per document")
    p.add_argument("--no-mirror-sources", action="store_true",
                   help="do not write references/ mirrors of the source documents")
    p.add_argument("--stale-after", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--ingest", action="store_true", help="also load it into Neo4j")
    p.add_argument("--reset", action="store_true", help="delete this bundle first")
    p.add_argument("--embed", action="store_true")
    p.add_argument("--embedding-provider", default=None, choices=["openai", "hash"])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="okf-graph", description="OKF <-> Neo4j")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add_ingest(sub)
    _add_project(sub)
    _add_fetch(sub)
    _add_wiki(sub)
    sub.add_parser("stats", help="node/relationship counts")
    p_rst = sub.add_parser("reset", help="delete a bundle")
    p_rst.add_argument("--name", default=None, help="bundle to delete")
    p_rst.add_argument("--all", action="store_true",
                       help="delete EVERY node in the database")
    return ap


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def _embed(writer: GraphWriter, args, bundle: str) -> None:
    provider = args.embedding_provider or os.getenv("EMBEDDING_PROVIDER", "openai")
    if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set (checked env and .env). Either add it, "
            "or rehearse offline with: --embedding-provider hash")
    embedder, dims = get_embedder(provider)
    n = writer.embed_sections(embedder, bundle=bundle, dimensions=dims)
    writer.create_retrieval_indexes(dims)
    print(f"embedded {n} sections ({dims} dims); vector + fulltext indexes ready")


def cmd_ingest(writer: GraphWriter, args) -> None:
    pb = parse_bundle(args.bundle_dir, args.name)
    for warning in pb.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.reset:
        writer.reset(pb.name)
    stats = writer.ingest(pb)
    print(f"ingested bundle '{pb.name}' (okf_version={pb.okf_version})")
    print(json.dumps(stats, indent=2))
    if args.embed:
        _embed(writer, args, pb.name)


def cmd_project(writer: GraphWriter, args) -> None:
    # `ingest` takes a directory and `project` takes a bundle name; accept
    # either here so the two commands can be typed the same way.
    bundle = args.bundle
    if Path(bundle).is_dir():
        bundle = Path(bundle).resolve().name

    spec = ProjectionSpec(
        bundle=bundle, name=args.name, ids=args.ids, seed=args.seed,
        hops=args.hops, tags=args.tags, types=args.types, statuses=args.statuses,
        min_trust=args.min_trust, exclude_stale=args.exclude_stale,
        include_referenced=args.include_referenced,
        include_index=not args.no_index, include_logs=not args.no_log,
        include_artifacts=not args.no_artifacts,
    )
    result = project(writer, spec)
    manifest = result.manifest

    print(f"projected '{spec.bundle}' -> '{spec.out_name}': "
          f"{manifest['stats']['selected']} concepts, {manifest['files']} files")
    for entry in manifest["dangling_links"]:
        section = f" (§ {entry['section']})" if entry.get("section") else ""
        print(f"  dangling link: {entry['from']}{section} -> {entry['to']}"
              f"  [{entry['reason']}]")
    for art in manifest["unmaterialized_artifacts"]:
        print(f"  artifact not written: {art['path']} ({art['reason']})")

    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        for path in sorted(result.files):
            print(f"  {path}")
        return

    if args.format == "dir":
        out = write_dir(result.files, args.out)
    elif args.format == "tar":
        out = write_tar(result.files, args.out, arcroot=spec.out_name)
    else:
        out = write_zip(result.files, args.out, arcroot=spec.out_name)
    print(f"wrote {out}")


def cmd_fetch(args) -> None:
    policy = FetchPolicy(obey_robots=not args.ignore_robots,
                         allow_private_hosts=args.allow_private_hosts)
    docs = crawl(args.urls, max_pages=max(args.crawl, len(args.urls)), policy=policy,
                 same_domain=not args.any_domain,
                 include=args.include, exclude=args.exclude)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    index = []
    for doc in docs:
        path = out / f"{doc.slug}.md"
        n = 2
        while path.exists():
            path, n = out / f"{doc.slug}-{n}.md", n + 1
        path.write_text(doc.text, encoding="utf-8")
        index.append({"file": path.name, "url": doc.location, "title": doc.title,
                      "author": doc.author, "last_modified": doc.last_modified,
                      "retrieved_at": doc.retrieved_at, "sha256": doc.sha256})
        print(f"  {path.name}  <- {doc.location}")
    (out / "fetch-index.json").write_text(
        json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"fetched {len(docs)} documents into {out}")


def cmd_wiki(writer_factory, args) -> None:
    if not args.paths and not args.urls:
        raise SystemExit("give at least one --path or --url")

    docs = load_paths(args.paths) if args.paths else []
    if args.urls:
        policy = FetchPolicy(allow_private_hosts=args.allow_private_hosts)
        docs += crawl(args.urls, max_pages=max(args.crawl, len(args.urls)),
                      policy=policy)
    if not docs:
        raise SystemExit("no documents were loaded")

    name = args.name or Path(args.out).resolve().name
    spec = WikiSpec(
        name=name, extractor=args.extractor, model=args.model,
        ontology=([t.strip() for t in args.ontology.split(",") if t.strip()]
                  if args.ontology else list(DEFAULT_ONTOLOGY)),
        max_concepts_per_doc=args.max_concepts,
        mirror_sources=not args.no_mirror_sources,
        stale_after=args.stale_after,
    )
    build = build_wiki(docs, spec)
    out = write_dir(build.files, args.out)
    print(f"authored bundle '{name}' at {out}")
    print(json.dumps(build.stats, indent=2))

    pb = parse_bundle(out, name)
    for warning in pb.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"re-parsed: {json.dumps(pb.stats())}")
    if pb.stubs:
        print("authoring backlog (links to knowledge nobody has written yet):")
        for stub in sorted(pb.stubs.values(), key=lambda s: s.id):
            print(f"  {stub.id}")

    if args.ingest:
        writer = writer_factory()
        if args.reset:
            writer.reset(name)
        print(json.dumps(writer.ingest(pb), indent=2))
        if args.embed:
            _embed(writer, args, name)


def cmd_stats(writer: GraphWriter) -> None:
    for r in writer.run(
        """MATCH (n) WITH labels(n) AS ls, count(*) AS c
           UNWIND ls AS l RETURN l AS label, sum(c) AS nodes ORDER BY nodes DESC"""
    ).records:
        print(f"{r['label']:>22}  {r['nodes']}")
    for r in writer.run(
        "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC"
    ).records:
        print(f"{r['rel']:>22}  {r['n']}")


def cmd_reset(writer: GraphWriter, args) -> None:
    if not args.name and not args.all:
        raise SystemExit(
            "refusing to wipe the database: pass --name <bundle> to delete one "
            "bundle, or --all to delete everything")
    if args.name and args.all:
        raise SystemExit("--name and --all are mutually exclusive")
    writer.reset(args.name)
    print(f"deleted {'bundle ' + args.name if args.name else 'ALL data'}")


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)

    # fetch never touches Neo4j, and `wiki` only does with --ingest: neither
    # should fail on a machine with no database running.
    if args.cmd == "fetch":
        cmd_fetch(args)
        return

    driver = None

    def writer_factory() -> GraphWriter:
        nonlocal driver
        driver = driver or get_driver()
        return GraphWriter(driver)

    try:
        if args.cmd == "wiki":
            cmd_wiki(writer_factory, args)
        elif args.cmd == "ingest":
            cmd_ingest(writer_factory(), args)
        elif args.cmd == "project":
            cmd_project(writer_factory(), args)
        elif args.cmd == "stats":
            cmd_stats(writer_factory())
        elif args.cmd == "reset":
            cmd_reset(writer_factory(), args)
    finally:
        if driver is not None:
            driver.close()


if __name__ == "__main__":
    main()
