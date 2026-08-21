from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
MAP_PATH = ROOT / "artifacts" / "implementation" / "repo-map-v1.json"
SCHEMA_PATH = ROOT / "artifacts" / "implementation" / "repo-map-v1.schema.json"


class SchemaViolation(ValueError):
    pass


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if reference is None:
        return schema
    assert reference.startswith("#/$defs/")
    return root["$defs"][reference.removeprefix("#/$defs/")]


def _validate(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> None:
    schema = _resolve(schema, root)
    if "const" in schema and value != schema["const"]:
        raise SchemaViolation("const")
    kind = schema.get("type")
    if kind is None:
        assert set(schema) == {"const"}
        return
    if kind == "object":
        if not isinstance(value, dict):
            raise SchemaViolation("object")
        required = set(schema.get("required", []))
        if not required <= set(value):
            raise SchemaViolation("required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise SchemaViolation("additionalProperties")
        for key, item_schema in properties.items():
            if key in value:
                _validate(value[key], item_schema, root)
    elif kind == "array":
        if not isinstance(value, list) or len(value) < schema.get("minItems", 0):
            raise SchemaViolation("array")
        for item in value:
            _validate(item, schema["items"], root)
    elif kind == "string":
        if not isinstance(value, str) or len(value) < schema.get("minLength", 0):
            raise SchemaViolation("string")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise SchemaViolation("pattern")
    else:
        raise AssertionError(f"unsupported schema type: {kind}")


def _map_with_quiz_path(path: str) -> dict[str, Any]:
    data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    data["paths"]["quiz_page_files"] = [path]
    return data


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_actual_frozen_map_validates_against_its_schema() -> None:
    _validate(json.loads(MAP_PATH.read_text(encoding="utf-8")), _schema(), _schema())


@pytest.mark.parametrize("path", ["src/oms_hub/app.py", "docs/with spaces/file.md"])
def test_schema_accepts_normalized_repository_relative_paths(path: str) -> None:
    schema = _schema()
    _validate(_map_with_quiz_path(path), schema, schema)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/map.json",
        "C:/temp/map.json",
        "C:\\temp\\map.json",
        "\\\\server\\share\\map.json",
        "../map.json",
        "src/../map.json",
    ],
)
def test_schema_rejects_absolute_and_traversal_paths(path: str) -> None:
    schema = _schema()

    with pytest.raises(SchemaViolation, match="pattern"):
        _validate(_map_with_quiz_path(path), schema, schema)
