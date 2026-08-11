import inspect

import pytest

from oms_hub.task_scheduler_evidence import verify_scheduler_restart as _verify_scheduler_restart

TASK = r"\OMS Study Hub V2"
ACTION = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
PRIMARY_ACTION_ID = "f28-primary-0"
RECOVERY_ACTION_ID = "f28-recovery-1"
RECOVERY_ARGUMENTS = '-NoProfile -File "C:\\Services\\restart-hub-after-failure.ps1" -ActionIndex 1'


def verify_scheduler_restart(**kwargs: object) -> dict[str, object]:
    kwargs.setdefault("recovery_action_arguments", RECOVERY_ARGUMENTS)
    return _verify_scheduler_restart(**kwargs)  # type: ignore[arg-type]


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
    return [
        _event(
            11,
            201,
            TaskName=TASK,
            TaskInstanceId=old,
            ActionName=PRIMARY_ACTION_ID,
            ResultCode="2147942475",
        ),
        _event(
            12,
            200,
            TaskName=TASK,
            TaskInstanceId=old,
            ActionName=RECOVERY_ACTION_ID,
            EnginePID="700",
        ),
        _event(
            13,
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
            "command_line": (
                'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe '
                '-NoProfile -File "C:\\Services\\restart-hub-after-failure.ps1" -ActionIndex 1'
            ),
        },
        {
            "pid": 701,
            "parent_pid": 700,
            "name": "python.exe",
            "executable_path": r"C:\Services\oms-study-automation-v2\.venv\Scripts\python.exe",
            "creation_date": "2026-08-10T19:20:03.1250000Z",
            "command_line": "python.exe -m oms_hub",
        },
        {
            "pid": 702,
            "parent_pid": 701,
            "name": "oms-hub.exe",
            "executable_path": r"C:\Services\oms-study-automation-v2\.venv\Scripts\oms-hub.exe",
            "creation_date": "2026-08-10T19:20:03.1250000Z",
            "command_line": "oms-hub.exe serve",
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
    assert proof["old_action_id"] == PRIMARY_ACTION_ID
    assert proof["replacement_action_id"] == RECOVERY_ACTION_ID
    assert proof["replacement_process_id"] == 700
    assert proof["replacement_hub_ancestor_pids"] == [700, 701, 702]
    assert proof["replacement_process_event_time"] == "2026-08-10T19:20:03.2500000Z"
    assert proof["replacement_process_creation_time"] == "2026-08-10T19:20:03.1250000Z"


def test_scheduler_verifier_requires_exact_recovery_arguments_api() -> None:
    parameter = inspect.signature(_verify_scheduler_restart).parameters[
        "recovery_action_arguments"
    ]
    assert parameter.default is inspect.Parameter.empty


def test_scheduler_verifier_requires_the_native_exit_75_hresult_not_a_low_word() -> None:
    """Task Scheduler records PowerShell's exit 75 as 0x8007004B, not 75."""
    events = _valid_events()
    events[0] = _event(
        11,
        201,
        TaskName=TASK,
        TaskInstanceId="11111111-1111-1111-1111-111111111111",
        ActionName=PRIMARY_ACTION_ID,
        ResultCode="2147942475",
    )

    proof = verify_scheduler_restart(
        events=events,
        cursor_event_record_id=10,
        full_task_name=TASK,
        action_path=ACTION,
        replacement_hub_pid=702,
        process_snapshot=_processes(),
    )

    assert proof["old_result_code"] == 2147942475


def test_scheduler_verifier_allows_native_129_then_200_recovery_order() -> None:
    events = _valid_events()
    events[1] = _event(
        12,
        129,
        system_time="2026-08-10T19:20:03.2500000Z",
        TaskName=TASK,
        Path=ACTION,
        ProcessID="700",
    )
    events[2] = _event(
        13,
        200,
        TaskName=TASK,
        TaskInstanceId="11111111-1111-1111-1111-111111111111",
        ActionName=RECOVERY_ACTION_ID,
        EnginePID="700",
    )

    proof = verify_scheduler_restart(
        events=events,
        cursor_event_record_id=10,
        full_task_name=TASK,
        action_path=ACTION,
        replacement_hub_pid=702,
        process_snapshot=_processes(),
        recovery_action_arguments=(
            '-NoProfile -File "C:\\Services\\restart-hub-after-failure.ps1" -ActionIndex 1'
        ),
    )

    assert proof["replacement_process_id"] == 700


def test_scheduler_verifier_rejects_engine_pid_or_extra_recovery_arguments() -> None:
    events = _valid_events()
    events[1] = _event(
        12,
        200,
        TaskName=TASK,
        TaskInstanceId="11111111-1111-1111-1111-111111111111",
        ActionName=RECOVERY_ACTION_ID,
        EnginePID="701",
    )
    with pytest.raises(ValueError, match="EnginePID"):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )


@pytest.mark.parametrize(
    ("event_index", "action_id"),
    [
        (0, "F28-primary-0"),
        (0, ACTION),
        (1, "f28-recovery-2"),
        (1, "F28-recovery-1"),
        (1, ACTION),
    ],
)
def test_scheduler_verifier_rejects_wrong_or_case_varied_native_action_ids(
    event_index: int, action_id: str
) -> None:
    events = _valid_events()
    replacement = _event(
        11 + event_index,
        201 if event_index == 0 else 200,
        TaskName=TASK,
        TaskInstanceId="11111111-1111-1111-1111-111111111111",
        ActionName=action_id,
        **({"ResultCode": "2147942475"} if event_index == 0 else {"EnginePID": "700"}),
    )
    events[event_index] = replacement

    with pytest.raises(ValueError, match="exact .* action"):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )


