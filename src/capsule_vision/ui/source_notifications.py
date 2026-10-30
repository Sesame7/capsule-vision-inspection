"""Queued Qt delivery of source completion failures."""

import traceback

from PySide6.QtCore import QObject, Qt, Slot

from .future_delivery import FutureDelivery
from .production_bridge import QtProductionBridge


class SourceNotifications(QObject):
    """Source Future -> existing queued Qt command-error presentation."""

    def __init__(self, bridge: QtProductionBridge, delivery: FutureDelivery) -> None:
        super().__init__(bridge)
        self._bridge = bridge
        delivery.emitter.completed.connect(
            self.completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(delivery.detach, Qt.ConnectionType.DirectConnection)

    @Slot(str, object, object)
    def completed(self, name: str, _value: object, error: object) -> None:
        if isinstance(error, BaseException):
            traceback.print_exception(error)
            self._bridge.command_failed.emit(name, error)
