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

RECOVERY_POINT="$(python - "${URL}" "${STATE_DIR}/public_01_dev.sqlite3" "${CONTROLLER_PID}" "${TARGET_MINUTE}" <<'PY'
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import urlopen

url, db_path, controller_pid, target_minute = sys.argv[1:]
deadline = time.monotonic() + 120
database = Path(db_path)
while not database.exists():
    if time.monotonic() >= deadline:
        raise SystemExit("Controller state database was not created")
    time.sleep(0.01)

connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
connection.execute("PRAGMA query_only=ON")
try:
    while time.monotonic() < deadline:
        try:
            proposal = connection.execute(
                "SELECT proposal_id FROM proposals WHERE state='pending' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            experiment_count = connection.execute(
                "SELECT COUNT(*) FROM experiments"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            proposal = None
            experiment_count = 0

        if proposal and experiment_count:
            with urlopen(f"{url}/sim/state", timeout=2) as response:
                state = json.load(response)
            if state["sim_minute"] >= int(target_minute):
                event_cursor = connection.execute(
                    "SELECT chunk FROM cursors WHERE name='events'"
                ).fetchone()
                event_cursor = event_cursor[0] if event_cursor else -1
                os.kill(int(controller_pid), signal.SIGKILL)
                print(
                    f"{proposal[0]}\t{state['sim_minute']}\t{event_cursor}\t{experiment_count}"
                )
                break

        try:
            os.kill(int(controller_pid), 0)
        except ProcessLookupError:
            raise SystemExit("Controller exited before a pending approval recovery point")
        time.sleep(0.005)
    else:
        raise SystemExit("No pending approval with persisted experiment state before timeout")
finally:
    connection.close()
PY
)"
IFS=$'\t' read -r PENDING_PROPOSAL_ID SIM_MINUTE EVENTS_CURSOR_AT_KILL EXPERIMENTS_AT_KILL <<<"${RECOVERY_POINT}"
if wait "${CONTROLLER_PID}"; then
  echo "Controller was expected to be killed" >&2
  exit 1
fi
echo "Killed controller at simulated minute ${SIM_MINUTE} with pending proposal ${PENDING_PROPOSAL_ID}, event cursor ${EVENTS_CURSOR_AT_KILL}, and ${EXPERIMENTS_AT_KILL} persisted experiments; restarting with the same SQLite state."

python -m controller --url "${URL}" --state-dir "${STATE_DIR}" --resume \
  >"${LOG_DIR}/controller-after-restart.log" 2>&1
curl --silent --fail "${URL}/sim/state" | python -c \
  'import json,sys; s=json.load(sys.stdin); print(s); sys.exit(0 if s["finished"] else 1)'
python - "${URL}" "${STATE_DIR}/public_01_dev.sqlite3" "${PENDING_PROPOSAL_ID}" \
  "${EVENTS_CURSOR_AT_KILL}" "${EXPERIMENTS_AT_KILL}" <<'PY'
import json
import sqlite3
import sys
from collections import Counter
from urllib.parse import quote
from urllib.request import urlopen

url, db_path, pending_id, cursor_at_kill, experiments_at_kill = sys.argv[1:]
connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
proposal = connection.execute(
    "SELECT state FROM proposals WHERE proposal_id=?", (pending_id,)
).fetchone()
if proposal is None or proposal["state"] in {
    "submitting", "auto_submitting", "pending", "approved", "applying"
}:
    raise SystemExit(f"Pending proposal {pending_id} was not reconciled after restart")

event_cursor = connection.execute(
    "SELECT chunk FROM cursors WHERE name='events'"
).fetchone()["chunk"]
if event_cursor < int(cursor_at_kill):
    raise SystemExit("Event cursor moved backwards after restart")

experiment_count = connection.execute(
    "SELECT COUNT(*) FROM experiments"
).fetchone()[0]
counter_count = connection.execute(
    "SELECT COUNT(*) FROM experiment_counters"
).fetchone()[0]
if experiment_count < int(experiments_at_kill) or counter_count == 0:
    raise SystemExit("Persisted experiment learning state is missing after restart")

audit_counts = connection.execute(
    "SELECT COUNT(*) AS total, COUNT(DISTINCT event_key) AS unique_keys "
    "FROM audit WHERE event_key IS NOT NULL"
).fetchone()
if audit_counts["total"] != audit_counts["unique_keys"]:
    raise SystemExit("Duplicate idempotent audit events were persisted")
if connection.execute(
    "SELECT COUNT(*) FROM audit WHERE kind='decision_checks'"
).fetchone()[0] == 0:
    raise SystemExit("No workflow decision checks were retained in the audit store")

proposals = connection.execute(
    "SELECT proposal_id, state, workflow_id FROM proposals"
).fetchall()
connection.close()
history_ids = []
with urlopen(f"{url}/workflows", timeout=5) as response:
    workflows = json.load(response)
for workflow in workflows:
    workflow_id = quote(workflow["workflow_id"], safe="")
    with urlopen(f"{url}/workflows/{workflow_id}/config/history", timeout=5) as response:
        history_ids.extend(
            item["proposal_id"] for item in json.load(response) if item["proposal_id"]
        )
history_counts = Counter(history_ids)
if any(count > 1 for count in history_counts.values()):
    raise SystemExit("Simulator config history contains a duplicate proposal application")
for row in proposals:
    applied_count = history_counts[row["proposal_id"]]
    if row["state"] == "applied" and applied_count != 1:
        raise SystemExit(f"Applied proposal {row['proposal_id']} appears {applied_count} times")

print(
    f"Verified pending proposal {pending_id} reconciled as {proposal['state']}; "
    f"{len(history_ids)} unique config applications; {experiment_count} experiments; "
    f"{counter_count} aggregate rows; {audit_counts['total']} idempotent decision checks."
)
PY
echo "Recovery logs and state: ${ROOT}"
