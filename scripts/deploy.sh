#!/usr/bin/env bash
# Deploy the committed tree to the VPS and re-verify.
#
# Discovery runs AFTER the file sync: docs/mcp-tools.json is a committed
# artifact, but the connected server is the authority, so a deploy refreshes it
# rather than trusting whatever the repo happened to carry.
set -euo pipefail
HOST="${OG_HOST:-optiongenome}"
DIR="${OG_DIR:-/opt/optiongenome}"

git archive --format=tar HEAD | ssh -o BatchMode=yes "$HOST" "tar -x -C $DIR"
ssh -o BatchMode=yes "$HOST" "cd $DIR && .venv/bin/python -m pytest -q 2>&1 | tail -2"
ssh -o BatchMode=yes "$HOST" "cd $DIR && set -a && . ./.env && set +a && .venv/bin/python scripts/discover_mcp.py 2>/dev/null | tail -3"
ssh -o BatchMode=yes "$HOST" "cd $DIR && .venv/bin/python -m src.main --check 2>/dev/null | tail -3"
