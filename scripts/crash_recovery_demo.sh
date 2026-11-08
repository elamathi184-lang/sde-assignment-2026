#!/usr/bin/env bash
set -euo pipefail

SCENARIO="${1:-scenarios/public_01_dev.yaml}"
PORT="${2:-8710}"
STAMP="$(date +%Y%m%d%H%M%S)"
ROOT="runs/crash-recovery-${STAMP}"
SIM_DIR="${ROOT}/sim"
STATE_DIR="${ROOT}/state"
LOG_DIR="${ROOT}/logs"
URL="http://127.0.0.1:${PORT}"

mkdir -p "${LOG_DIR}"
python -m sim serve --scenario "${SCENARIO}" --run-dir "${SIM_DIR}" --port "${PORT}" \
  >"${LOG_DIR}/simulator.log" 2>&1 &
SIM_PID=$!

cleanup() {
  if kill -0 "${SIM_PID}" 2>/dev/null; then
    kill "${SIM_PID}"
    wait "${SIM_PID}" || true
  fi
}
trap cleanup EXIT

READY=0
for _ in $(seq 1 120); do
  if curl --silent --fail "${URL}/health" >/dev/null; then
    READY=1
    break
  fi
  sleep 0.5
done
if [[ "${READY}" != 1 ]]; then
  echo "Simulator did not become ready; see ${LOG_DIR}/simulator.log" >&2
  exit 1
fi

python -m controller --url "${URL}" --state-dir "${STATE_DIR}" \
  >"${LOG_DIR}/controller-before-kill.log" 2>&1 &
CONTROLLER_PID=$!

TARGET_MINUTE=0
while [[ "${TARGET_MINUTE}" == 0 ]]; do
  SIM_STATE="$(curl --silent --fail "${URL}/sim/state")"
  TARGET_MINUTE="$(printf '%s' "${SIM_STATE}" | python -c \
    'import json,sys; s=json.load(sys.stdin); print(min(1440, s["end_minute"] // 2))')"
  if [[ "${SIM_STATE}" == *'"finished":true'* ]]; then
    echo "Scenario finished before the recovery point" >&2
    exit 1
  fi
  sleep 0.05
done

while true; do
  SIM_STATE="$(curl --silent --fail "${URL}/sim/state")"
  SIM_MINUTE="$(printf '%s' "${SIM_STATE}" | python -c 'import json,sys; print(json.load(sys.stdin)["sim_minute"])')"
  if (( SIM_MINUTE >= TARGET_MINUTE )); then
    break
  fi
  if ! kill -0 "${CONTROLLER_PID}" 2>/dev/null; then
    wait "${CONTROLLER_PID}"
    echo "Controller exited before the recovery point" >&2
    exit 1
  fi
  sleep 0.05
done

kill -KILL "${CONTROLLER_PID}"
if wait "${CONTROLLER_PID}"; then
  echo "Controller was expected to be killed" >&2
  exit 1
fi
echo "Killed controller at simulated minute ${SIM_MINUTE}; restarting with the same SQLite state."

python -m controller --url "${URL}" --state-dir "${STATE_DIR}" \
  >"${LOG_DIR}/controller-after-restart.log" 2>&1
curl --silent --fail "${URL}/sim/state" | python -c \
  'import json,sys; s=json.load(sys.stdin); print(s); sys.exit(0 if s["finished"] else 1)'
echo "Recovery logs and state: ${ROOT}"
