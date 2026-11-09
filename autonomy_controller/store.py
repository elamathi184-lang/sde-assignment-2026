from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, func, inspect, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from .constants import ACTIVE_PROPOSAL_STATES
from .models import (
    AuditRecord,
    Base,
    CallEvent,
    CursorRecord,
    ExperimentCounter,
    ExperimentRecord,
    HourlyStat,
    LegacyBase,
    LegacyCallRecord,
    MetadataRecord,
    MetricCounts,
    ProposalRecord,
)


DIMENSIONS = ("voice", "welcome_message", "caller_id", "prompt_version")


class Store:
    def __init__(self, path: Path, resume: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        had_state = path.exists() and path.stat().st_size > 0
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 30})
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA synchronous=FULL")
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self._migrate_experiment_schema()

        with self.session_factory() as session:
            has_cursors = session.scalar(select(CursorRecord.name).limit(1)) is not None
        if had_state and has_cursors and not resume:
            self.engine.dispose()
            raise RuntimeError(
                f"controller state already exists at {path}; pass --resume only when reconnecting "
                "to the same simulator run"
            )
        if resume and not has_cursors:
            self.engine.dispose()
            raise RuntimeError(f"cannot resume {path}: no persisted simulator cursor was found")
        self._migrate_legacy_records()

    def _migrate_experiment_schema(self) -> None:
        columns = {column["name"] for column in inspect(self.engine).get_columns("experiments")}
        with self.engine.begin() as connection:
            if "challenger" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE experiments ADD COLUMN challenger VARCHAR NOT NULL DEFAULT ''"
                )
            if "config_version" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE experiments ADD COLUMN config_version INTEGER NOT NULL DEFAULT 0"
                )

    def _migrate_legacy_records(self) -> None:
        table_names = set(inspect(self.engine).get_table_names())
        has_legacy_calls = "call_records" in table_names
        has_legacy_leads = "lead_records" in table_names
        with self.session_factory.begin() as session:
            marker = session.get(MetadataRecord, "experiment_counters_backfilled")
            if marker is None:
                if has_legacy_calls:
                    self._backfill_call_records(session)
                session.add(MetadataRecord(key="experiment_counters_backfilled", value="1"))

        if has_legacy_calls or has_legacy_leads:
            with self.engine.begin() as connection:
                if has_legacy_calls:
                    LegacyCallRecord.__table__.drop(connection, checkfirst=True)
                if has_legacy_leads:
                    LegacyBase.metadata.tables["lead_records"].drop(connection, checkfirst=True)

    def _backfill_call_records(self, session: Session) -> None:
        session.execute(update(ExperimentCounter).values(
            dials=0, connected=0, engaged=0, qa_calls=0, hallucination_flags=0
        ))
        counters: dict[tuple[str, int, str, str, str, str], MetricCounts] = defaultdict(MetricCounts)
        statement = (
            select(LegacyCallRecord)
            .order_by(LegacyCallRecord.event_id)
            .execution_options(yield_per=2000)
        )
        rows = session.scalars(statement)
        for row in rows:
            variant = json.loads(row.variant)
            qa = json.loads(row.qa or "{}")
            connected = int(row.connected)
            counts = {
                "connected": connected,
                "engaged": int(row.engaged),
                "qa_calls": int(connected and "hallucination_flag" in qa),
                "hallucination_flags": int(connected and bool(qa.get("hallucination_flag"))),
            }
            for dimension in DIMENSIONS:
                option = variant.get(dimension)
                if option is None:
                    continue
                key = (
                    row.workflow_id, row.config_version, row.cohort_id, dimension,
                    str(option), row.ts,
                )
                counters[key].add(**counts)
                if len(counters) >= 4000:
                    self._upsert_experiment_counters(session, counters)
                    counters.clear()
        self._upsert_experiment_counters(session, counters)

    def close(self) -> None:
        self.engine.dispose()

    def cursor(self, name: str) -> int:
        with self.session_factory() as session:
            record = session.get(CursorRecord, name)
            return record.chunk if record else -1

    def bind_run(self, signature: dict[str, Any]) -> None:
        encoded = json.dumps(signature, sort_keys=True, separators=(",", ":"))
        with self.session_factory.begin() as session:
            record = session.get(MetadataRecord, "run_signature")
            if record and record.value != encoded:
                raise RuntimeError(
                    "saved controller state does not match the live simulator metadata; "
                    "use a fresh state directory for a new run"
                )
            if record is None:
                has_history = session.get(CursorRecord, "history") is not None
                has_events = session.get(CursorRecord, "events") is not None
                if has_history or has_events:
                    raise RuntimeError(
                        "saved controller state has cursors but no run identity; refusing unsafe recovery"
                    )
                session.add(MetadataRecord(key="run_signature", value=encoded))

    def metadata_value(self, key: str) -> str | None:
        with self.session_factory() as session:
            record = session.get(MetadataRecord, key)
            return record.value if record else None

    def experiment_stats(
        self,
        workflow_id: str,
        dimension: str,
        since: str,
        config_version: int,
        cohort_id: str | None = None,
        until: str | None = None,
    ) -> dict[str, dict[str, int]]:
        columns = [
            ExperimentCounter.variant,
            func.sum(ExperimentCounter.dials).label("dials"),
            func.sum(ExperimentCounter.connected).label("connected"),
            func.sum(ExperimentCounter.engaged).label("engaged"),
            func.sum(ExperimentCounter.qa_calls).label("qa_calls"),
            func.sum(ExperimentCounter.hallucination_flags).label("hallucination_flags"),
        ]
        statement = select(*columns).where(
            ExperimentCounter.workflow_id == workflow_id,
            ExperimentCounter.config_version == config_version,
            ExperimentCounter.dimension == dimension,
            ExperimentCounter.ts >= since,
        )
        if cohort_id is not None:
            statement = statement.where(ExperimentCounter.cohort_id == cohort_id)
        if until is not None:
            statement = statement.where(ExperimentCounter.ts < until)
        statement = statement.group_by(ExperimentCounter.variant)
        with self.session_factory() as session:
            rows = session.execute(statement).all()
        return {
            row.variant: {
                key: int(getattr(row, key) or 0)
                for key in ("dials", "connected", "engaged", "qa_calls", "hallucination_flags")
            }
            for row in rows
        }

    def ingest_chunk(self, kind: str, chunk: int, lines: str | Iterable[str]) -> int:
        if chunk <= self.cursor(kind):
            return 0
        source = lines.splitlines() if isinstance(lines, str) else lines
        inserted = 0
        batch: list[dict[str, Any]] = []
        with self.session_factory.begin() as session:
            record = session.get(CursorRecord, kind)
            if record is not None and chunk <= record.chunk:
                return 0
            for line in source:
                if not line.strip():
                    continue
                batch.append(json.loads(line))
                if len(batch) == 4000:
                    inserted += self._flush_batch(session, batch)
                    batch.clear()
            if batch:
                inserted += self._flush_batch(session, batch)
            if record is None:
                session.add(CursorRecord(name=kind, chunk=chunk))
            else:
                record.chunk = max(record.chunk, chunk)
        return inserted

    def _flush_batch(self, session: Session, events: list[dict[str, Any]]) -> int:
        hourly: dict[tuple[str, str, str, str, str], MetricCounts] = defaultdict(MetricCounts)
        experiment: dict[tuple[str, int, str, str, str, str], MetricCounts] = defaultdict(MetricCounts)
        for event in events:
            if event["type"] == "call":
                call = CallEvent.from_payload(event)
                hour = call.ts[:13]
                metric_values = {
                    "connected": call.connected,
                    "engaged": call.engaged,
                    "qa_calls": call.qa_calls,
                    "hallucination_flags": call.hallucination_flags,
                }
                for dimension in DIMENSIONS:
                    option = call.variant.get(dimension)
                    if option is None:
                        continue
                    hourly[(call.workflow_id, call.cohort_id, dimension, str(option), hour)].add(
                        **metric_values
                    )
                    experiment[
                        (
                            call.workflow_id, call.config_version, call.cohort_id, dimension,
                            str(option), call.ts,
                        )
                    ].add(**metric_values)
            elif event["type"] == "config_changed":
                self._ingest_config_change(session, event)
            elif event["type"] == "approval_decided":
                self.audit(
                    event["ts"],
                    event.get("workflow_id"),
                    "approval_decided",
                    {
                        "approval_id": event.get("approval_id"),
                        "status": event.get("status"),
                        "reasons": event.get("reasons", []),
                    },
                    session=session,
                )
        self._upsert_hourly_stats(session, hourly)
        self._upsert_experiment_counters(session, experiment)
        return len(events)

    def _ingest_config_change(self, session: Session, event: dict[str, Any]) -> None:
        self.audit(
            event["ts"],
            event.get("workflow_id"),
            "config_changed",
            {
                "actor": event.get("actor"),
                "version": event.get("version"),
                "changed_dims": event.get("changed_dims"),
                "approval_id": event.get("approval_id"),
            },
            session=session,
        )
        proposal = session.get(ProposalRecord, event.get("proposal_id")) if event.get("proposal_id") else None
        changes_experiment = event.get("actor") == "supervisor" or (
            proposal is not None and proposal.kind not in ("experiment_explore", "experiment_promote")
        )
        if changes_experiment:
            session.execute(
                update(ExperimentRecord)
                .where(
                    ExperimentRecord.workflow_id == event.get("workflow_id"),
                    ExperimentRecord.phase.in_(("exploring", "promoted")),
                )
                .values(phase="abandoned", last_action_at=event["ts"])
            )

    @staticmethod
    def _upsert_hourly_stats(
        session: Session,
        counters: dict[tuple[str, str, str, str, str], MetricCounts],
    ) -> None:
        if not counters:
            return
        rows = [
            {
                "workflow_id": key[0],
                "cohort_id": key[1],
                "dimension": key[2],
                "variant": key[3],
                "hour": key[4],
                **counts.as_dict(),
            }
            for key, counts in counters.items()
        ]
        statement = sqlite_insert(HourlyStat)
        statement = statement.on_conflict_do_update(
            index_elements=[
                HourlyStat.workflow_id,
                HourlyStat.cohort_id,
                HourlyStat.dimension,
                HourlyStat.variant,
                HourlyStat.hour,
            ],
            set_={
                column: getattr(HourlyStat, column) + getattr(statement.excluded, column)
                for column in ("dials", "connected", "engaged", "qa_calls", "hallucination_flags")
            },
        )
        session.execute(statement, rows)

    @staticmethod
    def _upsert_experiment_counters(
        session: Session,
        counters: dict[tuple[str, int, str, str, str, str], MetricCounts],
    ) -> None:
        if not counters:
            return
        rows = [
            {
                "workflow_id": key[0],
                "config_version": key[1],
                "cohort_id": key[2],
                "dimension": key[3],
                "variant": key[4],
                "ts": key[5],
                **counts.as_dict(),
            }
            for key, counts in counters.items()
        ]
        statement = sqlite_insert(ExperimentCounter)
        statement = statement.on_conflict_do_update(
            index_elements=[
                ExperimentCounter.workflow_id,
                ExperimentCounter.config_version,
                ExperimentCounter.cohort_id,
                ExperimentCounter.dimension,
                ExperimentCounter.variant,
                ExperimentCounter.ts,
            ],
            set_={
                column: getattr(ExperimentCounter, column) + getattr(statement.excluded, column)
                for column in ("dials", "connected", "engaged", "qa_calls", "hallucination_flags")
            },
        )
        session.execute(statement, rows)

    def audit(
        self,
        ts: str,
        workflow_id: str | None,
        kind: str,
        detail: dict[str, Any],
        *,
        session: Session | None = None,
    ) -> None:
        if session is not None:
            session.add(AuditRecord(
                ts=ts,
                workflow_id=workflow_id,
                kind=kind,
                detail=json.dumps(detail, sort_keys=True),
            ))
            return
        with self.session_factory.begin() as owned_session:
            self.audit(ts, workflow_id, kind, detail, session=owned_session)

    def recent_stats(
        self,
        workflow_id: str,
        dimension: str,
        since: str,
        cohort_id: str | None = None,
        until: str | None = None,
    ) -> dict[str, dict[str, int]]:
        statement = select(
            HourlyStat.variant,
            func.sum(HourlyStat.dials).label("dials"),
            func.sum(HourlyStat.connected).label("connected"),
            func.sum(HourlyStat.engaged).label("engaged"),
            func.sum(HourlyStat.qa_calls).label("qa_calls"),
            func.sum(HourlyStat.hallucination_flags).label("hallucination_flags"),
        ).where(
            HourlyStat.workflow_id == workflow_id,
            HourlyStat.dimension == dimension,
            HourlyStat.hour >= since[:13],
        )
        if cohort_id is not None:
            statement = statement.where(HourlyStat.cohort_id == cohort_id)
        if until is not None:
            statement = statement.where(HourlyStat.hour < until[:13])
        statement = statement.group_by(HourlyStat.variant)
        with self.session_factory() as session:
            rows = session.execute(statement).all()
        return {
            row.variant: {
                key: int(getattr(row, key) or 0)
                for key in ("dials", "connected", "engaged", "qa_calls", "hallucination_flags")
            }
            for row in rows
        }

    def open_proposals(self, workflow_id: str | None = None) -> list[ProposalRecord]:
        statement = select(ProposalRecord).where(ProposalRecord.state.in_(ACTIVE_PROPOSAL_STATES))
        if workflow_id is not None:
            statement = statement.where(ProposalRecord.workflow_id == workflow_id)
        statement = statement.order_by(ProposalRecord.created_at)
        with self.session_factory() as session:
            return list(session.scalars(statement))

    def recent_proposals(self, workflow_id: str, since: str) -> int:
        statement = select(func.count()).select_from(ProposalRecord).where(
            ProposalRecord.workflow_id == workflow_id,
            ProposalRecord.created_at >= since,
        )
        with self.session_factory() as session:
            return int(session.scalar(statement) or 0)

    def experiment(self, workflow_id: str) -> ExperimentRecord | None:
        with self.session_factory() as session:
            return session.get(ExperimentRecord, workflow_id)

    def proposal(self, proposal_id: str) -> ProposalRecord | None:
        with self.session_factory() as session:
            return session.get(ProposalRecord, proposal_id)

    def update_proposal(self, proposal_id: str, **values: Any) -> None:
        with self.session_factory.begin() as session:
            proposal = session.get(ProposalRecord, proposal_id)
            if proposal is None:
                raise RuntimeError(f"proposal {proposal_id} disappeared from controller state")
            for key, value in values.items():
                setattr(proposal, key, value)

    def finish_proposal(
        self,
        proposal_id: str,
        workflow_id: str,
        kind: str,
        state: str,
        ts: str,
        detail: dict[str, Any],
        *,
        experiment_update: dict[str, Any] | None = None,
        active_experiment_only: bool = False,
    ) -> None:
        with self.session_factory.begin() as session:
            proposal = session.get(ProposalRecord, proposal_id)
            if proposal is None:
                raise RuntimeError(f"proposal {proposal_id} disappeared from controller state")
            proposal.state = state
            self.audit(
                ts,
                workflow_id,
                "proposal_" + state,
                {"proposal_id": proposal_id, "kind": kind, "detail": detail},
                session=session,
            )
            if experiment_update is not None:
                experiment = session.get(ExperimentRecord, workflow_id)
                if experiment is None and not active_experiment_only and experiment_update.get("phase") == "exploring":
                    session.add(ExperimentRecord(workflow_id=workflow_id, **experiment_update))
                elif experiment is not None and (
                    not active_experiment_only or experiment.phase in ("exploring", "promoted")
                ):
                    for key, value in experiment_update.items():
                        setattr(experiment, key, value)

    def latest_applied_proposal(self, workflow_id: str, kind: str) -> ProposalRecord | None:
        statement = select(ProposalRecord).where(
            ProposalRecord.workflow_id == workflow_id,
            ProposalRecord.kind == kind,
            ProposalRecord.state == "applied",
        ).order_by(ProposalRecord.created_at.desc()).limit(1)
        with self.session_factory() as session:
            return session.scalar(statement)

    def latest_audit(self, workflow_id: str, kinds: tuple[str, ...]) -> AuditRecord | None:
        statement = select(AuditRecord).where(
            AuditRecord.workflow_id == workflow_id,
            AuditRecord.kind.in_(kinds),
        ).order_by(AuditRecord.id.desc()).limit(1)
        with self.session_factory() as session:
            return session.scalar(statement)

    def latest_proposal(self, workflow_id: str, kind: str, state: str) -> ProposalRecord | None:
        statement = select(ProposalRecord).where(
            ProposalRecord.workflow_id == workflow_id,
            ProposalRecord.kind == kind,
            ProposalRecord.state == state,
        ).order_by(ProposalRecord.created_at.desc()).limit(1)
        with self.session_factory() as session:
            return session.scalar(statement)

    def save_proposal(
        self,
        proposal_id: str,
        workflow_id: str,
        kind: str,
        state: str,
        created_at: str,
        base_version: int,
        payload: dict[str, Any],
        audit_detail: dict[str, Any],
    ) -> None:
        with self.session_factory.begin() as session:
            session.add(ProposalRecord(
                proposal_id=proposal_id,
                workflow_id=workflow_id,
                kind=kind,
                state=state,
                created_at=created_at,
                base_version=base_version,
                payload=json.dumps(payload, sort_keys=True),
            ))
            self.audit(created_at, workflow_id, "proposal_created", audit_detail, session=session)

    def upsert_experiment(
        self,
        workflow_id: str,
        *,
        dimension: str,
        cohort_id: str,
        phase: str,
        control: str,
        challenger: str,
        config_version: int,
        started_at: str,
        baseline_config: str,
        last_action_at: str,
        baseline_pickup: float | None = None,
        baseline_engagement: float | None = None,
        audit_detail: dict[str, Any] | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            experiment = session.get(ExperimentRecord, workflow_id)
            if experiment is None:
                experiment = ExperimentRecord(
                    workflow_id=workflow_id,
                    dimension=dimension,
                    cohort_id=cohort_id,
                    phase=phase,
                    control=control,
                    challenger=challenger,
                    config_version=config_version,
                    winner=None,
                    started_at=started_at,
                    promoted_at=None,
                    baseline_config=baseline_config,
                    baseline_pickup=baseline_pickup,
                    baseline_engagement=baseline_engagement,
                    last_action_at=last_action_at,
                )
                session.add(experiment)
            else:
                for key, value in {
                    "dimension": dimension,
                    "cohort_id": cohort_id,
                    "phase": phase,
                    "control": control,
                    "challenger": challenger,
                    "config_version": config_version,
                    "winner": None,
                    "started_at": started_at,
                    "promoted_at": None,
                    "baseline_config": baseline_config,
                    "baseline_pickup": baseline_pickup,
                    "baseline_engagement": baseline_engagement,
                    "last_action_at": last_action_at,
                }.items():
                    setattr(experiment, key, value)
            if audit_detail is not None:
                self.audit(
                    started_at,
                    workflow_id,
                    "experiment_started_existing_allocation",
                    audit_detail,
                    session=session,
                )

    def update_experiment(self, workflow_id: str, **values: Any) -> None:
        with self.session_factory.begin() as session:
            experiment = session.get(ExperimentRecord, workflow_id)
            if experiment is not None:
                for key, value in values.items():
                    setattr(experiment, key, value)

    def start_experiment(
        self,
        workflow_id: str,
        *,
        dimension: str,
        control: str,
        challenger: str,
        config_version: int,
        started_at: str,
        baseline_config: str,
        baseline_pickup: float | None = None,
        baseline_engagement: float | None = None,
        audit_detail: dict[str, Any] | None = None,
    ) -> None:
        self.upsert_experiment(
            workflow_id,
            dimension=dimension,
            cohort_id="default",
            phase="exploring",
            control=control,
            challenger=challenger,
            config_version=config_version,
            started_at=started_at,
            baseline_config=baseline_config,
            last_action_at=started_at,
            baseline_pickup=baseline_pickup,
            baseline_engagement=baseline_engagement,
            audit_detail=audit_detail,
        )
