import argparse
from collections.abc import Sequence

from oms_anki_agent import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oms-anki-agent")
    parser.add_argument("--version", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"oms-anki-agent {__version__}")
        return 0
    build_parser().print_help()
    return 0


def main() -> None:
    raise SystemExit(run())
