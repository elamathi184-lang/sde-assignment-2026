from __future__ import annotations

import logging
import resource
import sys
import time
from typing import Any

import httpx

from .models import RunSignature
from .store import Store
from .governance import ProposalLifecycleMixin
from .decisions import DecisionMixin

LOG = logging.getLogger("controller")


class Controller(ProposalLifecycleMixin, DecisionMixin):
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
        self.store.bind_run(RunSignature(
            scenario=initial_state["scenario"],
            end_minute=initial_state["end_minute"],
            tick_minutes=initial_state["tick_minutes"],
            workflows={
                workflow_id: {
                    key: value for key, value in workflow.items() if key != "current_version"
                }
                for workflow_id, workflow in self.workflows.items()
            },
            history_chunks=[
                {key: chunk[key] for key in ("chunk", "first_event_id", "last_event_id", "until_ts")}
                for chunk in history_chunks
            ],
        ).as_dict())
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
