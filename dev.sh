#!/usr/bin/env bash
# dev.sh — start ppt-master-agent in dev mode with live UI reload.
#
#   * bind-mounts ./ui into the container so edits to ui/index.html
#     are picked up on the next browser refresh
#   * runs uvicorn with --reload so app.py changes also auto-reload
#   * listens on 8080, same as the production compose
#
# Usage:
#   cp agent.env.example agent.env   # first time only
#   ./dev.sh
#
# Stop with Ctrl-C, then `docker compose -f docker-compose.dev.yml down`
# if the container was left running in the background.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f agent.env ]]; then
  echo "agent.env not found; copying from agent.env.example."
  cp agent.env.example agent.env
  echo "  -> fill in real DEEPSEEK_API_KEY and image backend keys, then re-run."
  exit 1
fi

exec docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  up --build
