from __future__ import annotations

import json
import re
from copy import deepcopy
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


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(*pair) for pair in zip(left, right, strict=True)
        )
    return left == right


def _validate(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> None:
    schema = _resolve(schema, root)
    kind = schema.get("type")
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
    elif kind == "integer":
        if type(value) is not int:
            raise SchemaViolation("integer")
    elif kind is None:
        assert set(schema) == {"const"}
    else:
        raise AssertionError(f"unsupported schema type: {kind}")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise SchemaViolation("const")


def _map_with_quiz_path(path: str) -> dict[str, Any]:
    data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    data["paths"]["quiz_page_files"] = [path]
    return data


def _actual_map() -> dict[str, Any]:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def _mutate(data: dict[str, Any], name: str) -> None:
    match name:
        case "boolean_version":
            data["version"] = True
        case "wrong_version":
            data["version"] = 2
        case "missing_required":
            del data["base_sha"]
        case "unknown_root":
            data["unexpected"] = "value"
        case "unknown_paths":
            data["paths"]["unexpected"] = ["src/extra.py"]
        case "unknown_commands":
            data["commands"]["unexpected"] = "command"
        case "wrong_path_list_type":
            data["paths"]["quiz_page_files"] = "src/quiz.py"
        case "wrong_command_type":
            data["commands"]["python_lint"] = []
        case "empty_path_list":
            data["paths"]["quiz_page_files"] = []
        case "empty_path":
            data["paths"]["quiz_page_files"] = [""]
        case "empty_command":
            data["commands"]["python_lint"] = ""
        case _:
            raise AssertionError(name)


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_actual_frozen_map_validates_against_its_schema() -> None:
    _validate(json.loads(MAP_PATH.read_text(encoding="utf-8")), _schema(), _schema())


@pytest.mark.parametrize(
    "name",
    [
        "boolean_version",
        "wrong_version",
        "missing_required",
        "unknown_root",
        "unknown_paths",
        "unknown_commands",
        "wrong_path_list_type",
        "wrong_command_type",
        "empty_path_list",
        "empty_path",
        "empty_command",
    ],
)
def test_schema_rejects_compact_constraint_mutations(name: str) -> None:
    schema = _schema()
    data = deepcopy(_actual_map())
    _mutate(data, name)

    with pytest.raises(SchemaViolation):
        _validate(data, schema, schema)


def test_schema_const_does_not_treat_true_as_one() -> None:
    with pytest.raises(SchemaViolation, match="const"):
        _validate(True, {"const": 1}, {})


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
