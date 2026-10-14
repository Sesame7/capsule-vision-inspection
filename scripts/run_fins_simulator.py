"""Independent loopback FINS/UDP DM simulator; Ctrl+C stops and closes it."""

import argparse
from threading import Event

from vimo_demo.communication.simulator import FinsUdpSimulator


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9600, help="UDP port (0: allocate)")
    parser.add_argument(
        "--max-requests", type=int, help="Exit after this many datagrams (smoke check)"
    )
    args = parser.parse_args()
    try:
        with FinsUdpSimulator(port=args.port) as simulator:
            print(
                f"FINS simulator 127.0.0.1:{simulator.port}; "
                "address=0/2/0; D100=1234; Ctrl+C to stop",
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
