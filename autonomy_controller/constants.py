from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


_DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / "credentials.env"
_ENV_FILE = Path(os.environ.get("CONTROLLER_ENV_FILE", _DEFAULT_ENV_FILE))
load_dotenv(_ENV_FILE, override=False)


def _int_setting(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def _float_setting(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


MIN_VARIANT_DIALS = _int_setting("MIN_VARIANT_DIALS", 60)
MIN_VARIANT_CONNECTIONS = _int_setting("MIN_VARIANT_CONNECTIONS", 30)
MIN_INCIDENT_CALLS = _int_setting("MIN_INCIDENT_CALLS", 40)
LOCAL_PROPOSAL_CAP = _int_setting("LOCAL_PROPOSAL_CAP", 2)
EARLIEST_APPROVAL_HOUR = _int_setting("EARLIEST_APPROVAL_HOUR", 11)
FAMILYWISE_ERROR_RATE = _float_setting("FAMILYWISE_ERROR_RATE", 0.05)
Z_95 = _float_setting("Z_95", 1.96)

for _name, _value in (
    ("MIN_VARIANT_DIALS", MIN_VARIANT_DIALS),
    ("MIN_VARIANT_CONNECTIONS", MIN_VARIANT_CONNECTIONS),
    ("MIN_INCIDENT_CALLS", MIN_INCIDENT_CALLS),
    ("LOCAL_PROPOSAL_CAP", LOCAL_PROPOSAL_CAP),
):
    if _value < 1:
        raise ValueError(f"{_name} must be at least 1")
if not 0 <= EARLIEST_APPROVAL_HOUR <= 23:
    raise ValueError("EARLIEST_APPROVAL_HOUR must be between 0 and 23")
if not 0 < FAMILYWISE_ERROR_RATE < 1:
    raise ValueError("FAMILYWISE_ERROR_RATE must be between 0 and 1")
if Z_95 <= 0:
    raise ValueError("Z_95 must be greater than 0")

ACTIVE_PROPOSAL_STATES = ("submitting", "pending", "approved", "applying", "auto_submitting")
