"""okf_graph — Google's Open Knowledge Format (OKF) and Neo4j, both directions.

    OKF bundle  --parse-->  ParsedBundle  --ingest-->  Neo4j
    OpenWiki    --openwiki->  ParsedBundle+groundings  --ingest-->  Neo4j
    documents   --wiki---->  ParsedBundle  --emit---->  OKF bundle
    Neo4j       --project->  ParsedBundle  --emit---->  OKF bundle

`ParsedBundle` is the hub: everything that produces knowledge produces one, and
everything that serializes knowledge consumes one. Bundles authored by the wiki
builder go into the graph through the *same* deterministic parser as Google's
sample bundles, so there is only ever one mapping to keep correct.
"""

from . import openwiki, queries
from .documents import Document, FetchPolicy, crawl, fetch, load_paths
from .embedding import HashEmbedder, get_embedder
from .emit import ROUNDTRIP_NOTES, concept_markdown, render_bundle
from .ingest import FULLTEXT_INDEX, VECTOR_INDEX, GraphWriter, get_driver
from .openwiki import OpenWikiBundle, ingest_openwiki, parse_openwiki
from .parser import ParsedBundle, parse_bundle
from .project import (Projection, ProjectionSpec, project, project_bundle,
                      write_dir, write_tar, write_zip)
from .wiki import WikiBuild, WikiSpec, build_wiki

__all__ = [
    # OKF -> graph
    "parse_bundle", "ParsedBundle", "GraphWriter", "get_driver",
    # graph -> OKF
    "project", "project_bundle", "Projection", "ProjectionSpec",
    "render_bundle", "concept_markdown", "ROUNDTRIP_NOTES",
    "write_dir", "write_tar", "write_zip",
    # documents -> OKF
    "load_paths", "fetch", "crawl", "Document", "FetchPolicy",
    "build_wiki", "WikiSpec", "WikiBuild",
    # retrieval
    "get_embedder", "HashEmbedder", "queries", "VECTOR_INDEX", "FULLTEXT_INDEX",
    "parse_openwiki", "ingest_openwiki", "OpenWikiBundle", "openwiki",
]

__version__ = "0.3.0"
