import pytest

from oms_hub.task_scheduler_evidence import verify_scheduler_restart

TASK = r"\OMS Study Hub V2"
ACTION = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def _event(
    record_id: int,
    event_id: int,
    *,
    system_time: str = "2026-08-10T19:20:00.0000000Z",
    **data: object,
) -> dict[str, object]:
    fields = "".join(
        f'<Data Name="{name}">{value}</Data>' for name, value in data.items()
    )
    return {
        "event_record_id": record_id,
        "xml": (
            '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">'
            f"<System><EventID>{event_id}</EventID><EventRecordID>{record_id}</EventRecordID>"
            f'<TimeCreated SystemTime="{system_time}"/>'
            f"</System><EventData>{fields}</EventData></Event>"
        ),
    }


def _valid_events() -> list[dict[str, object]]:
    old = "11111111-1111-1111-1111-111111111111"
    replacement = "22222222-2222-2222-2222-222222222222"
    return [
        _event(11, 201, TaskName=TASK, TaskInstanceId=old, ActionName=ACTION, ResultCode="75"),
        _event(12, 100, TaskName=TASK, InstanceId=replacement),
        _event(13, 200, TaskName=TASK, TaskInstanceId=replacement, ActionName=ACTION),
        _event(
            14,
            129,
            system_time="2026-08-10T19:20:03.2500000Z",
            TaskName=TASK,
            Path=ACTION,
            ProcessID="700",
        ),
    ]


def _processes() -> list[dict[str, object]]:
    return [
        {
            "pid": 700,
            "parent_pid": 4,
            "name": "powershell.exe",
            "executable_path": ACTION,
            "creation_date": "2026-08-10T19:20:03.1250000Z",
        },
        {
            "pid": 701,
            "parent_pid": 700,
            "name": "python.exe",
            "executable_path": r"C:\Services\oms-study-automation-v2\.venv\Scripts\python.exe",
        },
        {
            "pid": 702,
            "parent_pid": 701,
            "name": "oms-hub.exe",
            "executable_path": r"C:\Services\oms-study-automation-v2\.venv\Scripts\oms-hub.exe",
        },
    ]


def test_scheduler_verifier_proves_one_automatic_restart_and_parent_chain() -> None:
    proof = verify_scheduler_restart(
        events=_valid_events(),
        cursor_event_record_id=10,
        full_task_name=TASK,
        action_path=ACTION,
        replacement_hub_pid=702,
        process_snapshot=_processes(),
    )

    assert proof["old_action_event_record_id"] == 11
    assert proof["replacement_process_id"] == 700
    assert proof["replacement_hub_ancestor_pids"] == [700, 701, 702]
    assert proof["replacement_process_event_time"] == "2026-08-10T19:20:03.2500000Z"
    assert proof["replacement_process_creation_time"] == "2026-08-10T19:20:03.1250000Z"


def test_scheduler_verifier_allows_system_entry_without_executable_path() -> None:
    processes = _processes()
    processes.append({"pid": 4, "parent_pid": 0, "name": "System", "executable_path": ""})
    processes.append(
        {"pid": 0, "parent_pid": 0, "name": "System Idle Process", "executable_path": ""}
    )

    proof = verify_scheduler_restart(
        events=_valid_events(),
        cursor_event_record_id=10,
        full_task_name=TASK,
        action_path=ACTION,
        replacement_hub_pid=702,
        process_snapshot=processes,
    )

    assert proof["replacement_process_id"] == 700


@pytest.mark.parametrize(("event_id", "label"), [(107, "trigger"), (110, "manual")])
def test_scheduler_verifier_rejects_manual_or_triggered_replacement(
    event_id: int, label: str
) -> None:
    events = _valid_events()
    events.append(_event(15, event_id, TaskName=TASK))

    try:
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )
    except ValueError as error:
        assert label in str(error)
    else:
        raise AssertionError("manual launch must not satisfy automatic restart evidence")


def test_scheduler_verifier_rejects_replacement_event_before_old_failure() -> None:
    events = _valid_events()
    events.insert(
        0,
        _event(
            9,
            100,
            TaskName=TASK,
            InstanceId="33333333-3333-3333-3333-333333333333",
        ),
    )

    with pytest.raises(ValueError, match="before old"):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=8,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )


def test_scheduler_verifier_rejects_duplicate_eligible_created_process_records() -> None:
    events = _valid_events()
    events.append(_event(15, 129, TaskName=TASK, Path=ACTION, ProcessID="700"))

    with pytest.raises(ValueError, match="exactly one Event 129"):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )


def test_scheduler_verifier_rejects_mismatched_replacement_instance_guid() -> None:
    events = _valid_events()
    events[2] = _event(
        13,
        200,
        TaskName=TASK,
        TaskInstanceId="33333333-3333-3333-3333-333333333333",
        ActionName=ACTION,
    )

    with pytest.raises(ValueError, match="Event 200"):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )


def test_scheduler_verifier_rejects_a_process_not_ancestral_to_replacement_hub() -> None:
    processes = _processes()
    processes[0] = {
        "pid": 700,
        "parent_pid": 4,
        "name": "powershell.exe",
        "executable_path": ACTION,
        "creation_date": "2026-08-10T19:20:03.1250000Z",
    }
    processes[1] = {
        "pid": 701,
        "parent_pid": 4,
        "name": "python.exe",
        "executable_path": r"C:\Services\oms-study-automation-v2\.venv\Scripts\python.exe",
    }

    try:
        verify_scheduler_restart(
            events=_valid_events(),
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=processes,
        )
    except ValueError as error:
        assert "ancestor" in str(error)
    else:
        raise AssertionError("unrelated created process must not satisfy restart evidence")


def test_scheduler_verifier_rejects_ancestral_pid_with_wrong_action_executable() -> None:
    processes = _processes()
    processes[0]["executable_path"] = r"C:\Windows\System32\cmd.exe"

    try:
        verify_scheduler_restart(
            events=_valid_events(),
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=processes,
        )
    except ValueError as error:
        assert "executable" in str(error)
    else:
        raise AssertionError("ancestral PID with the wrong executable must be rejected")


def test_scheduler_verifier_rejects_reused_pid_created_after_event_129() -> None:
    processes = _processes()
    processes[0]["creation_date"] = "2026-08-10T19:20:03.5000000Z"

    with pytest.raises(ValueError, match="postdates Event 129"):
        verify_scheduler_restart(
            events=_valid_events(),
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=processes,
        )


def test_scheduler_verifier_rejects_process_created_too_early_for_event_129() -> None:
    processes = _processes()
    processes[0]["creation_date"] = "2026-08-10T19:19:00.0000000Z"

    with pytest.raises(ValueError, match="outside the Event 129 correlation window"):
        verify_scheduler_restart(
            events=_valid_events(),
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=processes,
        )
