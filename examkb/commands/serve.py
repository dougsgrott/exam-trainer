"""`examkb serve` -- the web app on a socket this command bound itself.

The work is in `examkb/web/serve.py`; what lives here is argument parsing, the
line that tells you the URL, and an exit code. Two flags are deliberately absent:

- **`--reload`.** uvicorn's reloader restarts the process on every file write,
  and from 030 this process runs backfills measured in hours. A reloader that
  silently kills them is a worse default than restarting by hand.
- **anything that widens the bind.** `--host` exists only so that asking for a
  routable address gets an explanation instead of an argparse error; it cannot
  produce one. That is decision D1.
"""

from __future__ import annotations

import argparse
import sys

from examkb.cli import subcommand
from examkb.web import serve as serve_module


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--host",
        default=serve_module.DEFAULT_HOST,
        help="Loopback address to bind. Anything else is refused (decision D1).",
    )
    parser.add_argument(
        "--port", type=int, default=serve_module.DEFAULT_PORT, help="Port to bind."
    )
    parser.add_argument(
        "--log-level", default="info", help="uvicorn log level: critical|error|warning|info|debug."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Bind, report the address, and exit without serving.",
    )


@subcommand("serve", "Run the web app on 127.0.0.1.", configure=configure)
def run(args: argparse.Namespace) -> int:
    try:
        bound = serve_module.bind_or_explain(args.host, args.port)
    except serve_module.BindRefused as error:
        print(f"examkb serve: {error}", file=sys.stderr)
        return 1

    print(f"examkb serve: {bound.url}", flush=True)
    if args.check:
        # The bind check without the server: what `make doctor` and a test want.
        bound.close()
        return 0

    try:
        serve_module.run(bound, log_level=args.log_level)
    except KeyboardInterrupt:  # pragma: no cover -- Ctrl-C is not a failure
        pass
    finally:
        bound.close()
    return 0
