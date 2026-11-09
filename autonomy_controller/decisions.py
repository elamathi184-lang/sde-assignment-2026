from __future__ import annotations

import copy
import json
import logging
import math
import uuid
from datetime import timedelta
from typing import Any

from .constants import (
    EARLIEST_APPROVAL_HOUR,
    FAMILYWISE_ERROR_RATE,
    LOCAL_PROPOSAL_CAP,
    MIN_INCIDENT_CALLS,
    MIN_VARIANT_CONNECTIONS,
    MIN_VARIANT_DIALS,
)
from .models import ExperimentRecord, ProposalRecord
from .statistics import (
    alpha_spending_z,
    calculate_rates,
    configs_equivalent,
    max_distribution_shift,
    parse_ts,
    primary_counts,
    sequential_look,
    wilson_interval,
)
from .store import Store

LOG = logging.getLogger("controller")


class DecisionMixin:
    store: Store
    workflows: dict[str, dict[str, Any]]

    def make_decisions(self, state: dict[str, Any]) -> None:
        now = state["ts"]
        for wid, workflow in self.workflows.items():
            checks: list[dict[str, Any]] = []
            pending = self.store.open_proposals(wid)
            if pending:
                checks.append({
                    "check": "open_proposals",
                    "outcome": "blocked",
                    "proposal_ids": [proposal["proposal_id"] for proposal in pending],
                })
                checks.extend([
                    {
                        "check": "caller_id_health",
                        "outcome": "skipped",
                        "reason": "an approval is already open for this workflow",
                    },
                    {
                        "check": "prompt_incident",
                        "outcome": "skipped",
                        "reason": "an approval is already open for this workflow",
                    },
                    {
                        "check": "experiment",
                        "outcome": "skipped",
                        "reason": "an approval is already open for this workflow",
                    },
                ])
                self._audit_decision_checks(wid, now, checks)
                continue

            incident = self.caller_id_incident(wid, workflow, now)
            checks.append({
                "check": "caller_id_health",
                "outcome": "candidate" if incident else "no_action",
                **({"evidence": incident["evidence"]} if incident else {}),
            })
            if incident:
                created = self.create_change(wid, workflow, incident, now)
                checks[-1]["proposal_created"] = created
                if created:
                    checks.extend([
                        {"check": "prompt_incident", "outcome": "skipped", "reason": "caller ID proposal created"},
                        {"check": "experiment", "outcome": "skipped", "reason": "caller ID proposal created"},
                    ])
                    self._audit_decision_checks(wid, now, checks)
                    continue

            prompt = self.prompt_incident(wid, workflow, now, state.get("credits_remaining"))
            checks.append({
                "check": "prompt_incident",
                "outcome": "candidate" if prompt else "no_action",
                **({"evidence": prompt} if prompt else {}),
            })
            if prompt:
                created = self.create_prompt_change(
                    wid, workflow, prompt, now, state.get("credits_remaining")
                )
                checks[-1]["proposal_created"] = created
                if created:
                    checks.append({
                        "check": "experiment",
                        "outcome": "skipped",
                        "reason": "prompt incident proposal created",
                    })
                    self._audit_decision_checks(wid, now, checks)
                    continue

            experiment_before = self.store.experiment(wid)
            self.check_experiment(wid, workflow, now)
            experiment_after = self.store.experiment(wid)
            pending_after = self.store.open_proposals(wid)
            experiment_changed = (
                experiment_before is None and experiment_after is not None
            ) or (
                experiment_before is not None
                and experiment_after is not None
                and (
                    experiment_before.phase != experiment_after.phase
                    or experiment_before.last_action_at != experiment_after.last_action_at
                )
            )
            checks.append({
                "check": "experiment",
                "outcome": (
                    "proposal_created" if pending_after
                    else "experiment_state_changed" if experiment_changed
                    else "no_action"
                ),
                "phase_before": experiment_before.phase if experiment_before else None,
                "phase_after": experiment_after.phase if experiment_after else None,
                "pending_proposal_ids": [
                    proposal["proposal_id"] for proposal in pending_after
                ],
                "reason": (
                    None if pending_after or experiment_changed
                    else "no experiment action met its eligibility or evidence thresholds"
                ),
            })
            self._audit_decision_checks(wid, now, checks)

    def _audit_decision_checks(
        self, workflow_id: str, now: str, checks: list[dict[str, Any]]
    ) -> None:
        self.store.audit(
            now,
            workflow_id,
            "decision_checks",
            {"checks": checks},
            event_key=f"decision_checks:{workflow_id}:{now}",
        )

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
        recent_health_change = self.store.latest_proposal(wid, "caller_id_health", "applied")
        if recent_health_change and parse_ts(now) - parse_ts(recent_health_change["created_at"]) < timedelta(hours=48):
            return None
        since = (parse_ts(now) - timedelta(hours=48)).isoformat()
        stats_by_cohort = self.store.recent_stats_by_cohort(wid, "caller_id", since)
        total_dials = sum(
            values["dials"] for stats in stats_by_cohort.values() for values in stats.values()
        )
        current = None
        for cohort_id, stats in sorted(stats_by_cohort.items()):
            if len(stats) < 2:
                continue
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
                if did_rate > other_rate - 0.10 or did_upper >= other_lower:
                    continue

                if current is None:
                    current = self.get(f"/workflows/{wid}/config")
                cfg = copy.deepcopy(current["config"])
                allocations = cfg["allocations"]
                default_distribution = allocations.get("default", {}).get("caller_id")
                if cohort_id == "default":
                    if not default_distribution:
                        continue
                    for cohort in cfg.get("cohorts", []):
                        cohort_allocations = allocations.get(cohort["id"], {})
                        if not cohort_allocations.get("caller_id"):
                            allocations.setdefault(cohort["id"], {})["caller_id"] = copy.deepcopy(
                                default_distribution
                            )
                    distribution = allocations["default"]["caller_id"]
                else:
                    cohort_allocations = allocations.get(cohort_id, {})
                    distribution = cohort_allocations.get("caller_id")
                    if not distribution:
                        if not default_distribution:
                            continue
                        distribution = copy.deepcopy(default_distribution)
                        allocations.setdefault(cohort_id, {})["caller_id"] = distribution

                if did not in distribution or len(distribution) < 2:
                    continue
                mass = distribution[did] * 0.5
                if mass <= 1e-6:
                    continue
                distribution[did] -= mass
                others = [name for name in distribution if name != did]
                total = sum(distribution[name] for name in others)
                if total <= 0:
                    alternatives = [option for option in workflow["options"]["caller_id"] if option != did]
                    if not alternatives:
                        continue
                    distribution[alternatives[0]] = distribution.get(alternatives[0], 0) + mass
                else:
                    for name in others:
                        distribution[name] += mass * distribution[name] / total
                distribution[did] = round(distribution[did], 6)
                for name in distribution:
                    distribution[name] = round(distribution[name], 6)
                cohort_dials = sum(values["dials"] for values in stats.values())
                return {
                    "kind": "caller_id_health",
                    "config": cfg,
                    "rationale": (
                        f"Reduced {did} allocation for cohort {cohort_id} after {values['dials']} "
                        f"dials showed pickup {did_rate:.1%} versus {other_rate:.1%} for other caller IDs."
                    ),
                    "evidence": {
                        "caller_id": did,
                        "cohort_id": cohort_id,
                        "dials": values["dials"],
                        "pickup_rate": did_rate,
                        "other_dials": other_dials,
                        "other_pickup_rate": other_rate,
                        "cohort_share": cohort_dials / total_dials if total_dials else 0,
                        "allocation_reduction": mass,
                    },
                    "dimension": "caller_id",
                }
        return None

    def prompt_incident(self, wid: str, workflow: dict[str, Any], now: str, credits: float | None) -> dict[str, Any] | None:
        cutoff = (parse_ts(now) - timedelta(hours=24)).isoformat()
        attempted = self.store.latest_audit(
            wid, ("prompt_suggestion_attempt", "prompt_incident_unresolved")
        )
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
            cohort_id = stats.get("cohort_id", "default")
            allocations = current["config"].get("allocations", {})
            allocation = allocations.get(cohort_id, {}).get("caller_id")
            if not allocation:
                allocation = allocations.get("default", {}).get("caller_id", {})
            cohort_share = float(stats.get("cohort_share", 1))
            allocation_reduction = float(
                stats.get("allocation_reduction", allocation.get(did, 0))
            )
            affected = round(batch_size * cohort_share * allocation_reduction)
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
            self.store.start_experiment(
                wid,
                dimension=dimension,
                control=control,
                challenger=challenger,
                config_version=current["version"],
                started_at=now,
                baseline_config=json.dumps(current["config"]),
                audit_detail={
                    "dimension": dimension,
                    "control": control,
                    "challenger": challenger,
                    "config_version": current["version"],
                },
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

    def try_promote(
        self, wid: str, workflow: dict[str, Any], exp: ExperimentRecord, now: str
    ) -> None:
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
        exp: ExperimentRecord,
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

    def try_decay_rollback(
        self, wid: str, workflow: dict[str, Any], exp: ExperimentRecord, now: str
    ) -> None:
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
        exp: ExperimentRecord,
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
