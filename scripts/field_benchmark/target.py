"""Thin ownership wrapper for the maintained localhost UDP simulator."""

from threading import Event, Thread

from vimo_demo.communication.reporting import LocalReportingSettings
from vimo_demo.communication.simulator import FinsUdpSimulator

from .observations import Observations


class IsolatedTarget:
    def __init__(self, observe: Observations, pulse_budget: int) -> None:
        if type(pulse_budget) is not int or not 1 <= pulse_budget <= 40000:
            raise ValueError("Finite isolated target request budget required")
        self._observe = observe
        self._stop = Event()
        self._server = FinsUdpSimulator(port=0, on_first_station_write=observe.target)
        self.settings = LocalReportingSettings("127.0.0.1", self._server.port)
        self._thread = Thread(
            target=self._server.serve,
            args=(self._stop,),
            kwargs={"max_requests": 3 * pulse_budget + 4},
            name="BenchmarkIsolatedUdpTarget",
        )
        self._thread.start()

    def close(self, timeout: float = 3) -> bool:
        self._stop.set()
        self._thread.join(timeout)
        terminated = not self._thread.is_alive()
        if terminated:
            self._server.close()
        self._observe.owner("target", terminated, terminated and self._server.closed)
        return terminated and self._server.closed
