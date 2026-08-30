from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from oms_hub.providers.gemini.evidence import validate_private_shadow_record

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "scripts" / "private-shadow-operator-entry.py"


def _load_entrypoint() -> ModuleType:
    assert ENTRYPOINT.is_file(), "composition entrypoint must be committed"
    spec = importlib.util.spec_from_file_location("task_2_8_composition_entrypoint", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_entrypoint_loads_only_the_hash_bound_evidence_module() -> None:
    module = _load_entrypoint()
    source = ROOT / "src" / "oms_hub" / "providers" / "gemini" / "evidence.py"
    loaded = module._load_hash_bound_evidence(ROOT)
    smoke = module._load_source_bound_smoke(ROOT)

    assert Path(str(loaded.__file__)).resolve() == source.resolve()
    assert Path(str(smoke.__file__)).resolve() == (
        ROOT / "scripts" / "run-gemini-contract-smoke.py"
    ).resolve()
    assert "_FAILURE_KEYS" not in ENTRYPOINT.read_text(encoding="utf-8")
    assert "sys.path.insert" not in ENTRYPOINT.read_text(encoding="utf-8")
    assert "private-shadow-operator-reviewed.py" not in ENTRYPOINT.read_text(encoding="utf-8")


def test_entrypoint_runs_the_real_source_bound_composition_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_entrypoint()
    monkeypatch.setenv("OMS_TASK28_COMPOSITION_VERIFY", "1")
    monkeypatch.setenv("OMS_TASK28_PRIVATE_PROJECT", str(ROOT))
    monkeypatch.delenv("RUN_PRIVATE_GEMINI_SHADOW", raising=False)

    assert module.main() == 0
    record = json.loads(capsys.readouterr().out)

    assert validate_private_shadow_record(record, 0)["status"] == "passed"


def test_entrypoint_rejects_an_evidence_package_symlink_escape(tmp_path: Path) -> None:
    module = _load_entrypoint()
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "oms_hub").symlink_to(ROOT / "src" / "oms_hub", target_is_directory=True)

    with pytest.raises(RuntimeError, match="private_shadow_evidence_binding_invalid"):
        module._load_hash_bound_evidence(project)


def test_entrypoint_close_and_cleanup_failures_still_emit_fail_closed_json() -> None:
    fixture = ROOT / "tests" / "scripts" / "private_shadow_entrypoint_fixture.py"
    for mode in ("close_failure", "cleanup_failure"):
        result = subprocess.run(
            [sys.executable, str(fixture), "--entrypoint", str(ENTRYPOINT), "--mode", mode],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1
        assert result.stderr == ""
        record = json.loads(result.stdout)
        assert validate_private_shadow_record(record, 1)["status"] == "blocked"
        assert record["provider_cleanup_outcome"] == "unknown"
        assert record["provider_reconciliation_outcome"] == "unknown"
