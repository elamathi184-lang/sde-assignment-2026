import json

import pytest

from controller import (
    Store,
    alpha_spending_z,
    calculate_rates,
    configs_equivalent,
    max_distribution_shift,
    sequential_look,
    wilson_interval,
)


def test_wilson_interval_handles_empty_and_certainty():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lower, upper = wilson_interval(100, 100)
    assert 0 < lower < upper <= 1


def test_store_ingestion_is_idempotent_and_aggregates_calls(tmp_path):
    store = Store(tmp_path / "controller.sqlite3")
    event = {
        "event_id": 7,
        "type": "call",
        "ts": "2027-01-04T10:15:00+05:30",
        "workflow_id": "wf",
        "lead_id": "wf:l1",
        "person_id": "wf:p1",
        "config_version": 1,
        "cohort_id": "default",
        "variant": {
            "voice": "V1",
            "welcome_message": "W1",
            "caller_id": "D1",
            "prompt_version": "P1",
        },
        "connected": True,
        "engaged": True,
        "qa": {"hallucination_flag": False},
    }
    chunk = json.dumps(event) + "\n"

    assert store.ingest_chunk("events", 3, chunk) == 1
    assert store.ingest_chunk("events", 3, chunk) == 0
    assert store.cursor("events") == 3
    voice = store.recent_stats("wf", "voice", "2027-01-04T00:00:00+05:30", "default")
    assert voice["V1"] == {
        "dials": 1,
        "connected": 1,
        "engaged": 1,
        "qa_calls": 1,
        "hallucination_flags": 0,
    }
    assert calculate_rates(voice)["engagement_rate"] == 1
    call = store.db.execute("SELECT workflow_id, variant, config_version FROM call_records").fetchone()
    assert call["workflow_id"] == "wf"
    assert json.loads(call["variant"])["voice"] == "V1"
    assert call["config_version"] == 1
    version_two_event = {**event, "event_id": 8, "config_version": 2, "variant": {
        **event["variant"], "voice": "V2",
    }}
    assert store.ingest_chunk("events", 4, json.dumps(version_two_event) + "\n") == 1
    experiment_stats = store.experiment_stats(
        "wf", "voice", "2027-01-04T00:00:00+05:30", config_version=2
    )
    assert experiment_stats["V2"]["dials"] == 1
    assert "V1" not in experiment_stats
    store.close()


def test_max_distribution_shift_includes_cohort_fallback():
    old = {
        "cohorts": [{"id": "senior", "where": {"age_band": ["60+"]}}],
        "allocations": {"default": {"caller_id": {"D1": 0.5, "D2": 0.5}}},
    }
    new = {
        "cohorts": [{"id": "senior", "where": {"age_band": ["60+"]}}],
        "allocations": {"default": {"caller_id": {"D1": 0.75, "D2": 0.25}}},
    }
    assert max_distribution_shift(old, new) == 0.25


def test_max_distribution_shift_detects_voice_changes():
    old = {"cohorts": [], "allocations": {"default": {"voice": {"V1": 1.0}}}}
    new = {"cohorts": [], "allocations": {"default": {"voice": {"V1": 0.5, "V2": 0.5}}}}
    assert max_distribution_shift(old, new) == 0.5


def test_config_equivalence_handles_normalized_allocations():
    current = {
        "cohorts": [],
        "prompt_version": "P1",
        "schedule": {},
        "allocations": {
            "default": {
                "voice": {"V1": 0.5, "V2": 0.5},
                "caller_id": {"D1": 1},
                "welcome_message": {"W1": 1},
            }
        },
    }
    no_op = {
        **current,
        "allocations": {
            "default": {
                "voice": {"V1": 0.5000000001, "V2": 0.4999999999, "V3": 0},
                "caller_id": {"D1": 3},
                "welcome_message": {"W1": 2},
            }
        },
    }
    assert configs_equivalent(current, no_op)


def test_alpha_spending_makes_repeated_looks_more_conservative():
    assert alpha_spending_z(1) > 1.96
    assert alpha_spending_z(10) > alpha_spending_z(1)
    with pytest.raises(ValueError):
        alpha_spending_z(0)
    assert sequential_look("2027-01-04T12:00:00+05:30", "2027-01-04T10:00:00+05:30") == 3


def test_store_requires_explicit_matching_resume(tmp_path):
    path = tmp_path / "state.sqlite3"
    signature = {"scenario": "test", "end_minute": 100}
    store = Store(path)
    store.bind_run(signature)
    store.ingest_chunk("history", 0, "")
    store.close()

    with pytest.raises(RuntimeError, match="pass --resume"):
        Store(path)

    resumed = Store(path, resume=True)
    resumed.bind_run(signature)
    with pytest.raises(RuntimeError, match="does not match"):
        resumed.bind_run({"scenario": "different", "end_minute": 100})
    resumed.close()
