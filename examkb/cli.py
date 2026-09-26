"""The `examkb` entry point: a subcommand registry and nothing else.

Subcommands register themselves with `@subcommand(...)`, so adding one is a new
module plus an import in `_load_subcommands()` -- never an edit to the dispatch
logic below. This issue (001) shipped the skeleton and four stubs; each remaining stub names
the issue that replaces it with a real implementation.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from examkb import __version__

Runner = Callable[[argparse.Namespace], int]
Configurer = Callable[[argparse.ArgumentParser], None]


@dataclass(frozen=True)
class Command:
    """One `examkb <name>` subcommand."""

    name: str
    help: str
    run: Runner
    configure: Configurer | None = None
    aliases: tuple[str, ...] = field(default=())
    lenient: bool = False
    """Accept arguments this command does not declare, instead of erroring.

    Only stubs set this: `examkb ingest --verify` should report that ingest is not
    implemented, not fail on an unknown flag it will accept once 006 lands.
    """


_REGISTRY: dict[str, Command] = {}


def subcommand(
    name: str,
    help: str,
    *,
    configure: Configurer | None = None,
    aliases: Sequence[str] = (),
    lenient: bool = False,
) -> Callable[[Runner], Runner]:
    """Register `run` as `examkb <name>`. Returns the function unchanged."""

    def decorate(run: Runner) -> Runner:
        if name in _REGISTRY:
            raise RuntimeError(f"subcommand {name!r} is already registered")
        _REGISTRY[name] = Command(
            name=name,
            help=help,
            run=run,
            configure=configure,
            aliases=tuple(aliases),
            lenient=lenient,
        )
        return run

    return decorate


def registered() -> list[Command]:
    """Every registered subcommand, in registration order."""
    return list(_REGISTRY.values())


# --------------------------------------------------------------------------- stubs
#
# Until its owning issue lands, a subcommand is `lenient`: it accepts arguments it
# does not declare and reports that it is not implemented, rather than failing on an
# argument its issue will introduce. `db` left this section in 005, `ingest` in 006
# and `serve` in 008; each lives in `examkb/commands/`, which is the path the one
# below follows.


def _not_implemented(name: str, issue: str) -> Runner:
    def run(_args: argparse.Namespace) -> int:
        print(f"examkb {name}: not implemented -- see issues/{issue}", file=sys.stderr)
        return 1

    return run


@subcommand("doctor", "Check that this box is set up correctly.", lenient=True)
def _doctor(args: argparse.Namespace) -> int:
    return _not_implemented("doctor", "047-doctor.md")(args)


# ----------------------------------------------------------------------- dispatch


def _load_subcommands() -> None:
    """Import the modules that register subcommands.

    The one above is declared in this module because it is a stub. As each
    owning issue lands it moves its subcommand into its own module under
    `examkb/commands/`, and that module gets imported here.

    The import is inside the function, not at module scope: a subcommand module
    imports `subcommand` from this one, and doing it at the top would be a cycle.
    """
    from examkb.commands import (  # noqa: F401  -- register them
        backup,
        db,
        ingest,
        marks,
        serve,
    )


def build_parser() -> argparse.ArgumentParser:
    _load_subcommands()
    parser = argparse.ArgumentParser(
        prog="examkb",
        description="Study platform over the exam-kb corpus.",
    )
    parser.add_argument("--version", action="version", version=f"examkb {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for command in registered():
        sub = subparsers.add_parser(
            command.name,
            help=command.help,
            description=command.help,
            aliases=command.aliases,
        )
        if command.configure is not None:
            command.configure(sub)
        sub.set_defaults(_run=command.run)
    return parser


def _lenient(argv: Sequence[str]) -> bool:
    """True when the command named in `argv` tolerates undeclared arguments."""
    for token in argv:
        if token.startswith("-"):
            continue
        for command in registered():
            if token == command.name or token in command.aliases:
                return command.lenient
        return False
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if _lenient(argv):
        args, _unrecognised = parser.parse_known_args(argv)
    else:
        args = parser.parse_args(argv)
    run: Runner | None = getattr(args, "_run", None)
    if run is None:
        parser.print_help()
        return 2
    return run(args)


if __name__ == "__main__":
    # Under `python -m examkb.cli` this file runs as `__main__`, and a subcommand
    # module importing `examkb.cli` gets a *second* copy with its own registry --
    # so the subcommands register somewhere this process would never look. Dispatch
    # through the canonical module instead of the copy.
    from examkb.cli import main as canonical_main

    sys.exit(canonical_main())