def test_scheduler_verifier_rejects_recovery_action_id_for_wrong_index() -> None:
    with pytest.raises(ValueError, match="exact task action"):
        verify_scheduler_restart(
            events=_valid_events(),
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
            recovery_action_index=2,
        )

    processes = _processes()
    processes[0]["command_line"] += " -ActionIndex 10"
    with pytest.raises(ValueError, match="command line differs"):
        verify_scheduler_restart(
            events=_valid_events(),
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=processes,
            recovery_action_arguments=(
                '-NoProfile -File "C:\\Services\\restart-hub-after-failure.ps1" -ActionIndex 1'
            ),
        )


@pytest.mark.parametrize("result_code", ["75", "2147942476", "0x8007004B"])
def test_scheduler_verifier_rejects_low_word_wrong_or_nondecimal_hresult(result_code: str) -> None:
    events = _valid_events()
    events[0] = _event(
        11,
        201,
        TaskName=TASK,
        TaskInstanceId="11111111-1111-1111-1111-111111111111",
        ActionName=ACTION,
        ResultCode=result_code,
    )

    with pytest.raises(ValueError):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )


def test_scheduler_verifier_rejects_task_completion_between_failure_and_recovery() -> None:
    events = _valid_events()
    events.insert(1, _event(12, 102, TaskName=TASK))
    events[2]["event_record_id"] = 13
    events[2]["xml"] = str(events[2]["xml"]).replace("EventRecordID>12", "EventRecordID>13")
    events[3]["event_record_id"] = 14
    events[3]["xml"] = str(events[3]["xml"]).replace("EventRecordID>13", "EventRecordID>14")

    with pytest.raises(ValueError, match="task completion"):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )


def test_scheduler_verifier_allows_system_entry_without_executable_path() -> None:
    processes = _processes()
    processes.append(
        {
            "pid": 4,
            "parent_pid": 0,
            "name": "System",
            "executable_path": "",
            "creation_date": "2026-08-10T19:20:00.0000000Z",
            "command_line": "",
        }
    )
    processes.append(
        {
            "pid": 0,
            "parent_pid": 0,
            "name": "System Idle Process",
            "executable_path": "",
            "creation_date": "2026-08-10T19:20:00.0000000Z",
            "command_line": "",
        }
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


def test_scheduler_verifier_rejects_reordered_recovery_event_before_old_failure() -> None:
    events = _valid_events()
    events.insert(
        0,
        _event(
            9,
            200,
            TaskName=TASK,
            TaskInstanceId="33333333-3333-3333-3333-333333333333",
            ActionName=ACTION,
        ),
    )

    with pytest.raises(ValueError, match="cannot precede"):
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

    with pytest.raises(ValueError, match="unexpected or reordered"):
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
    events[1] = _event(
        12,
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
            "command_line": (
                'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe '
                '-NoProfile -File "C:\\Services\\restart-hub-after-failure.ps1" -ActionIndex 1'
            ),
    }
    processes[1] = {
        "pid": 701,
        "parent_pid": 4,
        "name": "python.exe",
        "executable_path": r"C:\Services\oms-study-automation-v2\.venv\Scripts\python.exe",
        "creation_date": "2026-08-10T19:20:03.1250000Z",
        "command_line": "python.exe -m oms_hub",
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


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("", "TimeCreated"),
        ('<TimeCreated SystemTime="not-a-time"/>', "UTC timestamp"),
        (
            '<TimeCreated SystemTime="2026-08-10T20:20:03.2500000+01:00"/>',
            "UTC timestamp",
        ),
        (
            '<TimeCreated SystemTime="2026-08-10T19:20:03.2500000Z"/>'
            '<TimeCreated SystemTime="2026-08-10T19:20:03.2500000Z"/>',
            "exactly one TimeCreated",
        ),
    ],
)
def test_scheduler_verifier_rejects_invalid_system_time_metadata(
    replacement: str, message: str
) -> None:
    events = _valid_events()
    events[2]["xml"] = str(events[2]["xml"]).replace(
        '<TimeCreated SystemTime="2026-08-10T19:20:03.2500000Z"/>', replacement
    )

    with pytest.raises(ValueError, match=message):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )


def test_scheduler_verifier_rejects_time_created_outside_system_node() -> None:
    events = _valid_events()
    events[2]["xml"] = str(events[2]["xml"]).replace(
        '<TimeCreated SystemTime="2026-08-10T19:20:03.2500000Z"/>', ""
    ).replace(
        "<EventData>",
        '<EventData><TimeCreated SystemTime="2026-08-10T19:20:03.2500000Z"/>',
    )

    with pytest.raises(ValueError, match="direct child of Event/System"):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )


@pytest.mark.parametrize("element_name", ["EventID", "EventRecordID"])
def test_scheduler_verifier_rejects_duplicate_system_identifiers(element_name: str) -> None:
    events = _valid_events()
    value = "129" if element_name == "EventID" else "14"
    events[2]["xml"] = str(events[2]["xml"]).replace(
        "</System>", f"<{element_name}>{value}</{element_name}></System>"
    )

    with pytest.raises(ValueError, match=f"exactly one {element_name}"):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )


def test_scheduler_verifier_rejects_event_identifier_outside_system_node() -> None:
    events = _valid_events()
    events[2]["xml"] = str(events[2]["xml"]).replace(
        "<EventData>", "<EventData><EventID>129</EventID>"
    )

    with pytest.raises(ValueError, match="direct child of Event/System"):
        verify_scheduler_restart(
            events=events,
            cursor_event_record_id=10,
            full_task_name=TASK,
            action_path=ACTION,
            replacement_hub_pid=702,
            process_snapshot=_processes(),
        )
