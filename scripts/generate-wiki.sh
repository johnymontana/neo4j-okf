#!/usr/bin/env bash
# Generate (or refresh) an OpenWiki wiki for any repo, ready for `okf-graph ingest-wiki`.
#
#   ./scripts/generate-wiki.sh /path/to/repo [bundle-name]
#
# Needs the OpenWiki CLI (npm install -g openwiki) and a configured model
# provider — the first run walks you through picking one, or pre-seed
# ~/.openwiki/.env (e.g. OPENWIKI_PROVIDER + its API key). See
# https://github.com/langchain-ai/openwiki#model-providers
set -euo pipefail

REPO="${1:?usage: generate-wiki.sh /path/to/repo [bundle-name]}"
NAME="${2:-$(basename "$(cd "$REPO" && pwd)")}"

command -v openwiki >/dev/null 2>&1 || {
  echo "openwiki CLI not found — install it with: npm install -g openwiki" >&2
  exit 1
}

if [ -d "$REPO/openwiki" ]; then
  echo "→ existing wiki found; running openwiki --update"
  (cd "$REPO" && openwiki --update --print)
else
  echo "→ no wiki yet; running openwiki --init"
  (cd "$REPO" && openwiki --init --print)
fi

cat <<EOF

Wiki ready at $REPO/openwiki. Ingest it:

  uv run okf-graph ingest-wiki "$REPO/openwiki" --name "$NAME" --reset --embed
  uv run okf-graph impact --bundle "$NAME" --repo "$REPO"

Serve it to coding agents:

  OPENWIKI_BUNDLE="$NAME" OPENWIKI_REPO_ROOT="$REPO" uv run okf-graph-mcp
EOF
