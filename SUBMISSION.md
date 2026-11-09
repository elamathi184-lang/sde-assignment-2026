# Submission status

## Known gaps

- **#6 — Auditability:** The controller records proposals, approvals, config changes, and aggregate outcomes, but it does not persist every decision or no-action check. See [`store.py`](./autonomy_controller/store.py).
- **#8 — Crash safety verification:** Recovery is implemented and a demo script exists, but a kill-and-restart test hasn’t been run and verified. See [`crash_recovery_demo.sh`](./scripts/crash_recovery_demo.sh).
