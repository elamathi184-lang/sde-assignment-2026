#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> runs until the scenario finishes.
# Runs directly with Python; Docker is not required.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
exec python -m controller --url "$SIM_URL"
