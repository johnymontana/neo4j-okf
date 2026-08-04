"""Command-line interface.

    python -m okf_graph ingest bundles/acme_retail [--name acme_retail] [--embed] [--reset]
    python -m okf_graph stats
    python -m okf_graph reset [--name acme_retail]
"""

from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from .embedding import get_embedder
from .ingest import GraphWriter, get_driver
from .parser import parse_bundle


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="okf_graph", description="OKF bundle -> Neo4j")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="parse a bundle and write it to Neo4j")
    p_ing.add_argument("bundle_dir")
    p_ing.add_argument("--name", default=None, help="bundle name (default: dir name)")
    p_ing.add_argument("--embed", action="store_true", help="embed sections + create indexes")
    p_ing.add_argument("--embedding-provider", default=None, choices=["openai", "hash"])
    p_ing.add_argument("--reset", action="store_true", help="delete this bundle first")

    sub.add_parser("stats", help="node/relationship counts")

    p_rst = sub.add_parser("reset", help="delete a bundle (or everything)")
    p_rst.add_argument("--name", default=None)

    args = ap.parse_args(argv)
    driver = get_driver()
    writer = GraphWriter(driver)

    try:
        if args.cmd == "ingest":
            pb = parse_bundle(args.bundle_dir, args.name)
            if args.reset:
                writer.reset(pb.name)
            stats = writer.ingest(pb)
            print(f"ingested bundle '{pb.name}' (okf_version={pb.okf_version})")
            print(json.dumps(stats, indent=2))
            if args.embed:
                import os
                provider = (args.embedding_provider
                            or os.getenv("EMBEDDING_PROVIDER", "openai"))
                if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
                    raise SystemExit(
                        "OPENAI_API_KEY is not set (checked env and .env). Either add it, "
                        "or rehearse offline with: --embedding-provider hash")
                embedder, dims = get_embedder(provider)
                n = writer.embed_sections(embedder, bundle=pb.name, dimensions=dims)
                writer.create_retrieval_indexes(dims)
                print(f"embedded {n} sections ({dims} dims); vector + fulltext indexes ready")
        elif args.cmd == "stats":
            rec = writer.run(
                """MATCH (n) WITH labels(n) AS ls, count(*) AS c
                   UNWIND ls AS l RETURN l AS label, sum(c) AS nodes ORDER BY nodes DESC"""
            ).records
            for r in rec:
                print(f"{r['label']:>22}  {r['nodes']}")
            rels = writer.run(
                """MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC"""
            ).records
            for r in rels:
                print(f"{r['rel']:>22}  {r['n']}")
        elif args.cmd == "reset":
            writer.reset(args.name)
            print(f"deleted {'bundle ' + args.name if args.name else 'ALL data'}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
