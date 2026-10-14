"""Independent loopback FINS/UDP DM/WR simulator; Ctrl+C stops and closes it."""

import argparse
from datetime import datetime
from threading import Event

from vimo_demo.communication.simulator import FinsUdpSimulator


def trace_first_station(sequence: int, words: tuple[int, int, int]) -> None:
    """Observe accepted memory writes, not client ACK receipt or PLC consumption."""
    channels = ",".join(
        str(channel)
        for channel in range(1, 37)
        if words[(channel - 1) // 16] & (1 << ((channel - 1) % 16))
    )
    timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    print(
        f"[{timestamp}] W49={sequence} "
        f"W50=0x{words[0]:04X} W51=0x{words[1]:04X} W52=0x{words[2]:04X} "
        f"reject_CH={channels or '-'}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9600, help="UDP port (0: allocate)")
    parser.add_argument(
        "--max-requests", type=int, help="Exit after this many datagrams (smoke check)"
    )
    parser.add_argument(
        "--trace-first-station",
        action="store_true",
        help="Print each accepted W49 write with current W50..W52 and reject channels",
    )
    args = parser.parse_args()
    try:
        with FinsUdpSimulator(
            port=args.port,
            on_first_station_write=trace_first_station
            if args.trace_first_station
            else None,
        ) as simulator:
            print(
                f"FINS simulator 127.0.0.1:{simulator.port}; "
                "address=0/2/0; D100=1234; W0..W511=0; Ctrl+C to stop",
                flush=True,
            )
            try:
                simulator.serve(Event(), max_requests=args.max_requests)
            except KeyboardInterrupt:
                print("Stopping simulator")
        print("Simulator socket closed", flush=True)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
