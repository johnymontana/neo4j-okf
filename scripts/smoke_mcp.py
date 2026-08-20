"""Smoke-test the openwiki-graph MCP server end to end over stdio.

    uv run python scripts/smoke_mcp.py

Spawns `okf-graph-mcp`, lists tools, and exercises every one against the
ingested bundle. Exits non-zero on any failure.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "okf_graph.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools:", ", ".join(names))
            expected = {"wiki_list_bundles", "wiki_search", "wiki_get_page",
                        "wiki_change_surface", "wiki_stale_pages", "wiki_coverage_gaps"}
            missing = expected - set(names)
            assert not missing, f"missing tools: {missing}"

            async def call(name: str, args: dict) -> str:
                res = await session.call_tool(name, args)
                text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
                status = "ERROR" if text.startswith("Error:") else "ok"
                print(f"\n=== {name} [{status}] ===\n{text[:600]}")
                assert not res.is_error, f"{name} transport error"
                return text

            t = await call("wiki_list_bundles", {})
            assert "openwiki_self" in t

            t = await call("wiki_search", {"params": {
                "query": "update no-op gitHead", "bundle": "openwiki_self", "limit": 3}})
            assert "grounded in:" in t

            t = await call("wiki_get_page", {"params": {
                "page_id": "agent/workflow", "bundle": "openwiki_self"}})
            assert "linked from:" in t and "grounded in:" in t

            t = await call("wiki_get_page", {"params": {
                "page_id": "agent/nope", "bundle": "openwiki_self"}})
            assert t.startswith("Error:") and "similar ids" in t   # actionable miss

            t = await call("wiki_change_surface", {"params": {
                "paths": ["src/agent/index.ts", "src/telemetry/senders.ts"],
                "bundle": "openwiki_self"}})
            assert "affected page(s)" in t and "agent/workflow" in t

            t = await call("wiki_stale_pages", {"params": {
                "bundle": "openwiki_self",
                "changed_paths": ["src/agent/utils.ts"]}})
            assert "re-verify" in t

            t = await call("wiki_coverage_gaps", {"params": {
                "bundle": "openwiki_self", "path_prefix": "src/"}})
            assert "undocumented" in t

    print("\nall MCP smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
