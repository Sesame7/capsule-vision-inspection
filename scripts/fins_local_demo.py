"""Read/write/read D100 on localhost only, then show a bounded timeout."""

import argparse
import socket

from vimo_demo.communication import (
    FinsAddress,
    FinsError,
    FinsTimeoutError,
    FinsUdpClient,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9600)
    parser.add_argument(
        "--value", type=int, default=4321, help="New D100 unsigned word"
    )
    parser.add_argument("--timeout", type=float, default=1.0, help="Request seconds")
    args = parser.parse_args()
    if not 0 <= args.value <= 65535:
        parser.error("--value must be in 0..65535")
    destination, source = FinsAddress(0, 2, 0), FinsAddress(0, 1, 0)
    try:
        with FinsUdpClient(
            "127.0.0.1",
            port=args.port,
            destination=destination,
            source=source,
            timeout=args.timeout,
        ) as client:
            print(f"Read D100: {client.read_words(100)[0]}")
            client.write_words(100, (args.value,))
            print(f"Wrote D100: {args.value} (localhost)")
            observed = client.read_words(100)[0]
            print(f"Read D100: {observed}")
            if observed != args.value:
                raise FinsError("Readback differs from the written value")
        # Bound UDP socket intentionally never responds, avoiding OS-dependent ICMP.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent_peer:
            silent_peer.bind(("127.0.0.1", 0))
            with FinsUdpClient(
                "127.0.0.1",
                port=silent_peer.getsockname()[1],
                destination=destination,
                source=source,
                timeout=args.timeout,
            ) as client:
                try:
                    client.read_words(100)
                except FinsTimeoutError as exc:
                    print(f"Expected timeout: {exc}")
                else:
                    raise FinsError("Silent peer unexpectedly replied")
        print("Demo sockets closed")
    except (FinsError, ValueError, OSError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
