"""okf_graph — ingest Google's Open Knowledge Format (OKF) bundles into Neo4j
and query them with GraphRAG (neo4j-graphrag).
"""

from .embedding import HashEmbedder, get_embedder
from .ingest import FULLTEXT_INDEX, VECTOR_INDEX, GraphWriter, get_driver
from .parser import ParsedBundle, parse_bundle
from . import queries

__all__ = [
    "parse_bundle", "ParsedBundle", "GraphWriter", "get_driver",
    "get_embedder", "HashEmbedder", "queries", "VECTOR_INDEX", "FULLTEXT_INDEX",
]

__version__ = "0.2.0"
