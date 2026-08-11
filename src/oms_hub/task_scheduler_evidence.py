"""Fail-closed verification for F28 Task Scheduler operational XML evidence."""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import tempfile
import uuid
import xml.etree.ElementTree as element_tree
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

_MAX_EVENT_PROCESS_LAG = timedelta(seconds=30)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _strict_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _strict_decimal(value: str | None, label: str, *, minimum: int = 0) -> int:
    if value is None or not value.isascii() or not value.isdecimal():
        raise ValueError(f"{label} must be an unsigned decimal integer")
    parsed = int(value)
    if parsed < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return parsed


def _canonical_guid(value: str | None, label: str) -> str:
    if value is None:
        raise ValueError(f"{label} is missing")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{label} is not a GUID") from error
    canonical = str(parsed)
    if value.casefold() not in {canonical, "{" + canonical + "}"}:
        raise ValueError(f"{label} is not a canonical GUID")
    return canonical


def _canonical_windows_path(value: str, label: str) -> str:
    if not value or not ntpath.isabs(value):
        raise ValueError(f"{label} must be an absolute Windows path")
    return ntpath.normcase(ntpath.normpath(value))


def _utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{label} must be an ISO 8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO 8601 UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be an ISO 8601 UTC timestamp")
    return parsed.astimezone(UTC)


def _parse_event(raw_event: Mapping[str, object]) -> dict[str, object]:
    record_id = _strict_int(raw_event.get("event_record_id"), "event_record_id", minimum=1)
    xml = raw_event.get("xml")
    if not isinstance(xml, str):
        raise ValueError("event XML must be a string")
    try:
        root = element_tree.fromstring(xml)
    except element_tree.ParseError as error:
        raise ValueError("event XML is invalid") from error

    system_values: dict[str, str] = {}
    fields: dict[str, str] = {}
    system_time: str | None = None
    for element in root.iter():
        name = _local_name(element.tag)
        if name in {"EventID", "EventRecordID"}:
            system_values[name] = element.text or ""
        if name == "Data":
            field_name = element.attrib.get("Name")
            if field_name is None or field_name in fields:
                raise ValueError("event XML has invalid EventData fields")
            fields[field_name] = element.text or ""
        if name == "TimeCreated":
            if system_time is not None or "SystemTime" not in element.attrib:
                raise ValueError("event XML has invalid TimeCreated metadata")
            system_time = element.attrib["SystemTime"]
    event_id = _strict_decimal(system_values.get("EventID"), "EventID", minimum=1)
    xml_record_id = _strict_decimal(
        system_values.get("EventRecordID"), "EventRecordID", minimum=1
    )
    if xml_record_id != record_id:
        raise ValueError("event record identifier differs from EventRecordID XML")
    _utc_timestamp(system_time, "TimeCreated SystemTime")
    return {
        "record_id": record_id,
        "event_id": event_id,
        "system_time": system_time,
        "fields": fields,
    }


def _event_field(event: Mapping[str, object], name: str) -> str | None:
    fields = cast(dict[str, str], event["fields"])
    return fields.get(name)


def _event_record_id(event: Mapping[str, object]) -> int:
    return cast(int, event["record_id"])


def _event_id(event: Mapping[str, object]) -> int:
    return cast(int, event["event_id"])


def _event_system_time(event: Mapping[str, object]) -> str:
    return cast(str, event["system_time"])


def _task_events_after_cursor(
    events: Sequence[Mapping[str, object]], cursor_event_record_id: int, full_task_name: str
) -> list[dict[str, object]]:
    parsed = [_parse_event(event) for event in events]
    record_ids = [_event_record_id(event) for event in parsed]
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("Task Scheduler evidence contains duplicate record identifiers")
    if record_ids != sorted(record_ids):
        raise ValueError("Task Scheduler evidence is not ordered by EventRecordID")
    return [
        event
        for event in parsed
        if _event_record_id(event) > cursor_event_record_id
        and _event_field(event, "TaskName") == full_task_name
    ]


