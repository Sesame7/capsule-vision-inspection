"""Application facts and immutable snapshots; no physical product counting."""

from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path

from capsule_vision.configuration import (
    ResolvedInspectionConfiguration,
    validate_model_artifact,
)
from capsule_vision.inspection.production import (
    ChannelBusinessStatus,
    ProductionInspectionResult,
    ProductJudgment,
    TechnicalStatus,
)


class ApplicationError(RuntimeError):
    """Application command or lifecycle failure; causes remain chained."""


class ApplicationStateError(ApplicationError):
    """Command is invalid in the current application state."""


class ApplicationCapacityError(ApplicationError):
    """Bounded outstanding-cycle capacity reached."""


class ApplicationState(Enum):
    UNCONFIGURED = auto()
    READY = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    FAULT = auto()
    CLOSED = auto()


@dataclass(frozen=True, slots=True)
class PreparedConfiguration:
    configuration: ResolvedInspectionConfiguration
    model_path: Path
    managed_model: bool = False


def prepare_configuration(
    configuration: ResolvedInspectionConfiguration, *, base_directory: Path
) -> PreparedConfiguration:
    """Blocking file verification, performed off the future GUI thread before configure."""
    return PreparedConfiguration(
        configuration,
        validate_model_artifact(configuration.model, base_directory=base_directory),
    )


@dataclass(frozen=True, slots=True)
class ConfigurationIdentity:
    recipe_id: str
    model_id: str
    model_sha256: str

    @classmethod
    def from_prepared(cls, prepared: PreparedConfiguration) -> ConfigurationIdentity:
        config = prepared.configuration
        return cls(
            config.recipe_id, config.model.model_id, config.model.expected_sha256
        )


@dataclass(frozen=True, slots=True)
class ApplicationIssue:
    stage: str
    code: str
    exception_type: str
    message: str


@dataclass(frozen=True, slots=True)
class ProductionCounters:
    cycles_started: int = 0
    cycles_completed: int = 0
    channel_ok: int = 0
    channel_ng: int = 0
    channel_unknown: int = 0
    channel_disabled: int = 0
    production_ng_cycles: int = 0
    technical_error_cycles: int = 0
    business_ok: int = 0
    business_ng: int = 0
    business_empty: int = 0
    business_unknown: int = 0

    def admitted(self) -> ProductionCounters:
        return replace(self, cycles_started=self.cycles_started + 1)

    def completed(self, result: ProductionInspectionResult) -> ProductionCounters:
        if self.cycles_completed >= self.cycles_started:
            raise ApplicationError("Cannot complete an unadmitted cycle")
        ok = ng = unknown = disabled = 0
        business = dict.fromkeys(ChannelBusinessStatus, 0)
        for channel in result.channels:
            business[channel.business_status] += 1
            assessment = channel.assessment
            if assessment.technical_status is TechnicalStatus.DISABLED:
                disabled += 1
            elif assessment.judgment is ProductJudgment.OK:
                ok += 1
            elif assessment.judgment is ProductJudgment.NG:
                ng += 1
            else:
                unknown += 1
        assessment = result.assessment
        return replace(
            self,
            cycles_completed=self.cycles_completed + 1,
            channel_ok=self.channel_ok + ok,
            channel_ng=self.channel_ng + ng,
            channel_unknown=self.channel_unknown + unknown,
            channel_disabled=self.channel_disabled + disabled,
            production_ng_cycles=self.production_ng_cycles
            + int(assessment.judgment is ProductJudgment.NG),
            technical_error_cycles=self.technical_error_cycles
            + int(assessment.technical_status is TechnicalStatus.ERROR),
            business_ok=self.business_ok + business[ChannelBusinessStatus.OK],
            business_ng=self.business_ng + business[ChannelBusinessStatus.NG],
            business_empty=self.business_empty + business[ChannelBusinessStatus.EMPTY],
            business_unknown=self.business_unknown
            + business[ChannelBusinessStatus.UNKNOWN],
        )


@dataclass(frozen=True, slots=True)
class ProductionApplicationSnapshot:
    state: ApplicationState
    generation: int
    active: ConfigurationIdentity | None
    candidate: ConfigurationIdentity | None
    batch_id: str | None
    counters: ProductionCounters
    latest_production_result: ProductionInspectionResult | None
    runtime_owned: bool
    last_issue: ApplicationIssue | None
    active_prepared: PreparedConfiguration | None = None
    candidate_prepared: PreparedConfiguration | None = None

    @property
    def has_pending_configuration(self) -> bool:
        return (
            self.active_prepared is not None
            and self.candidate_prepared is not None
            and self.active_prepared.configuration
            != self.candidate_prepared.configuration
        )
