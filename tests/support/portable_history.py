"""Build a closed, old History package from the real writer, using synthetic facts."""

from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from shutil import copyfile

from capsule_vision.application import ConfigurationIdentity
from capsule_vision.application.alarm_records import AlarmEvent
from capsule_vision.application.archive_links import PublishedImage
from capsule_vision.application.batch_review import BatchInformation, StartAccount
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.image_archive import ArchiveImage, write_png
from capsule_vision.inspection import Frame
from tests.support.application import WAIT
from tests.support.business import history_review, mixed_result


@dataclass(frozen=True, slots=True)
class CopiedHistory:
    root: Path
    original_database: Path
    original_image: Path
    image: Path
    batch: str

    @property
    def database(self) -> Path:
        return self.root / "history.sqlite3"


def copied_history(workspace: Path) -> CopiedHistory:
    root = workspace / "Copied History 测试 #1"
    root.mkdir()
    original_root = workspace / "equipment" / "line" / "archive"
    original_database = workspace / "equipment" / "history.sqlite3"
    result, image = history_review(workspace)
    camera = result.cameras[1]
    assert camera.frame_id is not None
    old = (
        (datetime.now() - timedelta(days=45))
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    observed = old - 1
    owner = ProductionHistory(original_database, clock=lambda: observed)

    def authorize(_label: str, _information: BatchInformation) -> nullcontext[None]:
        return nullcontext()

    try:
        idle = owner.select_batch("Idle lot").result(WAIT)
        assert idle is not None
        setup = owner.batch_setup("Copied lot").result(WAIT)
        batch = owner.setup_batch(
            "Copied lot",
            setup,
            BatchInformation("Training capsules", "Size 0", "Synthetic records"),
            authorize,
        ).result(WAIT)
        assert batch is not None
        context = HistoryContext(
            old,
            batch.uuid,
            batch.label,
            "copied-run",
            ConfigurationIdentity("saved-recipe", "saved-model", "c" * 64),
            "Recorded selection",
        )
        owner.start_run(
            batch.uuid,
            context.run_id,
            context.active,
            None,
            StartAccount("test-account", "Recorded operator"),
        ).result(WAIT)
        assert owner.offer(context, result)
        for cycle in range(2, 54):
            assert owner.offer(
                replace(context, completed_at=old + cycle), mixed_result(cycle)
            )
        observed = old + 60
        owner.finish_run(context.run_id, "stopped", False).result(WAIT)
        # One open Run is stored evidence, even when this copy no longer has an owner.
        owner.start_run(batch.uuid, "open-run", context.active, None).result(WAIT)
        alarm = AlarmEvent(
            old,
            "low_air",
            "observed_active",
            "equipment",
            "气压低",
            "Low air pressure",
            "fins_tcp://192.0.2.7:9600",
            2,
            118,
            5,
            "Recorded definitions fixture",
            batch.uuid,
            batch.label,
            translations=(("de", "Niedriger Luftdruck"),),
        )
        idle_day = old - 86400
        owner.offer_alarms(
            (
                AlarmEvent(
                    idle_day - 1,
                    "application_fault",
                    "activated",
                    "software",
                    "检测故障",
                    "Inspection fault",
                    "application",
                    None,
                    None,
                    None,
                    "fixture",
                    None,
                    None,
                    "runtime · TimeoutError",
                ),
                replace(
                    alarm,
                    observed=idle_day,
                    signal="feed_latch",
                    state="observed_recovered",
                    category="interlock",
                    zh="进料锁止已解除",
                    en="Feed latch cleared",
                    translations=(),
                    batch_id=idle.uuid,
                    batch_label=idle.label,
                ),
            )
        )
        states = ("observed_active", "recovered", "observed_recovered", "activated")
        for index in range(53):
            state = states[index % len(states)]
            owner.offer_alarms(
                (
                    replace(
                        alarm,
                        observed=old + index,
                        state=state,
                        detail="First observation after a communication gap"
                        if state.startswith("observed")
                        else "",
                    ),
                )
            )
        original_image = write_png(
            original_root,
            ArchiveImage(
                1,
                Frame(camera.frame_id, 0, image),
                datetime.fromtimestamp(old),
                1,
                context.run_id,
            ),
        )
        owner.offer_image(
            PublishedImage(context.run_id, 1, "CAM2", original_image, old)
        )
        owner.offer_image(
            PublishedImage(context.run_id, 1, "CAM3", original_image, old)
        )
    finally:
        owner.request_stop()
        assert owner.join(WAIT)
    assert owner.snapshot().persisted == 53 and owner.snapshot().images_linked == 2
    assert owner.alarm_storage() == (0, "")
    relocated = root / original_image.relative_to(original_root)
    relocated.parent.mkdir(parents=True)
    copyfile(original_image, relocated)
    copyfile(original_database, root / "history.sqlite3")
    return CopiedHistory(root, original_database, original_image, relocated, batch.uuid)
