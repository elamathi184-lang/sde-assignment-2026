import json

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from controller import (
    Store,
    alpha_spending_z,
    calculate_rates,
    configs_equivalent,
    max_distribution_shift,
    sequential_look,
    wilson_interval,
)
from autonomy_controller.decisions import DecisionMixin
from autonomy_controller.models import (
    AuditRecord,
    ExperimentRecord,
    LegacyBase,
    LegacyCallRecord,
    ProposalRecord,
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
    assert not inspect(store.engine).has_table("call_records")
    other_cohort_event = {
        **event,
        "event_id": 8,
        "cohort_id": "senior",
        "variant": {**event["variant"], "caller_id": "D2"},
    }
    assert store.ingest_chunk("events", 4, json.dumps(other_cohort_event) + "\n") == 1
    caller_id_stats = store.recent_stats_by_cohort(
        "wf", "caller_id", "2027-01-04T00:00:00+05:30"
    )
    assert caller_id_stats["default"]["D1"]["dials"] == 1
    assert caller_id_stats["senior"]["D2"]["dials"] == 1
    version_two_event = {**event, "event_id": 9, "config_version": 2, "variant": {
        **event["variant"], "voice": "V2",
    }}
    assert store.ingest_chunk("events", 5, json.dumps(version_two_event) + "\n") == 1
    experiment_stats = store.experiment_stats(
        "wf", "voice", "2027-01-04T00:00:00+05:30", config_version=2
    )
    assert experiment_stats["V2"] == {
        "dials": 1,
        "connected": 1,
        "engaged": 1,
        "qa_calls": 1,
        "hallucination_flags": 0,
    }
    assert "V1" not in experiment_stats
    assert store.experiment_stats(
        "wf", "voice", "2027-01-04T10:30:00+05:30", config_version=2
    ) == {}
    store.close()


def test_caller_id_incident_changes_only_the_affected_cohort():
    class IncidentStore:
        def latest_proposal(self, workflow_id, kind, state):
            return None

        def recent_stats_by_cohort(self, workflow_id, dimension, since):
            return {
                "good": {
                    "D1": {"dials": 200, "connected": 180},
                    "D2": {"dials": 200, "connected": 170},
                },
                "weak": {
                    "D1": {"dials": 200, "connected": 10},
                    "D2": {"dials": 200, "connected": 160},
                },
            }

    class IncidentController(DecisionMixin):
        def __init__(self):
            self.store = IncidentStore()
            self.config = {
                "version": 1,
                "config": {
                    "cohorts": [{"id": "good"}, {"id": "weak"}],
                    "allocations": {
                        "default": {"caller_id": {"D1": 0.5, "D2": 0.5}},
                        "good": {"caller_id": {"D1": 0.7, "D2": 0.3}},
                    },
                },
            }

        def get(self, path):
            return self.config

    controller = IncidentController()
    incident = controller.caller_id_incident(
        "wf",
        {"options": {"caller_id": ["D1", "D2"]}},
        "2027-01-04T12:00:00+05:30",
    )

    assert incident is not None
    assert incident["evidence"]["cohort_id"] == "weak"
    assert incident["evidence"]["cohort_share"] == 0.5
    assert incident["evidence"]["allocation_reduction"] == 0.25
    assert incident["config"]["allocations"]["good"]["caller_id"] == {"D1": 0.7, "D2": 0.3}
    assert incident["config"]["allocations"]["default"]["caller_id"] == {"D1": 0.5, "D2": 0.5}
    assert incident["config"]["allocations"]["weak"]["caller_id"] == {"D1": 0.25, "D2": 0.75}
    proposal = controller.proposal_body(
        "wf",
        {"daily_batch": {"size": 1000}},
        controller.config,
        incident["config"],
        incident,
        "2027-01-04T12:00:00+05:30",
    )
    assert proposal["affected_leads"] == 125


def test_default_caller_id_incident_preserves_named_cohort_inheritance():
    class IncidentStore:
        def latest_proposal(self, workflow_id, kind, state):
            return None

        def recent_stats_by_cohort(self, workflow_id, dimension, since):
            return {
                "default": {
                    "D1": {"dials": 200, "connected": 10},
                    "D2": {"dials": 200, "connected": 160},
                },
                "senior": {"D1": {"dials": 200, "connected": 180}},
            }

    class IncidentController(DecisionMixin):
        def __init__(self):
            self.store = IncidentStore()
            self.config = {
                "version": 1,
                "config": {
                    "cohorts": [{"id": "senior"}],
                    "allocations": {"default": {"caller_id": {"D1": 0.5, "D2": 0.5}}},
                },
            }

        def get(self, path):
            return self.config

    controller = IncidentController()
    incident = controller.caller_id_incident(
        "wf",
        {"options": {"caller_id": ["D1", "D2"]}},
        "2027-01-04T12:00:00+05:30",
    )

    assert incident is not None
    assert incident["evidence"]["cohort_id"] == "default"
    assert incident["config"]["allocations"]["default"]["caller_id"] == {"D1": 0.25, "D2": 0.75}
    assert incident["config"]["allocations"]["senior"]["caller_id"] == {"D1": 0.5, "D2": 0.5}


def test_experiment_stats_backfills_legacy_rows_once_after_upgrade(tmp_path):
    path = tmp_path / "controller.sqlite3"
    legacy_engine = create_engine(f"sqlite:///{path}")
    LegacyBase.metadata.create_all(legacy_engine)
    with Session(legacy_engine) as legacy_session:
        legacy_session.add(
            LegacyCallRecord(
                event_id=3,
                workflow_id="wf",
                ts="2027-01-04T10:15:00+05:30",
                config_version=1,
                cohort_id="default",
                variant=json.dumps({"voice": "V1"}),
                connected=1,
                engaged=0,
                qa=json.dumps({"hallucination_flag": True}),
            )
        )
        legacy_session.commit()
    legacy_engine.dispose()

    store = Store(path)
    assert store.experiment_stats(
        "wf", "voice", "2027-01-04T10:00:00+05:30", config_version=1
    )["V1"] == {
        "dials": 1,
        "connected": 1,
        "engaged": 0,
        "qa_calls": 1,
        "hallucination_flags": 1,
    }
    assert store.metadata_value("experiment_counters_backfilled") == "1"
    assert not inspect(store.engine).has_table("call_records")
    assert not inspect(store.engine).has_table("lead_records")
    store.close()


def test_finishing_proposal_updates_audit_and_experiment_atomically(tmp_path):
    store = Store(tmp_path / "controller.sqlite3")
    with store.session_factory.begin() as session:
        session.add(ProposalRecord(
            proposal_id="proposal-1",
            workflow_id="wf",
            kind="experiment_promote",
            state="pending",
            created_at="2027-01-04T10:00:00+00:00",
            base_version=1,
            payload="{}",
        ))
        session.add(ExperimentRecord(
            workflow_id="wf",
            dimension="voice",
            cohort_id="default",
            phase="exploring",
            control="V1",
            challenger="V2",
            config_version=2,
            started_at="2027-01-04T10:00:00+00:00",
            baseline_config="{}",
            last_action_at="2027-01-04T10:00:00+00:00",
        ))

    with pytest.raises(IntegrityError):
        store.finish_proposal(
            "proposal-1",
            "wf",
            "experiment_promote",
            "applied",
            "2027-01-04T11:00:00+00:00",
            {"version": 3},
            experiment_update={"phase": None},
        )

    with store.session_factory() as session:
        assert session.get(ProposalRecord, "proposal-1").state == "pending"
        assert session.get(ExperimentRecord, "wf").phase == "exploring"
        assert session.query(AuditRecord).filter_by(kind="proposal_applied").count() == 0

    store.finish_proposal(
        "proposal-1",
        "wf",
        "experiment_promote",
        "applied",
        "2027-01-04T11:00:00+00:00",
        {"version": 3},
        experiment_update={"phase": "promoted", "config_version": 3},
    )
    with store.session_factory() as session:
        assert session.get(ProposalRecord, "proposal-1").state == "applied"
        experiment = session.get(ExperimentRecord, "wf")
        assert experiment.phase == "promoted"
        assert experiment.config_version == 3
        assert session.query(AuditRecord).filter_by(kind="proposal_applied").count() == 1

    with store.session_factory.begin() as session:
        session.add(ProposalRecord(
            proposal_id="proposal-2",
            workflow_id="wf2",
            kind="experiment_explore",
            state="pending",
            created_at="2027-01-04T10:00:00+00:00",
            base_version=1,
            payload="{}",
        ))
    store.finish_proposal(
        "proposal-2",
        "wf2",
        "experiment_explore",
        "applied",
        "2027-01-04T11:00:00+00:00",
        {"version": 2},
        experiment_update={
            "dimension": "voice",
            "cohort_id": "default",
            "phase": "exploring",
            "control": "V1",
            "challenger": "V2",
            "config_version": 2,
            "winner": None,
            "started_at": "2027-01-04T11:00:00+00:00",
            "promoted_at": None,
            "baseline_config": "{}",
            "baseline_pickup": None,
            "baseline_engagement": None,
            "last_action_at": "2027-01-04T11:00:00+00:00",
        },
    )
    with store.session_factory() as session:
        assert session.get(ExperimentRecord, "wf2").phase == "exploring"
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
