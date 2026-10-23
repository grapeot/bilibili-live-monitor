#!/usr/bin/env bash
set -euo pipefail
source .venv/bin/activate
exec python -m bilibili_live_monitor serve