def _replacement_ancestor_chain(
    process_snapshot: Sequence[Mapping[str, object]],
    replacement_process_id: int,
    replacement_hub_pid: int,
    expected_action: str,
    created_process_event_time: str,
) -> tuple[list[int], str]:
    parents: dict[int, int] = {}
    replacement_executable_path: str | None = None
    replacement_creation_date: str | None = None
    for process in process_snapshot:
        pid = _strict_int(process.get("pid"), "process pid", minimum=0)
        parent_pid = _strict_int(process.get("parent_pid"), "process parent_pid", minimum=0)
        if pid in parents:
            raise ValueError("process snapshot contains duplicate PIDs")
        parents[pid] = parent_pid
        if pid == replacement_process_id:
            executable_path = process.get("executable_path")
            if not isinstance(executable_path, str):
                raise ValueError("Task Scheduler process executable_path must be a string")
            replacement_executable_path = _canonical_windows_path(
                executable_path, "Task Scheduler process executable_path"
            )
            creation_date = process.get("creation_date")
            if not isinstance(creation_date, str):
                raise ValueError("Task Scheduler process creation_date must be a string")
            replacement_creation_date = creation_date
    if replacement_process_id not in parents or replacement_hub_pid not in parents:
        raise ValueError("scheduler process is absent from the same-snapshot process list")
    if replacement_executable_path != expected_action:
        raise ValueError("Task Scheduler created process executable differs from exact action path")
    event_time = _utc_timestamp(created_process_event_time, "Event 129 SystemTime")
    process_time = _utc_timestamp(
        replacement_creation_date, "Task Scheduler process creation_date"
    )
    if process_time > event_time:
        raise ValueError("Task Scheduler process creation postdates Event 129")
    if event_time - process_time > _MAX_EVENT_PROCESS_LAG:
        raise ValueError(
            "Task Scheduler process creation is outside the Event 129 correlation window"
        )
    chain = [replacement_hub_pid]
    seen = {replacement_hub_pid}
    while chain[-1] in parents and parents[chain[-1]] != 0:
        parent = parents[chain[-1]]
        if parent in seen:
            raise ValueError("process snapshot contains a parent cycle")
        chain.append(parent)
        seen.add(parent)
    if replacement_process_id not in chain:
        raise ValueError("Task Scheduler created process is not an ancestor of replacement oms-hub")
    return (
        list(reversed(chain[: chain.index(replacement_process_id) + 1])),
        cast(str, replacement_creation_date),
    )


