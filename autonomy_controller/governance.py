from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .models import ProposalRecord
from .store import Store

LOG = logging.getLogger("controller")


class ProposalLifecycleMixin:
    store: Store
    client: httpx.Client

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
                self.store.update_proposal(
                    proposal_id, approval_id=approval["approval_id"], state=approval["status"]
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

    def apply_approval(self, row: ProposalRecord, approval: dict[str, Any], now: str) -> None:
        wid = row["workflow_id"]
        current = self.get(f"/workflows/{wid}/config")
        if current["version"] != row["base_version"]:
            self.finish_proposal(row, "stale", now, {"expected_version": row["base_version"], "current_version": current["version"]})
            return
        self.store.update_proposal(row["proposal_id"], state="applying")
        response = self.post(f"/workflows/{wid}/apply", {"approval_id": approval["approval_id"]})
        if response.status_code in (409, 410):
            body = response.json()
            self.finish_proposal(row, "stale" if body.get("error") == "stale_base_version" else "lapsed",
                                 now, {"error": body.get("error"), "detail": body.get("detail")})
            return
        response.raise_for_status()
        result = response.json()
        self.finish_proposal(row, "applied", now, result)

    def submit_saved_approval(self, row: ProposalRecord, payload: dict[str, Any], now: str) -> None:
        payload = payload.get("_request", payload)
        response = self.post("/approvals", payload)
        if response.status_code == 409:
            body = response.json()
            if body.get("error") == "duplicate_proposal_id":
                matches = self.get("/approvals", params={"workflow_id": row["workflow_id"]})
                approval = next((a for a in matches if a["proposal_id"] == row["proposal_id"]), None)
                if approval:
                    self.store.update_proposal(
                        row["proposal_id"],
                        approval_id=approval["approval_id"],
                        state=approval["status"],
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
        self.store.update_proposal(
            row["proposal_id"], approval_id=approval["approval_id"], state=approval["status"]
        )
        self.store.audit(now, row["workflow_id"], "approval_submitted",
                         {"proposal_id": row["proposal_id"], "approval_id": approval["approval_id"], "kind": row["kind"]})
        LOG.info("%s proposal %s -> %s (%s)", row["workflow_id"], row["kind"], approval["status"], approval["approval_id"])

    def submit_saved_auto(self, row: ProposalRecord, payload: dict[str, Any], now: str) -> None:
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

    def finish_proposal(self, row: ProposalRecord, state: str, now: str, detail: dict[str, Any]) -> None:
        experiment_update, active_experiment_only = (
            self.experiment_update_for_proposal(row, now, detail) if state == "applied" else (None, False)
        )
        self.store.finish_proposal(
            row["proposal_id"],
            row["workflow_id"],
            row["kind"],
            state,
            now,
            detail,
            experiment_update=experiment_update,
            active_experiment_only=active_experiment_only,
        )
        LOG.info("%s proposal %s: %s", row["workflow_id"], row["kind"], state)

    def experiment_update_for_proposal(
        self, row: ProposalRecord, now: str, detail: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, bool]:
        payload = json.loads(row["payload"])
        kind = row["kind"]
        if kind == "experiment_explore":
            request_payload = payload.get("_request", payload)
            version = int(detail.get("version", request_payload["base_version"] + 1))
            return ({
                "dimension": payload["dimension"],
                "cohort_id": "default",
                "phase": "exploring",
                "control": payload["control"],
                "challenger": payload["challenger"],
                "config_version": version,
                "winner": None,
                "started_at": now,
                "promoted_at": None,
                "baseline_config": json.dumps(payload["baseline_config"]),
                "baseline_pickup": payload.get("baseline_pickup"),
                "baseline_engagement": payload.get("baseline_engagement"),
                "last_action_at": now,
            }, False)
        elif kind == "experiment_promote":
            request_payload = payload.get("_request", payload)
            version = int(detail.get("version", request_payload["base_version"] + 1))
            return ({
                "phase": "promoted",
                "winner": payload["winner"],
                "config_version": version,
                "promoted_at": now,
                "last_action_at": now,
            }, False)
        elif kind in ("experiment_rollback", "guardrail_rollback"):
            return ({"phase": "rolled_back", "last_action_at": now}, False)
        elif kind in ("caller_id_health", "prompt_incident"):
            return ({"phase": "abandoned", "last_action_at": now}, True)
        return (None, False)

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
        self.store.save_proposal(
            proposal_id,
            wid,
            kind,
            state,
            now,
            version,
            payload,
            {
                "proposal_id": proposal_id,
                "kind": kind,
                "base_version": version,
                "diff": request_payload.get("diff"),
                "evidence": request_payload.get("dry_run"),
            },
        )
