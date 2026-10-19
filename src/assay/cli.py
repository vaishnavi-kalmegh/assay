"""The `assay` command line: export, run and map."""

from __future__ import annotations

import argparse

from assay import mapping, runner


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the right subcommand."""
    parser = argparse.ArgumentParser(
        prog="assay",
        description="Offline API test generation and requirement traceability.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    runner.register_commands(commands)
    mapping.register_commands(commands)
    arguments = parser.parse_args(argv)
    if arguments.command == "map":
        return mapping.handle(arguments)
    return runner.handle(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
