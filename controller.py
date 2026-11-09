from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import os
import resource
import sqlite3
import statistics
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import httpx


LOG = logging.getLogger("controller")
MIN_VARIANT_DIALS = 60
MIN_VARIANT_CONNECTIONS = 30
MIN_INCIDENT_CALLS = 40
Z_95 = 1.96
LOCAL_PROPOSAL_CAP = 2
EARLIEST_APPROVAL_HOUR = 11
ACTIVE_PROPOSAL_STATES = ("submitting", "pending", "approved", "applying", "auto_submitting")
FAMILYWISE_ERROR_RATE = 0.05


def alpha_spending_z(look: int) -> float:
    if look < 1:
        raise ValueError("look must be positive")
    look_alpha = FAMILYWISE_ERROR_RATE / (look * (look + 1))
    return statistics.NormalDist().inv_cdf(1 - look_alpha / 4)


def wilson_interval(successes: int, total: int, z: float = Z_95) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 1.0)
    p = successes / total
    z2 = z * z
    denom = 1 + z2 / total
    center = (p + z2 / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def sim_hour(value: str) -> str:
    return value[:13]


class Store:
    def __init__(self, path: Path, resume: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        had_state = path.exists() and path.stat().st_size > 0
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cursors (
                name TEXT PRIMARY KEY,
                chunk INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lead_records (
                lead_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                features TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS call_records (
                event_id INTEGER PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                lead_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                config_version INTEGER NOT NULL,
                cohort_id TEXT NOT NULL,
                variant TEXT NOT NULL,
                connected INTEGER NOT NULL,
                engaged INTEGER NOT NULL,
                qa TEXT
            );
            CREATE INDEX IF NOT EXISTS calls_workflow_ts ON call_records(workflow_id, ts);
            CREATE TABLE IF NOT EXISTS hourly_stats (
                workflow_id TEXT NOT NULL,
                cohort_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                variant TEXT NOT NULL,
                hour TEXT NOT NULL,
                dials INTEGER NOT NULL DEFAULT 0,
                connected INTEGER NOT NULL DEFAULT 0,
                engaged INTEGER NOT NULL DEFAULT 0,
                qa_calls INTEGER NOT NULL DEFAULT 0,
                hallucination_flags INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(workflow_id, cohort_id, dimension, variant, hour)
            );
            CREATE INDEX IF NOT EXISTS stats_lookup
                ON hourly_stats(workflow_id, dimension, cohort_id, hour);
            CREATE TABLE IF NOT EXISTS experiment_counters (
                workflow_id TEXT NOT NULL,
                config_version INTEGER NOT NULL,
                cohort_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                variant TEXT NOT NULL,
                ts TEXT NOT NULL,
                dials INTEGER NOT NULL DEFAULT 0,
                connected INTEGER NOT NULL DEFAULT 0,
                engaged INTEGER NOT NULL DEFAULT 0,
                qa_calls INTEGER NOT NULL DEFAULT 0,
                hallucination_flags INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(workflow_id, config_version, dimension, cohort_id, ts, variant)
            );
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                base_version INTEGER NOT NULL,
                approval_id TEXT,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS proposals_workflow_state
                ON proposals(workflow_id, state, created_at);
            CREATE TABLE IF NOT EXISTS experiments (
                workflow_id TEXT PRIMARY KEY,
                dimension TEXT NOT NULL,
                cohort_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                control TEXT NOT NULL,
                challenger TEXT NOT NULL DEFAULT '',
                config_version INTEGER NOT NULL DEFAULT 0,
                winner TEXT,
                started_at TEXT NOT NULL,
                promoted_at TEXT,
                baseline_config TEXT NOT NULL,
                baseline_pickup REAL,
                baseline_engagement REAL,
                last_action_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                workflow_id TEXT,
                kind TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(experiments)")}
        if "challenger" not in columns:
            self.db.execute("ALTER TABLE experiments ADD COLUMN challenger TEXT NOT NULL DEFAULT ''")
        if "config_version" not in columns:
            self.db.execute("ALTER TABLE experiments ADD COLUMN config_version INTEGER NOT NULL DEFAULT 0")
        has_cursors = self.db.execute("SELECT 1 FROM cursors LIMIT 1").fetchone() is not None
        if had_state and has_cursors and not resume:
            self.db.close()
            raise RuntimeError(
                f"controller state already exists at {path}; pass --resume only when reconnecting "
                "to the same simulator run"
            )
        if resume and not has_cursors:
            self.db.close()
            raise RuntimeError(f"cannot resume {path}: no persisted simulator cursor was found")
        with self.db:
            self.db.execute("DROP INDEX IF EXISTS calls_experiment_lookup")
            row = self.db.execute(
                "SELECT 1 FROM metadata WHERE key='experiment_counters_backfilled'"
            ).fetchone()
            if row is None:
                self.db.execute(
                    """WITH dimensions(name) AS (
                           SELECT 'voice' UNION ALL
                           SELECT 'welcome_message' UNION ALL
                           SELECT 'caller_id' UNION ALL
                           SELECT 'prompt_version'
                       ), expanded AS (
                           SELECT c.workflow_id, c.config_version, c.cohort_id, d.name AS dimension,
                                  json_extract(c.variant, '$.' || d.name) AS variant, c.ts,
                                  c.connected, c.engaged,
                                  CASE WHEN c.connected
                                            AND json_type(c.qa, '$.hallucination_flag') IS NOT NULL
                                       THEN 1 ELSE 0 END AS qa_calls,
                                  CASE WHEN c.connected
                                            AND json_extract(c.qa, '$.hallucination_flag') = 1
                                       THEN 1 ELSE 0 END AS hallucination_flags
                           FROM call_records c
                           CROSS JOIN dimensions d
                           WHERE json_type(c.variant, '$.' || d.name) IS NOT NULL
                       )
                       INSERT INTO experiment_counters
                           (workflow_id, config_version, cohort_id, dimension, variant, ts, dials,
                            connected, engaged, qa_calls, hallucination_flags)
                       SELECT workflow_id, config_version, cohort_id, dimension, variant, ts,
                              COUNT(*), SUM(connected), SUM(engaged), SUM(qa_calls),
                              SUM(hallucination_flags)
                       FROM expanded
                       GROUP BY workflow_id, config_version, cohort_id, dimension, variant, ts"""
                )
                self.db.execute(
                    "INSERT INTO metadata(key, value) VALUES ('experiment_counters_backfilled', '1')"
                )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def cursor(self, name: str) -> int:
        row = self.db.execute("SELECT chunk FROM cursors WHERE name=?", (name,)).fetchone()
        return row["chunk"] if row else -1

    def bind_run(self, signature: dict[str, Any]) -> None:
        encoded = json.dumps(signature, sort_keys=True, separators=(",", ":"))
        with self.db:
            row = self.db.execute("SELECT value FROM metadata WHERE key='run_signature'").fetchone()
            if row is not None and row["value"] != encoded:
                raise RuntimeError(
                    "saved controller state does not match the live simulator metadata; "
                    "use a fresh state directory for a new run"
                )
            if row is None:
                if self.cursor("history") >= 0 or self.cursor("events") >= 0:
                    raise RuntimeError(
                        "saved controller state has cursors but no run identity; refusing unsafe recovery"
                    )
                self.db.execute(
                    "INSERT INTO metadata(key, value) VALUES ('run_signature', ?)", (encoded,)
                )

    def experiment_stats(
        self,
        workflow_id: str,
        dimension: str,
        since: str,
        config_version: int,
        cohort_id: str | None = None,
        until: str | None = None,
    ) -> dict[str, dict[str, int]]:
        sql = """SELECT variant, SUM(dials) AS dials, SUM(connected) AS connected,
                        SUM(engaged) AS engaged, SUM(qa_calls) AS qa_calls,
                        SUM(hallucination_flags) AS hallucination_flags
                 FROM experiment_counters
                 WHERE workflow_id=? AND config_version=? AND dimension=? AND ts>=?"""
        args: list[Any] = [workflow_id, config_version, dimension, since]
        if cohort_id is not None:
            sql += " AND cohort_id=?"
            args.append(cohort_id)
        if until is not None:
            sql += " AND ts<?"
            args.append(until)
        sql += " GROUP BY variant"
        result = {
            row["variant"]: {
                name: int(row[name] or 0)
                for name in ("dials", "connected", "engaged", "qa_calls", "hallucination_flags")
            }
            for row in self.db.execute(sql, args)
        }

        return result

    def first_observation_time(self, workflow_id: str) -> str | None:
        row = self.db.execute(
            "SELECT MIN(ts) AS first_ts FROM call_records WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        return row["first_ts"] if row and row["first_ts"] else None

    def ingest_chunk(self, kind: str, chunk: int, lines: str | Iterable[str]) -> int:
        if chunk <= self.cursor(kind):
            return 0
        source = lines.splitlines() if isinstance(lines, str) else lines
        inserted = 0
        batch: list[dict[str, Any]] = []
        with self.db:
            for line in source:
                if not line.strip():
                    continue
                batch.append(json.loads(line))
                if len(batch) == 4000:
                    inserted += self._flush_batch(batch)
                    batch.clear()
            if batch:
                inserted += self._flush_batch(batch)
            self.db.execute(
                """INSERT INTO cursors(name, chunk) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET chunk=MAX(chunk, excluded.chunk)""",
                (kind, chunk),
            )
        return inserted

    def _flush_batch(self, events: list[dict[str, Any]]) -> int:
        calls = []
        leads = []
        stats: dict[tuple[str, str, str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
        experiment_stats: dict[tuple[str, int, str, str, str, str], list[int]] = defaultdict(
            lambda: [0, 0, 0, 0, 0]
        )
        for event in events:
            event_type = event["type"]
            if event_type == "call":
                variant = event.get("variant") or {}
                qa = event.get("qa") or {}
                cohort = event.get("cohort_id") or "default"
                connected = int(bool(event.get("connected")))
                engaged = int(bool(event.get("engaged")))
                qa_calls = int(connected and "hallucination_flag" in qa)
                hallucination_flags = int(qa_calls and bool(qa["hallucination_flag"]))
                calls.append(
                    (
                        event["event_id"], event["workflow_id"], event["ts"], event["lead_id"],
                        event["person_id"], event["config_version"], cohort,
                        json.dumps(variant, sort_keys=True), connected, engaged,
                        json.dumps(qa, sort_keys=True) if qa else None,
                    )
                )
                hour = sim_hour(event["ts"])
                for dimension in ("voice", "welcome_message", "caller_id", "prompt_version"):
                    option = variant.get(dimension)
                    if option is None:
                        continue
                    key = (event["workflow_id"], cohort, dimension, str(option), hour)
                    counts = stats[key]
                    counts[0] += 1
                    counts[1] += connected
                    counts[2] += engaged
                    counts[3] += qa_calls
                    counts[4] += hallucination_flags
                    experiment_key = (
                        event["workflow_id"], event["config_version"], cohort, dimension,
                        str(option), event["ts"],
                    )
                    experiment_counts = experiment_stats[experiment_key]
                    experiment_counts[0] += 1
                    experiment_counts[1] += connected
                    experiment_counts[2] += engaged
                    experiment_counts[3] += qa_calls
                    experiment_counts[4] += hallucination_flags
            elif event_type == "lead":
                leads.append(
                    (
                        event["lead_id"], event["person_id"], event["workflow_id"],
                        event["campaign_id"], event["ts"],
                        json.dumps(event.get("features") or {}, sort_keys=True),
                    )
                )
            elif event_type == "config_changed":
                self.audit(
                    event["ts"],
                    event.get("workflow_id"),
                    "config_changed",
                    {"actor": event.get("actor"), "version": event.get("version"),
                     "changed_dims": event.get("changed_dims"), "approval_id": event.get("approval_id")},
                )
                proposal_id = event.get("proposal_id")
                proposal = self.db.execute(
                    "SELECT kind FROM proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone() if proposal_id else None
                changes_experiment = event.get("actor") == "supervisor" or (
                    proposal is not None
                    and proposal["kind"] not in ("experiment_explore", "experiment_promote")
                )
                if changes_experiment:
                    self.db.execute(
                        """UPDATE experiments SET phase='abandoned', last_action_at=?
                           WHERE workflow_id=? AND phase IN ('exploring', 'promoted')""",
                        (event["ts"], event.get("workflow_id")),
                    )
            elif event_type == "approval_decided":
                self.audit(
                    event["ts"],
                    event.get("workflow_id"),
                    "approval_decided",
                    {"approval_id": event.get("approval_id"), "status": event.get("status"),
                     "reasons": event.get("reasons", [])},
                )

        if calls:
            self.db.executemany(
                """INSERT INTO call_records
                   (event_id, workflow_id, ts, lead_id, person_id, config_version, cohort_id,
                    variant, connected, engaged, qa)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                calls,
            )
        if leads:
            self.db.executemany(
                """INSERT OR REPLACE INTO lead_records
                   (lead_id, person_id, workflow_id, campaign_id, ts, features)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                leads,
            )
        if stats:
            self.db.executemany(
                """INSERT INTO hourly_stats
                   (workflow_id, cohort_id, dimension, variant, hour, dials, connected,
                    engaged, qa_calls, hallucination_flags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workflow_id, cohort_id, dimension, variant, hour)
                   DO UPDATE SET dials=dials+excluded.dials, connected=connected+excluded.connected,
                     engaged=engaged+excluded.engaged, qa_calls=qa_calls+excluded.qa_calls,
                     hallucination_flags=hallucination_flags+excluded.hallucination_flags""",
                [(*key, *counts) for key, counts in stats.items()],
            )
        if experiment_stats:
            self.db.executemany(
                """INSERT INTO experiment_counters
                   (workflow_id, config_version, cohort_id, dimension, variant, ts, dials,
                    connected, engaged, qa_calls, hallucination_flags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workflow_id, config_version, cohort_id, dimension, ts, variant)
                   DO UPDATE SET dials=dials+excluded.dials, connected=connected+excluded.connected,
                     engaged=engaged+excluded.engaged, qa_calls=qa_calls+excluded.qa_calls,
                     hallucination_flags=hallucination_flags+excluded.hallucination_flags""",
                [(*key, *counts) for key, counts in experiment_stats.items()],
            )
        return len(events)

    def audit(self, ts: str, workflow_id: str | None, kind: str, detail: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO audit(ts, workflow_id, kind, detail) VALUES (?, ?, ?, ?)",
            (ts, workflow_id, kind, json.dumps(detail, sort_keys=True)),
        )

    def recent_stats(
        self,
        workflow_id: str,
        dimension: str,
        since: str,
        cohort_id: str | None = None,
        until: str | None = None,
    ) -> dict[str, dict[str, int]]:
        args: list[Any] = [workflow_id, dimension, since[:13]]
        cohort_clause = ""
        if cohort_id is not None:
            cohort_clause = " AND cohort_id=?"
            args.append(cohort_id)
        until_clause = ""
        if until is not None:
            until_clause = " AND hour<?"
            args.append(until[:13])
        rows = self.db.execute(
            f"""SELECT variant, SUM(dials) AS dials, SUM(connected) AS connected,
                       SUM(engaged) AS engaged, SUM(qa_calls) AS qa_calls,
                       SUM(hallucination_flags) AS hallucination_flags
                FROM hourly_stats
                WHERE workflow_id=? AND dimension=? AND hour>=?{cohort_clause}{until_clause}
                GROUP BY variant""",
            args,
        ).fetchall()
        return {
            row["variant"]: {
                name: int(row[name] or 0)
                for name in ("dials", "connected", "engaged", "qa_calls", "hallucination_flags")
            }
            for row in rows
        }

    def open_proposals(self, workflow_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM proposals WHERE state IN (%s)" % ",".join("?" * len(ACTIVE_PROPOSAL_STATES))
        args: list[Any] = list(ACTIVE_PROPOSAL_STATES)
        if workflow_id is not None:
            sql += " AND workflow_id=?"
            args.append(workflow_id)
        sql += " ORDER BY created_at"
        return self.db.execute(sql, args).fetchall()

    def recent_proposals(self, workflow_id: str, since: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM proposals WHERE workflow_id=? AND created_at>=?",
            (workflow_id, since),
        ).fetchone()
        return int(row["n"])

    def experiment(self, workflow_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM experiments WHERE workflow_id=?", (workflow_id,)).fetchone()

    def proposal(self, proposal_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()


class Controller:
    def __init__(self, url: str, store: Store):
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=120)
        self.store = store
        self.workflows: dict[str, dict[str, Any]] = {}
        self.step_minutes = 60

    def close(self) -> None:
        self.client.close()

    def get(self, path: str, **kwargs: Any) -> Any:
        response = self.client.get(path, **kwargs)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self.client.post(path, json=body)

    def sync_chunks(self, kind: str, chunks: list[dict[str, Any]] | None = None) -> None:
        if chunks is None:
            chunks = self.get("/events/chunks", params={"kind": kind, "after": self.store.cursor(kind)})
        for chunk in chunks:
            if chunk["kind"] != kind or chunk["chunk"] <= self.store.cursor(kind):
                continue
            with self.client.stream("GET", f"/events/chunks/{chunk['chunk']}") as response:
                response.raise_for_status()
                inserted = self.store.ingest_chunk(kind, int(chunk["chunk"]), response.iter_lines())
            LOG.debug("ingested %s chunk %s (%s new events)", kind, chunk["chunk"], inserted)

    def run(self) -> dict[str, Any]:
        run_started = time.perf_counter()
        health = self.get("/health")
        if not health.get("ok"):
            raise RuntimeError("simulator health check did not return ok")
        self.workflows = {
            wf["workflow_id"]: wf for wf in self.get("/workflows")
        }
        if not self.workflows:
            raise RuntimeError("simulator returned no workflows")
        initial_state = self.get("/sim/state")
        history_chunks = self.get("/events/chunks", params={"kind": "history", "after": -1})
        self.store.bind_run({
            "scenario": initial_state["scenario"],
            "end_minute": initial_state["end_minute"],
            "tick_minutes": initial_state["tick_minutes"],
            "workflows": {
                workflow_id: {
                    key: value for key, value in workflow.items() if key != "current_version"
                }
                for workflow_id, workflow in self.workflows.items()
            },
            "history_chunks": [
                {key: chunk[key] for key in ("chunk", "first_event_id", "last_event_id", "until_ts")}
                for chunk in history_chunks
            ],
        })
        self.sync_chunks("history")
        self.sync_chunks("events")
        state = self.get("/sim/state")
        tick = int(state["tick_minutes"])
        self.step_minutes = max(tick, (60 // tick) * tick)
        last_logged_day = state["ts"][:10]
        self.reconcile_proposals(state["ts"])
        while not state["finished"]:
            advanced = self.post_advance()
            for chunk in advanced["new_chunks"]:
                if chunk["kind"] == "events":
                    self.sync_chunks("events", [chunk])
            state = self.get("/sim/state")
            state["new_chunks"] = advanced["new_chunks"]
            self.reconcile_proposals(state["ts"])
            self.make_decisions(state)
            if state["ts"][:10] != last_logged_day:
                LOG.info("progress ts=%s events=%s credits=%s", state["ts"], state["last_event_id"],
                         state["credits_remaining"])
                last_logged_day = state["ts"][:10]
        elapsed = time.perf_counter() - run_started
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_rss_mb = peak_rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
        LOG.info(
            "scenario finished: %s wall_seconds=%.2f ingest_events_per_second=%.0f peak_rss_mb=%.1f",
            state,
            elapsed,
            state["last_event_id"] / elapsed if elapsed else 0,
            peak_rss_mb,
        )
        return state

    def post_advance(self) -> dict[str, Any]:
        response = self.post("/sim/advance", {"minutes": self.step_minutes})
        if response.status_code == 409 and response.json().get("error") == "finished":
            return self.get("/sim/state") | {"new_chunks": []}
        response.raise_for_status()
        return response.json()

    def reconcile_proposals(self, now: str) -> None:
        for row in self.store.open_proposals():
            proposal_id = row["proposal_id"]
            payload = json.loads(row["payload"])
            request_payload = payload.get("_request", payload)
            state = row["state"]
            if state == "submitting":
                matches = self.get("/approvals", params={"workflow_id": row["workflow_id"]})
                approval = next((a for a in matches if a["proposal_id"] == proposal_id), None)
                if approval is None:
                    self.submit_saved_approval(row, request_payload, now)
                    continue
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE proposals SET approval_id=?, state=? WHERE proposal_id=?",
                        (approval["approval_id"], approval["status"], proposal_id),
                    )
                row = self.store.proposal(proposal_id)
                state = approval["status"]
            if state == "auto_submitting":
                history = self.get(f"/workflows/{row['workflow_id']}/config/history")
                applied = next((h for h in history if h.get("proposal_id") == proposal_id), None)
                if applied:
                    self.finish_proposal(row, "applied", now, {"version": applied["version"], "actor": applied["actor"]})
                    continue
                self.submit_saved_auto(row, request_payload, now)
                continue
            if state in ("pending", "approved", "applying"):
                approval_id = row["approval_id"]
                if not approval_id:
                    raise RuntimeError(f"proposal {proposal_id} has state {state} without an approval ID")
                approval = self.get(f"/approvals/{approval_id}")
                approval_state = approval["status"]
                if approval_state == "pending":
                    continue
                if approval_state == "approved":
                    if state == "applying":
                        history = self.get(f"/workflows/{row['workflow_id']}/config/history")
                        applied = next((h for h in history if h.get("approval_id") == approval_id), None)
                        if applied:
                            self.finish_proposal(row, "applied", now, {"version": applied["version"], "actor": applied["actor"]})
                            continue
                    self.apply_approval(row, approval, now)
                    continue
                if approval_state in ("applied", "rejected", "expired", "lapsed"):
                    self.finish_proposal(row, approval_state, now, {"approval_id": approval_id, "reasons": approval.get("reasons", [])})

    def apply_approval(self, row: sqlite3.Row, approval: dict[str, Any], now: str) -> None:
        wid = row["workflow_id"]
        current = self.get(f"/workflows/{wid}/config")
        if current["version"] != row["base_version"]:
            self.finish_proposal(row, "stale", now, {"expected_version": row["base_version"], "current_version": current["version"]})
            return
        with self.store.db:
            self.store.db.execute(
                "UPDATE proposals SET state='applying' WHERE proposal_id=?", (row["proposal_id"],)
            )
        response = self.post(f"/workflows/{wid}/apply", {"approval_id": approval["approval_id"]})
        if response.status_code in (409, 410):
            body = response.json()
            self.finish_proposal(row, "stale" if body.get("error") == "stale_base_version" else "lapsed",
                                 now, {"error": body.get("error"), "detail": body.get("detail")})
            return
        response.raise_for_status()
        result = response.json()
        self.finish_proposal(row, "applied", now, result)

    def submit_saved_approval(self, row: sqlite3.Row, payload: dict[str, Any], now: str) -> None:
        payload = payload.get("_request", payload)
        response = self.post("/approvals", payload)
        if response.status_code == 409:
            body = response.json()
            if body.get("error") == "duplicate_proposal_id":
                matches = self.get("/approvals", params={"workflow_id": row["workflow_id"]})
                approval = next((a for a in matches if a["proposal_id"] == row["proposal_id"]), None)
                if approval:
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE proposals SET approval_id=?, state=? WHERE proposal_id=?",
                            (approval["approval_id"], approval["status"], row["proposal_id"]),
                        )
                    return
            self.finish_proposal(row, "stale" if body.get("error") == "stale_base_version" else "failed",
                                 now, body)
            return
        if response.status_code >= 500:
            response.raise_for_status()
        if response.status_code >= 400:
            body = response.json()
            self.finish_proposal(row, "failed", now, body)
            LOG.error("approval proposal failed for %s: %s", row["workflow_id"], body)
            return
        approval = response.json()
        with self.store.db:
            self.store.db.execute(
                "UPDATE proposals SET approval_id=?, state=? WHERE proposal_id=?",
                (approval["approval_id"], approval["status"], row["proposal_id"]),
            )
        self.store.audit(now, row["workflow_id"], "approval_submitted",
                         {"proposal_id": row["proposal_id"], "approval_id": approval["approval_id"], "kind": row["kind"]})
        LOG.info("%s proposal %s -> %s (%s)", row["workflow_id"], row["kind"], approval["status"], approval["approval_id"])

    def submit_saved_auto(self, row: sqlite3.Row, payload: dict[str, Any], now: str) -> None:
        payload = payload.get("_request", payload)
        body = {
            "proposal_id": row["proposal_id"],
            "base_version": row["base_version"],
            "proposed_config": payload["proposed_config"],
            "rationale": payload["rationale"],
        }
        if payload.get("evidence"):
            body["evidence"] = payload["evidence"]
        response = self.client.put(f"/workflows/{row['workflow_id']}/config", json=body)
        if response.status_code == 409 and response.json().get("error") == "duplicate_proposal_id":
            history = self.get(f"/workflows/{row['workflow_id']}/config/history")
            applied = next((h for h in history if h.get("proposal_id") == row["proposal_id"]), None)
            if applied:
                self.finish_proposal(row, "applied", now, {"version": applied["version"], "actor": applied["actor"]})
                return
        if response.status_code in (409, 403):
            detail = response.json()
            self.finish_proposal(row, "stale" if detail.get("error") == "stale_base_version" else "failed",
                                 now, detail)
            LOG.error("auto-apply rejected for %s: %s", row["workflow_id"], detail)
            return
        if response.status_code >= 500:
            response.raise_for_status()
        response.raise_for_status()
        result = response.json()
        self.finish_proposal(row, "applied", now, result)

    def finish_proposal(self, row: sqlite3.Row, state: str, now: str, detail: dict[str, Any]) -> None:
        with self.store.db:
            self.store.db.execute(
                "UPDATE proposals SET state=? WHERE proposal_id=?", (state, row["proposal_id"])
            )
            self.store.audit(
                now, row["workflow_id"], "proposal_" + state,
                {"proposal_id": row["proposal_id"], "kind": row["kind"], "detail": detail},
            )
            if state == "applied":
                self.on_applied(row, now, detail)
        LOG.info("%s proposal %s: %s", row["workflow_id"], row["kind"], state)

    def on_applied(self, row: sqlite3.Row, now: str, detail: dict[str, Any]) -> None:
        payload = json.loads(row["payload"])
        kind = row["kind"]
        wid = row["workflow_id"]
        if kind == "experiment_explore":
            request_payload = payload.get("_request", payload)
            version = int(detail.get("version", request_payload["base_version"] + 1))
            with self.store.db:
                self.store.db.execute(
                    """INSERT INTO experiments
                       (workflow_id, dimension, cohort_id, phase, control, challenger, config_version,
                        winner, started_at, promoted_at, baseline_config, baseline_pickup,
                        baseline_engagement, last_action_at)
                       VALUES (?, ?, 'default', 'exploring', ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?)
                       ON CONFLICT(workflow_id) DO UPDATE SET
                         dimension=excluded.dimension, cohort_id='default', phase='exploring',
                         control=excluded.control, challenger=excluded.challenger,
                         config_version=excluded.config_version, winner=NULL,
                         started_at=excluded.started_at, promoted_at=NULL,
                         baseline_config=excluded.baseline_config,
                         baseline_pickup=excluded.baseline_pickup,
                         baseline_engagement=excluded.baseline_engagement,
                         last_action_at=excluded.last_action_at""",
                    (
                        wid, payload["dimension"], payload["control"], payload["challenger"],
                        version, now, json.dumps(payload["baseline_config"]),
                        payload.get("baseline_pickup"), payload.get("baseline_engagement"), now,
                    ),
                )
        elif kind == "experiment_promote":
            request_payload = payload.get("_request", payload)
            version = int(detail.get("version", request_payload["base_version"] + 1))
            with self.store.db:
                self.store.db.execute(
                    """UPDATE experiments SET phase='promoted', winner=?, config_version=?,
                       promoted_at=?, last_action_at=? WHERE workflow_id=?""",
                    (payload["winner"], version, now, now, wid),
                )
        elif kind in ("experiment_rollback", "guardrail_rollback"):
            with self.store.db:
                self.store.db.execute(
                    "UPDATE experiments SET phase='rolled_back', last_action_at=? WHERE workflow_id=?",
                    (now, wid),
                )
        elif kind in ("caller_id_health", "prompt_incident"):
            with self.store.db:
                self.store.db.execute(
                    """UPDATE experiments SET phase='abandoned', last_action_at=?
                       WHERE workflow_id=? AND phase IN ('exploring', 'promoted')""",
                    (now, wid),
                )

    def make_decisions(self, state: dict[str, Any]) -> None:
        now = state["ts"]
        for wid, workflow in self.workflows.items():
            if self.store.open_proposals(wid):
                continue
            incident = self.caller_id_incident(wid, workflow, now)
            if incident and self.create_change(wid, workflow, incident, now):
                continue
            prompt = self.prompt_incident(wid, workflow, now, state.get("credits_remaining"))
            if prompt and self.create_prompt_change(wid, workflow, prompt, now, state.get("credits_remaining")):
                continue
            self.check_experiment(wid, workflow, now)

    def within_approver_hours(self, workflow: dict[str, Any], now: str) -> bool:
        hour = parse_ts(now).hour
        start, end = workflow["governance"]["approver_hours"]
        return max(start, EARLIEST_APPROVAL_HOUR) <= hour < end

    def can_request(self, workflow_id: str, workflow: dict[str, Any], now: str) -> bool:
        if not self.within_approver_hours(workflow, now):
            return False
        cutoff = (parse_ts(now) - timedelta(hours=24)).isoformat()
        remote = self.get("/approvals", params={"workflow_id": workflow_id})
        remote_count = sum(1 for approval in remote if approval["created_at"] >= cutoff)
        local_count = self.store.recent_proposals(workflow_id, cutoff)
        allowed_requests = min(workflow["governance"]["max_requests_per_24h"], LOCAL_PROPOSAL_CAP)
        return max(remote_count, local_count) < allowed_requests

    def caller_id_incident(self, wid: str, workflow: dict[str, Any], now: str) -> dict[str, Any] | None:
        recent_health_change = self.store.db.execute(
            """SELECT created_at FROM proposals
               WHERE workflow_id=? AND kind='caller_id_health' AND state='applied'
               ORDER BY created_at DESC LIMIT 1""",
            (wid,),
        ).fetchone()
        if recent_health_change and parse_ts(now) - parse_ts(recent_health_change["created_at"]) < timedelta(hours=48):
            return None
        since = (parse_ts(now) - timedelta(hours=48)).isoformat()
        stats = self.store.recent_stats(wid, "caller_id", since)
        if len(stats) < 2:
            return None
        for did, values in sorted(stats.items()):
            if values["dials"] < 100:
                continue
            other_dials = sum(v["dials"] for k, v in stats.items() if k != did)
            other_connected = sum(v["connected"] for k, v in stats.items() if k != did)
            if other_dials < 100:
                continue
            did_rate = values["connected"] / values["dials"]
            other_rate = other_connected / other_dials
            did_upper = wilson_interval(values["connected"], values["dials"])[1]
            other_lower = wilson_interval(other_connected, other_dials)[0]
            if did_rate <= other_rate - 0.10 and did_upper < other_lower:
                current = self.get(f"/workflows/{wid}/config")
                cfg = copy.deepcopy(current["config"])
                changed = False
                for distributions in cfg["allocations"].values():
                    distribution = distributions.get("caller_id")
                    if not distribution or did not in distribution or len(distribution) < 2:
                        continue
                    mass = distribution[did] * 0.5
                    if mass <= 1e-6:
                        continue
                    distribution[did] -= mass
                    others = [name for name in distribution if name != did]
                    total = sum(distribution[name] for name in others)
                    if total <= 0:
                        alternatives = [o for o in workflow["options"]["caller_id"] if o != did]
                        if not alternatives:
                            continue
                        distribution[alternatives[0]] = distribution.get(alternatives[0], 0) + mass
                    else:
                        for name in others:
                            distribution[name] += mass * distribution[name] / total
                    distribution[did] = round(distribution[did], 6)
                    for name in distribution:
                        distribution[name] = round(distribution[name], 6)
                    changed = True
                if changed:
                    return {
                        "kind": "caller_id_health",
                        "config": cfg,
                        "rationale": f"Reduced {did} allocation after {values['dials']} dials showed pickup {did_rate:.1%} versus {other_rate:.1%} for other caller IDs.",
                        "evidence": {"caller_id": did, "dials": values["dials"], "pickup_rate": did_rate,
                                    "other_dials": other_dials, "other_pickup_rate": other_rate},
                        "dimension": "caller_id",
                    }
        return None

    def prompt_incident(self, wid: str, workflow: dict[str, Any], now: str, credits: float | None) -> dict[str, Any] | None:
        cutoff = (parse_ts(now) - timedelta(hours=24)).isoformat()
        attempted = self.store.db.execute(
            """SELECT ts FROM audit WHERE workflow_id=?
               AND kind IN ('prompt_suggestion_attempt', 'prompt_incident_unresolved')
               ORDER BY id DESC LIMIT 1""",
            (wid,),
        ).fetchone()
        if attempted and attempted["ts"] >= cutoff:
            return None
        current = self.get(f"/workflows/{wid}/config")
        prompt = current["config"]["prompt_version"]
        since = (parse_ts(now) - timedelta(hours=48)).isoformat()
        stats = self.store.recent_stats(wid, "prompt_version", since)
        values = stats.get(prompt)
        if not values or values["qa_calls"] < MIN_INCIDENT_CALLS:
            return None
        rate = values["hallucination_flags"] / values["qa_calls"]
        lower = wilson_interval(values["hallucination_flags"], values["qa_calls"])[0]
        if rate < 0.04 or lower < 0.02:
            return None
        if credits is not None and credits < 10:
            LOG.error("%s prompt incident detected but credit reserve prevents LLM/validation calls", wid)
            self.store.audit(now, wid, "prompt_incident_unresolved",
                             {"prompt": prompt, "qa_calls": values["qa_calls"], "flag_rate": rate, "credits": credits})
            return None
        return {"prompt": prompt, "qa_calls": values["qa_calls"], "flag_rate": rate, "lower_bound": lower}

    def create_prompt_change(
        self,
        wid: str,
        workflow: dict[str, Any],
        incident: dict[str, Any],
        now: str,
        credits: float | None,
    ) -> bool:
        if not self.can_request(wid, workflow, now):
            return False
        if credits is not None and credits < 10:
            return False
        self.store.audit(now, wid, "prompt_suggestion_attempt",
                         {"prompt": incident["prompt"], "qa_calls": incident["qa_calls"],
                          "flag_rate": incident["flag_rate"]})
        suggestions_response = self.post(
            "/llm/suggest-fixes",
            {"workflow_id": wid, "problem": f"Population hallucination rate for {incident['prompt']} is elevated."},
        )
        suggestions_response.raise_for_status()
        suggestions = suggestions_response.json().get("suggestions", [])
        valid = []
        minimum = workflow["governance"].get("min_validation_pass_rate")
        for suggestion in suggestions[:2]:
            result = self.post(
                "/validation/runs",
                {"workflow_id": wid, "prompt_version": suggestion["prompt_version"], "n": 100},
            )
            result.raise_for_status()
            validation = result.json()
            if validation["pass_rate"] >= minimum:
                valid.append((validation["pass_rate"], suggestion, validation))
        if not valid:
            self.store.audit(now, wid, "prompt_validation_no_safe_candidate",
                             {"incident": incident, "suggestions_checked": len(suggestions[:2])})
            return False
        _, suggestion, validation = max(valid, key=lambda candidate: candidate[0])
        current = self.get(f"/workflows/{wid}/config")
        cfg = copy.deepcopy(current["config"])
        cfg["prompt_version"] = suggestion["prompt_version"]
        return self.create_change(
            wid, workflow,
            {
                "kind": "prompt_incident",
                "config": cfg,
                "rationale": f"Population QA flags increased to {incident['flag_rate']:.1%} over {incident['qa_calls']} connected calls; validated a safer prompt candidate.",
                "evidence": {
                    "validation_run_id": validation["validation_run_id"],
                    "flag_rate": incident["flag_rate"],
                    "qa_calls": incident["qa_calls"],
                    "validation_pass_rate": validation["pass_rate"],
                },
                "dimension": "prompt_version",
                "incident": incident,
                "summary": f"Replace {incident['prompt']} after elevated hallucination flags",
            },
            now,
        )

    def create_change(self, wid: str, workflow: dict[str, Any], decision: dict[str, Any], now: str) -> bool:
        current = self.get(f"/workflows/{wid}/config")
        cfg = decision["config"]
        if configs_equivalent(cfg, current["config"]):
            return False
        if not self.can_request(wid, workflow, now):
            return False
        body = self.proposal_body(wid, workflow, current, cfg, decision, now)
        kind = decision["kind"]
        policy = workflow["governance"]["auto_apply"]
        if (decision.get("kind") == "caller_id_health"
                and decision.get("dimension") in policy.get("allowed_dims", [])
                and policy.get("enabled")):
            shift = max_distribution_shift(current["config"], cfg)
            if shift <= policy.get("max_shift", 0) + 1e-9:
                body["rationale"] = decision["rationale"]
                metadata = {k: decision[k] for k in ("dimension", "winner", "baseline_config", "baseline_pickup",
                                                       "baseline_engagement") if k in decision}
                metadata["_request"] = body
                self.save_proposal(wid, kind, "auto_submitting", current["version"], metadata, now)
                row = self.store.proposal(body["proposal_id"])
                self.submit_saved_auto(row, metadata, now)
                return True
        if not self.within_approver_hours(workflow, now):
            return False
        metadata = {k: decision[k] for k in ("dimension", "winner", "control", "baseline_config",
                                               "baseline_pickup", "baseline_engagement") if k in decision}
        metadata["_request"] = body
        self.save_proposal(wid, kind, "submitting", current["version"], metadata, now)
        self.submit_saved_approval(self.store.proposal(body["proposal_id"]), metadata, now)
        return True

    def proposal_body(
        self,
        wid: str,
        workflow: dict[str, Any],
        current: dict[str, Any],
        cfg: dict[str, Any],
        decision: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        stats = decision.get("evidence", {})
        batch_size = int(workflow.get("daily_batch", {}).get("size", 0))
        affected = decision.get("affected_leads")
        if affected is None and decision.get("kind") == "caller_id_health":
            did = stats.get("caller_id")
            allocation = current["config"].get("allocations", {}).get("default", {}).get("caller_id", {})
            affected = round(batch_size * allocation.get(did, 0))
        if affected is None:
            affected = batch_size
        estimated_lift = stats.get("estimated_lift")
        if estimated_lift is None and "other_pickup_rate" in stats and "pickup_rate" in stats:
            estimated_lift = (stats["other_pickup_rate"] - stats["pickup_rate"]) * (
                affected / batch_size if batch_size else 0
            )
        if estimated_lift is None and "winner_rate" in stats and "control_rate" in stats:
            estimated_lift = stats["winner_rate"] - stats["control_rate"]
        if estimated_lift is None and "flag_rate" in stats:
            estimated_lift = stats["flag_rate"]
        impact_score = 0.05 if decision.get("kind") == "experiment_explore" else min(
            1.0, max(0.05, abs(float(estimated_lift or 0)) * 5)
        )
        dry_run = {
            "method": "compare recent API-observed population outcomes; no hidden scenario data used",
            "current_version": current["version"],
            "evidence": stats,
            "impact_score_method": "5x estimated absolute KPI-rate impact, capped at 1; exploratory proposals are 0.05",
        }
        return {
            "proposal_id": str(uuid.uuid4()),
            "workflow_id": wid,
            "base_version": current["version"],
            "proposed_config": cfg,
            "change_summary": decision.get("summary", decision["rationale"][:120]),
            "diff": {decision.get("dimension", "config"): decision.get("evidence", {"proposed_config": cfg})},
            "affected_leads": affected,
            "dry_run": dry_run,
            "impact_score": round(impact_score, 3),
            "rationale": decision["rationale"],
            **({"evidence": decision["evidence"]} if decision.get("evidence") and "validation_run_id" in decision["evidence"] else {}),
        }

    def save_proposal(
        self,
        wid: str,
        kind: str,
        state: str,
        version: int,
        payload: dict[str, Any],
        now: str,
    ) -> None:
        request_payload = payload.get("_request", payload)
        proposal_id = request_payload["proposal_id"]
        with self.store.db:
            self.store.db.execute(
                """INSERT INTO proposals
                   (proposal_id, workflow_id, kind, state, created_at, base_version, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (proposal_id, wid, kind, state, now, version,
                 json.dumps(payload, sort_keys=True)),
            )
            self.store.audit(now, wid, "proposal_created",
                             {"proposal_id": proposal_id, "kind": kind,
                              "base_version": version, "diff": request_payload.get("diff"),
                              "evidence": request_payload.get("dry_run")})

    def check_experiment(self, wid: str, workflow: dict[str, Any], now: str) -> None:
        exp = self.store.experiment(wid)
        if exp is None:
            self.start_experiment(wid, workflow, now)
            return
        if exp["phase"] in ("abandoned", "rolled_back"):
            if parse_ts(now) - parse_ts(exp["last_action_at"]) >= timedelta(hours=24):
                self.start_experiment(wid, workflow, now)
            return
        if exp["phase"] not in ("exploring", "promoted"):
            return
        guardrail = self.guardrail_breached(wid, workflow, exp, now)
        if guardrail:
            self.propose_rollback(wid, workflow, exp, now, "guardrail_rollback", guardrail)
            return
        if exp["phase"] == "exploring":
            self.try_promote(wid, workflow, exp, now)
        elif exp["phase"] == "promoted":
            self.try_decay_rollback(wid, workflow, exp, now)

    def start_experiment(self, wid: str, workflow: dict[str, Any], now: str) -> None:
        if not self.within_approver_hours(workflow, now) or not self.can_request(wid, workflow, now):
            return
        objective = workflow.get("objective", {})
        dimension = "voice" if objective.get("primary") == "engagement_rate" else "caller_id"
        options = workflow["options"][dimension]
        if len(options) < 2:
            return
        current = self.get(f"/workflows/{wid}/config")
        distributions = current["config"]["allocations"].get("default", {})
        current_dist = distributions.get(dimension) or {options[0]: 1.0}
        control = max(current_dist, key=current_dist.get)
        if all(math.isclose(current_dist.get(option, 0), 1 / len(options), abs_tol=0.01) for option in options):
            return
        cfg = copy.deepcopy(current["config"])
        challenger = next(option for option in options if option != control)
        cfg["allocations"].setdefault("default", {})[dimension] = {
            control: 0.8,
            challenger: 0.2,
        }
        if configs_equivalent(cfg, current["config"]):
            with self.store.db:
                self.store.db.execute(
                    """INSERT INTO experiments
                       (workflow_id, dimension, cohort_id, phase, control, challenger, config_version,
                        winner, started_at, promoted_at, baseline_config, last_action_at)
                       VALUES (?, ?, 'default', 'exploring', ?, ?, ?, NULL, ?, NULL, ?, ?)
                       ON CONFLICT(workflow_id) DO UPDATE SET
                         dimension=excluded.dimension, cohort_id='default', phase='exploring',
                         control=excluded.control, challenger=excluded.challenger,
                         config_version=excluded.config_version, winner=NULL,
                         started_at=excluded.started_at, promoted_at=NULL,
                         baseline_config=excluded.baseline_config,
                         last_action_at=excluded.last_action_at""",
                    (
                        wid, dimension, control, challenger, current["version"], now,
                        json.dumps(current["config"]), now,
                    ),
                )
                self.store.audit(
                    now, wid, "experiment_started_existing_allocation",
                    {"dimension": dimension, "control": control, "challenger": challenger,
                     "config_version": current["version"]},
                )
            return
        baseline = self.store.recent_stats(
            wid, dimension, (parse_ts(now) - timedelta(hours=48)).isoformat(), "default"
        )
        baseline_rates = calculate_rates(baseline)
        decision = {
            "kind": "experiment_explore",
            "config": cfg,
            "dimension": dimension,
            "control": control,
            "challenger": challenger,
            "rationale": f"Begin a sequential {dimension} comparison with 80% retained on the current control and 20% assigned to one challenger.",
            "summary": f"Start controlled {dimension} exploration",
            "evidence": {"current_distribution": current_dist, "challenger": challenger,
                         "baseline_rates": baseline_rates, "allocation": cfg["allocations"]["default"][dimension]},
            "baseline_config": current["config"],
            "baseline_pickup": baseline_rates.get("pickup_rate"),
            "baseline_engagement": baseline_rates.get("engagement_rate"),
        }
        body = self.proposal_body(wid, workflow, current, cfg, decision, now)
        body["diff"] = {dimension: cfg["allocations"]["default"][dimension]}
        body["impact_score"] = 0.05
        body["rationale"] = decision["rationale"]
        body["dry_run"]["note"] = "Exploration only; no winner is claimed. One challenger is tested at 20% while control retains 80%."
        body["proposal_id"] = str(uuid.uuid4())
        metadata = {
            "dimension": dimension,
            "control": control,
            "challenger": challenger,
            "baseline_config": decision["baseline_config"],
            "baseline_pickup": decision["baseline_pickup"],
            "baseline_engagement": decision["baseline_engagement"],
            "_request": body,
        }
        self.save_proposal(wid, decision["kind"], "submitting", current["version"], metadata, now)
        self.submit_saved_approval(self.store.proposal(body["proposal_id"]), metadata, now)

    def try_promote(self, wid: str, workflow: dict[str, Any], exp: sqlite3.Row, now: str) -> None:
        since = exp["started_at"]
        stats = self.store.experiment_stats(
            wid, exp["dimension"], since, exp["config_version"], exp["cohort_id"]
        )
        control = stats.get(exp["control"])
        if not control:
            return
        primary = workflow["objective"]["primary"]
        control_success, control_n = primary_counts(control, primary)
        min_n = MIN_VARIANT_CONNECTIONS if primary == "engagement_rate" else MIN_VARIANT_DIALS
        if control_n < min_n:
            return
        look_reference = exp["promoted_at"] if exp["phase"] == "promoted" else exp["started_at"]
        look = sequential_look(now, look_reference)
        z = alpha_spending_z(look)
        control_low, control_high = wilson_interval(control_success, control_n, z)
        candidates = []
        option = exp["challenger"]
        values = stats.get(option)
        if values is not None:
            success, n = primary_counts(values, primary)
            min_n = MIN_VARIANT_CONNECTIONS if primary == "engagement_rate" else MIN_VARIANT_DIALS
            if n >= min_n:
                low, high = wilson_interval(success, n, z)
                if low > control_high:
                    candidates.append((low, success / n, option, n, high))
        if not candidates or not self.can_request(wid, workflow, now):
            return
        _, rate, winner, n, high = max(candidates)
        current = self.get(f"/workflows/{wid}/config")
        cfg = copy.deepcopy(current["config"])
        cfg["allocations"]["default"][exp["dimension"]] = {winner: 0.6, exp["control"]: 0.4}
        decision = {
            "kind": "experiment_promote",
            "config": cfg,
            "dimension": exp["dimension"],
            "winner": winner,
            "rationale": (
                f"{winner}'s alpha-spending-adjusted Wilson lower bound exceeds the control's upper "
                f"bound after {n} relevant observations; promoting cautiously while retaining 40% control."
            ),
            "summary": f"Promote {winner} cautiously for {exp['dimension']}",
            "evidence": {"winner": winner, "winner_rate": rate, "winner_n": n,
                         "control": exp["control"], "control_rate": control_success / control_n,
                         "control_n": control_n, "winner_ci_upper": high,
                         "control_ci": [control_low, control_high],
                         "look": look, "look_alpha": FAMILYWISE_ERROR_RATE / (look * (look + 1))},
        }
        self.create_change(wid, workflow, decision, now)

    def guardrail_breached(
        self,
        wid: str,
        workflow: dict[str, Any],
        exp: sqlite3.Row,
        now: str,
    ) -> dict[str, Any] | None:
        since = (parse_ts(now) - timedelta(hours=48)).isoformat()
        primary = workflow["objective"]["primary"]
        guard_name = "pickup_rate" if primary == "engagement_rate" else "engagement_rate"
        stats = self.store.experiment_stats(
            wid, exp["dimension"], since, exp["config_version"], exp["cohort_id"]
        )
        treatment_name = exp["winner"] if exp["phase"] == "promoted" else exp["challenger"]
        treatment = stats.get(treatment_name)
        control = stats.get(exp["control"])
        if treatment is None or control is None:
            return None
        guard = workflow["objective"]["guardrail"]
        threshold = guard.get(f"{guard_name}_max_drop")
        if threshold is None:
            return None
        if guard_name == "pickup_rate":
            treatment_success, treatment_n = treatment["connected"], treatment["dials"]
            control_success, control_n = control["connected"], control["dials"]
            min_sample = MIN_INCIDENT_CALLS
        else:
            treatment_success, treatment_n = treatment["engaged"], treatment["connected"]
            control_success, control_n = control["engaged"], control["connected"]
            min_sample = MIN_VARIANT_CONNECTIONS
        if treatment_n < min_sample or control_n < min_sample:
            return None
        look = sequential_look(now, exp["started_at"])
        z = alpha_spending_z(look)
        treatment_upper = wilson_interval(treatment_success, treatment_n, z)[1]
        control_lower = wilson_interval(control_success, control_n, z)[0]
        treatment_rate = treatment_success / treatment_n
        control_rate = control_success / control_n
        if treatment_upper < control_lower - float(threshold):
            return {
                "guardrail": guard_name,
                "treatment": treatment_name,
                "treatment_rate": treatment_rate,
                "treatment_upper_95": treatment_upper,
                "treatment_n": treatment_n,
                "control": exp["control"],
                "control_rate": control_rate,
                "control_lower_95": control_lower,
                "control_n": control_n,
                "look": look,
                "max_drop": threshold,
            }
        return None

    def try_decay_rollback(self, wid: str, workflow: dict[str, Any], exp: sqlite3.Row, now: str) -> None:
        if not exp["winner"] or not exp["promoted_at"]:
            return
        since = max(exp["promoted_at"], (parse_ts(now) - timedelta(hours=24)).isoformat())
        stats = self.store.experiment_stats(
            wid, exp["dimension"], since, exp["config_version"], exp["cohort_id"]
        )
        winner = stats.get(exp["winner"])
        control = stats.get(exp["control"])
        if not winner or not control:
            return
        primary = workflow["objective"]["primary"]
        w_success, w_n = primary_counts(winner, primary)
        c_success, c_n = primary_counts(control, primary)
        min_n = MIN_VARIANT_CONNECTIONS if primary == "engagement_rate" else MIN_VARIANT_DIALS
        if w_n < min_n or c_n < min_n:
            return
        look = sequential_look(now, exp["promoted_at"])
        z = alpha_spending_z(look)
        _, winner_high = wilson_interval(w_success, w_n, z)
        control_low, _ = wilson_interval(c_success, c_n, z)
        if control_low > winner_high:
            self.propose_rollback(
                wid, workflow, exp, now, "experiment_rollback",
                {"winner": exp["winner"], "winner_rate": w_success / w_n, "winner_n": w_n,
                 "control": exp["control"], "control_rate": c_success / c_n, "control_n": c_n},
            )

    def propose_rollback(
        self,
        wid: str,
        workflow: dict[str, Any],
        exp: sqlite3.Row,
        now: str,
        kind: str,
        evidence: dict[str, Any],
    ) -> None:
        if not self.can_request(wid, workflow, now):
            return
        current = self.get(f"/workflows/{wid}/config")
        cfg = json.loads(exp["baseline_config"])
        decision = {
            "kind": kind,
            "config": cfg,
            "dimension": exp["dimension"],
            "rationale": "Restore the previously active configuration because recent population evidence indicates a guardrail breach or sustained deterioration.",
            "summary": "Restore the pre-experiment configuration",
            "evidence": evidence,
        }
        if not configs_equivalent(cfg, current["config"]):
            self.create_change(wid, workflow, decision, now)


def primary_counts(values: dict[str, int], primary: str) -> tuple[int, int]:
    if primary == "engagement_rate":
        return values["engaged"], values["connected"]
    return values["connected"], values["dials"]


def calculate_rates(stats: dict[str, dict[str, int]]) -> dict[str, float | int]:
    dials = sum(v["dials"] for v in stats.values())
    connected = sum(v["connected"] for v in stats.values())
    engaged = sum(v["engaged"] for v in stats.values())
    result: dict[str, float | int] = {}
    if dials:
        result["pickup_rate"] = connected / dials
        result["pickup_rate_n"] = dials
    if connected:
        result["engagement_rate"] = engaged / connected
        result["engagement_rate_n"] = connected
    return result


def sequential_look(now: str, started_at: str) -> int:
    elapsed_hours = int((parse_ts(now) - parse_ts(started_at)).total_seconds() // 3600)
    return max(1, elapsed_hours + 1)


def configs_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if any(left.get(key) != right.get(key) for key in ("cohorts", "prompt_version", "schedule")):
        return False
    cohort_ids = {"default"}
    cohort_ids.update(cohort["id"] for cohort in left.get("cohorts", []))
    cohort_ids.update(cohort["id"] for cohort in right.get("cohorts", []))
    for cohort_id in cohort_ids:
        for dimension in ("welcome_message", "voice", "caller_id"):
            left_dist = (
                left.get("allocations", {}).get(cohort_id, {}).get(dimension)
                or left.get("allocations", {}).get("default", {}).get(dimension, {})
            )
            right_dist = (
                right.get("allocations", {}).get(cohort_id, {}).get(dimension)
                or right.get("allocations", {}).get("default", {}).get(dimension, {})
            )
            options = set(left_dist) | set(right_dist)
            left_total = sum(left_dist.values())
            right_total = sum(right_dist.values())
            if left_total <= 0 or right_total <= 0:
                return False
            if any(
                not math.isclose(
                    left_dist.get(option, 0) / left_total,
                    right_dist.get(option, 0) / right_total,
                    rel_tol=0,
                    abs_tol=1e-8,
                )
                for option in options
            ):
                return False
    return True


def max_distribution_shift(old: dict[str, Any], new: dict[str, Any]) -> float:
    keys = {"default"}
    keys.update(c["id"] for c in old.get("cohorts", []))
    keys.update(c["id"] for c in new.get("cohorts", []))
    maximum = 0.0
    for key in keys:
        for dimension in ("welcome_message", "voice", "caller_id"):
            old_dist = old.get("allocations", {}).get(key, {}).get(dimension, {})
            new_dist = new.get("allocations", {}).get(key, {}).get(dimension, {})
            if key != "default":
                old_dist = old_dist or old.get("allocations", {}).get("default", {}).get(dimension, {})
                new_dist = new_dist or new.get("allocations", {}).get("default", {}).get(dimension, {})
            options = set(old_dist) | set(new_dist)
            maximum = max(maximum, 0.5 * sum(abs(old_dist.get(o, 0) - new_dist.get(o, 0)) for o in options))
    return maximum


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the outcome-first controller against the simulator.")
    parser.add_argument("--url", default=os.environ.get("SIM_URL", "http://127.0.0.1:8700"))
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("CONTROLLER_STATE_DIR", "runs/controller-state")))
    parser.add_argument(
        "--resume",
        action="store_true",
        default=os.environ.get("CONTROLLER_RESUME", "").lower() in ("1", "true", "yes"),
        help="resume state only when reconnecting to the same simulator run",
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with httpx.Client(base_url=args.url.rstrip("/"), timeout=120) as client:
        response = client.get("/sim/state")
        response.raise_for_status()
        scenario = response.json()["scenario"]
    store = Store(args.state_dir / f"{scenario}.sqlite3", resume=args.resume)
    controller = Controller(args.url, store)
    try:
        controller.run()
    finally:
        controller.close()
        store.close()


if __name__ == "__main__":
    main()
