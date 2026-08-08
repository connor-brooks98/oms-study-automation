import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from oms_hub.anki.evaluation import EvaluationDataset, evaluate_dataset


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate versioned Anki retrieval observations and release "
            "guardrails."
        )
    )
    parser.add_argument(
        "--gold",
        type=Path,
        required=True,
        help="Versioned JSON gold set with observed rankings and timings.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Write the machine-readable report here instead of stdout.",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        help="Write the concise report here instead of stderr.",
    )
    parser.add_argument(
        "--require-release-ready",
        action="store_true",
        help="Also fail unless copied-profile acceptance passes.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        dataset = EvaluationDataset.model_validate_json(
            arguments.gold.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        print(f"Invalid evaluation set: {exc}", file=sys.stderr)
        return 2

    report = evaluate_dataset(dataset)
    json_report = report.model_dump_json(indent=2) + "\n"
    markdown_report = report.to_markdown()

    if arguments.json_out is None:
        sys.stdout.write(json_report)
    else:
        arguments.json_out.parent.mkdir(parents=True, exist_ok=True)
        arguments.json_out.write_text(json_report, encoding="utf-8")
    if arguments.markdown_out is None:
        sys.stderr.write(markdown_report)
    else:
        arguments.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        arguments.markdown_out.write_text(
            markdown_report,
            encoding="utf-8",
        )

    if report.gates.automated.status != "pass":
        return 1
    if arguments.require_release_ready and not report.gates.release_ready:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
