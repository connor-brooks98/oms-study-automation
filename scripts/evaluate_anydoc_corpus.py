"""Read-only Anydoc comparison reports for a local lecture-document corpus."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from oms_hub.document_processing.anydoc_adapter import AnydocProcessor
from oms_hub.document_processing.domain import SourceSnapshot
from oms_hub.document_processing.pptx_locator import PptxLocatorEnricher
from oms_hub.document_processing.shadow import DocumentShadowEvaluator, LegacyPptxProcessor

_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".epub": "application/epub+zip",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "application/rtf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def evaluate_corpus(root: Path, output: Path) -> int:
    evaluator = DocumentShadowEvaluator(
        AnydocProcessor(PptxLocatorEnricher()),
        LegacyPptxProcessor(),
    )
    reports: list[dict[str, object]] = []
    blockers: list[str] = []
    with TemporaryDirectory(prefix="oms-anydoc-corpus-") as temporary_directory:
        asset_root = Path(temporary_directory)
        for path in sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(root).as_posix().casefold(),
        ):
            media_type = _MEDIA_TYPES.get(path.suffix.casefold())
            if media_type is None:
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot = SourceSnapshot(
                id=digest[:24],
                title=path.name,
                path=path,
                media_type=media_type,
                sha256=digest,
            )
            comparison = evaluator.compare(snapshot, asset_root / digest)
            report = dict(comparison.report)
            report["source"] = path.relative_to(root).as_posix()
            reports.append(report)
            blockers.extend(
                f"{report['source']}: {blocker}"
                for blocker in cast(tuple[str, ...], report["promotion_blockers"])
            )
    aggregate: dict[str, object] = {
        "files": reports,
        "promotion_blockers": tuple(sorted(blockers)),
        "root_file_count": len(reports),
    }
    DocumentShadowEvaluator.write_report(aggregate, output)
    return 1 if blockers else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Read-only corpus root")
    parser.add_argument("--output", type=Path, required=True, help="Aggregate JSON report")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        parser.error("--root must be an existing directory")
    return evaluate_corpus(args.root, args.output)


if __name__ == "__main__":
    sys.exit(main())
