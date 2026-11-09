from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


@dataclass(slots=True)
class MetricCounts:
    dials: int = 0
    connected: int = 0
    engaged: int = 0
    qa_calls: int = 0
    hallucination_flags: int = 0

    def add(
        self,
        *,
        connected: int,
        engaged: int,
        qa_calls: int,
        hallucination_flags: int,
    ) -> None:
        self.dials += 1
        self.connected += connected
        self.engaged += engaged
        self.qa_calls += qa_calls
        self.hallucination_flags += hallucination_flags

    def as_dict(self) -> dict[str, int]:
        return {
            "dials": self.dials,
            "connected": self.connected,
            "engaged": self.engaged,
            "qa_calls": self.qa_calls,
            "hallucination_flags": self.hallucination_flags,
        }


@dataclass(frozen=True, slots=True)
class CallEvent:
    workflow_id: str
    ts: str
    config_version: int
    cohort_id: str
    variant: dict[str, Any]
    connected: int
    engaged: int
    qa_calls: int
    hallucination_flags: int

    @classmethod
    def from_payload(cls, event: dict[str, Any]) -> CallEvent:
        qa = event.get("qa") or {}
        connected = int(bool(event.get("connected")))
        qa_calls = int(connected and "hallucination_flag" in qa)
        return cls(
            workflow_id=event["workflow_id"],
            ts=event["ts"],
            config_version=int(event["config_version"]),
            cohort_id=event.get("cohort_id") or "default",
            variant=event.get("variant") or {},
            connected=connected,
            engaged=int(bool(event.get("engaged"))),
            qa_calls=qa_calls,
            hallucination_flags=int(qa_calls and bool(qa["hallucination_flag"])),
        )


@dataclass(frozen=True, slots=True)
class RunSignature:
    scenario: str
    end_minute: int
    tick_minutes: int
    workflows: dict[str, Any]
    history_chunks: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "end_minute": self.end_minute,
            "tick_minutes": self.tick_minutes,
            "workflows": self.workflows,
            "history_chunks": self.history_chunks,
        }


class Base(DeclarativeBase):
    pass


class LegacyBase(DeclarativeBase):
    pass


class RowAccess:
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class MetadataRecord(Base):
    __tablename__ = "metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class CursorRecord(Base):
    __tablename__ = "cursors"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    chunk: Mapped[int] = mapped_column(Integer, nullable=False)


class HourlyStat(Base):
    __tablename__ = "hourly_stats"

    workflow_id: Mapped[str] = mapped_column(String, primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String, primary_key=True)
    dimension: Mapped[str] = mapped_column(String, primary_key=True)
    variant: Mapped[str] = mapped_column(String, primary_key=True)
    hour: Mapped[str] = mapped_column(String, primary_key=True)
    dials: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    connected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    engaged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qa_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hallucination_flags: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ExperimentCounter(Base):
    __tablename__ = "experiment_counters"

    workflow_id: Mapped[str] = mapped_column(String, primary_key=True)
    config_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String, primary_key=True)
    dimension: Mapped[str] = mapped_column(String, primary_key=True)
    variant: Mapped[str] = mapped_column(String, primary_key=True)
    ts: Mapped[str] = mapped_column(String, primary_key=True)
    dials: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    connected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    engaged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qa_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hallucination_flags: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ProposalRecord(RowAccess, Base):
    __tablename__ = "proposals"

    proposal_id: Mapped[str] = mapped_column(String, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class ExperimentRecord(RowAccess, Base):
    __tablename__ = "experiments"

    workflow_id: Mapped[str] = mapped_column(String, primary_key=True)
    dimension: Mapped[str] = mapped_column(String, nullable=False)
    cohort_id: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    control: Mapped[str] = mapped_column(String, nullable=False)
    challenger: Mapped[str] = mapped_column(String, default="", nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    winner: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String, nullable=False)
    promoted_at: Mapped[str | None] = mapped_column(String)
    baseline_config: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_pickup: Mapped[float | None] = mapped_column(Float)
    baseline_engagement: Mapped[float | None] = mapped_column(Float)
    last_action_at: Mapped[str] = mapped_column(String, nullable=False)


class AuditRecord(RowAccess, Base):
    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String, nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)


class LegacyCallRecord(LegacyBase):
    __tablename__ = "call_records"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String)
    ts: Mapped[str] = mapped_column(String)
    config_version: Mapped[int] = mapped_column(Integer)
    cohort_id: Mapped[str] = mapped_column(String)
    variant: Mapped[str] = mapped_column(Text)
    connected: Mapped[int] = mapped_column(Integer)
    engaged: Mapped[int] = mapped_column(Integer)
    qa: Mapped[str | None] = mapped_column(Text)


class LegacyLeadRecord(LegacyBase):
    __tablename__ = "lead_records"

    lead_id: Mapped[str] = mapped_column(String, primary_key=True)
