"""Compatibility entry point for the outcome controller."""

from autonomy_controller.cli import main
from autonomy_controller.service import Controller
from autonomy_controller.statistics import (
    alpha_spending_z,
    calculate_rates,
    configs_equivalent,
    max_distribution_shift,
    parse_ts,
    primary_counts,
    sequential_look,
    sim_hour,
    wilson_interval,
)
from autonomy_controller.store import Store

__all__ = [
    "Controller", "Store", "alpha_spending_z", "calculate_rates",
    "configs_equivalent", "max_distribution_shift", "parse_ts",
    "primary_counts", "sequential_look", "sim_hour", "wilson_interval",
    "main",
]

if __name__ == "__main__":
    main()
