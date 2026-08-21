#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

MAP = Path(__file__).resolve().parents[1] / "artifacts" / "implementation" / "repo-map-v1.json"


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: scripts/frozen_paths.py PATH_KEY")
    try:
        data = json.loads(MAP.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("unable to read frozen repository map") from exc
    key = sys.argv[1]
    try:
        paths = data["paths"][key]
    except KeyError as exc:
        raise SystemExit(f"unknown frozen path key: {key}") from exc
    if not isinstance(paths, list) or not paths:
        raise SystemExit(f"frozen path key has no paths: {key}")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
