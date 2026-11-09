from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any

from .constants import FAMILYWISE_ERROR_RATE, Z_95


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
