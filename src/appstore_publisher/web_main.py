"""Entry point for the AppStore Publisher Web GUI.

Usage:
    python -m appstore_publisher.web_main
    python -m appstore_publisher.web_main --port 8080
    python -m appstore_publisher.web_main --host 0.0.0.0 --port 8080
"""

import argparse
import sys

from .web.server import run_server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AppStore Publisher Web GUI",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=8580,
        help="Port to listen on (default: 8580)",
    )

    args = parser.parse_args()
    run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
