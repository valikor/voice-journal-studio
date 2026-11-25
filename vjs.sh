#!/bin/bash
# Voice Journal Studio launcher:  ./vjs.sh serve | run | status | publish | usage | retry | reguess
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")" && exec ./.venv/bin/python -u -m vjs.cli "$@"