def verify_scheduler_restart(
    *,
    events: Sequence[Mapping[str, object]],
    cursor_event_record_id: int,
    full_task_name: str,
    action_path: str,
    replacement_hub_pid: int,
    process_snapshot: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Return normalized proof only for one unambiguous automatic task restart."""
    cursor = _strict_int(cursor_event_record_id, "cursor_event_record_id", minimum=0)
    if not full_task_name.startswith("\\"):
        raise ValueError("full_task_name must be an exact full Task Scheduler path")
    expected_action = _canonical_windows_path(action_path, "action_path")
    hub_pid = _strict_int(replacement_hub_pid, "replacement_hub_pid", minimum=1)
    task_events = _task_events_after_cursor(events, cursor, full_task_name)
    if not task_events:
        raise ValueError("no exact task events occurred after the event cursor")
    forbidden = [event for event in task_events if _event_id(event) in {107, 110}]
    if forbidden:
        labels = {107: "trigger", 110: "manual"}
        raise ValueError(f"{labels[_event_id(forbidden[0])]} launch occurred after event cursor")

    old_actions = [event for event in task_events if _event_id(event) == 201]
    if len(old_actions) != 1:
        raise ValueError("exactly one old Event 201 action-completed failure is required")
    old_action = old_actions[0]
    if (
        _strict_decimal(_event_field(old_action, "ResultCode"), "ResultCode") != 75
        or _canonical_windows_path(_event_field(old_action, "ActionName") or "", "ActionName")
        != expected_action
    ):
        raise ValueError("old Event 201 does not match exact failure action")
    old_instance = _canonical_guid(_event_field(old_action, "TaskInstanceId"), "old TaskInstanceId")
    failure_record_id = _event_record_id(old_action)

    replacement_event_ids = {100, 200, 129}
    reordered = [
        event
        for event in task_events
        if _event_id(event) in replacement_event_ids
        and _event_record_id(event) < failure_record_id
    ]
    if reordered:
        raise ValueError("replacement task evidence occurred before old action failure")
    start_events = [event for event in task_events if _event_id(event) == 100]
    if len(start_events) != 1:
        raise ValueError("exactly one replacement Event 100 is required after the old failure")
    replacement_start = start_events[0]
    replacement_instance = _canonical_guid(
        _event_field(replacement_start, "InstanceId"), "replacement InstanceId"
    )
    if replacement_instance == old_instance:
        raise ValueError("replacement task instance must differ from old failed instance")

    replacement_actions = [event for event in task_events if _event_id(event) == 200]
    if len(replacement_actions) != 1:
        raise ValueError("exactly one replacement Event 200 action is required")
    replacement_action = replacement_actions[0]
    if (
        _canonical_guid(
            _event_field(replacement_action, "TaskInstanceId"), "Event 200 TaskInstanceId"
        )
        != replacement_instance
        or _canonical_windows_path(
            _event_field(replacement_action, "ActionName") or "", "ActionName"
        )
        != expected_action
    ):
        raise ValueError("replacement Event 200 does not match exact task action")

    created_processes = [event for event in task_events if _event_id(event) == 129]
    if len(created_processes) != 1:
        raise ValueError("exactly one Event 129 CreatedTaskProcess is required")
    created_process = created_processes[0]
    if (
        _canonical_windows_path(_event_field(created_process, "Path") or "", "Event 129 Path")
        != expected_action
    ):
        raise ValueError("Event 129 does not match exact task action")
    created_pid = _strict_decimal(
        _event_field(created_process, "ProcessID"), "Event 129 ProcessID", minimum=1
    )
    created_process_event_time = _event_system_time(created_process)
    ancestor_pids, created_process_creation_time = _replacement_ancestor_chain(
        process_snapshot,
        created_pid,
        hub_pid,
        expected_action,
        created_process_event_time,
    )
    return {
        "schema_version": 1,
        "event_cursor_record_id": cursor,
        "full_task_name": full_task_name,
        "action_path": action_path,
        "old_action_event_record_id": failure_record_id,
        "old_task_instance_id": old_instance,
        "replacement_start_event_record_id": _event_record_id(replacement_start),
        "replacement_action_event_record_id": _event_record_id(replacement_action),
        "replacement_task_instance_id": replacement_instance,
        "replacement_process_event_record_id": _event_record_id(created_process),
        "replacement_process_event_time": created_process_event_time,
        "replacement_process_id": created_pid,
        "replacement_process_creation_time": created_process_creation_time,
        "replacement_hub_pid": hub_pid,
        "replacement_hub_ancestor_pids": ancestor_pids,
    }


def _read_input(path: Path) -> Mapping[str, object]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("scheduler evidence input cannot be read") from error
    if not isinstance(value, dict):
        raise ValueError("scheduler evidence input must be an object")
    return cast(Mapping[str, object], value)


def _write_new_json(path: Path, value: Mapping[str, object]) -> None:
    if path.exists():
        raise ValueError("scheduler evidence output already exists")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(encoded)
        temporary_path = Path(temporary.name)
    try:
        os.link(temporary_path, path)
    except FileExistsError as error:
        raise ValueError("scheduler evidence output already exists") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--full-task-name", required=True)
    parser.add_argument("--action-path", required=True)
    parser.add_argument("--replacement-hub-pid", type=int, required=True)
    arguments = parser.parse_args()
    payload = _read_input(arguments.input)
    events = payload.get("events")
    process_snapshot = payload.get("process_snapshot")
    if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
        raise ValueError("scheduler evidence events must be an array of objects")
    if not isinstance(process_snapshot, list) or not all(
        isinstance(item, dict) for item in process_snapshot
    ):
        raise ValueError("scheduler evidence process_snapshot must be an array of objects")
    proof = verify_scheduler_restart(
        events=cast(list[Mapping[str, object]], events),
        cursor_event_record_id=_strict_int(
            payload.get("cursor_event_record_id"), "cursor_event_record_id", minimum=0
        ),
        full_task_name=arguments.full_task_name,
        action_path=arguments.action_path,
        replacement_hub_pid=arguments.replacement_hub_pid,
        process_snapshot=cast(list[Mapping[str, object]], process_snapshot),
    )
    _write_new_json(arguments.output, proof)


if __name__ == "__main__":
    main()
