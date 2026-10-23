#!/usr/bin/env bash
set -euo pipefail
source .venv/bin/activate
eval "$(python -c 'import shlex; from bilibili_live_monitor.config import load_settings; from urllib.parse import urlparse; s=load_settings();print("CDP_PORT="+str(urlparse(s.cdp_url).port or 9222));print("PROFILE="+shlex.quote(str(s.data_dir / "chrome_profile")))')"
mkdir -p "$PROFILE"
if [[ "$(uname)" == "Darwin" ]]; then
  open -na "Google Chrome" --args --remote-debugging-port="$CDP_PORT" --user-data-dir="$PROFILE" --no-first-run --disable-background-timer-throttling --disable-renderer-backgrounding --autoplay-policy=no-user-gesture-required about:blank
else
  exec "${CHROME_BIN:-google-chrome}" --remote-debugging-port="$CDP_PORT" --user-data-dir="$PROFILE" --no-first-run --disable-background-timer-throttling --disable-renderer-backgrounding --autoplay-policy=no-user-gesture-required about:blank
fi
